import fcntl
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

import run
from common import APINetworkError, Paths, atomic_write, iso_utc
from reader_api import ReaderAuthError, ReaderNetworkError
from tests.support import NOW, RunTestCase, make_doc


class TaggingTests(RunTestCase):
    def test_tags_the_best_25_plus_3_near_misses(self):
        self.assertEqual(self.go(), 0)
        self.assertEqual(len(self.reader.set_calls), 1)
        updates = self.reader.set_calls[0]
        self.assertEqual(len(updates), 28)
        for _doc_id, tags in updates:
            self.assertIn("newsdigest", tags)
        tracked = self.tracked()
        self.assertEqual(len(tracked), 28)
        self.assertEqual(sum(e["near_miss"] for e in tracked.values()), 3)
        self.assertIn("tagged=28 scored=40", self.run_log()[-1])

    def test_existing_tags_are_kept(self):
        self.reader.docs["doc29"]["tags"] = {
            "keepme": {"name": "keepme", "type": "manual"}}
        self.go()
        tags = dict(self.reader.set_calls[0])["doc29"]
        self.assertEqual(sorted(tags), ["keepme", "newsdigest"])

    def test_tag_keeps_its_display_name_not_its_key(self):
        self.reader.docs["doc29"]["tags"] = {
            "to-read": {"name": "To Read", "type": "manual"}}
        self.go()
        self.assertEqual(sorted(dict(self.reader.set_calls[0])["doc29"]),
                         ["To Read", "newsdigest"])

    def test_the_tag_is_not_added_twice(self):
        self.reader.docs["doc29"]["tags"] = {
            "newsdigest": {"name": "newsdigest", "type": "manual"}}
        self.go()
        self.assertEqual(dict(self.reader.set_calls[0])["doc29"], ["newsdigest"])

    def test_a_tag_added_while_scoring_is_kept(self):
        def user_tags_it():
            doc = self.reader.docs["doc29"]
            doc["tags"] = {"mine": {"name": "mine", "type": "manual"}}
            doc["updated_at"] = NOW.isoformat()
        self.on_first_score = user_tags_it
        self.go()
        self.assertEqual(sorted(dict(self.reader.set_calls[0])["doc29"]),
                         ["mine", "newsdigest"])

    def test_tracked_entry_holds_what_grading_will_need(self):
        self.go()
        entry = self.tracked()["doc29"]
        self.assertEqual(entry["host"], "magazine.example")
        self.assertEqual(entry["tagged_date"], "2026-10-28")
        self.assertEqual(entry["primary_topic"], "Topic A")
        self.assertEqual(entry["raw_topic_score"], 9)
        self.assertIsNone(entry["grade"])
        self.assertFalse(entry["frozen"])
        snap = entry["snapshot"]
        self.assertEqual(snap["location"], "feed")
        self.assertIn("newsdigest", snap["tags"])
        self.assertFalse(snap["highlighted"])
        self.assertFalse(snap["shortlisted"])

    def test_state_records_the_day_only_after_a_good_write(self):
        self.go()
        state = self.state()
        self.assertEqual(state["last_tag_date"], "2026-10-28")
        self.assertEqual(state["last_tag_count"], 28)
        self.assertEqual(state["pending_scores"], {})
        self.assertEqual(len(state["scored_ids"]), 40)

    def test_fewer_candidates_than_28(self):
        self.reader.docs = {d["id"]: d for d in
                            (make_doc(i) for i in range(10))}
        self.go()
        self.assertEqual(len(self.tracked()), 10)

    def test_no_new_items_leaves_the_day_open(self):
        self.reader.docs = {}
        self.assertEqual(self.go(), 0)
        self.assertIn("Nothing new to tag yet", self.said())
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(self.paths.tracked.exists())


class CandidateTests(RunTestCase):
    def test_only_digest_feed_items_are_scored(self):
        docs = [
            make_doc(1),                                         # digest rss
            make_doc(2, host="other.example"),                   # not a digest feed
            make_doc(3, category="article", location="later"),   # your own save
            make_doc(4, parent_id="doc1"),                       # a highlight
            make_doc(5, location="later"),                       # moved on phone
            make_doc(6, source_url="https://www.magazine.example/video/clip"),
        ]
        self.reader.docs = {d["id"]: d for d in docs}
        self.go()
        self.assertEqual(sorted(self.scored_titles), ["Article 1", "Article 5"])

    def test_the_same_article_from_two_feeds_is_scored_once(self):
        twin = make_doc(2, source_url=self.reader.docs["doc1"]["source_url"])
        self.reader.docs = {d["id"]: d for d in [make_doc(1), twin]}
        self.go()
        self.assertEqual(len(self.scored_titles), 1)

    def test_the_same_title_at_two_addresses_on_one_site_is_scored_once(self):
        a = make_doc(1, title="The Quiet Pendulum",
                     source_url="https://magazine.example/interviews/sample-interview-2/")
        b = make_doc(2, title="The  Quiet pendulum ",
                     source_url="https://magazine.example/interviews/sample-interview-2026/")
        self.reader.docs = {d["id"]: d for d in [a, b]}
        self.go()
        self.assertEqual(len(self.scored_titles), 1)

    def test_the_same_title_on_different_sites_is_two_articles(self):
        a = make_doc(1, title="Same Title")
        b = make_doc(2, title="Same Title", host="newyorker.com")
        self.reader.docs = {d["id"]: d for d in [a, b]}
        self.go()
        self.assertEqual(len(self.scored_titles), 2)

    def test_a_feed_item_reader_re_created_after_tagging_is_not_tagged_again(self):
        self.go()
        self.now = NOW + timedelta(days=1)
        fresh = (self.now - timedelta(hours=1)).isoformat()
        again = make_doc(29, created_at=fresh, updated_at=fresh,   # a sure pick
                         id="doc-recreated")
        self.reader.docs = {again["id"]: again}
        self.reader.set_calls.clear()
        self.go()
        self.assertEqual(self.reader.set_calls, [])

    def test_tracked_and_already_scored_items_are_never_candidates_again(self):
        atomic_write(self.paths.tracked, {"doc1": {"id": "doc1"}})
        atomic_write(self.paths.state, {"scored_ids": {"doc2": "2026-10-27"}})
        self.reader.docs = {d["id"]: d for d in
                            (make_doc(i) for i in range(1, 5))}
        self.go()
        self.assertEqual(sorted(self.scored_titles), ["Article 3", "Article 4"])

    def test_first_run_ever_looks_back_28_hours(self):
        self.reader.docs = {d["id"]: d for d in [
            make_doc(1, age_hours=27), make_doc(2, age_hours=30)]}
        self.go()
        self.assertEqual(self.scored_titles, ["Article 1"])

    def test_later_runs_start_at_the_last_tagging_run(self):
        last = NOW - timedelta(hours=10)
        atomic_write(self.paths.state, {"last_tag_date": "2026-10-27",
                                        "last_tag_at": iso_utc(last)})
        self.reader.docs = {d["id"]: d for d in [
            make_doc(1, age_hours=5), make_doc(2, age_hours=15)]}
        self.go()
        self.assertEqual(self.scored_titles, ["Article 1"])

    def test_the_lookback_is_capped_at_36_hours(self):
        atomic_write(self.paths.state, {
            "last_tag_date": "2026-10-25",
            "last_tag_at": iso_utc(NOW - timedelta(days=3))})
        self.reader.docs = {d["id"]: d for d in [
            make_doc(1, age_hours=30), make_doc(2, age_hours=40)]}
        self.go()
        self.assertEqual(self.scored_titles, ["Article 1"])


class DailyGateTests(RunTestCase):
    def test_nothing_happens_before_five(self):
        self.now = datetime(2026, 10, 28, 4, 30).astimezone()
        self.assertEqual(self.go(), 0)
        self.assertEqual(self.reader.set_calls, [])
        self.assertEqual(self.reader.list_calls, [])
        self.assertIn("Too early", self.said())
        self.assertIn("tagged=0", self.run_log()[-1])

    def test_tags_once_per_day_then_says_so(self):
        self.now = datetime(2026, 10, 28, 5, 0).astimezone()
        self.go()
        self.set_time(datetime(2026, 10, 28, 9, 0).astimezone())
        self.assertEqual(self.go(), 0)
        self.assertEqual(len(self.reader.set_calls), 1)
        self.assertIn("Already tagged today at 05:00, 28 items", self.said())

    def test_a_new_day_tags_again(self):
        self.go()
        self.now = NOW + timedelta(days=1)
        fresh = (self.now - timedelta(hours=2)).isoformat()
        self.reader.docs = {
            d["id"]: d for d in (make_doc(f"new{i}", created_at=fresh,
                                          updated_at=fresh) for i in range(5))}
        self.assertEqual(self.go(), 0)
        self.assertEqual(len(self.reader.set_calls), 2)
        self.assertEqual(len(self.reader.set_calls[1]), 5)


class DryRunTests(RunTestCase):
    def test_dry_run_prints_the_picks_and_writes_nothing(self):
        self.assertEqual(self.go(dry_run=True), 0)
        self.assertIn("Would tag 28 items (25 picks + 3 near-misses)", self.said())
        self.assertEqual(self.reader.set_calls, [])
        self.assertEqual(sorted(p.name for p in self.paths.base.iterdir()),
                         ["interest-profile.json"])

    def test_dry_run_ignores_the_hour(self):
        self.now = datetime(2026, 10, 28, 3, 0).astimezone()
        self.go(dry_run=True)
        self.assertIn("Would tag", self.said())

    def test_dry_run_ignores_the_once_a_day_limit(self):
        self.go()
        self.now = NOW + timedelta(hours=1)
        fresh = (NOW + timedelta(minutes=30)).isoformat()
        self.reader.docs = {
            d["id"]: d for d in (make_doc(f"x{i}", created_at=fresh,
                                          updated_at=fresh) for i in range(5))}
        self.out.clear()
        self.go(dry_run=True)
        self.assertIn("Would tag 5 items", self.said())

    def test_dry_run_reads_the_feed_list_straight_from_downloads(self):
        self.go(dry_run=True)
        self.assertFalse(self.paths.opml.exists())
        self.assertFalse(self.paths.feed_sites.exists())

    def test_dry_run_does_not_notify_about_problems(self):
        self.score_error = APINetworkError("down")
        self.assertEqual(self.go(dry_run=True), 1)
        self.assertEqual(self.notices, [])
        self.assertFalse(self.paths.run_log.exists())


class FailureTests(RunTestCase):
    def test_a_failed_write_keeps_the_scores_and_the_retry_tags_without_paying(self):
        self.reader.fail_set = ReaderNetworkError("down")
        self.assertEqual(self.go(), 1)
        state = self.state()
        self.assertNotIn("last_tag_date", state)
        self.assertEqual(len(state["pending_scores"]["items"]), 40)
        self.assertFalse(self.paths.tracked.exists())
        paid = self.score_calls

        self.reader.fail_set = None
        self.assertEqual(self.go(), 0)
        self.assertEqual(self.score_calls, paid)   # no new Haiku calls
        self.assertEqual(len(self.tracked()), 28)
        self.assertEqual(self.state()["last_tag_date"], "2026-10-28")
        self.assertIn("problem=readwise-network", self.run_log()[0])
        self.assertIn("tagged=28", self.run_log()[1])

    def test_partial_207_records_only_what_reader_confirmed(self):
        self.reader.fail_first_n = 8
        self.assertEqual(self.go(), 0)
        self.assertEqual(len(self.tracked()), 20)
        self.assertEqual(self.state()["last_tag_count"], 20)

    def test_nothing_confirmed_is_a_failure_and_the_day_stays_open(self):
        self.reader.fail_first_n = 99
        self.assertEqual(self.go(), 1)
        self.assertNotIn("last_tag_date", self.state())
        self.assertFalse(self.paths.tracked.exists())
        self.assertIn("refused", self.notices[0][1])

    def test_scoring_failure_tags_nothing_and_notifies_once_a_day(self):
        self.score_error = APINetworkError("down")
        self.assertEqual(self.go(), 1)
        self.assertEqual(self.go(), 1)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("Scoring was incomplete", self.notices[0][1])
        self.assertNotIn("last_tag_date", self.state())
        self.assertEqual(self.reader.set_calls, [])

    def test_a_good_batch_is_kept_when_a_later_one_fails(self):
        self.reader.docs = {d["id"]: d for d in
                            (make_doc(i) for i in range(80))}
        original = self.fake_score

        def flaky(api_key, messages, system=None, max_tokens=0):
            if self.score_calls == 1:        # second batch fails
                self.score_calls += 1
                raise APINetworkError("down")
            return original(api_key, messages, system, max_tokens)

        self.deps.score_call = flaky
        self.assertEqual(self.go(), 1)
        self.assertEqual(len(self.state()["pending_scores"]["items"]), 50)

    def test_the_same_problem_notifies_again_the_next_day(self):
        self.score_error = APINetworkError("down")
        self.go()
        self.now = NOW + timedelta(days=1)
        self.go()
        about_it = [m for _, m in self.notices if "No articles" not in m]
        self.assertEqual(len(about_it), 2)

    def test_a_rejected_readwise_token_is_reported(self):
        def boom():
            raise ReaderAuthError("rejected", 401)
        self.reader.list_documents = lambda **kw: boom()
        self.assertEqual(self.go(), 1)
        self.assertIn("rejected the access token", self.notices[0][1])

    def test_missing_token_is_reported_without_a_client(self):
        from common import ConfigError

        def no_token():
            raise ConfigError("No Readwise token in the Keychain.")
        deps = run.Deps(client=None, client_factory=no_token,
                        notify=lambda t, m: self.notices.append((t, m)),
                        now=lambda: NOW)
        self.assertEqual(run.run_once(deps, self.paths, self.reporter, False), 1)
        self.assertIn("No Readwise token", self.notices[0][1])
        self.assertIn("problem=config", self.run_log()[-1])

    def test_missing_feed_list_is_reported(self):
        (self.downloads / "Reader_Feeds.opml").unlink()
        self.assertEqual(self.go(), 1)
        self.assertIn("No feed list yet", self.notices[0][1])
        self.assertEqual(self.reader.set_calls, [])

    def test_missing_profile_is_reported(self):
        self.paths.profile.unlink()
        self.assertEqual(self.go(), 1)
        self.assertIn("interest profile", self.notices[0][1])

    def test_an_unreadable_state_file_is_never_overwritten(self):
        self.paths.state.write_text("{ not json")
        self.assertEqual(self.go(), 1)
        self.assertEqual(self.paths.state.read_text(), "{ not json")
        self.assertEqual(self.reader.set_calls, [])


class MembershipInRunTests(RunTestCase):
    def test_real_run_copies_the_feed_list_and_caches_the_sites(self):
        self.go()
        self.assertTrue(self.paths.opml.exists())
        cache = json.loads(self.paths.feed_sites.read_text())
        self.assertEqual(cache["https://magazine.example/feed/"]["hosts"],
                         ["magazine.example"])
        self.assertTrue(cache["https://magazine.example/feed/"]["resolved"])

    def test_cleaning_downloads_afterwards_changes_nothing(self):
        self.go()
        (self.downloads / "Reader_Feeds.opml").unlink()
        self.now = NOW + timedelta(days=1)
        fresh = (self.now - timedelta(hours=1)).isoformat()
        self.reader.docs = {"docn1": make_doc("n1", created_at=fresh,
                                              updated_at=fresh)}
        self.assertEqual(self.go(), 0)
        self.assertEqual(len(self.reader.set_calls), 2)


class HelperTests(unittest.TestCase):
    def test_tag_names_handles_every_shape_reader_may_send(self):
        self.assertEqual(run.tag_names({"tags": {}}), [])
        self.assertEqual(run.tag_names({"tags": None}), [])
        self.assertEqual(run.tag_names(
            {"tags": {"to-read": {"name": "To Read"}}}), ["To Read"])
        self.assertEqual(run.tag_names({"tags": {"plain": {}}}), ["plain"])
        self.assertEqual(run.tag_names({"tags": {"bare": None}}), ["bare"])
        self.assertEqual(run.tag_names({"tags": ["a", {"name": "b"}]}), ["a", "b"])


class LockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = Paths(self._tmp.name)
        self.out = []
        self.reporter = run.Reporter(self.paths, out=self.out.append)

    def test_a_free_lock_is_taken_at_once(self):
        lock = run.RunLock(self.paths, self.reporter,
                           sleep=lambda s: self.fail("should not wait"))
        lock.acquire()
        lock.release()
        self.assertEqual(self.out, [])

    def test_a_second_run_waits_shows_the_stage_then_proceeds(self):
        holder = os.open(self.paths.lock, os.O_RDWR | os.O_CREAT)
        self.addCleanup(lambda: os.close(holder))
        fcntl.flock(holder, fcntl.LOCK_EX)
        atomic_write(self.paths.status, {"started": "07:12",
                                         "stage": "Scoring batch 3/7"})
        polls = []

        def sleep(seconds):
            polls.append(seconds)
            if len(polls) == 1:
                atomic_write(self.paths.status, {"started": "07:12",
                                                 "stage": "Tagging 28 items"})
            if len(polls) == 2:
                fcntl.flock(holder, fcntl.LOCK_UN)

        lock = run.RunLock(self.paths, self.reporter, sleep=sleep)
        lock.acquire()
        lock.release()
        said = "\n".join(self.out)
        self.assertIn("Waiting for the run that started at 07:12 to finish…", said)
        self.assertIn("now: Scoring batch 3/7", said)
        self.assertIn("now: Tagging 28 items", said)
        self.assertEqual(len(polls), 2)

    def test_the_lock_is_free_again_after_release(self):
        first = run.RunLock(self.paths, self.reporter)
        first.acquire()
        first.release()
        second = run.RunLock(self.paths, self.reporter,
                             sleep=lambda s: self.fail("should not wait"))
        second.acquire()
        second.release()

    def test_status_follows_the_stage(self):
        self.reporter.enable_status()
        self.reporter.stage("Scoring batch 1/2")
        status = json.loads(self.paths.status.read_text())
        self.assertEqual(status["stage"], "Scoring batch 1/2")


if __name__ == "__main__":
    unittest.main()


class VoteNoteTests(RunTestCase):
    def prompts(self):
        seen = []
        inner = self.deps.score_call

        def record(api_key, messages, system=None, max_tokens=0):
            seen.append(messages[0]["content"])
            return inner(api_key, messages, system=system, max_tokens=max_tokens)
        self.deps.score_call = record
        return seen

    def vote_next_day(self, mark, note):
        self.go()
        doc = self.reader.docs["doc29"]
        doc["tags"] = {"newsdigest": {"name": "newsdigest", "type": "public_api"},
                       mark: {"name": mark, "type": "manual"}}
        doc["notes"] = note
        self.set_time(NOW + timedelta(days=1))
        doc["updated_at"] = self.now.isoformat()
        fresh = (self.now - timedelta(hours=1)).isoformat()
        self.reader.docs["doc99"] = make_doc(99, created_at=fresh, updated_at=fresh)
        seen = self.prompts()
        self.go()
        return "\n".join(seen)

    def test_a_noted_thumbs_down_reaches_the_next_days_scoring(self):
        prompt = self.vote_next_day("👎", "An event announcement, not an article.")
        self.assertIn("The Reader's Own Verdicts", prompt)
        self.assertIn("👎", prompt)
        self.assertIn("An event announcement, not an article.", prompt)

    def test_a_thumbs_down_without_a_note_is_not_sent(self):
        prompt = self.vote_next_day("👎", "")
        self.assertNotIn("The Reader's Own Verdicts", prompt)

    def test_a_thumbs_down_is_not_graded_as_a_tag_of_yours(self):
        self.vote_next_day("👎", "")
        self.assertNotEqual(self.tracked()["doc29"]["grade"], 5)
