import json
import re
import unittest

import scoring
from common import APIAuthError, APINetworkError


def candidate(i, **kw):
    item = {"id": f"id{i}", "title": f"Article {i}", "url": f"https://magazine.example/{i}",
            "summary": "s", "source": "magazine.example", "source_tier": "daily_mixed",
            "url_hints": [], "published": ""}
    item.update(kw)
    return item


def haiku_reply(prompt_text, topic=8, form=7):
    """A well-formed Haiku answer for every <item index="n"> in a prompt."""
    indexes = re.findall(r'<item index="(\d+)">', prompt_text)
    return json.dumps([{"index": int(n), "topic_score": topic, "form_score": form,
                        "primary_topic": "Topic A", "form": "analysis",
                        "frame": "angle", "reason": "r"} for n in indexes])


class CandidateTests(unittest.TestCase):
    def doc(self, **kw):
        doc = {"id": "d1", "source_url": "https://www.theguardian.com/world/x",
               "title": "T", "summary": "<p>Hello   <b>world</b></p>",
               "published_date": "2026-10-27"}
        doc.update(kw)
        return doc

    def test_builds_a_candidate_with_host_tier_and_clean_summary(self):
        item = scoring.candidate_from_document(self.doc())
        self.assertEqual(item["source"], "theguardian.com")
        self.assertEqual(item["source_tier"], "daily_mixed")
        self.assertEqual(item["summary"], "Hello world")
        self.assertEqual(item["url"], "https://www.theguardian.com/world/x")

    def test_video_and_podcast_items_are_skipped(self):
        self.assertIsNone(scoring.candidate_from_document(
            self.doc(source_url="https://example.com/video/clip")))
        self.assertIsNone(scoring.candidate_from_document(
            self.doc(source_url="https://example.com/podcast/ep1")))

    def test_missing_address_is_skipped(self):
        self.assertIsNone(scoring.candidate_from_document(self.doc(source_url=None)))
        self.assertIsNone(scoring.candidate_from_document(self.doc(source_url="")))

    def test_missing_title_and_summary_are_tolerated(self):
        item = scoring.candidate_from_document(self.doc(title=None, summary=None))
        self.assertEqual(item["title"], "Untitled")
        self.assertEqual(item["summary"], "")

    def test_tier_also_matches_a_parent_domain(self):
        self.assertEqual(scoring.source_tier("blogs.nytimes.com"), "daily_mixed")
        self.assertEqual(scoring.source_tier("unknown.example"), "unknown")

    def test_your_own_tiers_from_prefs_add_to_and_override_the_built_in_list(self):
        extra = {"unknown.example": "weekly_longform", "nytimes.com": "wire"}
        self.assertEqual(scoring.source_tier("unknown.example", extra), "weekly_longform")
        self.assertEqual(scoring.source_tier("blogs.nytimes.com", extra), "wire")
        self.assertEqual(scoring.source_tier("apnews.com", extra), "wire")
        item = scoring.candidate_from_document(
            self.doc(source_url="https://www.unknown.example/a"), extra)
        self.assertEqual(item["source_tier"], "weekly_longform")

    def test_url_hints(self):
        self.assertEqual(scoring.extract_url_hints(
            "https://x.com/the-long-read/2026/a"), ["longread"])
        self.assertEqual(scoring.extract_url_hints("https://x.com/live/blog"),
                         ["live"])
        self.assertEqual(scoring.extract_url_hints(""), [])

    def test_summary_is_truncated(self):
        out = scoring.clean_summary("word " * 400)
        self.assertTrue(out.endswith("..."))
        self.assertLessEqual(len(out), scoring.SUMMARY_MAX_CHARS + 3)


class PromptTests(unittest.TestCase):
    def test_item_content_is_escaped_inside_xml(self):
        xml = scoring.format_items_xml([candidate(0, title="A <b> & </item>")])
        self.assertIn("A &lt;b&gt; &amp; &lt;/item&gt;", xml)
        self.assertEqual(xml.count("<item "), 1)

    def test_system_prompt_lists_topics_and_frames(self):
        prompt = scoring.build_system_prompt(["Topic A", "Topic B"],
                                             [("doom-take", 3)])
        self.assertIn("- Topic A", prompt)
        self.assertIn('"doom-take"', prompt)

    def test_profile_context_lists_topics_by_score(self):
        profile = {"topics": [{"topic": "T1", "score": 1.0, "tags": ["a", "b"]}]}
        self.assertIn("- T1 (score: 1.0) [a, b]",
                      scoring.build_profile_context(profile))
        self.assertEqual(scoring.profile_topic_names(profile), ["T1"])


class ApplyScoresTests(unittest.TestCase):
    def test_attaches_scores_and_stashes_the_raw_ones(self):
        batch = [candidate(0)]
        scoring.apply_scores(batch, [{"index": 0, "topic_score": 8, "form_score": 6,
                                      "primary_topic": "Topic A", "form": "essay",
                                      "frame": " angle ", "reason": "r"}],
                             ["Topic A"])
        item = batch[0]
        self.assertEqual((item["raw_topic_score"], item["raw_form_score"]), (8, 6))
        self.assertEqual(item["score"], 6)
        self.assertEqual(item["frame"], "angle")

    def test_unknown_topic_and_form_fall_back(self):
        batch = [candidate(0)]
        scoring.apply_scores(batch, [{"index": 0, "topic_score": 5, "form_score": 5,
                                      "primary_topic": "Made up", "form": "poem"}],
                             ["Topic A"])
        self.assertEqual(batch[0]["primary_topic"], "other")
        self.assertEqual(batch[0]["form"], "news")

    def test_legacy_single_score_is_used_for_both_axes(self):
        batch = [candidate(0)]
        scoring.apply_scores(batch, [{"index": 0, "score": 7}], ["Topic A"])
        self.assertEqual((batch[0]["topic_score"], batch[0]["form_score"]), (7, 7))

    def test_an_item_haiku_left_out_scores_zero(self):
        batch = [candidate(0), candidate(1)]
        scoring.apply_scores(batch, [{"index": 0, "topic_score": 8,
                                      "form_score": 8}], ["Topic A"])
        self.assertEqual(batch[1]["score"], 0)


class ScoreItemsTests(unittest.TestCase):
    def run_scoring(self, items, call):
        batches = []
        scored, failed = scoring.score_items(
            items, "ctx", ["Topic A"], [], "key",
            on_batch=lambda b, n, total: batches.append((len(b), n, total)),
            call=call, sleep=lambda s: None)
        return scored, failed, batches

    def test_scores_in_batches_of_fifty(self):
        calls = []

        def call(key, messages, system=None, max_tokens=0):
            calls.append(1)
            return haiku_reply(messages[0]["content"])

        scored, failed, batches = self.run_scoring(
            [candidate(i) for i in range(120)], call)
        self.assertEqual((len(scored), len(failed)), (120, 0))
        self.assertEqual(len(calls), 3)
        self.assertEqual(batches, [(50, 1, 3), (50, 2, 3), (20, 3, 3)])

    def test_a_batch_that_cannot_be_scored_is_reported_failed(self):
        def call(key, messages, system=None, max_tokens=0):
            raise APINetworkError("down")

        scored, failed, batches = self.run_scoring([candidate(i) for i in range(5)],
                                                   call)
        self.assertEqual((len(scored), len(failed)), (0, 5))
        self.assertEqual(batches, [])

    def test_bad_json_is_retried_in_halves(self):
        state = {"first": True}

        def call(key, messages, system=None, max_tokens=0):
            if state["first"]:
                state["first"] = False
                return "not json"
            return haiku_reply(messages[0]["content"])

        scored, failed, _ = self.run_scoring([candidate(i) for i in range(20)], call)
        self.assertEqual((len(scored), len(failed)), (20, 0))

    def test_a_rejected_key_stops_everything(self):
        def call(key, messages, system=None, max_tokens=0):
            raise APIAuthError("401")

        with self.assertRaises(APIAuthError):
            self.run_scoring([candidate(0)], call)


class AdjustmentTests(unittest.TestCase):
    def scored(self, **kw):
        item = candidate(0, primary_topic="Topic A", form="essay", frame="angle",
                         raw_topic_score=7, raw_form_score=7, topic_score=7,
                         form_score=7, score=7)
        item.update(kw)
        return item

    def test_learned_weights_nudge_and_clamp(self):
        item = self.scored(raw_topic_score=9, raw_form_score=2)
        scoring.apply_learned_weights([item], {
            "topics": {"Topic A": 2.0}, "sources": {"magazine.example": 1.0},
            "forms": {"essay": -2.0}, "frames": {}})
        self.assertEqual(item["topic_score"], 10)            # 9 + 2 + 0.5, clamped
        self.assertAlmostEqual(item["form_score"], 1)        # 2 - 2 + 0.5 -> 1 floor
        self.assertEqual(item["score"], 1)

    def test_applying_weights_twice_gives_the_same_scores(self):
        item = self.scored()
        weights = {"topics": {"Topic A": 1.5}, "sources": {}, "forms": {},
                   "frames": {}}
        scoring.apply_learned_weights([item], weights)
        once = item["score"]
        scoring.apply_learned_weights([item], weights)
        self.assertEqual(item["score"], once)

    def test_the_junk_floor_reads_raw_scores(self):
        nudged_up = self.scored(raw_topic_score=2, topic_score=4, score=4)
        fine = self.scored(id="id2")
        self.assertEqual(scoring.above_junk_floor([nudged_up, fine]), [fine])

    def test_prefs_filter_each_kind_of_mute(self):
        base = {"primary_topic": "Topic A", "form": "recap", "frame": "f",
                "source": "a.com"}
        items = [self.scored(id="1", **base),
                 self.scored(id="2", **{**base, "form": "news"}),
                 self.scored(id="3", **{**base, "form": "essay", "frame": "muted"}),
                 self.scored(id="4", **{**base, "form": "essay", "source": "b.com"}),
                 self.scored(id="5", **{**base, "form": "essay"})]
        prefs = {"form_mutes": {"Topic A": ["recap"], "*": ["news"]},
                 "frame_mutes": ["muted"], "muted_sources": ["b.com"]}
        kept, drops = scoring.apply_prefs_filter(items, prefs)
        self.assertEqual([i["id"] for i in kept], ["5"])
        self.assertEqual(drops, {"form_per_topic": 1, "form_wildcard": 1,
                                 "frame": 1, "source": 1})

    def test_recent_frames_counts_recently_tagged_only(self):
        from common import local_date_days_ago
        tracked = {
            "a": {"frame": "doom-take", "tagged_date": local_date_days_ago(2)},
            "b": {"frame": "doom-take", "tagged_date": local_date_days_ago(5)},
            "c": {"frame": "old", "tagged_date": local_date_days_ago(90)},
            "d": {"frame": "", "tagged_date": local_date_days_ago(1)},
        }
        self.assertEqual(scoring.recent_frames(tracked), [("doom-take", 2)])


if __name__ == "__main__":
    unittest.main()


def voted(title, mark, note, tagged="2026-10-20", copy=False, host="n.example"):
    snap = {"tags": ["newsdigest", mark] if mark else ["newsdigest"],
            "notes": note}
    e = {"id": title, "title": title, "host": host, "tagged_date": tagged,
         "snapshot": {"tags": ["newsdigest"], "notes": ""} if copy else snap}
    if copy:
        e["copy_snapshot"] = snap
    return e


class FeedbackTests(unittest.TestCase):
    def test_only_votes_with_a_note_are_sent(self):
        tracked = {e["id"]: e for e in (
            voted("A", "👎", "Event notice, not an article."),
            voted("B", "👎", ""),
            voted("C", "👍", "   "),
            voted("D", None, "a note without a vote"),
            voted("E", "👍", "More on-the-ground reporting.", copy=True))}
        rows = scoring.reader_feedback(tracked)
        self.assertEqual(sorted(r[1] for r in rows), ["A", "E"])

    def test_newest_first_and_limited(self):
        tracked = {str(i): voted(str(i), "👎", "why", tagged=f"2026-10-{i + 10:02d}")
                   for i in range(5)}
        rows = scoring.reader_feedback(tracked, limit=2)
        self.assertEqual([r[1] for r in rows], ["4", "3"])

    def test_the_section_quotes_each_note(self):
        text = scoring.build_feedback_context(
            [("👎", "A Night of Readings", "magazine.example", "Event notice.")])
        self.assertIn("The Reader's Own Verdicts", text)
        self.assertIn("- 👎 “A Night of Readings” (magazine.example): Event notice.", text)

    def test_no_votes_no_section(self):
        self.assertEqual(scoring.build_feedback_context([]), "")
