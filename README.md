# bluesky

Post store + CI publisher for a Bluesky (AT Protocol) account. Drop a post in
`posts/`; CI publishes it. **What's in the file is what publishes** — no
image logic here.

- Design & handoff: **[DESIGN.md](DESIGN.md)**
- Publisher: [`publish/publish.py`](publish/publish.py) (reconcile vs `posts/<slug>.state.json`)
- AT Protocol client: [`publish/bluesky.py`](publish/bluesky.py) (session, post, threading, image blobs)
- Post-text builder: [`publish/richmessage.py`](publish/richmessage.py) (300-grapheme cap)
- Workflow: [`.github/workflows/publish.yml`](.github/workflows/publish.yml)

Setup: repo secret `BLUESKY_APP_PASSWORD`; repo variable (or secret)
`BLUESKY_HANDLE`; optional repo variable `BLUESKY_SERVICE` (defaults to
`https://bsky.social`).

Post file contract (`posts/YYYY-MM-DD-<slug>.md`):
```
---
link: https://...        # optional outbound URL, faceted as a link
image: https://...       # optional final image URL, uploaded verbatim
reply_to: <slug>          # optional: another post's slug to thread under
---
<body text>
```

```
python publish/publish.py --dry-run     # no network -> prints what it would post
python -m pytest                        # unit tests
```
