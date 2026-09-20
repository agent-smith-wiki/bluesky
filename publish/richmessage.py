"""Build a Bluesky post record body: plain text, capped at 300 graphemes.

A Bluesky post has no headings, footers, or rich blocks like Telegram's Rich
Message -- it's a single text field plus byte-range "facets" that mark spans
of that text as links, mentions, or tags. This module composes the text (body
+ an optional appended link), truncates it on a paragraph boundary when it
would exceed the 300-grapheme limit, and builds the link facet.

Grapheme counting: the AT Protocol limit is in Unicode *extended grapheme
clusters*, not code points or bytes (an emoji with a skin-tone modifier or a
flag counts once). Python's stdlib has no grapheme-cluster segmenter, so
`graphemes()` below is a deliberately narrow approximation: it merges
combining marks, variation selectors, skin-tone modifiers, tag characters,
ZWJ sequences, and regional-indicator (flag) pairs into the preceding
cluster. It undercounts a few exotic cluster types (e.g. Hangul jamo
sequences) but matches the common case -- plain text, links, and simple
emoji -- exactly.
"""

import re
import unicodedata

MAX_GRAPHEMES = 300

_ZWJ = "\u200d"
_VARIATION_SELECTORS = range(0xFE00, 0xFE10)
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_TAGS = range(0xE0000, 0xE0080)
_REGIONAL_INDICATORS = range(0x1F1E6, 0x1F200)


def _attaches_to_previous(cp: int) -> bool:
    """True when the code point continues the current grapheme cluster
    instead of starting a new one (combining marks, variation selectors,
    skin-tone modifiers, tag characters)."""
    if unicodedata.combining(chr(cp)):
        return True
    return cp in _VARIATION_SELECTORS or cp in _SKIN_TONES or cp in _TAGS


def graphemes(text: str) -> list[str]:
    """Split `text` into an approximation of Unicode extended grapheme
    clusters -- see the module docstring for what's covered."""
    clusters: list[str] = []
    buf = ""
    prev_ri = False
    zwj_pending = False
    for ch in text:
        cp = ord(ch)
        if not buf:
            buf = ch
            prev_ri = cp in _REGIONAL_INDICATORS
            zwj_pending = False
            continue
        if zwj_pending:
            buf += ch
            zwj_pending = ch == _ZWJ
            prev_ri = False
            continue
        if ch == _ZWJ:
            buf += ch
            zwj_pending = True
            continue
        if _attaches_to_previous(cp):
            buf += ch
            continue
        if prev_ri and cp in _REGIONAL_INDICATORS:
            buf += ch
            prev_ri = False
            continue
        clusters.append(buf)
        buf = ch
        prev_ri = cp in _REGIONAL_INDICATORS
    if buf:
        clusters.append(buf)
    return clusters


def grapheme_len(text: str) -> int:
    return len(graphemes(text))


def truncate_graphemes(text: str, limit: int, ellipsis: str = "\u2026") -> str:
    """Truncate `text` to at most `limit` graphemes (including `ellipsis`
    when a cut happens), preferring whole paragraphs, else a word boundary."""
    if grapheme_len(text) <= limit:
        return text
    budget = max(0, limit - grapheme_len(ellipsis))

    paras = re.split(r"\n\s*\n", text.strip())
    if len(paras) > 1:
        kept: list[str] = []
        total = 0
        for p in paras:
            n = grapheme_len(p)
            if total + n > budget:
                break
            kept.append(p)
            total += n + 2  # blank line between kept paragraphs
        if kept:
            return "\n\n".join(kept).rstrip() + ellipsis

    clusters = graphemes(text)
    cut = "".join(clusters[:budget])
    sp = cut.rfind(" ")
    if sp > budget * 0.6:
        cut = cut[:sp]
    return cut.rstrip() + ellipsis


def link_facet(text: str, link: str) -> dict | None:
    """A byte-range `app.bsky.richtext.facet#link` for the first occurrence
    of `link` in `text`, or None if it isn't present. Byte offsets (not
    character offsets) are what the AT Protocol facet spec requires."""
    start = text.find(link)
    if start < 0:
        return None
    prefix_bytes = len(text[:start].encode("utf-8"))
    link_bytes = len(link.encode("utf-8"))
    return {
        "index": {"byteStart": prefix_bytes, "byteEnd": prefix_bytes + link_bytes},
        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": link}],
    }


_URL_RE = re.compile(r'https?://[^\s<>()\[\]"\']+')


def first_url(text: str) -> str | None:
    """The first http(s) URL in `text` (trailing punctuation trimmed), or None."""
    m = _URL_RE.search(text or "")
    return m.group(0).rstrip('.,);:]') if m else None


def build(body: str, link: str | None = None, max_graphemes: int = MAX_GRAPHEMES) -> dict:
    """Compose the final post text: `body`, plus `link` appended on its own
    line when given and not already present in the body. Truncates the body
    (never the link) on a paragraph boundary so the whole thing fits within
    `max_graphemes`. Returns {"text": ..., "facets": [...]}."""
    body = (body or "").strip()
    link = (link or "").strip() or None

    if link and link not in body:
        reserve = grapheme_len(link) + 1  # +1 for the separating newline
        budget = max(0, max_graphemes - reserve)
        if grapheme_len(body) > budget:
            body = truncate_graphemes(body, budget)
        text = f"{body}\n{link}" if body else link
    else:
        if grapheme_len(body) > max_graphemes:
            body = truncate_graphemes(body, max_graphemes)
        text = body

    facets = []
    if link:
        facet = link_facet(text, link)
        if facet:
            facets.append(facet)
    return {"text": text, "facets": facets}
