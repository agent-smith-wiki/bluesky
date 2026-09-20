"""Behavioral tests for the 300-grapheme post-text builder."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "publish"))

import richmessage


class GraphemeCountingTestCase(unittest.TestCase):
    def test_plain_ascii_counts_one_per_character(self):
        self.assertEqual(richmessage.grapheme_len("hello"), 5)

    def test_flag_emoji_counts_as_one_grapheme(self):
        # Regional indicator pair (2 code points) -> 1 grapheme cluster.
        flag = "\U0001F1FA\U0001F1F8"
        self.assertEqual(richmessage.grapheme_len(flag), 1)

    def test_skin_tone_modifier_counts_as_one_grapheme(self):
        # Base emoji + Fitzpatrick modifier (2 code points) -> 1 cluster.
        waving = "\U0001F44B\U0001F3FD"
        self.assertEqual(richmessage.grapheme_len(waving), 1)

    def test_combining_accent_counts_as_one_grapheme(self):
        # "e" + combining acute accent -> 1 cluster, not 2.
        e_acute = "e\u0301"
        self.assertEqual(richmessage.grapheme_len(e_acute), 1)


class TruncateGraphemesTestCase(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(richmessage.truncate_graphemes("hi", 300), "hi")

    def test_keeps_whole_paragraphs_under_budget(self):
        body = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph is long."
        out = richmessage.truncate_graphemes(body, 40)
        self.assertLessEqual(richmessage.grapheme_len(out), 40)
        self.assertTrue(out.startswith("First paragraph."))
        self.assertNotIn("Third", out)
        self.assertTrue(out.endswith("\u2026"))

    def test_falls_back_to_word_boundary_for_single_long_paragraph(self):
        body = "word " * 100  # one paragraph, well over budget
        out = richmessage.truncate_graphemes(body, 30)
        self.assertLessEqual(richmessage.grapheme_len(out), 30)
        self.assertTrue(out.endswith("\u2026"))
        self.assertNotIn("word\u2026word", out)  # cut on a space, not mid-word


class BuildTestCase(unittest.TestCase):
    def test_body_only_under_limit_is_untouched(self):
        result = richmessage.build("Hello world.")
        self.assertEqual(result["text"], "Hello world.")
        self.assertEqual(result["facets"], [])

    def test_link_is_appended_and_faceted(self):
        result = richmessage.build("Check this out.", link="https://example.com/x")
        self.assertEqual(result["text"], "Check this out.\nhttps://example.com/x")
        self.assertEqual(len(result["facets"]), 1)
        facet = result["facets"][0]
        self.assertEqual(facet["features"][0]["uri"], "https://example.com/x")
        idx = facet["index"]
        text_bytes = result["text"].encode("utf-8")
        self.assertEqual(text_bytes[idx["byteStart"]:idx["byteEnd"]].decode("utf-8"),
                         "https://example.com/x")

    def test_link_already_in_body_is_not_duplicated(self):
        body = "See https://example.com/x for details."
        result = richmessage.build(body, link="https://example.com/x")
        self.assertEqual(result["text"], body)
        self.assertEqual(result["text"].count("https://example.com/x"), 1)

    def test_over_limit_body_is_truncated_but_link_survives(self):
        body = "word " * 200  # far over 300 graphemes
        result = richmessage.build(body, link="https://example.com/x", max_graphemes=60)
        self.assertLessEqual(richmessage.grapheme_len(result["text"]), 60)
        self.assertTrue(result["text"].endswith("https://example.com/x"))

    def test_body_alone_never_exceeds_max_graphemes(self):
        body = "x" * 500
        result = richmessage.build(body, max_graphemes=300)
        self.assertLessEqual(richmessage.grapheme_len(result["text"]), 300)


if __name__ == "__main__":
    unittest.main()
