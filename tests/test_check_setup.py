import json
import tempfile
import unittest
from pathlib import Path

import check_setup as cs
from common import APIAuthError, APINetworkError, ConfigError, Paths
from reader_api import ReaderNetworkError
from tests.support import OPML


class FakeClient:
    def __init__(self, token, accepted=True, error=None):
        self.accepted, self.error = accepted, error

    def check_token(self):
        if self.error:
            raise self.error
        return self.accepted


class ChecksTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.paths = Paths(root / "project")
        self.paths.base.mkdir()
        self.downloads = root / "Downloads"
        self.downloads.mkdir()

    def test_old_python_is_a_problem_new_python_is_fine(self):
        self.assertTrue(cs.check_python((3, 10, 0)).ok)
        self.assertFalse(cs.check_python((3, 9, 6)).ok)

    def test_missing_feedparser(self):
        def importer():
            raise ImportError("no")

        self.assertFalse(cs.check_feedparser(importer).ok)
        self.assertTrue(cs.check_feedparser(lambda: None).ok)

    def test_readwise_token_missing_refused_unreachable_and_fine(self):
        def missing():
            raise ConfigError("No Readwise token in the Keychain.")

        self.assertIn("No Readwise token", cs.check_readwise(missing).detail)
        self.assertFalse(cs.check_readwise(lambda: "t", lambda t: FakeClient(t, False)).ok)
        offline = cs.check_readwise(lambda: "t", lambda t: FakeClient(
            t, error=ReaderNetworkError("down")))
        self.assertFalse(offline.ok)
        self.assertIn("online", offline.detail)
        self.assertTrue(cs.check_readwise(lambda: "t", FakeClient).ok)

    def test_anthropic_key_missing_refused_failing_and_fine(self):
        def missing():
            raise ConfigError("No Anthropic API key")

        self.assertFalse(cs.check_anthropic(missing).ok)

        def refused(*a, **k):
            raise APIAuthError("401")

        def down(*a, **k):
            raise APINetworkError("offline")

        self.assertIn("refused", cs.check_anthropic(lambda: "k", refused).detail)
        self.assertFalse(cs.check_anthropic(lambda: "k", down).ok)
        self.assertTrue(cs.check_anthropic(lambda: "k", lambda *a, **k: "OK").ok)

    def test_no_feed_list(self):
        check = cs.check_feeds(self.paths, self.downloads)
        self.assertFalse(check.ok)
        self.assertIn("export", check.detail)

    def test_feeds_but_none_in_the_digest_folder(self):
        (self.downloads / "Reader_Feeds.opml").write_text(
            OPML.replace("News Digest feeds", "Something else"))
        check = cs.check_feeds(self.paths, self.downloads)
        self.assertFalse(check.ok)
        self.assertIn("News Digest feeds", check.detail)

    def test_feed_list_with_a_digest_folder(self):
        (self.downloads / "Reader_Feeds.opml").write_text(OPML)
        check = cs.check_feeds(self.paths, self.downloads)
        self.assertTrue(check.ok)
        self.assertIn("2 feeds", check.detail)
        self.assertTrue(self.paths.opml.exists())   # a copy is kept in the project folder

    def test_profile_missing_empty_and_present(self):
        self.assertFalse(cs.check_profile(self.paths).ok)
        self.paths.profile.write_text(json.dumps({"topics": []}))
        self.assertFalse(cs.check_profile(self.paths).ok)
        self.paths.profile.write_text(json.dumps({"topics": [{"topic": "A"}]}))
        self.assertTrue(cs.check_profile(self.paths).ok)

    def test_jobs_not_loaded_is_only_a_note(self):
        self.assertIsNone(cs.check_jobs(lambda label: False).ok)
        self.assertTrue(cs.check_jobs(lambda label: True).ok)

    def test_the_report_counts_problems_and_the_exit_status_follows(self):
        checks = [cs.Check(True, "A", "fine"), cs.Check(False, "B", "broken"),
                  cs.Check(None, "C", "later")]
        text = cs.render(checks)
        self.assertIn("✓ A: fine", text)
        self.assertIn("✗ B: broken", text)
        self.assertIn("– C: later", text)
        self.assertIn("1 thing to fix", text)
        self.assertIn("Everything needed", cs.render([cs.Check(True, "A", "fine")]))


if __name__ == "__main__":
    unittest.main()
