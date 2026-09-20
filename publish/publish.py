"""Publish new posts from posts/*.md to Bluesky (AT Protocol).

WHAT'S IN THE FILE IS WHAT PUBLISHES. No image processing happens here (no
screenshot / cropping / minting) -- the `image:` front-matter is a final URL,
fetched and uploaded to Bluesky verbatim as the post's image blob.

Idempotency = a sibling state file per post: posts/<slug>.state.json. CLAIM
BEFORE SEND: each post's state file is written and pushed to git BEFORE the
Bluesky call. So a crash or a failed send can only DROP a post, never
DUPLICATE it -- a duplicate storm would spam the timeline. We prefer a missed
post over a repeated one; a dropped post is recorded as `status: failed` and
skipped until you remove its sibling state file.

Order: oldest-first (files sort by their date-prefixed name).

Threading: `reply_to: <slug>` makes this post a reply to another post in this
same repo. The parent must already be published (its sibling state file must
carry a `uri`) -- a post whose parent isn't published yet is DEFERRED (left
unclaimed, retried next run), never dropped.

Post file  posts/YYYY-MM-DD-<slug>.md :
    ---
    link: https://...        # outbound URL, appended + faceted as a link (optional)
    image: https://...       # final image URL, fetched + uploaded verbatim (optional)
    reply_to: <slug>          # slug of another posts/*.md to thread under (optional)
    ---
    <body text>               # post text; truncated on paragraph boundaries at 300 graphemes

Sibling state file posts/YYYY-MM-DD-<slug>.state.json :
    {"status": "sending"}                     # claim before send
    {"status": "failed", "error": "..."}      # send failed, no auto-retry
    {"uri": "at://...", "cid": "...", "url": "https://bsky.app/..."}   # success

Env: BLUESKY_HANDLE, BLUESKY_APP_PASSWORD (secret). Optional BLUESKY_SERVICE
(defaults to https://bsky.social, the entryway most personal PDSes proxy
through). In CI `actions/checkout` provides push creds and the workflow sets
the git identity. Flags: --dry-run (no send), --no-push (send but don't touch
git; local testing only).
"""

import glob
import json
import os
import subprocess
import sys

import bluesky
import richmessage

POSTS = "posts"
DEFAULT_SERVICE = "https://bsky.social"


def _unquote(v: str) -> str:
    v = v.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'" else v


def parse(path: str) -> tuple[dict, str]:
    """Split `--- front-matter --- body` (flat key: value front-matter).

    Every front-matter field in this repo is optional (unlike Telegram's
    required `title`), so the front-matter block itself may be empty --
    line-based splitting handles that; a single regex with a literal `\\n---`
    separator can't match when there's no content between the delimiters.
    """
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}, text.strip()
    fm = {}
    for line in lines[1:end]:
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            fm[k.strip()] = _unquote(v)
    return fm, "\n".join(lines[end + 1:]).strip()


def slug_of(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def render(fm: dict, body: str) -> dict:
    """The file -> a Bluesky post {"text": ..., "facets": [...]}."""
    return richmessage.build(body, link=fm.get("link") or None)


def state_path(slug: str, root: str = ".") -> str:
    """Sibling state file path for a post slug."""
    return os.path.join(root, POSTS, f"{slug}.state.json")


def load_state(slug: str, root: str = ".") -> dict | None:
    """Load a post's sibling state file, or None if it doesn't exist."""
    path = state_path(slug, root)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _commit_push(msg: str, paths: list[str], max_push_retries: int = 5) -> None:
    """Commit the given paths and push.

    If the push fails because origin advanced since checkout (non-fast-forward),
    fetch and rebase onto origin/main before retrying. This keeps a safely-
    claimed post from being abandoned when parallel producers advance main
    between the workflow checkout and the state commit. Raises on failure (so
    we NEVER send having failed to persist the claim -- a failed claim just
    retries next run).
    """
    subprocess.run(["git", "add", *paths], check=True, capture_output=True, text=True)
    c = subprocess.run(["git", "commit", "-m", msg], capture_output=True, text=True)
    if c.returncode != 0:
        if "nothing to commit" in (c.stdout + c.stderr):
            return
        raise RuntimeError(f"git commit failed: {c.stderr.strip()}")

    stderr = ""
    for attempt in range(max_push_retries):
        p = subprocess.run(["git", "push"], capture_output=True, text=True)
        if p.returncode == 0:
            return
        stderr = p.stderr
        err = stderr.lower()
        if "non-fast-forward" in err or "fetch first" in err or "rejected" in err:
            subprocess.run(["git", "fetch", "origin"], check=True,
                           capture_output=True, text=True)
            r = subprocess.run(["git", "rebase", "origin/main"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                subprocess.run(["git", "rebase", "--abort"], check=False,
                               capture_output=True, text=True)
                raise RuntimeError(f"git rebase failed: {r.stderr.strip()}")
            continue
        break
    raise RuntimeError(f"git push failed: {stderr.strip()}")


def save_state(slug: str, record: dict, msg: str, push: bool, root: str = ".") -> None:
    """Write a sibling state file and optionally commit + push it."""
    path = state_path(slug, root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write("\n")
    if push:
        _commit_push(msg, [path])


def _post_paths(root: str = ".") -> list[str]:
    """All post files, oldest-first. State files are ignored."""
    return sorted(glob.glob(os.path.join(root, POSTS, "*.md")))


def main(argv: list[str] | None = None, root: str = ".") -> int:
    argv = argv if argv is not None else sys.argv[1:]
    dry = "--dry-run" in argv
    push = "--no-push" not in argv

    handle = (os.environ.get("BLUESKY_HANDLE") or "").strip() or None
    app_password = os.environ.get("BLUESKY_APP_PASSWORD")
    service = (os.environ.get("BLUESKY_SERVICE") or "").strip() or DEFAULT_SERVICE
    if app_password and not dry and not handle:
        raise RuntimeError("BLUESKY_HANDLE is required for Bluesky publishing")
    session = bluesky.login(service, handle, app_password) if (app_password and not dry) else None

    failed = 0
    for path in _post_paths(root):
        slug = slug_of(path)
        if load_state(slug, root) is not None:
            continue
        fm, body = parse(path)
        rendered = render(fm, body)
        text, facets = rendered["text"], rendered["facets"]

        reply_to_uri = None
        reply_to_ref = fm.get("reply_to") or None
        if reply_to_ref:
            if reply_to_ref.startswith("at://"):
                reply_to_uri = reply_to_ref            # cross-account: a raw AT-URI threads directly
            else:
                parent_state = load_state(reply_to_ref, root)   # same-repo: resolve a sibling slug
                if not parent_state or "uri" not in parent_state:
                    print(f"deferred {slug}: reply_to {reply_to_ref!r} not published yet")
                    continue
                reply_to_uri = parent_state["uri"]

        if session is None:
            n = richmessage.grapheme_len(text)
            note = f", reply to {reply_to_ref}" if reply_to_ref else ""
            print(f"[dry-run] would post {slug}: {n} graphemes{note}")
            continue

        # CLAIM FIRST: persist + push before sending. From here a failure can only
        # drop this post (recorded as failed), never duplicate it.
        save_state(slug, {"status": "sending"}, f"claim {slug} [skip ci]", push, root)
        try:
            result = bluesky.post(session, text, facets=facets, reply_to=reply_to_uri,
                                  image=fm.get("image") or None)
        except bluesky.BlueskyError as e:
            print(f"FAILED {slug}: {e}", file=sys.stderr)
            save_state(slug, {"status": "failed", "error": str(e)[:300]},
                       f"failed {slug} [skip ci]", push, root)
            failed += 1
            continue
        rec = {"uri": result["uri"], "cid": result["cid"], "url": result["url"]}
        save_state(slug, rec, f"published {slug} [skip ci]", push, root)
        print(f"posted {slug} -> {rec['url']}")

    if failed:
        print(f"{failed} post(s) failed AFTER claim -> dropped, not retried. "
              f"To re-send, delete their sibling state file.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
