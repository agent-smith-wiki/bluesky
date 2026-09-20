"""Behavioral tests for the Bluesky publisher's sibling-state reconciliation.

Tests run against a temporary root directory so they never touch the real
repo state. Bluesky API calls and git push are mocked; state files are real.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "publish"))

from publish import main, state_path, load_state

FAKE_SESSION = {"service": "https://bsky.social", "did": "did:plc:test",
                "handle": "test.example", "access_jwt": "jwt"}
ENV = {"BLUESKY_HANDLE": "test.example", "BLUESKY_APP_PASSWORD": "app-password"}


class PublishTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "posts"))

    def tearDown(self):
        self.tmp.cleanup()

    def _write_post(self, slug: str, body: str = "Hello", extra_fm: str = "") -> str:
        path = os.path.join(self.root, "posts", f"{slug}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"---\n{extra_fm}---\n\n{body}\n")
        return path

    def _write_state(self, slug: str, record: dict) -> str:
        path = state_path(slug, self.root)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return path

    @patch.dict(os.environ, ENV)
    def test_published_state_is_skipped(self):
        """A post already recorded as published is not sent again."""
        self._write_post("2026-09-15-example")
        self._write_state("2026-09-15-example",
                          {"uri": "at://did:plc:test/app.bsky.feed.post/abc",
                           "cid": "bafycid", "url": "https://bsky.app/profile/test.example/post/abc"})
        fake_bsky = MagicMock()
        with patch("publish.bluesky.login", return_value=FAKE_SESSION), \
             patch("publish.bluesky.post", fake_bsky):
            main(argv=["--no-push"], root=self.root)
            fake_bsky.assert_not_called()

    @patch.dict(os.environ, ENV)
    def test_claim_before_send(self):
        """The sending state file exists before the Bluesky API is called."""
        self._write_post("2026-09-20-claim")
        captured = []

        def send_and_capture(session, text, **kwargs):
            captured.append(load_state("2026-09-20-claim", self.root))
            return {"uri": "at://did:plc:test/app.bsky.feed.post/42", "cid": "bafycid",
                    "url": "https://bsky.app/profile/test.example/post/42"}

        with patch("publish.bluesky.login", return_value=FAKE_SESSION), \
             patch("publish.bluesky.post", side_effect=send_and_capture):
            main(argv=["--no-push"], root=self.root)

        self.assertEqual(captured, [{"status": "sending"}])
        state = load_state("2026-09-20-claim", self.root)
        self.assertEqual(state["uri"], "at://did:plc:test/app.bsky.feed.post/42")
        self.assertEqual(state["url"], "https://bsky.app/profile/test.example/post/42")

    @patch.dict(os.environ, ENV)
    def test_failed_state_is_skipped(self):
        """A post in the failed state is never auto-retried."""
        self._write_post("2026-09-20-fail")
        self._write_state("2026-09-20-fail", {"status": "failed", "error": "boom"})
        fake_post = MagicMock()
        with patch("publish.bluesky.login", return_value=FAKE_SESSION), \
             patch("publish.bluesky.post", fake_post):
            main(argv=["--no-push"], root=self.root)
            fake_post.assert_not_called()

    @patch.dict(os.environ, ENV)
    def test_manual_reset_allows_retry(self):
        """Removing a failed state file lets the publisher retry that post."""
        self._write_post("2026-09-20-retry")
        self._write_state("2026-09-20-retry", {"status": "failed", "error": "boom"})
        fake_post = MagicMock(return_value={
            "uri": "at://did:plc:test/app.bsky.feed.post/99", "cid": "bafycid",
            "url": "https://bsky.app/profile/test.example/post/99"})

        with patch("publish.bluesky.login", return_value=FAKE_SESSION), \
             patch("publish.bluesky.post", fake_post):
            main(argv=["--no-push"], root=self.root)
            self.assertEqual(fake_post.call_count, 0)

            os.remove(state_path("2026-09-20-retry", self.root))
            main(argv=["--no-push"], root=self.root)
            self.assertEqual(fake_post.call_count, 1)

        state = load_state("2026-09-20-retry", self.root)
        self.assertEqual(state["uri"], "at://did:plc:test/app.bsky.feed.post/99")

    @patch.dict(os.environ, ENV)
    def test_multiple_posts_published_in_order(self):
        """Oldest-first publishing creates independent state files per post."""
        self._write_post("2026-09-18-a", body="Post A")
        self._write_post("2026-09-19-b", body="Post B")
        fake_post = MagicMock(side_effect=[
            {"uri": "at://did:plc:test/app.bsky.feed.post/1", "cid": "c1",
             "url": "https://bsky.app/profile/test.example/post/1"},
            {"uri": "at://did:plc:test/app.bsky.feed.post/2", "cid": "c2",
             "url": "https://bsky.app/profile/test.example/post/2"},
        ])
        with patch("publish.bluesky.login", return_value=FAKE_SESSION), \
             patch("publish.bluesky.post", fake_post):
            main(argv=["--no-push"], root=self.root)

        texts = [c.args[1] for c in fake_post.call_args_list]
        self.assertEqual(texts, ["Post A", "Post B"])
        self.assertEqual(load_state("2026-09-18-a", self.root)["uri"],
                         "at://did:plc:test/app.bsky.feed.post/1")
        self.assertEqual(load_state("2026-09-19-b", self.root)["uri"],
                         "at://did:plc:test/app.bsky.feed.post/2")

    @patch.dict(os.environ, ENV)
    def test_reply_deferred_when_parent_not_yet_published(self):
        """A reply whose parent post has no published state is deferred: left
        unclaimed (no state file at all), never recorded as failed."""
        self._write_post("2026-09-21-reply", body="Reply text",
                         extra_fm="reply_to: 2026-09-20-parent\n")
        fake_post = MagicMock()

        with patch("publish.bluesky.login", return_value=FAKE_SESSION), \
             patch("publish.bluesky.post", fake_post):
            main(argv=["--no-push"], root=self.root)

        fake_post.assert_not_called()
        self.assertIsNone(load_state("2026-09-21-reply", self.root))

    @patch.dict(os.environ, ENV)
    def test_reply_threaded_once_parent_is_published(self):
        """Once the parent's sibling state carries a uri (a later commit adds
        the reply, or a later CI run retries it), the reply resolves reply_to
        to the parent's AT-URI and publishes."""
        self._write_post("2026-09-20-parent", body="Parent post")
        fake_post = MagicMock(return_value={
            "uri": "at://did:plc:test/app.bsky.feed.post/1", "cid": "c1",
            "url": "https://bsky.app/profile/test.example/post/1"})
        with patch("publish.bluesky.login", return_value=FAKE_SESSION), \
             patch("publish.bluesky.post", fake_post):
            main(argv=["--no-push"], root=self.root)  # publishes the parent

        self._write_post("2026-09-21-reply", body="Reply text",
                         extra_fm="reply_to: 2026-09-20-parent\n")
        fake_post.return_value = {
            "uri": "at://did:plc:test/app.bsky.feed.post/2", "cid": "c2",
            "url": "https://bsky.app/profile/test.example/post/2"}
        with patch("publish.bluesky.login", return_value=FAKE_SESSION), \
             patch("publish.bluesky.post", fake_post):
            main(argv=["--no-push"], root=self.root)  # publishes the reply, threaded

        self.assertEqual(fake_post.call_count, 2)
        reply_kwargs = fake_post.call_args_list[1].kwargs
        self.assertEqual(reply_kwargs["reply_to"], "at://did:plc:test/app.bsky.feed.post/1")
        self.assertEqual(load_state("2026-09-21-reply", self.root)["uri"],
                         "at://did:plc:test/app.bsky.feed.post/2")

    @patch.dict(os.environ, ENV)
    def test_link_frontmatter_reaches_render(self):
        """A post with `link:` front-matter gets a faceted, appended link."""
        self._write_post("2026-09-20-link", body="Check this out",
                         extra_fm="link: https://example.com/x\n")
        fake_post = MagicMock(return_value={
            "uri": "at://did:plc:test/app.bsky.feed.post/7", "cid": "c7",
            "url": "https://bsky.app/profile/test.example/post/7"})
        with patch("publish.bluesky.login", return_value=FAKE_SESSION), \
             patch("publish.bluesky.post", fake_post):
            main(argv=["--no-push"], root=self.root)

        call = fake_post.call_args_list[0]
        self.assertEqual(call.args[1], "Check this out\nhttps://example.com/x")
        self.assertEqual(len(call.kwargs["facets"]), 1)

    def test_dry_run_sends_nothing_and_writes_no_state(self):
        """--dry-run never touches the network or creates state files."""
        slug = "2026-09-15-example"
        self._write_post(slug)
        with patch("publish.bluesky.login") as mock_login:
            main(argv=["--dry-run"], root=self.root)
            mock_login.assert_not_called()
        self.assertIsNone(load_state(slug, self.root))


class CommitPushTestCase(unittest.TestCase):
    @patch("publish.subprocess.run")
    def test_rebase_and_retry_on_non_fast_forward(self, mock_run):
        """_commit_push fetches/rebases when origin advanced since commit."""
        from publish import _commit_push

        responses = [
            MagicMock(returncode=0),                           # git add
            MagicMock(returncode=0),                           # git commit
            MagicMock(returncode=1, stderr="non-fast-forward"), # push fails
            MagicMock(returncode=0),                           # git fetch origin
            MagicMock(returncode=0),                           # git rebase origin/main
            MagicMock(returncode=0),                           # push succeeds
        ]
        mock_run.side_effect = responses

        _commit_push("claim x [skip ci]", ["posts/x.state.json"])

        calls = [c.args[0] for c in mock_run.call_args_list]
        self.assertEqual(calls, [
            ["git", "add", "posts/x.state.json"],
            ["git", "commit", "-m", "claim x [skip ci]"],
            ["git", "push"],
            ["git", "fetch", "origin"],
            ["git", "rebase", "origin/main"],
            ["git", "push"],
        ])

    @patch("publish.subprocess.run")
    def test_raises_after_exhausted_retries(self, mock_run):
        """_commit_push gives up after repeated non-fast-forward failures."""
        from publish import _commit_push

        responses = [
            MagicMock(returncode=0),                           # git add
            MagicMock(returncode=0),                           # git commit
        ] + [
            MagicMock(returncode=1, stderr="non-fast-forward"), # push fails
            MagicMock(returncode=0),                           # fetch
            MagicMock(returncode=0),                           # rebase
        ] * 5 + [
            MagicMock(returncode=1, stderr="non-fast-forward"), # final push fails
        ]
        mock_run.side_effect = responses

        with self.assertRaises(RuntimeError) as ctx:
            _commit_push("claim x [skip ci]", ["posts/x.state.json"])

        self.assertIn("git push failed", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
