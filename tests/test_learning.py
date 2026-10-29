import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import learning
from common import Paths
from scoring import load_learned_weights

TODAY = "2026-12-01"


def tagged_days_ago(n):
    return (date.fromisoformat(TODAY) - timedelta(days=n)).isoformat()


def entry(i, age, grade, topic="A", host="a.com", form="essay", frame="f"):
    return {"id": f"e{i}", "tagged_date": tagged_days_ago(age), "grade": grade,
            "primary_topic": topic, "host": host, "form": form, "frame": frame}


def tracked_of(*entries):
    return {e["id"]: e for e in entries}


def population(n, age=30):
    """n items, half topic A graded 5, half topic B graded 1."""
    return tracked_of(*[
        entry(i, age, 5 if i % 2 == 0 else 1,
              topic="A" if i % 2 == 0 else "B",
              host="a.com" if i % 2 == 0 else "b.com")
        for i in range(n)])


class CountingTests(unittest.TestCase):
    def test_only_graded_items_30_to_120_days_old_count(self):
        tracked = tracked_of(
            entry(1, 29, 5), entry(2, 30, 5), entry(3, 120, 5), entry(4, 121, 5),
            entry(5, 60, None))
        ids = {e["id"] for e, _ in learning.counted_items(tracked, TODAY)}
        self.assertEqual(ids, {"e2", "e3"})

    def test_decay_is_a_true_30_day_half_life(self):
        tracked = tracked_of(entry(1, 30, 5), entry(2, 60, 5), entry(3, 90, 5))
        decays = {e["id"]: d for e, d in learning.counted_items(tracked, TODAY)}
        self.assertAlmostEqual(decays["e1"], 0.5)
        self.assertAlmostEqual(decays["e2"], 0.25)
        self.assertAlmostEqual(decays["e3"], 0.125)

    def test_an_item_that_never_gets_a_grade_never_counts(self):
        # e.g. a short piece you opened: grade stays None
        self.assertEqual(learning.counted_items(
            tracked_of(entry(1, 50, None)), TODAY), [])


class NoMinimumTests(unittest.TestCase):
    def test_a_few_items_already_nudge_but_only_a_little(self):
        weights = learning.compute_learned_weights(population(10), TODAY)
        self.assertGreater(weights["topics"]["A"], 0)
        self.assertLess(weights["topics"]["B"], 0)
        many = learning.compute_learned_weights(population(200), TODAY)
        self.assertLess(weights["topics"]["A"], many["topics"]["A"])

    def test_nothing_counted_means_no_nudges(self):
        weights = learning.compute_learned_weights({}, TODAY)
        self.assertEqual(weights["topics"], {})
        self.assertEqual(weights["decayed_total"], 0)


def thumbs_up(e):
    e["snapshot"] = {"tags": ["newsdigest", "👍"]}
    return e


class ThumbsUpTests(unittest.TestCase):
    def test_a_thumbs_up_counts_from_the_day_it_is_tagged(self):
        tracked = tracked_of(thumbs_up(entry(1, 0, 5)), entry(2, 0, 5))
        ids = {e["id"] for e, _ in learning.counted_items(tracked, TODAY)}
        self.assertEqual(ids, {"e1"})

    def test_a_thumbs_up_weighs_double_a_30_day_item(self):
        tracked = tracked_of(thumbs_up(entry(1, 3, 5)), thumbs_up(entry(2, 60, 5)))
        weights = {e["id"]: w for e, w in learning.counted_items(tracked, TODAY)}
        self.assertAlmostEqual(weights["e1"], 1.0)    # 2 x 0.5
        self.assertAlmostEqual(weights["e2"], 0.5)    # 2 x 0.25

    def test_a_thumbs_up_on_the_saved_copy_counts_too(self):
        e = entry(1, 3, 5)
        e["snapshot"] = {"tags": ["newsdigest"]}
        e["copy_snapshot"] = {"tags": ["newsdigest-copy", "👍"]}
        self.assertEqual(len(learning.counted_items(tracked_of(e), TODAY)), 1)

    def test_a_thumbs_up_lifts_its_values_against_ordinary_items(self):
        tracked = population(40)
        tracked.update(tracked_of(*[
            thumbs_up(entry(100 + i, 2, 5, topic="C", host="c.com"))
            for i in range(3)]))
        weights = learning.compute_learned_weights(tracked, TODAY)
        self.assertGreater(weights["topics"]["C"], 0)

    def test_one_fresh_thumbs_up_boosts_at_once_with_nothing_to_compare(self):
        tracked = tracked_of(thumbs_up(entry(1, 0, 5, topic="C", host="c.com")))
        weights = learning.compute_learned_weights(tracked, TODAY)
        for dim, value in (("topics", "C"), ("sources", "c.com"),
                           ("forms", "essay"), ("frames", "f")):
            self.assertAlmostEqual(weights[dim][value], learning.THUMBS_UP_BOOST)

    def test_the_boost_halves_in_30_days_and_adds_up(self):
        tracked = tracked_of(thumbs_up(entry(1, 30, 5, topic="C")),
                             thumbs_up(entry(2, 0, 5, topic="C")))
        weights = learning.compute_learned_weights(tracked, TODAY)
        self.assertAlmostEqual(weights["topics"]["C"],
                               1.5 * learning.THUMBS_UP_BOOST)

    def test_the_boost_stays_inside_the_clamp(self):
        tracked = tracked_of(*[thumbs_up(entry(i, 0, 5, topic="C"))
                               for i in range(20)])
        weights = learning.compute_learned_weights(tracked, TODAY)
        self.assertEqual(weights["topics"]["C"], learning.MAX_NUDGE)

    def test_no_boost_after_120_days(self):
        tracked = tracked_of(thumbs_up(entry(1, 121, 5, topic="C")))
        self.assertEqual(
            learning.compute_learned_weights(tracked, TODAY)["topics"], {})


class NudgeTests(unittest.TestCase):
    def setUp(self):
        self.weights = learning.compute_learned_weights(population(200), TODAY)

    def test_liked_values_warm_up_and_disliked_cool_down(self):
        self.assertGreater(self.weights["topics"]["A"], 0)
        self.assertLess(self.weights["topics"]["B"], 0)
        self.assertGreater(self.weights["sources"]["a.com"], 0)
        self.assertLess(self.weights["sources"]["b.com"], 0)

    def test_nudges_stay_inside_the_clamp(self):
        for dim in ("topics", "sources", "forms", "frames"):
            for delta in self.weights[dim].values():
                self.assertLessEqual(abs(delta), learning.MAX_NUDGE)

    def test_a_value_everything_shares_gets_no_nudge(self):
        # every item is form "essay" and frame "f": nothing to tell them apart
        self.assertNotIn("essay", self.weights["forms"])
        self.assertNotIn("f", self.weights["frames"])

    def test_nudges_are_centered_on_the_average(self):
        # equal halves: the two topics move by about the same amount, opposite ways
        self.assertAlmostEqual(self.weights["topics"]["A"],
                               -self.weights["topics"]["B"], places=6)

    def test_grade_one_contributes_nothing_and_grade_five_everything(self):
        low = tracked_of(*[entry(i, 30, 1) for i in range(120)])
        high = tracked_of(*[entry(i, 30, 5) for i in range(120)])
        # uniform populations have no contrast, so no nudge either way
        self.assertEqual(learning.compute_learned_weights(low, TODAY)["topics"], {})
        self.assertEqual(learning.compute_learned_weights(high, TODAY)["topics"], {})

    def test_a_graded_one_and_a_half_sits_between(self):
        mixed = tracked_of(*[entry(i, 30, 1.5 if i % 2 else 5,
                                   topic="B" if i % 2 else "A")
                             for i in range(200)])
        w = learning.compute_learned_weights(mixed, TODAY)
        self.assertGreater(w["topics"]["A"], 0)
        self.assertLess(w["topics"]["B"], 0)

    def test_biggest_nudges_lists_the_largest_either_way(self):
        top = learning.biggest_nudges(self.weights, n=2)
        self.assertEqual(len(top), 2)
        self.assertGreaterEqual(abs(top[0][2]), abs(top[1][2]))


class FileTests(unittest.TestCase):
    def test_nudges_are_saved_where_the_tagging_step_reads_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(Path(tmp))
            learning.update_learned_weights(paths, population(200), TODAY)
            self.assertTrue(json.loads(paths.learned_weights.read_text())["topics"])
            loaded = load_learned_weights(paths)
            self.assertGreater(loaded["topics"]["A"], 0)
            self.assertEqual(set(loaded), {"topics", "sources", "forms", "frames"})


if __name__ == "__main__":
    unittest.main()
