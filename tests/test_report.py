import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import report
from common import APIAuthError, Paths, load_weekly
from tests.support import FakeReader, make_doc

OCT = report.month_bounds("2026-10")
NOW = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)
TODAY = "2026-11-02"
OPENED = "2026-10-15T12:00:00+00:00"


def read_doc(i, host="magazine.example", opened=OPENED, progress=0.0, words=900, **kw):
    """A feed item as Reader lists it after you opened it."""
    stamp = kw.pop("stamp", "2026-10-15T09:00:00+00:00")
    fields = {"id": f"d{i}", "created_at": stamp, "updated_at": OPENED,
              "first_opened_at": opened, "reading_progress": progress,
              "word_count": words, "site_name": host, "summary": "About things."}
    fields.update(kw)
    return make_doc(i, host, **fields)


class MonthTests(unittest.TestCase):
    def test_previous_month_crosses_the_year(self):
        self.assertEqual(report.previous_month("2026-11-02"), "2026-10")
        self.assertEqual(report.previous_month("2027-01-01"), "2026-12")

    def test_bounds_cover_the_whole_month_even_in_december(self):
        start, end = report.month_bounds("2026-12")
        self.assertEqual((start.year, start.month, start.day), (2026, 12, 1))
        self.assertEqual((end.year, end.month, end.day), (2027, 1, 1))
        self.assertEqual(report.month_days("2026-02"), 28)

    def test_a_month_is_due_once_it_is_over_and_not_before_or_twice(self):
        self.assertEqual(report.due_month({}, TODAY), "2026-10")
        self.assertEqual(report.due_month({"last_report_month": "2026-09"}, TODAY), "2026-10")
        self.assertIsNone(report.due_month({"last_report_month": "2026-10"}, TODAY))
        self.assertIsNone(report.due_month({"last_report_month": "2026-10"}, "2026-11-30"))
        self.assertEqual(report.due_month({"last_report_month": "2026-10"}, "2026-12-01"),
                         "2026-11")

    def test_the_year_waits_for_decembers_report(self):
        stored = {"2026-10": {}, "2026-12": {}}
        self.assertIsNone(report.due_year({"last_report_month": "2026-11"}, "2027-01-01", stored))
        self.assertEqual(report.due_year({"last_report_month": "2026-12"}, "2027-01-01", stored),
                         2026)
        self.assertIsNone(report.due_year({"last_report_month": "2026-12"}, "2026-12-31", stored))
        self.assertIsNone(report.due_year(
            {"last_report_month": "2026-12", "last_annual_year": 2026}, "2027-01-02", stored))
        self.assertIsNone(report.due_year({"last_report_month": "2026-12"}, "2027-01-01", {}))


class ReadingsTests(unittest.TestCase):
    def readings(self, docs):
        return report.readings(docs, *OCT)

    def test_only_what_you_opened_in_the_month_counts(self):
        docs = [read_doc(1), read_doc(2, opened="2026-09-30T12:00:00+00:00"),
                read_doc(3, opened="2026-11-01T12:00:00+00:00"),
                read_doc(4, opened=None)]
        self.assertEqual([r["id"] for r in self.readings(docs)], ["d1"])

    def test_an_article_you_saved_counts_even_if_unopened_but_an_email_does_not(self):
        docs = [read_doc(1, opened=None, category="article"),
                read_doc(2, opened=None, category="email"),
                read_doc(3, opened=None, category="article",
                         created_at="2026-09-01T00:00:00+00:00")]
        found = self.readings(docs)
        self.assertEqual([r["id"] for r in found], ["d1"])
        self.assertTrue(found[0]["saved"])
        self.assertFalse(found[0]["opened"])

    def test_highlights_made_in_the_month_count_for_their_article(self):
        docs = [read_doc(1)]
        docs += [{"id": f"h{n}", "parent_id": "d1", "category": "highlight",
                  "created_at": OPENED} for n in range(3)]
        docs.append({"id": "old", "parent_id": "d1", "category": "highlight",
                     "created_at": "2026-09-01T00:00:00+00:00"})
        self.assertEqual(self.readings(docs)[0]["highlights"], 3)

    def test_notes_this_script_saved_and_non_reading_are_left_out(self):
        docs = [read_doc(1, source="readwise-news-digest"),
                read_doc(2, source_url="https://readwise-news-digest.invalid/report/2026-09"),
                read_doc(3, category="podcast"), read_doc(4, parent_id="d9"),
                read_doc(5, source_url="")]
        self.assertEqual(self.readings(docs), [])

    def test_a_feed_item_and_its_saved_copy_count_once_with_the_stronger_reading(self):
        twin = read_doc(1, title="The Long Piece", progress=0.1, words=60)
        copy = read_doc(2, title="The long piece!", progress=1.0, words=2400,
                        category="article")
        found = self.readings([twin, copy])
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["progress"], found[0]["words"]), (1.0, 2400))
        self.assertTrue(found[0]["saved"] and found[0]["opened"])


class LabelTests(unittest.TestCase):
    def batch(self, n):
        return [{"id": f"i{k}", "title": f"T{k}", "label": "", "host": "x.com",
                 "summary": ""} for k in range(n)]

    def test_unknown_labels_become_other_and_global(self):
        reply = json.dumps([{"index": 0, "topic": "Basket weaving", "region": "Atlantis"},
                            {"index": 1, "topic": "History", "region": "East Asia"}])
        labels = report.label_batch(self.batch(3), "k", lambda *a, **kw: reply)
        self.assertEqual(labels[0], {"topic": "Other", "region": report.GLOBAL})
        self.assertEqual(labels[1], {"topic": "History", "region": "East Asia"})
        self.assertEqual(labels[2], {"topic": "Other", "region": report.GLOBAL})   # skipped

    def test_an_unreadable_reply_is_retried_on_halves(self):
        calls = []

        def call(key, messages, system=None, max_tokens=0):
            count = len(re.findall(r'<item index="\d+">', messages[0]["content"]))
            calls.append(count)
            if count > 15:
                return "not json"
            return json.dumps([{"index": n, "topic": "History", "region": "Europe"}
                               for n in range(count)])

        labels = report.label_batch(self.batch(40), "k", call)
        self.assertEqual(len(labels), 40)
        self.assertEqual(calls, [40, 20, 10, 10, 20, 10, 10])

    def test_a_small_batch_that_still_fails_raises(self):
        with self.assertRaises(report.ReportError):
            report.label_batch(self.batch(5), "k", lambda *a, **kw: "nope")

    def test_a_rejected_key_is_not_retried(self):
        def call(*a, **kw):
            raise APIAuthError("401")

        with self.assertRaises(APIAuthError):
            report.label_batch(self.batch(40), "k", call)

    def test_classify_reuses_the_cache_and_saves_after_each_batch(self):
        items = self.batch(report.CLASSIFY_BATCH + 5)
        cache = {"i0": {"topic": "History", "region": "Europe"}}
        saves, sizes = [], []

        def call(key, messages, system=None, max_tokens=0):
            count = len(re.findall(r'<item index="\d+">', messages[0]["content"]))
            sizes.append(count)
            return json.dumps([{"index": n, "topic": "History", "region": "Europe"}
                               for n in range(count)])

        report.classify(items, cache, "k", call, lambda: saves.append(len(cache)))
        self.assertEqual(sizes, [report.CLASSIFY_BATCH, 4])
        self.assertEqual(saves, [1 + report.CLASSIFY_BATCH, 55])   # the cache started with one


class ListTests(unittest.TestCase):
    def test_defaults_when_prefs_say_nothing(self):
        self.assertEqual(report.label_lists({}), (report.TOPICS, report.REGIONS))

    def test_prefs_replace_the_lists_but_the_catch_alls_stay(self):
        topics, regions = report.label_lists({"report_topics": ["Cooking"],
                                              "report_regions": ["Iceland"]})
        self.assertEqual(topics, ["Cooking", "Other"])
        self.assertEqual(regions, ["Iceland", report.GLOBAL])

    def test_the_lists_reach_the_prompt_and_the_checking(self):
        seen = {}

        def call(key, messages, system=None, max_tokens=0):
            seen["system"] = system
            return json.dumps([{"index": 0, "topic": "Cooking", "region": "Iceland"},
                               {"index": 1, "topic": "History", "region": "Iceland"}])

        batch = [{"id": "a", "title": "t", "label": "", "host": "x.com", "summary": ""},
                 {"id": "b", "title": "t2", "label": "", "host": "x.com", "summary": ""}]
        labels = report.label_batch(batch, "k", call, ["Cooking", "Other"],
                                    ["Iceland", report.GLOBAL])
        self.assertIn("Cooking", seen["system"])
        self.assertNotIn("Politics and government", seen["system"])
        self.assertEqual(labels[0], {"topic": "Cooking", "region": "Iceland"})
        self.assertEqual(labels[1], {"topic": "Other", "region": "Iceland"})   # not in the list


class NumbersTests(unittest.TestCase):
    def items(self):
        docs = [read_doc(1, "magazine.example", progress=1.0, words=2000, site_name="Example Magazine"),
                read_doc(2, "magazine.example", progress=0.5, words=600, site_name="Example Magazine"),
                read_doc(3, "nytimes.com", progress=0.0, site_name="New York Times"),
                read_doc(4, "nytimes.com", opened="2026-10-16T12:00:00+00:00", progress=0.2)]
        docs += [{"id": "h1", "parent_id": "d1", "category": "highlight", "created_at": OPENED},
                 {"id": "h2", "parent_id": "d1", "category": "highlight", "created_at": OPENED}]
        return report.readings(docs, *OCT)

    def test_summary_counts(self):
        items = self.items()
        labels = {i["id"]: {"topic": "History", "region": "Europe"} for i in items}
        stats = report.summarize(items, labels, 31)
        self.assertEqual((stats["items"], stats["opened"], stats["read"]), (4, 4, 2))
        self.assertEqual(stats["long_reads"], 1)              # only d1 is long and read
        self.assertEqual((stats["highlights"], stats["highlighted_items"]), (2, 1))
        self.assertEqual(stats["sources_total"], 2)
        self.assertEqual(stats["sources"]["magazine.example"],
                         {"label": "Example Magazine", "items": 2, "read": 2, "highlights": 2, "saved": 0})
        self.assertEqual(stats["topics"], {"History": 4})
        # 2000*1 + 600*0.5 + 900*0 + 900*0.2 words at 238 a minute
        self.assertEqual(stats["minutes"], round((2000 + 300 + 180) / 238))
        self.assertEqual(stats["active_days"], 2)
        self.assertEqual(sum(stats["weekdays"]), 4)
        self.assertEqual(stats["top_highlighted"][0]["highlights"], 2)

    def test_digest_stats_come_from_the_tracked_picks(self):
        tracked = {
            "a": {"tagged_date": "2026-10-03", "near_miss": False, "grade": 5,
                  "snapshot": {"first_opened_at": OPENED, "reading_progress": 1}},
            "b": {"tagged_date": "2026-10-04", "near_miss": True, "grade": 1,
                  "snapshot": {"first_opened_at": None, "reading_progress": 0}},
            "c": {"tagged_date": "2026-10-05", "near_miss": True, "grade": None,
                  "snapshot": {"first_opened_at": None},
                  "copy_snapshot": {"first_opened_at": OPENED, "reading_progress": 0.6}},
            "d": {"tagged_date": "2026-09-30", "near_miss": False, "grade": 4, "snapshot": {}},
        }
        digest = report.digest_stats(tracked, "2026-10-01", "2026-10-31")
        self.assertEqual((digest["picks"], digest["near_misses"]), (3, 2))
        self.assertEqual((digest["opened"], digest["read"], digest["near_misses_opened"]),
                         (2, 2, 1))
        self.assertEqual((digest["graded"], digest["grade_sum"]), (2, 6))

    def test_months_add_up(self):
        items = self.items()
        labels = {i["id"]: {"topic": "History", "region": "Europe"} for i in items}
        one = report.summarize(items, labels, 31)
        one["digest"] = {"picks": 2, "opened": 1}
        total = report.combine([one, one])
        self.assertEqual((total["months"], total["items"], total["read"]), (2, 8, 4))
        self.assertEqual(total["sources"]["magazine.example"]["items"], 4)
        self.assertEqual(total["sources"]["magazine.example"]["label"], "Example Magazine")
        self.assertEqual(total["topics"], {"History": 8})
        self.assertEqual(total["digest"]["picks"], 4)
        self.assertEqual(total["sources_total"], 2)


class RunCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = Paths(Path(self._tmp.name) / "project")
        self.paths.base.mkdir()
        self.reader = FakeReader([read_doc(1, progress=1.0), read_doc(2),
                                  read_doc(3, "nytimes.com", site_name="New York Times")])
        self.weekly = load_weekly(self.paths)
        self.label_calls = 0
        self.label_error = None
        self.said = []
        self.suggestions = None

    def fake_call(self, key, messages, system=None, max_tokens=0):
        self.label_calls += 1
        if self.label_error:
            raise self.label_error
        count = len(re.findall(r'<item index="\d+">', messages[0]["content"]))
        return json.dumps([{"index": n, "topic": "Economy, business and labor",
                            "region": "East Asia"} for n in range(count)])

    def monthly(self, **kw):
        kw.setdefault("now", NOW)
        kw.setdefault("today", TODAY)
        today, now = kw.pop("today"), kw.pop("now")
        return report.run_monthly(
            self.paths, self.reader, today, now, self.weekly, api_key=lambda: "key",
            call=self.fake_call, say=self.said.append,
            prepare_suggestions=lambda *a, **k: self.suggestions, **kw)

    def stored(self):
        return json.loads(self.paths.reports.read_text())


class MonthlyRunTests(RunCase):
    def test_delivers_the_due_month_and_remembers_it(self):
        result = self.monthly()
        self.assertEqual(result["status"], "delivered")
        call = self.reader.save_calls[0]
        self.assertEqual(call["title"], "News Digest Monthly Report — October 2026")
        self.assertEqual(call["url"], "https://readwise-news-digest.invalid/report/2026-10")
        self.assertEqual(call["location"], "later")
        self.assertEqual(call["tags"], ["newsdigest", "newsdigest-report"])
        for text in ("Sources you used most", "What you read about", "Where in the world",
                     "Economy, business and labor", "East Asia", "magazine.example"):
            self.assertIn(text, call["html"])
        self.assertEqual(self.weekly["last_report_month"], "2026-10")
        self.assertEqual(load_weekly(self.paths)["last_report_month"], "2026-10")
        self.assertEqual(self.stored()["2026-10"]["items"], 3)

    def test_nothing_happens_when_the_month_was_already_delivered(self):
        self.weekly["last_report_month"] = "2026-10"
        self.assertEqual(self.monthly()["status"], "not due")
        self.assertEqual(self.reader.save_calls, [])
        self.assertEqual(self.label_calls, 0)

    def test_a_month_that_is_not_over_is_only_a_preview(self):
        result = self.monthly(month="2026-10", now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc),
                              today="2026-10-20")
        self.assertEqual(result["status"], "preview")
        self.assertEqual(self.reader.save_calls, [])
        self.assertNotIn("last_report_month", load_weekly(self.paths))
        self.assertFalse(self.paths.reports.exists())

    def test_a_dry_run_delivers_and_remembers_nothing_but_prints_the_report(self):
        result = self.monthly(dry_run=True)
        self.assertEqual(result["status"], "dry run")
        self.assertEqual(self.reader.save_calls, [])
        self.assertFalse(self.paths.reports.exists())
        self.assertFalse(self.paths.weekly.exists())
        self.assertIn("News Digest Monthly Report — October 2026", "\n".join(self.said))

    def test_the_report_says_how_the_program_ran(self):
        self.paths.run_log.write_text(
            "2026-10-05 07:07 tagged=28 scored=1 graded=0 learned=0.0\n"
            "2026-10-06 03:00 daily problem=profile\n")
        self.monthly(dry_run=True)
        said = "\n".join(self.said)
        self.assertIn("the hourly job ran on 1 day and the daily job on 1 day", said)
        self.assertIn("Problems reported: profile.", said)

    def test_html_can_be_drawn_to_a_file(self):
        target = Path(self._tmp.name) / "r.html"
        self.monthly(dry_run=True, html_path=str(target))
        self.assertIn("<h2>Sources you used most</h2>", target.read_text())

    def test_a_failed_labelling_changes_nothing_so_tomorrow_retries(self):
        self.label_error = RuntimeError("API error 500")
        with self.assertRaises(report.ReportError):
            self.monthly()
        self.assertEqual(self.reader.save_calls, [])
        self.assertFalse(self.paths.reports.exists())
        self.assertNotIn("last_report_month", self.weekly)

    def test_labels_are_remembered_so_a_retry_costs_nothing_more(self):
        self.monthly(dry_run=True)
        calls = self.label_calls
        self.monthly(dry_run=True)
        self.assertEqual(self.label_calls, calls)

    def test_a_month_with_no_reading_gets_no_report_but_is_marked_done(self):
        self.reader.docs.clear()
        self.assertEqual(self.monthly()["status"], "nothing read")
        self.assertEqual(self.reader.save_calls, [])
        self.assertEqual(load_weekly(self.paths)["last_report_month"], "2026-10")

    def test_the_comparison_with_last_month_uses_the_kept_numbers(self):
        self.paths.reports.write_text(json.dumps({"2026-09": {"items": 50, "read": 20}}))
        self.monthly()
        self.assertIn("Last month: 50 articles touched, 20 read",
                      self.reader.save_calls[0]["html"])

    def test_suggestions_ride_along_and_are_then_remembered(self):
        self.suggestions = [{"name": "new.com", "host": "new.com", "reason": "You saved 4.",
                             "feed_url": "https://new.com/feed"}]
        self.monthly()
        call = self.reader.save_calls[0]
        self.assertIn("Sites to consider following", call["html"])
        self.assertIn("https://new.com/feed", call["html"])
        self.assertEqual(call["tags"], ["newsdigest", "newsdigest-report",
                                        "newsdigest-suggestions"])
        saved = load_weekly(self.paths)
        self.assertEqual(saved["last_suggestions_date"], TODAY)
        self.assertEqual(saved["suggested"], {"new.com": [TODAY]})

    def test_due_suggestions_with_nothing_to_offer_still_count_as_done(self):
        self.suggestions = []
        self.monthly()
        self.assertNotIn("Sites to consider", self.reader.save_calls[0]["html"])
        self.assertEqual(load_weekly(self.paths)["last_suggestions_date"], TODAY)

    def test_suggestions_that_are_not_due_leave_no_trace(self):
        self.monthly()
        self.assertNotIn("last_suggestions_date", load_weekly(self.paths))
        self.assertNotIn("Sites to consider", self.reader.save_calls[0]["html"])

    def test_a_report_already_in_reader_is_left_alone_and_suggestions_are_not_recorded(self):
        self.suggestions = [{"name": "new.com", "host": "new.com", "reason": "r",
                             "feed_url": "https://new.com/feed"}]
        self.reader.docs["old"] = make_doc(
            "old", id="old", source_url="https://readwise-news-digest.invalid/report/2026-10")
        self.assertEqual(self.monthly()["status"], "already delivered")
        self.assertEqual(load_weekly(self.paths)["suggested"], {})
        self.assertEqual(self.weekly["last_report_month"], "2026-10")

    def test_the_digest_picks_are_reported_when_there_are_some(self):
        self.paths.tracked.write_text(json.dumps({"a": {
            "tagged_date": "2026-10-03", "near_miss": False, "grade": 4,
            "snapshot": {"first_opened_at": OPENED, "reading_progress": 0.9}}}))
        self.monthly()
        self.assertIn("The digest tagged 1 article", self.reader.save_calls[0]["html"])


class AnnualRunTests(RunCase):
    def setUp(self):
        super().setUp()
        self.weekly["last_report_month"] = "2026-12"

    def keep(self, *months):
        kept = {}
        for month in months:
            items = report.readings([read_doc(1, progress=1.0), read_doc(2)], *OCT)
            labels = {i["id"]: {"topic": "History", "region": "Europe"} for i in items}
            kept[month] = report.summarize(items, labels, 30)
            kept[month]["digest"] = {}
        self.paths.reports.write_text(json.dumps(kept))

    def annual(self, **kw):
        return report.run_annual(self.paths, self.reader, "2027-01-01", self.weekly,
                                 say=self.said.append, **kw)

    def test_adds_up_the_kept_months_into_one_note(self):
        self.keep("2026-10", "2026-11", "2026-12")
        result = self.annual()
        self.assertEqual(result["status"], "delivered")
        call = self.reader.save_calls[0]
        self.assertEqual(call["title"], "News Digest Annual Report — 2026")
        self.assertEqual(call["url"], "https://readwise-news-digest.invalid/report/2026")
        self.assertEqual(call["tags"], ["newsdigest", "newsdigest-report"])
        self.assertIn("Month by month", call["html"])
        self.assertIn("covers October 2026 to December 2026", call["html"])
        self.assertEqual(load_weekly(self.paths)["last_annual_year"], 2026)

    def test_waits_until_decembers_monthly_report_is_out(self):
        self.keep("2026-10", "2026-11")
        self.weekly["last_report_month"] = "2026-11"
        self.assertEqual(self.annual()["status"], "not due")
        self.assertEqual(self.reader.save_calls, [])

    def test_a_dry_run_changes_nothing(self):
        self.keep("2026-12")
        self.assertEqual(self.annual(dry_run=True)["status"], "dry run")
        self.assertEqual(self.reader.save_calls, [])
        self.assertNotIn("last_annual_year", self.weekly)

    def test_a_full_year_has_no_missing_months_note(self):
        self.keep(*[f"2026-{m:02d}" for m in range(1, 13)])
        self.annual()
        self.assertNotIn("covers", self.reader.save_calls[0]["html"])


if __name__ == "__main__":
    unittest.main()
