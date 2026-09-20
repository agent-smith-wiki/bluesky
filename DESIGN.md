# Bluesky post store — design & handoff

## What this repo is
A social account's post store **plus** its CI publisher for Bluesky (the AT
Protocol). Push a post into `posts/`; a GitHub Action publishes the ones not
yet published (oldest-first), then commits the sibling state file.

**The file is the source of truth: what's in the file is what publishes.**
There is **no image logic in this repo** — no screenshots, no minting, no
og:image cropping. The `image:` front-matter is a final URL, fetched and
uploaded to Bluesky **verbatim** as a blob; the upstream producer bakes the
already-hosted, Bluesky-ready image URL into the file.

This scaffold is identity-agnostic: the same code is deployed to more than
one Bluesky-account repo. Every account-specific value (handle, app
password, PDS service URL) comes from CI environment/secrets, never from
committed code. There is nothing account-specific to fork or branch on.

## Where it sits (the bigger picture)
Part of a **per-platform** social-publishing split — one repo per platform
(Telegram, Threads, X, Bluesky, ...), each with its own posts and its own CI
publisher. A content-producer pipeline commits a post file here and replies
with the commit/PR link, not a link only known after CI runs.

## Post contract — `posts/YYYY-MM-DD-<slug>.md`
```
---
link: https://...        # outbound URL: appended on its own line and faceted
                          # as a clickable link (optional)
image: https://...       # final image URL, fetched + uploaded verbatim as a
                          # blob embed (optional)
reply_to: <slug|at://…>   # thread under another post: a same-repo slug, or a
                          # full at:// URI for a post in another account (optional)
---
<body text>               # the post text (plain text, no Markdown syntax --
                           # Bluesky has no rich-block renderer like Telegram)
```
- `slug` = the filename without `.md`; it's the sibling state-file stem and
  must be stable.
- No `link:` → the body alone becomes the post text.
- With `link:` → the link is appended on its own line (unless already present
  in the body) and marked as a clickable facet.
- The composed text is capped at **300 graphemes** (the AT Protocol limit);
  `publish/richmessage.py` truncates on a paragraph boundary, keeping the
  link intact, when the body would push it over.
- **Link card**: with no `image:`, the first URL in the post text (or `link:`)
  becomes an `app.bsky.embed.external` card — OpenGraph title/description/thumb
  when the page exposes them, the domain as title otherwise. `image:` takes
  precedence: a post shows either an image or a link card, never both.
- `reply_to:` threads this post under an earlier one. A same-repo **slug** threads
  under that post (the parent must already be published — see "Deferred replies");
  a full **`at://…` URI** threads under a post in another account/repo directly —
  this is the dual-identity mirror (the agent replying to the operator's post).

## CI flow (`.github/workflows/publish.yml`)
`on: push` (main, `posts/**`) → `python publish/publish.py`. For each
`posts/*.md` whose sibling state file doesn't exist, **oldest-first**:
1. **claim first** — write `posts/<slug>.state.json` and `git commit`+`push`
   it (`[skip ci]`) BEFORE any Bluesky call;
2. `com.atproto.server.createSession` once per run, then
   `com.atproto.repo.createRecord` (`app.bsky.feed.post`) per post, then
   record `{uri, cid, url}` and push again.

`concurrency: bluesky-publish` → never two publishers at once.

**Why sibling state files:** a shared mutable `state.json` blocks
conflict-free parallel producers. Two different posts committed by different
producers would collide on the same file. Sibling files keep each post's
state next to the post, so independent posts/updates merge without touching
shared mutable state.

**Why claim-first:** a crash or a failed send can then only DROP a post
(recorded `status: failed`, skipped until you delete the sibling state file),
never DUPLICATE it — a duplicate storm would spam the timeline. Prefer a
missed post over a repeated one. (`--dry-run` = no send; `--no-push` = send
without git.)

**Deferred replies:** if `reply_to: <slug>` names a post that hasn't
published yet (no sibling state file, or one without a `uri`), the reply is
left **unclaimed** and retried on the next run — it is never recorded as
`failed`. This lets a thread's posts land in the same commit/push and still
resolve in order across runs.

## Threading (root/parent refs)
A reply's record needs strong refs (`{uri, cid}`) to both its **parent**
and the thread's **root**. `publish/bluesky.py`'s `post()` resolves both
from just the parent's AT-URI: it fetches the parent record
(`com.atproto.repo.getRecord`) and, if the parent is itself a reply, reuses
*its* root; otherwise the parent becomes the root. Callers (this repo's
`publish.py`) only ever need to track one thing per post — its own AT-URI,
already stored in the sibling state file — never a separate root chain.

## Grapheme limit
The AT Protocol's 300-character limit is actually 300 **extended grapheme
clusters** (an emoji with a skin-tone modifier or a flag counts once, not
per code point). Python's stdlib has no Unicode grapheme-cluster segmenter,
so `publish/richmessage.py` ships a narrow approximation (`graphemes()`):
it merges combining marks, variation selectors, skin-tone modifiers, tag
characters, ZWJ sequences, and regional-indicator (flag) pairs into the
preceding cluster. It's exact for plain text, links, and simple emoji, and
undercounts a few exotic cluster types — acceptable for this repo's post
bodies. `publish/bluesky.py`'s `post()` re-checks the limit as a defensive
guard right before sending.

## Reuse / provenance
`publish/richmessage.py` and `publish/bluesky.py` are original to this repo
(there is no upstream Bluesky client to vendor); the design mirrors the
sibling Telegram publisher's `publish.py` reconciliation loop — claim-first
sibling state, oldest-first, `--dry-run`/`--no-push` — swapped onto the AT
Protocol's session + record model. **stdlib only** — the workflow needs no
`pip install`.

## One-time setup
- Repo **secret**: `BLUESKY_APP_PASSWORD` — an [app password](https://bsky.app/settings/app-passwords),
  never the account password.
- Repo **variable** (or secret) `BLUESKY_HANDLE` — the account's handle.
- Optional repo **variable** `BLUESKY_SERVICE` — the PDS/entryway base URL;
  defaults to `https://bsky.social`.

## First run / seed
The repo starts **empty** → only NEW posts arrive → no seed step is needed.

## Local test
```
python publish/publish.py --dry-run     # no network -> prints what it would post
python -m pytest                        # unit tests (stdlib unittest, run via pytest)
```
