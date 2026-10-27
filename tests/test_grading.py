import unittest
from datetime import date, datetime, timedelta

import grading

TAGGED = "2026-10-01"


def day(n):
    """The local date n days after the tagged date."""
    return (date.fromisoformat(TAGGED) + timedelta(days=n)).isoformat()


def local_noon(n):
    """An ISO instant at local noon, n days after the tagged date."""
    d = date.fromisoformat(TAGGED) + timedelta(days=n)
    return datetime(d.year, d.month, d.day, 12, 0).astimezone().isoformat()


def snap(**kw):
    base = {"location": "feed", "reading_progress": 0, "word_count": 900,
            "first_opened_at": None, "last_moved_at": None,
            "tags": ["newsdigest"], "auto_tags": [],
            "highlighted": False, "shortlisted": False}
    base.update(kw)
    return base


OPENED = "2026-10-02T08:00:00+00:00"


def grade(today_offset=3, **kw):
    return grading.grade_document(snap(**kw), TAGGED, day(today_offset))


def entry(copy=None, **kw):
    e = {"id": "item", "tagged_date": TAGGED, "snapshot": snap(),
         "copy_id": "copy" if copy else None, "copy_snapshot": copy,
         "grade": None, "grade_reason": None, "grade_since": None,
         "frozen": False}
    e.update(kw)
    return e


class WordClassTests(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(grading.word_class(1500), "long")
        self.assertEqual(grading.word_class(1499), "medium")
        self.assertEqual(grading.word_class(500), "medium")
        self.assertEqual(grading.word_class(499), "short")
        self.assertEqual(grading.word_class(None), "unknown")


class FiveTests(unittest.TestCase):
    def test_highlight_is_five_and_beats_everything(self):
        self.assertEqual(grade(highlighted=True)[0], 5)
        self.assertEqual(grade(highlighted=True, location="archive",
                               first_opened_at=OPENED, word_count=2000,
                               reading_progress=1)[0], 5)

    def test_a_tag_of_your_own_is_five(self):
        g, reason = grade(tags=["newsdigest", "To Share"])
        self.assertEqual(g, 5)
        self.assertIn("To Share", reason)

    def test_our_own_tags_never_count_whatever_their_case(self):
        self.assertIsNone(grade(tags=["newsdigest", "NewsDigest-Copy",
                                      "newsdigest-suggestions"])[0])

    def test_auto_tags_never_count(self):
        self.assertIsNone(grade(tags=["newsdigest", "Politics"],
                                auto_tags=["politics"])[0])

    def test_a_tag_added_while_reading_ahead_counts(self):
        # present in the snapshot taken at tagging time
        self.assertEqual(grade(tags=["research", "newsdigest"])[0], 5)


class FourAndThreeTests(unittest.TestCase):
    def test_long_article_read_half_is_four_in_any_location(self):
        for location in ("feed", "later", "new", "shortlist", "archive"):
            g, _ = grade(word_count=2000, reading_progress=0.5,
                         first_opened_at=OPENED, location=location)
            self.assertEqual(g, 4, location)

    def test_long_article_read_just_under_half_is_not_four(self):
        self.assertIsNone(grade(word_count=2000, reading_progress=0.49,
                                first_opened_at=OPENED)[0])

    def test_shortlisted_is_three_and_stays_three_after_archiving(self):
        self.assertEqual(grade(shortlisted=True, location="shortlist")[0], 3)
        self.assertEqual(grade(shortlisted=True, location="archive",
                               first_opened_at=OPENED, word_count=900,
                               reading_progress=1)[0], 3)

    def test_a_long_read_beats_the_shortlist(self):
        self.assertEqual(grade(shortlisted=True, word_count=2000,
                               reading_progress=0.9, first_opened_at=OPENED)[0], 4)

    def test_medium_article_read_half_is_three(self):
        self.assertEqual(grade(word_count=900, reading_progress=0.5,
                               first_opened_at=OPENED)[0], 3)

    def test_short_article_read_in_full_gets_no_grade(self):
        g, reason = grade(word_count=300, reading_progress=1,
                          first_opened_at=OPENED, location="archive")
        self.assertIsNone(g)
        self.assertIn("short", reason)


class TwoTests(unittest.TestCase):
    def test_opened_read_less_than_half_then_archived_is_two(self):
        for words in (600, 3000):
            g, _ = grade(word_count=words, reading_progress=0.2,
                         first_opened_at=OPENED, location="archive")
            self.assertEqual(g, 2, words)

    def test_short_or_unknown_length_opened_and_archived_is_no_grade(self):
        for words in (200, None):
            self.assertIsNone(grade(word_count=words, first_opened_at=OPENED,
                                    location="archive")[0])

    def test_later_unopened_for_21_days_is_two(self):
        moved = local_noon(1)
        self.assertIsNone(grade(21, location="later", last_moved_at=moved)[0])
        g, reason = grade(22, location="later", last_moved_at=moved)
        self.assertEqual(g, 2)
        self.assertIn("21 days", reason)

    def test_inbox_is_treated_like_later(self):
        self.assertEqual(grade(30, location="new", last_moved_at=local_noon(1))[0], 2)

    def test_without_a_move_date_the_tagging_date_is_used(self):
        self.assertEqual(grade(21, location="later")[0], 2)
        self.assertIsNone(grade(20, location="later")[0])

    def test_opening_a_waiting_item_takes_it_back_to_no_grade(self):
        # current state decides: it was a 2, then you opened it
        g, reason = grade(30, location="later", last_moved_at=local_noon(1),
                          first_opened_at=OPENED, word_count=2000)
        self.assertIsNone(g)
        self.assertEqual(reason, "in progress")

    def test_freeze_grades_an_unfinished_long_or_medium_piece_two(self):
        for location in ("later", "new", "feed"):
            g, _ = grading.grade_document(
                snap(location=location, first_opened_at=OPENED, word_count=900,
                     reading_progress=0.1), TAGGED, day(90), freezing=True)
            self.assertEqual(g, 2, location)

    def test_freeze_rule_only_applies_at_the_freeze(self):
        self.assertIsNone(grade(89, location="later", first_opened_at=OPENED,
                                word_count=900, reading_progress=0.1,
                                last_moved_at=local_noon(80))[0])

    def test_short_opened_pieces_stay_ungraded_even_at_the_freeze(self):
        g, _ = grading.grade_document(
            snap(location="later", first_opened_at=OPENED, word_count=200),
            TAGGED, day(90), freezing=True)
        self.assertIsNone(g)


class OneTests(unittest.TestCase):
    def test_feed_unopened_for_30_days_is_one_and_a_half(self):
        self.assertIsNone(grade(29)[0])
        self.assertEqual(grade(30)[0], 1.5)

    def test_archived_without_opening_is_one(self):
        for words in (100, 900, 3000, None):
            self.assertEqual(grade(location="archive", word_count=words)[0], 1)

    def test_in_feed_and_recent_is_no_grade(self):
        g, reason = grade(5)
        self.assertIsNone(g)
        self.assertEqual(reason, "not opened yet")

    def test_missing_progress_counts_as_zero(self):
        self.assertEqual(grade(location="archive", reading_progress=None)[0], 1)


class DeletedTests(unittest.TestCase):
    def deleted(self, **kw):
        return grading.grade_document(snap(**kw), TAGGED, day(5), as_archived=True)

    def test_deleted_unopened_is_one(self):
        self.assertEqual(self.deleted()[0], 1)

    def test_deleted_long_opened_under_half_is_two(self):
        self.assertEqual(self.deleted(word_count=2000, first_opened_at=OPENED,
                                      reading_progress=0.1)[0], 2)

    def test_deleted_short_opened_is_no_grade(self):
        self.assertIsNone(self.deleted(word_count=100, first_opened_at=OPENED)[0])

    def test_a_read_item_that_is_deleted_is_not_a_one(self):
        self.assertEqual(self.deleted(word_count=2000, first_opened_at=OPENED,
                                      reading_progress=0.9)[0], 4)


class SavedCopyTests(unittest.TestCase):
    def grade_pair(self, copy, **item):
        e = entry(copy=copy)
        e["snapshot"] = snap(**item)
        return grading.grade_entry(e, day(3))

    def test_a_saved_copy_makes_it_at_least_three(self):
        g, reason = self.grade_pair(snap(location="later", word_count=1200))
        self.assertEqual(g, 3)
        self.assertIn("saved", reason)

    def test_the_copys_real_word_count_and_reading_decide(self):
        # the feed item is a 58-word teaser; the copy is a long read
        g, reason = self.grade_pair(
            snap(location="archive", word_count=2200, reading_progress=0.9,
                 first_opened_at=OPENED), word_count=58)
        self.assertEqual(g, 4)
        self.assertIn("your saved copy", reason)

    def test_a_medium_copy_read_95_percent(self):
        g, reason = self.grade_pair(
            snap(location="archive", word_count=1140, reading_progress=0.95,
                 first_opened_at=OPENED), word_count=58,
            first_opened_at=OPENED, location="archive")
        self.assertEqual(g, 3)
        self.assertIn("95%", reason)

    def test_a_highlight_on_the_copy_is_five(self):
        self.assertEqual(self.grade_pair(snap(highlighted=True))[0], 5)

    def test_a_tag_of_your_own_on_the_copy_is_five_but_our_copy_tag_is_not(self):
        self.assertEqual(self.grade_pair(snap(tags=["newsdigest-copy", "keep"]))[0], 5)
        self.assertEqual(self.grade_pair(snap(tags=["newsdigest-copy"]))[0], 3)

    def test_the_item_can_raise_the_pair_too(self):
        self.assertEqual(self.grade_pair(snap(), highlighted=True)[0], 5)

    def test_no_copy_means_the_item_alone(self):
        self.assertEqual(grading.grade_entry(entry(), day(3))[0], None)


class RegradeTests(unittest.TestCase):
    def test_grade_since_moves_only_when_the_grade_changes(self):
        e = entry()
        tracked = {"item": e}
        e["snapshot"] = snap(location="archive")
        grading.regrade(tracked, day(4))
        self.assertEqual((e["grade"], e["grade_since"]), (1, day(4)))
        grading.regrade(tracked, day(6))
        self.assertEqual((e["grade"], e["grade_since"]), (1, day(4)))
        e["snapshot"]["highlighted"] = True
        grading.regrade(tracked, day(7))
        self.assertEqual((e["grade"], e["grade_since"]), (5, day(7)))

    def test_returns_only_entries_whose_grade_changed(self):
        e = entry()
        tracked = {"item": e}
        self.assertEqual(grading.regrade(tracked, day(3)), [])
        e["snapshot"]["highlighted"] = True
        self.assertEqual(grading.regrade(tracked, day(3)), [e])
        self.assertEqual(grading.regrade(tracked, day(3)), [])

    def test_day_90_freezes_on_the_daily_check_only(self):
        e = entry()
        e["snapshot"] = snap(location="later", first_opened_at=OPENED,
                             word_count=900, reading_progress=0.1,
                             last_moved_at=local_noon(80))
        tracked = {"item": e}
        grading.regrade(tracked, day(90), daily=False)
        self.assertFalse(e["frozen"])
        self.assertIsNone(e["grade"])
        grading.regrade(tracked, day(90), daily=True)
        self.assertTrue(e["frozen"])
        self.assertEqual(e["grade"], 2)

    def test_day_89_is_not_frozen_even_on_the_daily_check(self):
        e = entry()
        grading.regrade({"item": e}, day(89), daily=True)
        self.assertFalse(e["frozen"])

    def test_frozen_entries_are_left_alone(self):
        e = entry(frozen=True, grade=3, grade_reason="kept")
        e["snapshot"]["highlighted"] = True
        grading.regrade({"item": e}, day(100), daily=True)
        self.assertEqual(e["grade"], 3)

    def test_a_deleted_item_is_graded_as_archived_then_frozen(self):
        e = entry(item_gone=True)
        grading.regrade({"item": e}, day(5))
        self.assertEqual(e["grade"], 1)
        self.assertTrue(e["frozen"])

    def test_a_deleted_read_item_keeps_its_read_grade(self):
        e = entry(item_gone=True)
        e["snapshot"] = snap(word_count=2000, reading_progress=0.8,
                             first_opened_at=OPENED)
        grading.regrade({"item": e}, day(5))
        self.assertEqual(e["grade"], 4)
        self.assertTrue(e["frozen"])

    def test_a_deleted_teaser_whose_copy_lives_is_not_frozen_and_can_still_rise(self):
        e = entry(item_gone=True, copy=snap(location="later", word_count=2000))
        tracked = {"item": e}
        grading.regrade(tracked, day(5))
        self.assertEqual(e["grade"], 3)
        self.assertFalse(e["frozen"])
        e["copy_snapshot"].update(reading_progress=0.9, first_opened_at=OPENED)
        grading.regrade(tracked, day(6))
        self.assertEqual(e["grade"], 4)

    def test_a_deleted_copy_freezes_the_pair_from_its_last_snapshot(self):
        e = entry(copy=snap(word_count=2000, reading_progress=0.9,
                            first_opened_at=OPENED), copy_gone=True)
        grading.regrade({"item": e}, day(5))
        self.assertEqual(e["grade"], 4)
        self.assertTrue(e["frozen"])


if __name__ == "__main__":
    unittest.main()
