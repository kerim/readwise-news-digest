import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import profile_builder as pb
from common import Paths

TODAY = "2026-12-01"
NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)

NOTE = """\
Title: [[A Piece About Cities]]
Authors: Some Author
Category: #articles
Date: [[2026-11-20]]
Source URL: https://www.example.com/story
Tags: #>>city-politics #>>zoning #misc
Number of Highlights: ==5==
"""


def entry(title, grade, since, host="a.com", tagged="2026-10-01"):
    return {"title": title, "grade": grade, "grade_since": since,
            "host": host, "tagged_date": tagged}


class NoteTests(unittest.TestCase):
    def test_parses_the_header_of_a_vault_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "n.md"
            path.write_text(NOTE)
            meta = pb.parse_note(path)
        self.assertEqual(meta["title"], "A Piece About Cities")
        self.assertEqual(meta["date"], "2026-11-20")
        self.assertEqual(meta["num_highlights"], 5)
        self.assertEqual(meta["tags"], [">>city-politics", ">>zoning", "misc"])

    def test_an_unreadable_note_is_skipped(self):
        self.assertIsNone(pb.parse_note("/no/such/file.md"))

    def test_domains(self):
        self.assertEqual(pb.extract_domain("https://www.example.com/x"), "example.com")
        self.assertIsNone(pb.extract_domain("not a url"))
        self.assertIsNone(pb.extract_domain(None))

    def test_recency_decays_with_a_half_life(self):
        self.assertAlmostEqual(pb.recency_weight("2026-12-01", NOW), 1.0)
        self.assertAlmostEqual(pb.recency_weight("2026-06-04", NOW), 0.5, places=1)
        self.assertEqual(pb.recency_weight("garbage", NOW), 0.1)

    def test_domain_profile_skips_archive_and_social_sites(self):
        notes = [{"source_url": "https://archive.ph/x", "date": "2026-11-30"},
                 {"source_url": "https://www.nytimes.com/a", "date": "2026-11-30"},
                 {"source_url": "https://nytimes.com/b", "date": "2026-11-29"}]
        profile = pb.build_domain_profile(notes, NOW)
        self.assertEqual([d["domain"] for d in profile], ["nytimes.com"])
        self.assertEqual(profile[0]["count"], 2)

    def test_topic_profile_uses_only_tags_with_the_prefix_and_adds_snippets(self):
        notes = [{"tags": [">>city-politics", "misc"], "date": "2026-11-30",
                  "num_highlights": 10}]
        topics = pb.build_topic_profile(notes, NOW, qmd_query=lambda tag: ["snip"],
                                        prefix=">>")
        self.assertEqual([t["tag"] for t in topics], ["city politics"])
        self.assertEqual(topics[0]["snippets"], ["snip"])

    def test_with_no_prefix_every_tag_is_a_topic(self):
        notes = [{"tags": ["city-politics", "misc"], "date": "2026-11-30",
                  "num_highlights": 10}]
        topics = pb.build_topic_profile(notes, NOW, qmd_query=lambda tag: [])
        self.assertEqual(sorted(t["tag"] for t in topics), ["city politics", "misc"])

    def test_the_prefix_comes_from_prefs(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp))
            self.assertEqual(pb.topic_prefix(paths), "")
            paths.prefs.write_text(json.dumps({"vault_topic_prefix": ">>"}))
            self.assertEqual(pb.topic_prefix(paths), ">>")


class SignalTests(unittest.TestCase):
    def test_recent_interests_are_grades_four_and_five_in_the_last_60_days(self):
        tracked = {
            "a": entry("Highlighted", 5, "2026-11-28"),
            "b": entry("Read long", 4, "2026-11-30"),
            "c": entry("Shortlisted only", 3, "2026-11-30"),
            "d": entry("Old fan", 5, "2026-09-01"),
            "e": entry("Ungraded", None, None),
        }
        self.assertEqual(pb.recent_interests(tracked, TODAY),
                         ["Read long (a.com)", "Highlighted (a.com)"])

    def test_less_of_this_is_only_grade_one(self):
        tracked = {
            "a": entry("Skipped", 1, "2026-11-28"),
            "b": entry("Ignored a month", 1.5, "2026-11-29"),
            "c": entry("Skipped long ago", 1, "2026-08-01"),
            "d": entry("Liked", 5, "2026-11-30"),
        }
        self.assertEqual(pb.less_of_this(tracked, TODAY), ["Skipped"])

    def test_most_recent_first_and_capped_at_thirty(self):
        tracked = {f"i{n}": entry(f"T{n}", 5, f"2026-11-{n % 28 + 1:02d}",
                                  tagged=f"2026-10-{n % 28 + 1:02d}")
                   for n in range(45)}
        titles = pb.recent_interests(tracked, TODAY)
        self.assertEqual(len(titles), 30)
        self.assertTrue(titles[0].startswith("T"))

    def test_the_window_follows_when_the_grade_arrived_not_when_tagged(self):
        tracked = {"a": entry("Late bloomer", 5, "2026-11-30", tagged="2026-06-01")}
        self.assertEqual(pb.recent_interests(tracked, TODAY),
                         ["Late bloomer (a.com)"])


class PromptTests(unittest.TestCase):
    topics = [{"tag": "city politics", "score": 1.0, "count": 3, "snippets": ["s"]}]

    def test_signal_sections_appear_only_when_there_is_something_to_say(self):
        bare = pb.build_prompt(self.topics)
        self.assertNotIn("Recent Interests", bare)
        self.assertNotIn("Less Of This", bare)
        full = pb.build_prompt(self.topics, ["Loved (a.com)"], ["Skipped"])
        self.assertIn("## Recent Interests", full)
        self.assertIn("- Loved (a.com)", full)
        self.assertIn("## Less Of This", full)
        self.assertIn("- Skipped", full)

    def test_synthesis_parses_a_list_or_a_wrapped_object(self):
        wrapped = json.dumps({"topics": [{"topic": "T", "tags": [], "score": 1}]})
        got = pb.synthesize_topics(self.topics, "key",
                                   call=lambda *a, **k: wrapped)
        self.assertEqual(got[0]["topic"], "T")

    def test_bad_json_falls_back_to_the_raw_tags(self):
        got = pb.synthesize_topics(self.topics, "key", call=lambda *a, **k: "oops")
        self.assertEqual(got[0]["topic"], "city politics")
        self.assertEqual(got[0]["score"], 1.0)

    def test_the_prompt_reaches_the_model_with_both_signals(self):
        seen = {}

        def call(api_key, messages, max_tokens=0):
            seen["prompt"] = messages[0]["content"]
            return "[]"
        pb.synthesize_topics(self.topics, "key", ["Loved"], ["Skipped"], call=call)
        self.assertIn("Loved", seen["prompt"])
        self.assertIn("Skipped", seen["prompt"])


class MainTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.paths = Paths(root / "project")
        self.paths.base.mkdir()
        self.vault = root / "vault"
        (self.vault / "Readwise" / "Articles").mkdir(parents=True)
        for n in range(pb.MIN_VAULT_FILES):
            (self.vault / "Readwise" / "Articles" / f"n{n}.md").write_text(NOTE)
        self.paths.prefs.write_text(json.dumps({"vault_dir": str(self.vault)}))
        self.notices = []
        self.report_calls = 0
        self.report_error = None
        self.annual_calls = 0
        patches = [
            mock.patch.object(pb, "Paths", lambda: self.paths),
            mock.patch.object(pb, "get_api_key", lambda: "key"),
            mock.patch.object(pb, "get_readwise_token", lambda: "tok"),
            mock.patch.object(pb, "ReaderClient", lambda token: object()),
            mock.patch.object(pb, "notify", lambda t, m: self.notices.append(m)),
            mock.patch.object(pb, "setup_logging", lambda paths: None),
            mock.patch.object(pb, "local_today", lambda: TODAY),
            mock.patch.object(pb.report, "run_monthly", self.fake_monthly),
            mock.patch.object(pb.report, "run_annual", self.fake_annual),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_monthly(self, *args, **kwargs):
        self.report_calls += 1
        if self.report_error:
            raise self.report_error

    def fake_annual(self, *args, **kwargs):
        self.annual_calls += 1

    def good_profile(self, *args, **kwargs):
        return {"generated": TODAY, "vault_notes_scanned": 100,
                "topics": [{"topic": "T", "tags": [], "score": 1}], "sources": []}

    def weekly(self):
        return json.loads(self.paths.weekly.read_text())

    def test_a_good_build_writes_the_profile_a_log_line_and_the_date(self):
        with mock.patch.object(pb, "build_profile", self.good_profile):
            self.assertEqual(pb.main([]), 0)
        profile = json.loads(self.paths.profile.read_text())
        self.assertEqual(profile["topics"][0]["topic"], "T")
        self.assertIn("profile topics=1 notes=100", self.paths.run_log.read_text())
        self.assertEqual(self.weekly()["last_profile_date"], TODAY)
        self.assertEqual(self.report_calls, 1)

    def test_each_run_leaves_a_daily_line_for_the_hourly_job(self):
        self.paths.weekly.write_text(json.dumps({"last_profile_date": "2026-11-29"}))
        pb.main([])
        self.assertRegex(self.paths.run_log.read_text(), r"\d{2}:\d{2} daily ok\n$")

    def test_a_failed_part_is_named_in_the_daily_line(self):
        self.paths.weekly.write_text(json.dumps({"last_profile_date": "2026-11-29"}))
        self.report_error = RuntimeError("no")
        pb.main([])
        self.assertIn("daily problem=monthly-report", self.paths.run_log.read_text())

    def test_a_stopped_hourly_job_is_notified(self):
        self.paths.weekly.write_text(json.dumps({"last_profile_date": "2026-11-29"}))
        self.paths.run_log.write_text(
            "2026-11-20 10:07 tagged=0 scored=0 graded=0 learned=0.0\n"
            "2026-11-28 03:00 daily ok\n")
        self.assertEqual(pb.main([]), 1)
        self.assertTrue(any("hourly job has not run" in m for m in self.notices))
        self.assertIn("daily problem=hourly-stopped", self.paths.run_log.read_text())

    def test_a_profile_built_this_week_is_left_alone_but_the_reports_still_run(self):
        self.paths.weekly.write_text(json.dumps({"last_profile_date": "2026-11-29"}))
        self.paths.profile.write_text('{"topics": [{"topic": "kept"}]}')
        self.assertEqual(pb.main([]), 0)
        self.assertIn("kept", self.paths.profile.read_text())
        self.assertEqual(self.report_calls, 1)

    def test_a_profile_a_week_old_is_rebuilt(self):
        self.paths.weekly.write_text(json.dumps({"last_profile_date": "2026-11-24"}))
        with mock.patch.object(pb, "build_profile", self.good_profile):
            pb.main([])
        self.assertEqual(self.weekly()["last_profile_date"], TODAY)

    def test_force_rebuilds_even_when_not_due(self):
        self.paths.weekly.write_text(json.dumps({"last_profile_date": "2026-11-30"}))
        with mock.patch.object(pb, "build_profile", self.good_profile):
            pb.main(["--force"])
        self.assertTrue(self.paths.profile.exists())

    def test_too_few_notes_keeps_the_old_profile_notifies_and_retries_tomorrow(self):
        self.paths.profile.write_text('{"topics": [{"topic": "old"}]}')
        for f in list((self.vault / "Readwise" / "Articles").glob("*.md"))[5:]:
            f.unlink()
        self.assertEqual(pb.main([]), 1)
        self.assertIn("old", self.paths.profile.read_text())
        self.assertIn("minimum", self.notices[0])
        self.assertFalse(self.paths.weekly.exists())      # not remembered: due again
        self.assertEqual(self.report_calls, 1)           # one failure doesn't stop the other

    def test_an_empty_profile_never_replaces_a_good_one(self):
        self.paths.profile.write_text('{"topics": [{"topic": "old"}]}')
        empty = {"generated": TODAY, "vault_notes_scanned": 1, "topics": [],
                 "sources": []}
        with mock.patch.object(pb, "build_profile", lambda *a, **k: empty):
            self.assertEqual(pb.main([]), 1)
        self.assertIn("old", self.paths.profile.read_text())

    def test_a_missing_vault_is_reported(self):
        self.paths.prefs.write_text(json.dumps(
            {"vault_dir": str(Path(self._tmp.name) / "nowhere")}))
        self.assertEqual(pb.main([]), 1)
        self.assertIn("Vault directory not found", self.notices[0])

    def test_without_a_vault_in_prefs_the_profile_is_left_to_the_user_and_the_rest_runs(self):
        self.paths.prefs.unlink()
        self.paths.profile.write_text('{"topics": [{"topic": "mine"}]}')
        self.assertEqual(pb.main([]), 0)
        self.assertIn("mine", self.paths.profile.read_text())
        self.assertEqual(self.notices, [])
        self.assertEqual(self.report_calls, 1)

    def test_a_failed_monthly_report_is_reported_but_the_profile_is_still_built(self):
        self.report_error = RuntimeError("search down")
        with mock.patch.object(pb, "build_profile", self.good_profile):
            self.assertEqual(pb.main([]), 1)
        self.assertTrue(self.paths.profile.exists())
        self.assertIn("The monthly report failed: search down", self.notices[0])
        self.assertEqual(self.annual_calls, 1)            # the annual report is still tried

    def test_an_unreadable_weekly_file_stops_everything_and_is_never_overwritten(self):
        self.paths.weekly.write_text("{ not json")
        self.assertEqual(pb.main([]), 1)
        self.assertEqual(self.paths.weekly.read_text(), "{ not json")
        self.assertEqual(self.report_calls, 0)


if __name__ == "__main__":
    unittest.main()
