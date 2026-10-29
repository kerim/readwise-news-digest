import unittest
from datetime import datetime, timedelta

import learning
import profile_builder as pb
import scoring
import tracking
from tests.support import NOW, RunTestCase, make_doc

TODAY = "2026-11-10"


def liked_doc(i, liked=TODAY, host="elsewhere.example", **kw):
    moment = datetime.fromisoformat(liked + "T12:00").astimezone()
    kw.setdefault("notes", "")
    doc = make_doc(i, host=host, category="article", location="later",
                   summary="About a subject.", **kw)
    doc["tags"] = {"👍": {"name": "👍", "type": "manual",
                          "created": int(moment.timestamp() * 1000)}}
    return doc


class RecordTests(unittest.TestCase):
    def record(self, docs, tracked=None):
        state = {}
        tracking.record_outside_likes(state, tracked or {}, docs, TODAY)
        return state["outside_likes"]

    def test_remembers_title_site_note_and_the_day_of_the_vote(self):
        doc = liked_doc(1, liked="2026-11-02", notes="More reporting like this.")
        like = self.record([doc])["doc1"]
        self.assertEqual(like["host"], "elsewhere.example")
        self.assertEqual(like["liked_date"], "2026-11-02")
        self.assertEqual(like["note"], "More reporting like this.")

    def test_picks_and_their_saved_copies_are_left_to_grading(self):
        tracked = {"doc1": {"id": "doc1", "copy_id": "doc2"}}
        likes = self.record([liked_doc(1), liked_doc(2), liked_doc(3)], tracked)
        self.assertEqual(list(likes), ["doc3"])

    def test_our_own_notes_are_never_counted(self):
        self.assertEqual(self.record([liked_doc(1, source="readwise-news-digest")]), {})

    def test_votes_older_than_120_days_are_dropped(self):
        old = (datetime.fromisoformat(TODAY) - timedelta(days=121)).date().isoformat()
        self.assertEqual(self.record([liked_doc(1, liked=old)]), {})

    def test_a_removed_vote_is_forgotten(self):
        state = {"outside_likes": {"doc9": {"liked_date": TODAY}}}
        tracking.record_outside_likes(state, {}, [liked_doc(1)], TODAY)
        self.assertEqual(list(state["outside_likes"]), ["doc1"])


class UseTests(unittest.TestCase):
    LIKES = {"a": {"title": "A new subject", "host": "elsewhere.example",
                   "summary": "About a subject.", "note": "", "liked_date": TODAY}}

    def test_only_the_site_is_boosted(self):
        weights = learning.compute_learned_weights(
            {}, TODAY, outside_likes=self.LIKES.values())
        self.assertAlmostEqual(weights["sources"]["elsewhere.example"],
                               learning.THUMBS_UP_BOOST)
        self.assertEqual(weights["topics"], {})

    def test_the_scorer_is_shown_them_even_for_new_subjects(self):
        text = scoring.build_likes_context(self.LIKES)
        self.assertIn("Articles the Reader Liked Elsewhere", text)
        self.assertIn("match no profile topic", text)
        self.assertIn("- “A new subject” (elsewhere.example): About a subject.", text)

    def test_a_note_is_shown_instead_of_the_summary(self):
        likes = {"a": dict(self.LIKES["a"], note="On-the-ground reporting.")}
        self.assertIn(": On-the-ground reporting.", scoring.build_likes_context(likes))

    def test_no_likes_no_section(self):
        self.assertEqual(scoring.build_likes_context({}), "")

    def test_the_weekly_profile_sees_them_first(self):
        tracked = {f"e{i}": {"title": f"Pick {i}", "host": "a.com", "grade": 5,
                             "grade_since": TODAY, "tagged_date": TODAY}
                   for i in range(40)}
        rows = pb.recent_interests(tracked, TODAY, self.LIKES)
        self.assertEqual(rows[0], "A new subject (elsewhere.example)")
        self.assertEqual(len(rows), pb.SIGNAL_CAP)


class EndToEndTests(RunTestCase):
    def test_a_like_from_elsewhere_reaches_the_next_days_scoring(self):
        self.go()
        self.set_time(NOW + timedelta(days=1))
        liked = liked_doc(500, liked=self.now.date().isoformat())
        liked["title"] = "A subject never picked"
        self.reader.docs[liked["id"]] = liked
        fresh = (self.now - timedelta(hours=1)).isoformat()
        self.reader.docs["doc99"] = make_doc(99, created_at=fresh, updated_at=fresh)
        prompts = []
        inner = self.deps.score_call

        def record(api_key, messages, system=None, max_tokens=0):
            prompts.append(messages[0]["content"])
            return inner(api_key, messages, system=system, max_tokens=max_tokens)
        self.deps.score_call = record
        self.go()
        self.assertIn("A subject never picked", "\n".join(prompts))
        self.assertIn("elsewhere.example",
                      self.paths.learned_weights.read_text())


if __name__ == "__main__":
    unittest.main()
