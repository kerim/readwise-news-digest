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


class GateTests(unittest.TestCase):
    def test_below_the_gate_there_are_no_nudges(self):
        weights = learning.compute_learned_weights(population(60), TODAY)  # 30 decayed
        self.assertEqual(weights["topics"], {})
        self.assertEqual(weights["sources"], {})
        self.assertAlmostEqual(weights["decayed_total"], 30.0)
        self.assertEqual(weights["counted"], 60)

    def test_at_the_gate_nudges_appear(self):
        weights = learning.compute_learned_weights(population(100), TODAY)  # 50
        self.assertTrue(weights["topics"])

    def test_the_gate_is_on_decayed_items_so_older_ones_count_less(self):
        weights = learning.compute_learned_weights(population(100, age=60), TODAY)
        self.assertAlmostEqual(weights["decayed_total"], 25.0)
        self.assertEqual(weights["topics"], {})


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
