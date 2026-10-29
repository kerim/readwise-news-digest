import json
from datetime import timedelta

from common import parse_iso
from reader_api import ReaderNetworkError
from tests.support import NOW, RunTestCase, make_doc

OPENED = "2026-10-28T07:30:00+00:00"
AUTO_TAG = {"politics": {"name": "Politics", "type": "auto", "created": 1}}
OWN_TAG = {"newsdigest": {"name": "newsdigest", "type": "public_api", "created": 1}}
MINE = {"research": {"name": "research", "type": "manual", "created": 1}}


class GradeStepCase(RunTestCase):
    """Day one is tagged at 07:12; each test then plays the part of you."""

    def setUp(self):
        super().setUp()
        self.go()
        self.assertEqual(len(self.tracked()), 28)

    def later(self, hours=1):
        self.set_time(NOW + timedelta(hours=hours))

    def next_day(self):
        self.set_time(NOW + timedelta(days=1))

    def entry(self, doc_id="doc29"):
        return self.tracked()[doc_id]

    def add_copy(self, cid="copy1", parent="doc29", **kw):
        src = self.reader.docs[parent]
        stamp = self.reader.clock.isoformat()
        fields = {"id": cid, "category": "article", "location": "later",
                  "source_url": src["source_url"] + "?smid=share",
                  "title": src["title"], "word_count": 2200,
                  "created_at": stamp, "updated_at": stamp, "tags": {}}
        fields.update(kw)
        self.reader.docs[cid] = make_doc(cid, **fields)
        self.reader.docs[cid]["id"] = cid
        return cid

    def fresh_docs(self, n=5):
        stamp = (self.now - timedelta(hours=2)).isoformat()
        for i in range(n):
            self.reader.docs[f"docnew{i}"] = make_doc(
                f"new{i}", created_at=stamp, updated_at=stamp)


class SignalTests(GradeStepCase):
    def test_nothing_done_means_no_grades(self):
        self.later()
        self.assertEqual(self.go(), 0)
        self.assertTrue(all(e["grade"] is None for e in self.tracked().values()))
        self.assertIn("graded=0", self.run_log()[-1])

    def test_a_highlight_after_tagging_is_five_and_sticks(self):
        self.reader.highlight("doc29")
        self.later()
        self.go()
        self.assertEqual(self.entry()["grade"], 5)
        self.assertEqual(self.entry()["grade_reason"], "you highlighted it")
        self.later(2)
        self.go()                       # the highlight is no longer in any listing
        self.assertEqual(self.entry()["grade"], 5)

    def test_reading_a_long_article_is_four(self):
        self.reader.touch("doc29", word_count=2000, reading_progress=0.8,
                          first_opened_at=OPENED, location="archive")
        self.later()
        self.go()
        self.assertEqual(self.entry()["grade"], 4)

    def test_shortlisting_is_three_and_survives_archiving(self):
        self.reader.touch("doc29", location="shortlist")
        self.later()
        self.go()
        self.assertEqual(self.entry()["grade"], 3)
        self.reader.touch("doc29", location="archive")
        self.later(2)
        self.go()
        self.assertTrue(self.entry()["snapshot"]["shortlisted"])
        self.assertEqual(self.entry()["grade"], 3)

    def test_archiving_without_opening_is_one(self):
        self.reader.touch("doc29", location="archive")
        self.later()
        self.go()
        self.assertEqual(self.entry()["grade"], 1)

    def test_opening_and_archiving_a_teaser_is_no_grade(self):
        self.reader.touch("doc29", word_count=58, first_opened_at=OPENED,
                          location="archive")
        self.later()
        self.go()
        self.assertIsNone(self.entry()["grade"])
        self.assertIn("short", self.entry()["grade_reason"])

    def test_a_tag_of_yours_is_five_but_an_auto_tag_is_not(self):
        self.reader.touch("doc29", tags={**OWN_TAG, **AUTO_TAG})
        self.later()
        self.go()
        self.assertIsNone(self.entry()["grade"])
        self.reader.touch("doc29", tags={**OWN_TAG, **AUTO_TAG, **MINE})
        self.later(2)
        self.go()
        self.assertEqual(self.entry()["grade"], 5)

    def test_grade_changes_are_reported(self):
        self.reader.highlight("doc29")
        self.later()
        self.go()
        self.assertIn("you highlighted it", self.said())
        self.assertIn("1 grades changed", self.said())

    def test_a_quiet_hour_changes_nothing_and_a_repeat_is_harmless(self):
        self.reader.highlight("doc29")
        self.later()
        self.go()
        before = json.dumps(self.tracked(), sort_keys=True)
        self.go()
        self.assertEqual(json.dumps(self.tracked(), sort_keys=True), before)

    def test_grade_since_records_when_the_grade_arrived(self):
        self.reader.highlight("doc29")
        self.later()
        self.go()
        self.assertEqual(self.entry()["grade_since"], "2026-10-28")

    def test_run_log_carries_graded_and_learned(self):
        self.reader.highlight("doc29")
        self.later()
        self.go()
        self.assertIn("graded=1 learned=0.0", self.run_log()[-1])

    def test_learned_weights_file_is_written_empty_below_the_gate(self):
        self.later()
        self.go()
        weights = json.loads(self.paths.learned_weights.read_text())
        self.assertEqual(weights["topics"], {})


class SavedCopyTests(GradeStepCase):
    def test_a_copy_saved_after_tagging_is_linked_tagged_and_graded(self):
        self.later()
        self.add_copy(reading_progress=0.9, first_opened_at=OPENED,
                      location="archive")
        self.go()
        entry = self.entry()
        self.assertEqual(entry["copy_id"], "copy1")
        self.assertEqual(entry["copy_how"], "address")
        self.assertTrue(entry["copy_tagged"])
        self.assertIn("newsdigest-copy", self.reader.docs["copy1"]["tags"])
        self.assertEqual(entry["grade"], 4)       # 2,200 words, read 90%
        self.assertIn("your saved copy", entry["grade_reason"])

    def test_the_copy_tag_keeps_tags_you_already_put_on_it(self):
        self.later()
        self.add_copy(tags={"to-read": {"name": "To Read", "type": "manual"}})
        self.go()
        names = [t["name"] for t in self.reader.docs["copy1"]["tags"].values()]
        self.assertEqual(sorted(names), ["To Read", "newsdigest-copy"])
        self.assertEqual(self.entry()["grade"], 5)   # your own tag on the copy

    def test_an_unread_copy_is_still_at_least_three(self):
        self.later()
        self.add_copy()
        self.go()
        self.assertEqual(self.entry()["grade"], 3)

    def test_the_copy_is_not_tagged_twice(self):
        self.later()
        self.add_copy()
        self.go()
        calls = len(self.reader.set_calls)
        self.later(2)
        self.go()
        self.assertEqual(len(self.reader.set_calls), calls)

    def test_a_copy_and_its_highlights_arriving_together_count(self):
        self.later()
        self.add_copy()
        self.reader.highlight("copy1")
        self.go()
        self.assertEqual(self.entry()["grade"], 5)

    def test_a_copy_for_one_site_never_links_to_another_sites_item(self):
        self.later()
        self.add_copy(source_url="https://www.elsewhere.com/2026/article-29")
        self.go()
        self.assertIsNone(self.entry()["copy_id"])

    def test_a_failed_copy_tag_is_retried_next_hour(self):
        self.later()
        self.add_copy()
        self.reader.fail_set = ReaderNetworkError("down")
        self.go()
        self.assertEqual(self.entry()["copy_id"], "copy1")
        self.assertFalse(self.entry()["copy_tagged"])
        self.reader.fail_set = None
        self.later(2)
        self.go()
        self.assertTrue(self.entry()["copy_tagged"])
        self.assertIn("newsdigest-copy", self.reader.docs["copy1"]["tags"])

    def test_your_reading_of_the_copy_updates_the_grade_later(self):
        self.later()
        self.add_copy()
        self.go()
        self.assertEqual(self.entry()["grade"], 3)
        self.reader.touch("copy1", reading_progress=0.9, first_opened_at=OPENED)
        self.later(2)
        self.go()
        self.assertEqual(self.entry()["grade"], 4)


class DeletionTests(GradeStepCase):
    def test_a_deleted_item_is_graded_as_archived_then_frozen(self):
        self.reader.delete("doc29")
        self.next_day()
        self.go()
        self.assertTrue(self.entry()["item_gone"])
        self.assertEqual(self.entry()["grade"], 1)
        self.assertTrue(self.entry()["frozen"])
        self.assertFalse(self.entry("doc28")["frozen"])

    def test_a_deleted_item_you_read_keeps_its_read_grade(self):
        self.reader.touch("doc29", word_count=2000, reading_progress=0.8,
                          first_opened_at=OPENED)
        self.later()
        self.go()
        self.reader.delete("doc29")
        self.next_day()
        self.go()
        self.assertEqual(self.entry()["grade"], 4)
        self.assertTrue(self.entry()["frozen"])

    def test_removing_the_tag_counts_as_deleting(self):
        self.reader.touch("doc29", tags={})
        self.next_day()
        self.go()
        self.assertTrue(self.entry()["item_gone"])
        self.assertTrue(self.entry()["frozen"])

    def test_a_deleted_teaser_whose_copy_lives_is_not_frozen(self):
        self.later()
        self.add_copy()
        self.go()
        self.reader.delete("doc29")
        self.next_day()
        self.go()
        self.assertTrue(self.entry()["item_gone"])
        self.assertFalse(self.entry()["frozen"])
        self.assertEqual(self.entry()["grade"], 3)
        self.reader.touch("copy1", reading_progress=0.9, first_opened_at=OPENED)
        self.set_time(NOW + timedelta(days=1, hours=1))
        self.go()
        self.assertEqual(self.entry()["grade"], 4)

    def test_a_deleted_copy_freezes_the_pair_from_its_last_snapshot(self):
        self.later()
        self.add_copy(reading_progress=0.9, first_opened_at=OPENED,
                      location="archive")
        self.go()
        self.reader.delete("copy1")
        self.next_day()
        self.go()
        self.assertTrue(self.entry()["copy_gone"])
        self.assertTrue(self.entry()["frozen"])
        self.assertEqual(self.entry()["grade"], 4)

    def test_an_interrupted_refresh_freezes_nothing(self):
        self.reader.delete("doc29")
        self.next_day()
        self.reader.fail_tag_listing = ReaderNetworkError("down")
        self.assertEqual(self.go(), 1)
        self.assertNotIn("item_gone", self.entry())
        self.assertFalse(self.entry()["frozen"])
        self.assertNotEqual(self.state().get("last_refresh_date"), "2026-10-29")
        self.reader.fail_tag_listing = None
        self.go()                       # the next try completes the refresh
        self.assertTrue(self.entry()["item_gone"])


class ReadAheadTests(RunTestCase):
    """Things you did *before* the item was tagged."""

    def test_a_copy_saved_before_tagging_is_linked_on_the_first_refresh(self):
        stamp = (NOW - timedelta(hours=2)).isoformat()
        self.reader.docs["copy1"] = make_doc(
            "copy1", id="copy1", category="article", location="archive",
            source_url=self.reader.docs["doc29"]["source_url"] + "?smid=1",
            title="Article 29", word_count=2200, reading_progress=0.9,
            first_opened_at=OPENED, created_at=stamp, updated_at=stamp)
        self.go()                                   # tags the day
        self.set_time(NOW + timedelta(hours=1))
        self.go()                                   # first daily refresh
        entry = self.tracked()["doc29"]
        self.assertEqual(entry["copy_id"], "copy1")
        self.assertEqual(entry["grade"], 4)

    def test_a_copy_saved_overnight_is_linked_when_the_next_day_is_tagged(self):
        self.go()
        self.set_time(NOW + timedelta(hours=1))
        self.go()                                   # day one's refresh is done
        day2 = NOW + timedelta(days=1)
        evening = (day2 - timedelta(hours=8)).isoformat()
        fresh = (day2 - timedelta(hours=2)).isoformat()
        self.reader.docs["docn1"] = make_doc("n1", created_at=fresh,
                                             updated_at=fresh, title="Article 59")
        self.reader.docs["copy2"] = make_doc(
            "copy2", id="copy2", category="article", location="later",
            source_url=self.reader.docs["docn1"]["source_url"] + "?x=1",
            title="Article 59", word_count=900, created_at=evening,
            updated_at=evening)
        self.set_time(day2)
        self.go()
        self.assertEqual(self.tracked()["docn1"]["copy_id"], "copy2")
        self.assertIn("newsdigest-copy", self.reader.docs["copy2"]["tags"])

    def test_a_highlight_made_before_tagging_is_credited(self):
        self.reader.highlight("doc29")      # while reading ahead in the folder
        self.go()
        self.set_time(NOW + timedelta(hours=1))
        self.go()
        self.assertEqual(self.tracked()["doc29"]["grade"], 5)

    def test_a_tag_added_while_reading_ahead_counts_from_the_start(self):
        self.reader.docs["doc29"]["tags"] = MINE
        self.go()
        self.set_time(NOW + timedelta(hours=1))
        self.go()
        self.assertEqual(self.tracked()["doc29"]["grade"], 5)


class FreezeAndFailureTests(GradeStepCase):
    def test_day_90_freezes_on_the_daily_check(self):
        tracked = self.tracked()
        tracked["doc29"]["tagged_date"] = "2026-07-29"        # 91 days before
        self.paths.tracked.write_text(json.dumps(tracked))
        self.later()
        self.go()
        self.assertTrue(self.entry()["frozen"])
        self.assertEqual(self.entry()["grade"], 1.5)           # unopened in Feed

    def test_a_failed_listing_keeps_the_cursor_and_is_reported(self):
        self.later()
        self.go()
        cursor = self.state()["cursor"]
        self.later(3)
        self.reader.fail_listing = ReaderNetworkError("down")
        self.assertEqual(self.go(), 1)
        self.assertEqual(self.state()["cursor"], cursor)
        self.assertIn("Could not reach Readwise", self.notices[-1][1])

    def test_the_cursor_moves_to_the_start_of_the_last_listing(self):
        self.later()
        self.go()
        self.assertEqual(parse_iso(self.state()["cursor"]), self.now)

    def test_a_problem_in_grading_does_not_stop_the_daily_tagging(self):
        self.next_day()
        self.fresh_docs(5)
        self.reader.fail_tag_listing = ReaderNetworkError("down")
        self.assertEqual(self.go(), 1)
        self.assertEqual(len(self.reader.set_calls), 2)
        self.assertEqual(len(self.reader.set_calls[1]), 5)

    def test_a_dry_run_does_not_grade_or_write(self):
        self.reader.highlight("doc29")
        self.later()
        self.go(dry_run=True)
        self.assertIsNone(self.entry()["grade"])
        self.assertNotIn("cursor", self.state())


class GradesReportTests(GradeStepCase):
    def test_grades_flag_lists_every_item_with_its_reason(self):
        self.reader.highlight("doc29")
        self.later()
        self.assertEqual(self.go(grades_only=True), 0)
        said = self.said()
        self.assertIn("28 tagged items; 1 graded, 27 not graded yet", said)
        self.assertIn("you highlighted it", said)
        self.assertIn("not opened yet", said)
        self.assertIn("Learning:", said)
        self.assertIn("decayed total 0.0", said)

    def test_grades_flag_counts_saved_articles_that_were_never_picked(self):
        self.later()
        self.add_copy("lost1", source_url="https://magazine.example/2026/something-else",
                      title="A piece I saved that nothing matched")
        self.go(grades_only=True)
        self.assertIn("1 other saved articles from digest sites", self.said())
        self.assertNotIn("a gap in the matching", self.said())

    def test_grades_flag_lists_a_saved_article_that_nearly_matched(self):
        self.later()
        self.add_copy("near1", source_url="https://magazine.example/2026/moved-address",
                      title="Article 29 revisited")      # close to "Article 29"
        self.go(grades_only=True)
        self.assertIn("a gap in the matching", self.said())
        self.assertIn("Article 29 revisited", self.said())
        self.assertNotIn("other saved articles", self.said())

    def test_grades_flag_does_not_tag(self):
        self.next_day()
        self.fresh_docs(5)
        self.go(grades_only=True)
        self.assertEqual(len(self.reader.set_calls), 1)

    def test_grades_flag_marks_copies_and_near_misses(self):
        self.later()
        self.add_copy()
        self.go(grades_only=True)
        self.assertIn("[+copy]", self.said())
        self.assertIn("[near-miss]", self.said())
