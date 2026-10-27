import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feed_discovery
import suggest
from common import Paths, load_weekly
from reader_api import ReaderNetworkError
from tests.support import FEED_LINKS, OPML, FakeReader, make_doc, tags_dict

TODAY = "2026-12-01"
NOW = datetime(2026, 12, 1, 4, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=suggest.ACTIVITY_DAYS)


def saved(i, host, progress=0, days_ago=3, **kw):
    stamp = (NOW - timedelta(days=days_ago)).isoformat()
    doc = make_doc(f"s{i}", id=f"s{i}", category="article", location="later",
                   source_url=f"https://www.{host}/post-{i}", title=f"Post {i}",
                   reading_progress=progress, created_at=stamp, updated_at=stamp)
    doc.update(kw)
    return doc


def trio(host, start=0, **kw):
    """Three articles saved from one site: the least that counts."""
    return [saved(start + n, host, **kw) for n in range(3)]


def highlight(parent, n=1):
    return [{"id": f"h-{parent}-{k}", "parent_id": parent, "category": "highlight"}
            for k in range(n)]


class WhenTests(unittest.TestCase):
    def test_due_about_every_two_months(self):
        self.assertTrue(suggest.due({}, TODAY))
        self.assertFalse(suggest.due({"last_suggestions_date": "2026-10-08"}, TODAY))
        self.assertTrue(suggest.due({"last_suggestions_date": "2026-10-07"}, TODAY))
        self.assertTrue(suggest.due({"last_suggestions_date": TODAY}, TODAY, force=True))

    def test_a_site_is_never_suggested_twice(self):
        weekly = {"suggested": {"once.com": ["2020-01-01"]}}
        self.assertEqual(suggest.already_suggested(weekly), {"once.com"})
        self.assertEqual(suggest.already_suggested({}), set())


class RankActivityTests(unittest.TestCase):
    def rank(self, docs, highlights=(), excluded=()):
        return suggest.rank_activity(docs, list(highlights), set(excluded), SINCE)

    def test_a_site_needs_three_saved_articles(self):
        rows = self.rank([saved(1, "two.com"), saved(2, "two.com")] + trio("three.com", 10))
        self.assertEqual([r["host"] for r in rows], ["three.com"])

    def test_highlights_alone_do_not_make_a_site_count(self):
        docs = [saved(1, "lit.com"), saved(2, "lit.com")]
        self.assertEqual(self.rank(docs, highlight("s1", 9)), [])

    def test_only_articles_saved_in_the_last_six_months_count(self):
        docs = [saved(1, "slow.com"), saved(2, "slow.com"),
                saved(3, "slow.com", days_ago=200)]      # saved too long ago
        self.assertEqual(self.rank(docs), [])

    def test_strength_counts_saves_highlights_and_reading(self):
        docs = trio("a.com", progress=0.0, start=0)
        docs[0]["reading_progress"] = 0.9
        rows = self.rank(docs + trio("b.com", start=10), highlight("s0", 2))
        by_host = {r["host"]: r for r in rows}
        self.assertEqual(by_host["a.com"]["score"], 3 + 2 + 1)
        self.assertEqual(by_host["b.com"]["score"], 3)
        self.assertEqual([r["host"] for r in rows], ["a.com", "b.com"])

    def test_highlights_on_one_article_stop_counting_at_five(self):
        rows = self.rank(trio("a.com"), highlight("s0", 12))
        self.assertEqual(rows[0]["highlights"], 5)

    def test_followed_sites_are_left_out(self):
        self.assertEqual(self.rank(trio("followed.com"), excluded={"followed.com"}), [])

    def test_social_archive_platform_and_our_own_notes_are_left_out(self):
        docs = []
        for n, host in enumerate(("x.com", "archive.fo", "archive.today", "patreon.com",
                                  "readwise-news-digest.invalid")):
            docs += trio(host, start=n * 10)
        self.assertEqual(self.rank(docs), [])

    def test_an_address_with_no_real_site_is_ignored(self):
        self.assertEqual(self.rank(trio("read")), [])

    def test_forwarded_emails_have_no_site(self):
        docs = [saved(n, "x.com", source_url="mailto:someone@example.com")
                for n in range(3)]
        self.assertEqual(self.rank(docs), [])

    def test_saved_copies_of_tagged_items_are_left_out(self):
        docs = trio("a.com", tags=tags_dict("newsdigest-copy"))
        self.assertEqual(self.rank(docs), [])

    def test_highlight_and_note_documents_are_not_saves(self):
        docs = [saved(n, "a.com", parent_id="x") for n in range(3)]
        self.assertEqual(self.rank(docs), [])

    def test_a_highlight_on_something_we_did_not_count_is_ignored(self):
        self.assertEqual(self.rank(trio("a.com"), highlight("nobody", 3))[0]["highlights"], 0)

    def test_reasons_read_naturally(self):
        self.assertEqual(
            suggest.activity_reason({"saves": 3, "highlights": 2, "read": 1}),
            "You saved 3 articles, highlighted 2 passages and read 1 at length "
            "from this site in the last 6 months.")
        self.assertEqual(
            suggest.activity_reason({"saves": 4, "highlights": 0, "read": 0}),
            "You saved 4 articles from this site in the last 6 months.")


class ActivityItemsTests(unittest.TestCase):
    def test_only_sites_with_a_working_feed_are_offered(self):
        ranked = [{"host": "has.com", "saves": 3, "highlights": 0, "read": 0},
                  {"host": "none.com", "saves": 3, "highlights": 0, "read": 0}]
        feeds = {"has.com": "https://has.com/feed"}
        items = suggest.activity_items(ranked, feeds.get)
        self.assertEqual([i["host"] for i in items], ["has.com"])
        self.assertEqual(items[0]["feed_url"], "https://has.com/feed")

    def test_at_most_five(self):
        ranked = [{"host": f"s{i}.com", "saves": 3, "highlights": 0, "read": 0}
                  for i in range(30)]
        items = suggest.activity_items(ranked, lambda h: f"https://{h}/feed")
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0]["host"], "s0.com")      # strongest first

    def test_strong_sites_without_a_feed_do_not_use_up_the_list(self):
        ranked = [{"host": f"s{i}.com", "saves": 3, "highlights": 0, "read": 0}
                  for i in range(30)]
        with_feed = {f"s{i}.com" for i in (12, 13, 20, 21, 22, 23)}   # all far down
        items = suggest.activity_items(
            ranked, lambda h: f"https://{h}/feed" if h in with_feed else None)
        self.assertEqual([i["host"] for i in items],
                         ["s12.com", "s13.com", "s20.com", "s21.com", "s22.com"])

    def test_it_gives_up_after_a_fixed_number_of_tries(self):
        ranked = [{"host": f"s{i}.com", "saves": 3, "highlights": 0, "read": 0}
                  for i in range(100)]
        tried = []
        suggest.activity_items(ranked, lambda h: tried.append(h) or None)
        self.assertEqual(len(tried), suggest.MAX_FEED_CHECKS)


class DocumentTests(unittest.TestCase):
    item = {"name": "Good <b>Mag</b>", "host": "good.com",
            "feed_url": "https://good.com/feed?a=1&b=2", "reason": "Fits & more"}

    def test_title_address_and_escaping(self):
        title, html = suggest.build_document(TODAY, [self.item])
        self.assertEqual(title, "Feed suggestions — 2026-12-01")
        self.assertIn("Good &lt;b&gt;Mag&lt;/b&gt;", html)
        self.assertIn("Fits &amp; more", html)
        self.assertIn('href="https://good.com/feed?a=1&amp;b=2"', html)
        self.assertEqual(suggest.document_url(TODAY),
                         "https://readwise-news-digest.invalid/suggestions/2026-12-01")

    def test_the_note_does_not_mention_an_inbox(self):
        _, html = suggest.build_document(TODAY, [self.item])
        self.assertNotIn("nbox", html)

    def test_the_plain_text_version_lists_each_feed(self):
        self.assertIn("Feed: https://good.com/feed?a=1&b=2",
                      suggest.describe("T", [self.item]))


class DiscoveryTests(unittest.TestCase):
    def test_link_tags_in_a_home_page(self):
        parser = feed_discovery.LinkParser()
        parser.feed('<head><link rel="alternate" type="application/rss+xml" href="/f.xml">'
                    '<link rel="stylesheet" href="/x.css"></head>')
        self.assertEqual(parser.feeds, ["/f.xml"])
        self.assertEqual(feed_discovery.absolute("/f.xml", "a.com"), "https://a.com/f.xml")
        self.assertEqual(feed_discovery.absolute("//cdn.a.com/f", "a.com"),
                         "https://cdn.a.com/f")

    def test_a_feed_is_only_offered_after_it_validates(self):
        found = feed_discovery.find_working_feed(
            "a.com", discover=lambda d: ["https://a.com/broken", "https://a.com/ok"],
            validate=lambda url: url.endswith("ok"))
        self.assertEqual(found, "https://a.com/ok")
        self.assertIsNone(feed_discovery.find_working_feed(
            "a.com", discover=lambda d: ["https://a.com/broken"],
            validate=lambda url: False))
        self.assertIsNone(feed_discovery.find_working_feed(
            "a.com", discover=lambda d: [], validate=lambda url: True))


class RunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.paths = Paths(root / "project")
        self.paths.base.mkdir()
        self.downloads = root / "Downloads"
        self.downloads.mkdir()
        (self.downloads / "Reader_Feeds.opml").write_text(OPML)
        docs = (trio("newsite.com", 0, progress=0.9)
                + trio("magazine.example", 10)            # followed (digest feed)
                + trio("other.example", 20)          # followed (not a digest feed)
                + trio("nofeed.com", 30)             # no working feed
                + [saved(40, "twice.com"), saved(41, "twice.com")])   # too few
        self.reader = FakeReader(docs)
        self.reader.highlight("s0")
        self.out = []
        self.weekly = {"suggested": {}}

    def feeds(self, host):
        return {"newsite.com": "https://newsite.com/feed"}.get(host)

    def go(self, **kw):
        options = {
            "discover": lambda host: [f"https://{host}/feed"],
            "validate": lambda url: self.feeds(url.split("/")[2]) is not None,
            "fetch_links": lambda url: FEED_LINKS.get(url, []),
            "downloads": self.downloads, "say": self.out.append,
        }
        options.update(kw)
        return suggest.run(self.paths, self.reader, TODAY, NOW, self.weekly, **options)

    def test_delivers_one_note_tagged_for_the_newsdigest_view(self):
        result = self.go()
        self.assertEqual(result["status"], "delivered")
        note = self.reader.save_calls[0]
        self.assertEqual(note["url"],
                         "https://readwise-news-digest.invalid/suggestions/2026-12-01")
        self.assertEqual(note["title"], "Feed suggestions — 2026-12-01")
        self.assertEqual(note["tags"], ["newsdigest", "newsdigest-suggestions"])
        self.assertEqual(note["location"], "later")      # there is no Inbox to use
        self.assertIn("newsite.com", note["html"])
        self.assertIn("https://newsite.com/feed", note["html"])
        self.assertIn("You saved 3 articles", note["html"])

    def test_sites_you_follow_or_with_no_feed_or_too_few_saves_are_never_offered(self):
        self.go()
        html = self.reader.save_calls[0]["html"]
        for host in ("magazine.example", "other.example", "nofeed.com", "twice.com"):
            self.assertNotIn(host, html)

    def test_delivery_is_remembered_only_after_it_happens(self):
        self.go()
        weekly = load_weekly(self.paths)
        self.assertEqual(weekly["last_suggestions_date"], TODAY)
        self.assertEqual(weekly["suggested"], {"newsite.com": [TODAY]})

    def test_a_site_already_suggested_is_never_suggested_again(self):
        self.weekly["suggested"] = {"newsite.com": ["2020-01-01"]}
        self.assertEqual(self.go()["status"], "nothing to suggest")
        self.assertEqual(self.reader.save_calls, [])

    def test_not_due_does_nothing(self):
        self.weekly["last_suggestions_date"] = "2026-11-28"
        self.assertEqual(self.go()["status"], "not due")
        self.assertEqual(self.reader.save_calls, [])
        self.assertEqual(self.reader.list_calls, [])

    def test_nothing_to_suggest_is_remembered_so_it_waits_two_months(self):
        self.weekly["suggested"] = {"newsite.com": ["2020-01-01"]}
        self.go()
        self.assertEqual(load_weekly(self.paths)["last_suggestions_date"], TODAY)

    def test_a_failed_delivery_is_not_remembered_so_tomorrow_retries(self):
        def boom(*a, **k):
            raise ReaderNetworkError("down")
        self.reader.save_document = boom
        with self.assertRaises(ReaderNetworkError):
            self.go()
        self.assertEqual(self.weekly["suggested"], {})
        self.assertNotIn("last_suggestions_date", self.weekly)
        self.assertFalse(self.paths.weekly.exists())

    def test_a_same_day_rerun_finds_the_note_and_leaves_it(self):
        self.go()
        self.weekly = {"suggested": {}}              # the first run died before remembering
        self.out.clear()
        result = self.go()
        self.assertEqual(result["status"], "already delivered")
        self.assertIn("already in your Reader library", " ".join(self.out))
        self.assertEqual(self.weekly["suggested"], {})      # not recorded twice
        self.assertEqual(self.weekly["last_suggestions_date"], TODAY)

    def test_a_dry_run_writes_nothing_anywhere(self):
        result = self.go(dry_run=True)
        self.assertEqual(result["status"], "dry run")
        self.assertEqual(self.reader.save_calls, [])
        self.assertFalse(self.paths.weekly.exists())
        self.assertFalse(self.paths.feed_sites.exists())
        self.assertFalse(self.paths.opml.exists())
        self.assertIn("Feed: https://newsite.com/feed", "\n".join(self.out))

    def test_force_delivers_even_when_not_due(self):
        self.weekly["last_suggestions_date"] = "2026-11-30"
        self.assertEqual(self.go(force=True)["status"], "delivered")

    def test_no_api_key_or_web_search_is_involved(self):
        # the job runs with no Anthropic access at all
        import inspect
        self.assertNotIn("api_key", inspect.signature(suggest.run).parameters)
        self.assertNotIn("call", inspect.signature(suggest.run).parameters)


if __name__ == "__main__":
    unittest.main()
