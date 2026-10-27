import random
import unittest

import selection


def pool(n, topics=8, sources=10):
    """n items with falling scores, spread over some topics and sources."""
    return [{"id": f"id{i}", "score": 10 - i * 0.05,
             "primary_topic": f"topic{i % topics}", "source": f"site{i % sources}"}
            for i in range(n)]


class SelectDigestTests(unittest.TestCase):
    def test_twenty_five_picks_and_three_near_misses(self):
        chosen, stats = selection.select_digest(pool(80), random.Random(1))
        picks = [c for c in chosen if not c["near_miss"]]
        near = [c for c in chosen if c["near_miss"]]
        self.assertEqual((len(picks), len(near)), (25, 3))
        self.assertEqual(stats["picks"], 25)
        self.assertEqual(stats["near_misses"], 3)

    def test_near_misses_are_never_among_the_picks(self):
        for seed in range(20):
            chosen, _ = selection.select_digest(pool(80), random.Random(seed))
            ids = [c["id"] for c in chosen]
            self.assertEqual(len(ids), len(set(ids)))

    def test_near_misses_come_from_ranks_26_to_60(self):
        for seed in range(20):
            chosen, _ = selection.select_digest(pool(120), random.Random(seed))
            for item in chosen:
                if item["near_miss"]:
                    self.assertTrue(26 <= item["rank"] <= 60, item["rank"])

    def test_picks_are_the_best_in_rank_order(self):
        chosen, _ = selection.select_digest(pool(80, topics=30, sources=30),
                                            random.Random(1))
        picks = [c for c in chosen if not c["near_miss"]]
        self.assertEqual([p["rank"] for p in picks], list(range(1, 26)))

    def test_fewer_candidates_than_28(self):
        chosen, stats = selection.select_digest(pool(10), random.Random(1))
        self.assertEqual(len(chosen), 10)
        self.assertEqual(stats["near_misses"], 0)

    def test_between_25_and_28_candidates(self):
        chosen, _ = selection.select_digest(pool(27, topics=27, sources=27),
                                            random.Random(1))
        self.assertEqual(len(chosen), 27)
        self.assertEqual(sum(c["near_miss"] for c in chosen), 2)

    def test_nothing_to_choose_from(self):
        chosen, stats = selection.select_digest([], random.Random(1))
        self.assertEqual(chosen, [])
        self.assertEqual(stats["pool"], 0)

    def test_diversity_caps_hold_when_the_pool_is_varied(self):
        chosen, stats = selection.select_digest(pool(80, topics=10, sources=10),
                                                random.Random(1))
        picks = [c for c in chosen if not c["near_miss"]]
        self.assertEqual(stats["per_source_cap"], 3)
        for site in {p["source"] for p in picks}:
            self.assertLessEqual(sum(p["source"] == site for p in picks), 3)

    def test_caps_widen_so_a_thin_pool_still_fills_the_digest(self):
        thin = [{"id": f"id{i}", "score": 9 - i * 0.01, "primary_topic": "only",
                 "source": "only.com"} for i in range(40)]
        chosen, stats = selection.select_digest(thin, random.Random(1))
        self.assertEqual(stats["picks"], 25)
        self.assertEqual(stats["per_topic_cap"], 25)

    def test_same_seed_same_choice(self):
        a, _ = selection.select_digest(pool(80), random.Random(7))
        b, _ = selection.select_digest(pool(80), random.Random(7))
        self.assertEqual([c["id"] for c in a], [c["id"] for c in b])


if __name__ == "__main__":
    unittest.main()
