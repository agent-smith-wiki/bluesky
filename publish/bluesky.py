"""Minimal AT Protocol (Bluesky) client, stdlib only: createSession,
createRecord (app.bsky.feed.post), getRecord (for reply threading), and
uploadBlob (for image embeds). No SDK, no multipart -- just JSON-over-HTTPS
via urllib, matching this repo's stdlib-only CI constraint.

The app password is read from the caller's environment at call time so it
never lands on argv or in a committed file.
"""

import json
import html as _html
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import richmessage

DEFAULT_SERVICE = "https://bsky.social"


class BlueskyError(RuntimeError):
    pass


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _call(service: str, method: str, payload: dict, token: str | None = None) -> dict:
    req = urllib.request.Request(
        f"{service}/xrpc/{method}",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise BlueskyError(f"{method} -> HTTP {e.code}: {detail}") from None


def _get_record(service: str, token: str, uri: str) -> dict:
    m = re.match(r"^at://([^/]+)/([^/]+)/([^/]+)$", uri)
    if not m:
        raise BlueskyError(f"malformed AT-URI: {uri}")
    repo, collection, rkey = m.groups()
    q = urllib.parse.urlencode({"repo": repo, "collection": collection, "rkey": rkey})
    req = urllib.request.Request(
        f"{service}/xrpc/com.atproto.repo.getRecord?{q}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise BlueskyError(f"getRecord -> HTTP {e.code}: {detail}") from None


def _reply_refs(session: dict, reply_to_uri: str) -> dict:
    """Build the {root, parent} StrongRefs for a reply. `root` is the parent
    post's own root when the parent is itself a reply (so nested replies stay
    attached to the top of the thread), else the parent post."""
    parent = _get_record(session["service"], session["access_jwt"], reply_to_uri)
    parent_ref = {"uri": reply_to_uri, "cid": parent["cid"]}
    root_ref = parent.get("value", {}).get("reply", {}).get("root") or parent_ref
    return {"root": root_ref, "parent": parent_ref}


def _upload_blob(session: dict, image_url: str) -> dict:
    """Fetch an already-hosted image URL verbatim and upload it to Bluesky's
    blob store, returning the blob ref. No cropping/resizing/minting -- the
    upstream producer bakes the final image URL into the post file."""
    req = urllib.request.Request(image_url, headers={"User-Agent": "bluesky-publisher/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        content_type = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    if not content_type or content_type == "application/octet-stream":
        content_type = mimetypes.guess_type(image_url)[0] or "image/jpeg"
    upload_req = urllib.request.Request(
        f"{session['service']}/xrpc/com.atproto.repo.uploadBlob",
        data=data,
        method="POST",
        headers={
            "Content-Type": content_type,
            "Authorization": f"Bearer {session['access_jwt']}",
        },
    )
    try:
        with urllib.request.urlopen(upload_req, timeout=120) as r:
            return json.loads(r.read().decode())["blob"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise BlueskyError(f"uploadBlob -> HTTP {e.code}: {detail}") from None


def _image_embed(session: dict, image_url: str, alt: str = "") -> dict:
    """app.bsky.embed.images from a single already-hosted image URL."""
    return {"$type": "app.bsky.embed.images",
            "images": [{"image": _upload_blob(session, image_url), "alt": alt}]}


def _meta_content(html: str, key: str) -> str | None:
    """The `content` of the first <meta> whose property/name equals `key`."""
    for m in re.finditer(r"<meta\b[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        if re.search(rf'(?:property|name)\s*=\s*["\']{re.escape(key)}["\']', tag, re.IGNORECASE):
            c = (re.search(r'content\s*=\s*"([^"]*)"', tag, re.IGNORECASE)
                 or re.search(r"content\s*=\s*'([^']*)'", tag, re.IGNORECASE))
            if c:
                return _html.unescape(c.group(1)).strip()
    return None


def _fetch_card_meta(url: str) -> dict:
    """Best-effort OpenGraph/title/description/image for a link card.

    Returns {} on any failure -- a card still renders from the URL alone."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bluesky-publisher/1.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            ctype = (r.headers.get("Content-Type") or "").lower()
            if ctype and "html" not in ctype and "xml" not in ctype:
                return {}
            charset = r.headers.get_content_charset() or "utf-8"
            html = r.read(500_000).decode(charset, "replace")
    except Exception:
        return {}
    title = _meta_content(html, "og:title")
    if not title:
        t = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
        title = _html.unescape(t.group(1)).strip() if t else None
    return {
        "title": title,
        "description": _meta_content(html, "og:description") or _meta_content(html, "description"),
        "image": _meta_content(html, "og:image"),
    }


def _external_embed(session: dict, url: str) -> dict:
    """app.bsky.embed.external: a link card for `url`, with OpenGraph title/
    description/thumbnail when the page exposes them (domain title otherwise)."""
    meta = _fetch_card_meta(url)
    external = {
        "uri": url,
        "title": (meta.get("title") or urllib.parse.urlparse(url).netloc)[:300],
        "description": (meta.get("description") or "")[:1000],
    }
    image = meta.get("image")
    if image:
        try:
            external["thumb"] = _upload_blob(session, urllib.parse.urljoin(url, image))
        except BlueskyError:
            pass  # a card without a thumb still renders
    return {"$type": "app.bsky.embed.external", "external": external}


def permalink(handle: str, uri: str) -> str:
    """Public URL of a post from its AT-URI and the author's handle."""
    rkey = uri.rsplit("/", 1)[-1]
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def login(service: str, handle: str, app_password: str) -> dict:
    """createSession -> a session dict {service, did, handle, access_jwt}."""
    data = _call(service, "com.atproto.server.createSession",
                 {"identifier": handle, "password": app_password})
    return {
        "service": service,
        "did": data["did"],
        "handle": data["handle"],
        "access_jwt": data["accessJwt"],
    }


def post(session: dict, text: str, *, facets: list | None = None,
         reply_to: str | None = None, link: str | None = None,
         image: str | None = None, alt: str = "") -> dict:
    """Create an app.bsky.feed.post record.

    - `facets`: pass the value from `richmessage.build()` when the caller
      already composed it; otherwise, if `link` is given and present in
      `text`, a link facet is derived for it here.
    - `reply_to`: the parent post's AT-URI (at://did/collection/rkey).
      Threads by fetching the parent to compute the root/parent StrongRefs.
    - `image`: a final, already-hosted image URL; fetched and uploaded as a
      blob and embedded. Omit for a text-only post.
    - With no image, the first URL in `text` (or `link`) becomes an
      app.bsky.embed.external link card.

    Returns {"uri": ..., "cid": ..., "url": ...}.
    """
    if facets is None and link:
        facet = richmessage.link_facet(text, link)
        facets = [facet] if facet else None

    n = richmessage.grapheme_len(text)
    if n > richmessage.MAX_GRAPHEMES:
        raise BlueskyError(
            f"post text is {n} graphemes, exceeds the {richmessage.MAX_GRAPHEMES} limit")

    record = {"$type": "app.bsky.feed.post", "text": text, "createdAt": _now_iso()}
    if facets:
        record["facets"] = facets
    if reply_to:
        record["reply"] = _reply_refs(session, reply_to)
    if image:
        record["embed"] = _image_embed(session, image, alt=alt)
    else:
        card_url = link or richmessage.first_url(text)
        if card_url:
            record["embed"] = _external_embed(session, card_url)

    data = _call(session["service"], "com.atproto.repo.createRecord", {
        "repo": session["did"],
        "collection": "app.bsky.feed.post",
        "record": record,
    }, token=session["access_jwt"])

    return {
        "uri": data["uri"],
        "cid": data["cid"],
        "url": permalink(session["handle"], data["uri"]),
    }
