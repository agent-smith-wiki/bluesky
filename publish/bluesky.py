"""Minimal AT Protocol (Bluesky) client, stdlib only: createSession,
createRecord (app.bsky.feed.post), getRecord (for reply threading), and
uploadBlob (for image embeds). No SDK, no multipart -- just JSON-over-HTTPS
via urllib, matching this repo's stdlib-only CI constraint.

The app password is read from the caller's environment at call time so it
never lands on argv or in a committed file.
"""

import json
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


def _image_embed(session: dict, image_url: str, alt: str = "") -> dict:
    """Fetch `image_url` verbatim and upload it as a blob, embedded as
    app.bsky.embed.images. No cropping/resizing/minting happens here -- the
    upstream producer bakes the final, already-hosted image URL into the
    post file; this just relays those bytes to Bluesky's blob store."""
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
            blob = json.loads(r.read().decode())["blob"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise BlueskyError(f"uploadBlob -> HTTP {e.code}: {detail}") from None
    return {"$type": "app.bsky.embed.images", "images": [{"image": blob, "alt": alt}]}


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
