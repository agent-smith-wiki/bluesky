"""Behavioural tests for the app.bsky.feed.post embed selection.

Network is mocked: `_call` (createRecord) captures the record, and the card
metadata / blob upload are stubbed so no HTTP happens.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "publish"))

import bluesky  # noqa: E402

FAKE_SESSION = {"service": "https://bsky.social", "did": "did:plc:test",
                "handle": "test.example", "access_jwt": "jwt"}


def _capture_record():
    """A fake `_call` that records the createRecord payload and returns a stub."""
    box = {}

    def fake_call(service, method, payload, token=None):
        if method == "com.atproto.repo.createRecord":
            box["record"] = payload["record"]
            return {"uri": "at://did:plc:test/app.bsky.feed.post/1", "cid": "cid1"}
        raise AssertionError(f"unexpected call: {method}")

    return box, fake_call


class ExternalEmbedTest(unittest.TestCase):
    def test_first_url_becomes_an_external_card_when_no_image(self):
        box, fake_call = _capture_record()
        meta = {"title": "A Title", "description": "A desc", "image": None}
        with patch.object(bluesky, "_call", fake_call), \
             patch.object(bluesky, "_fetch_card_meta", lambda url: meta):
            bluesky.post(FAKE_SESSION, "Worth a read: https://example.com/x — enjoy")
        embed = box["record"]["embed"]
        self.assertEqual(embed["$type"], "app.bsky.embed.external")
        self.assertEqual(embed["external"]["uri"], "https://example.com/x")
        self.assertEqual(embed["external"]["title"], "A Title")
        self.assertEqual(embed["external"]["description"], "A desc")

    def test_card_falls_back_to_domain_title_without_opengraph(self):
        box, fake_call = _capture_record()
        with patch.object(bluesky, "_call", fake_call), \
             patch.object(bluesky, "_fetch_card_meta", lambda url: {}):
            bluesky.post(FAKE_SESSION, "https://sub.example.org/deep/path")
        external = box["record"]["embed"]["external"]
        self.assertEqual(external["title"], "sub.example.org")
        self.assertEqual(external["description"], "")

    def test_opengraph_image_is_uploaded_as_thumb(self):
        box, fake_call = _capture_record()
        meta = {"title": "T", "description": "D", "image": "https://example.com/og.png"}
        with patch.object(bluesky, "_call", fake_call), \
             patch.object(bluesky, "_fetch_card_meta", lambda url: meta), \
             patch.object(bluesky, "_upload_blob", lambda s, u: {"blob": "ref"}):
            bluesky.post(FAKE_SESSION, "See https://example.com/x")
        self.assertEqual(box["record"]["embed"]["external"]["thumb"], {"blob": "ref"})

    def test_image_takes_precedence_over_link_card(self):
        box, fake_call = _capture_record()
        with patch.object(bluesky, "_call", fake_call), \
             patch.object(bluesky, "_upload_blob", lambda s, u: {"blob": "img"}):
            bluesky.post(FAKE_SESSION, "See https://example.com/x",
                         image="https://example.com/pic.jpg")
        self.assertEqual(box["record"]["embed"]["$type"], "app.bsky.embed.images")

    def test_plain_text_without_a_url_gets_no_embed(self):
        box, fake_call = _capture_record()
        with patch.object(bluesky, "_call", fake_call):
            bluesky.post(FAKE_SESSION, "Just a plain thought, no link here.")
        self.assertNotIn("embed", box["record"])


if __name__ == "__main__":
    unittest.main()
