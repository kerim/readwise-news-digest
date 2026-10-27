import os
import tempfile
import unittest
from pathlib import Path

import feeds_membership as fm
from common import ConfigError, Paths

FIXTURE = Path(__file__).parent / "fixtures" / "Reader_Feeds.opml"


class HostTests(unittest.TestCase):
    def test_host_of_strips_www_and_case(self):
        self.assertEqual(fm.host_of("https://WWW.NYTimes.com/2026/a.html?smid=1"),
                         "nytimes.com")
        self.assertEqual(fm.host_of("https://news.example.co.uk/article/1"),
                         "news.example.co.uk")

    def test_host_of_handles_junk(self):
        self.assertEqual(fm.host_of(""), "")
        self.assertEqual(fm.host_of(None), "")
        self.assertEqual(fm.host_of("not a url"), "")

    def test_own_host_drops_feed_prefixes(self):
        self.assertEqual(fm.own_host("https://feeds.npr.org/1001/rss.xml"), "npr.org")
        self.assertEqual(fm.own_host("https://rss.dw.com/rdf/rss-en-all"), "dw.com")
        self.assertEqual(fm.own_host("https://www.samplejournal.example/feed/"),
                         "samplejournal.example")


class OpmlTests(unittest.TestCase):
    def test_a_real_style_export_parses(self):
        feeds = fm.parse_opml(FIXTURE)
        self.assertEqual(len(feeds), 10)
        digest = fm.digest_feeds(feeds)
        self.assertEqual(len(digest), 6)
        review = next(f for f in digest if "Example Review" in f.title)
        self.assertEqual(review.folders[-1], "News Digest feeds")
        self.assertEqual(review.url, "https://review.example.org/feed")

    def test_a_feed_in_another_folder_only_is_not_a_digest_feed(self):
        feeds = fm.parse_opml(FIXTURE)
        inquiry = next(f for f in feeds if f.title == "The Weekly Inquiry")
        self.assertNotIn(fm.DIGEST_FOLDER, inquiry.folders)
        self.assertNotIn(inquiry, fm.digest_feeds(feeds))

    def test_entity_declarations_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.opml"
            bad.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]>'
                           '<opml><body/></opml>')
            with self.assertRaises(ConfigError):
                fm.parse_opml(bad)


class OpmlFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.downloads = self.root / "Downloads"
        self.downloads.mkdir()
        self.paths = Paths(self.root / "project")
        self.paths.base.mkdir()

    def export(self, name, mtime, text="<opml/>"):
        path = self.downloads / name
        path.write_text(text)
        os.utime(path, (mtime, mtime))
        return path

    def test_newest_export_wins(self):
        self.export("Reader_Feeds.opml", 1000)
        newer = self.export("Reader_Feeds (1).opml", 2000)
        self.export("something_else.opml", 3000)
        self.assertEqual(fm.newest_opml(self.downloads), newer)

    def test_none_when_there_is_no_export(self):
        self.assertIsNone(fm.newest_opml(self.downloads))

    def test_copies_a_newer_export_into_the_project(self):
        self.export("Reader_Feeds.opml", 2000, "<opml>new</opml>")
        self.assertTrue(fm.refresh_opml(self.paths, self.downloads))
        self.assertEqual(self.paths.opml.read_text(), "<opml>new</opml>")

    def test_leaves_the_copy_alone_when_nothing_is_newer(self):
        self.export("Reader_Feeds.opml", 2000, "<opml>x</opml>")
        fm.refresh_opml(self.paths, self.downloads)
        self.assertFalse(fm.refresh_opml(self.paths, self.downloads))

    def test_cleaning_downloads_changes_nothing(self):
        path = self.export("Reader_Feeds.opml", 2000, "<opml>keep</opml>")
        fm.refresh_opml(self.paths, self.downloads)
        path.unlink()
        self.assertFalse(fm.refresh_opml(self.paths, self.downloads))
        read_from, source = fm.locate_opml(self.paths, self.downloads)
        self.assertEqual(read_from, self.paths.opml)
        self.assertIsNone(source)

    def test_a_still_newer_export_replaces_the_copy(self):
        self.export("Reader_Feeds.opml", 2000, "<opml>old</opml>")
        fm.refresh_opml(self.paths, self.downloads)
        self.export("Reader_Feeds (1).opml", 3000, "<opml>newer</opml>")
        self.assertTrue(fm.refresh_opml(self.paths, self.downloads))
        self.assertEqual(self.paths.opml.read_text(), "<opml>newer</opml>")

    def test_no_feed_list_anywhere_is_a_config_error(self):
        with self.assertRaises(ConfigError):
            fm.refresh_opml(self.paths, self.downloads)

    def test_locate_does_not_copy(self):
        self.export("Reader_Feeds.opml", 2000)
        read_from, source = fm.locate_opml(self.paths, self.downloads)
        self.assertEqual(read_from, source)
        self.assertFalse(self.paths.opml.exists())


class HostsFromLinksTests(unittest.TestCase):
    def test_links_give_hosts_most_common_first(self):
        hosts, resolved = fm.hosts_from_links(
            ["https://www.dailypaper.example/a", "https://dailypaper.example/b", "https://www.dailypaper.example/c"],
            "https://feeds.newsfeeds.example/rss/world.xml")
        self.assertEqual(hosts, ["dailypaper.example"])
        self.assertTrue(resolved)

    def test_feedburner_feed_resolves_to_the_real_site(self):
        hosts, resolved = fm.hosts_from_links(
            ["https://www.booksreview.example/articles/1/"],
            "http://feeds.feedburner.com/booksreview")
        self.assertEqual(hosts, ["booksreview.example"])
        self.assertTrue(resolved)

    def test_no_entries_falls_back_to_the_feeds_own_site_and_stays_unresolved(self):
        hosts, resolved = fm.hosts_from_links([], "https://rss.nytimes.com/x.xml")
        self.assertEqual(hosts, ["nytimes.com"])
        self.assertFalse(resolved)

    def test_blank_links_count_as_no_entries(self):
        hosts, resolved = fm.hosts_from_links(["", ""], "https://rss.dw.com/rdf")
        self.assertEqual(hosts, ["dw.com"])
        self.assertFalse(resolved)

    def test_a_link_blog_maps_to_its_own_site_only(self):
        links = [f"https://site{i}.example/post" for i in range(8)]
        hosts, resolved = fm.hosts_from_links(links, "https://linkblog.example/feed/")
        self.assertEqual(hosts, ["linkblog.example"])
        self.assertTrue(resolved)

    def test_five_hosts_is_not_yet_a_link_blog(self):
        links = [f"https://site{i}.example/post" for i in range(5)]
        hosts, _ = fm.hosts_from_links(links, "https://linkblog.example/feed/")
        self.assertEqual(len(hosts), 5)


class MembershipTests(unittest.TestCase):
    def feeds(self):
        return [
            fm.Feed("BooksReview", "http://feeds.feedburner.com/booksreview",
                    ("News Digest feeds",)),
            fm.Feed("Dead", "https://rss.dw.com/rdf", ("News Digest feeds",)),
        ]

    def test_resolves_through_the_fetcher_and_matches_items(self):
        links = {"http://feeds.feedburner.com/booksreview":
                 ["https://www.booksreview.example/articles/1/"],
                 "https://rss.dw.com/rdf": []}
        m = fm.Membership(self.feeds(), {})
        resolved = m.resolve_pending(lambda url: links[url])
        self.assertEqual(resolved, 1)
        self.assertTrue(m.is_digest_item("https://www.booksreview.example/articles/2/"))
        self.assertTrue(m.is_digest_item("https://dw.com/en/story"))   # fallback
        self.assertFalse(m.is_digest_item("https://example.com/x"))
        self.assertEqual([f.title for f in m.unresolved], ["Dead"])

    def test_resolved_feeds_are_not_fetched_again_but_failed_ones_are(self):
        fetched = []

        def fetch(url):
            fetched.append(url)
            return ["https://www.dw.com/en/story"] if "dw" in url else \
                ["https://www.booksreview.example/a"]

        m = fm.Membership(self.feeds(), {
            "http://feeds.feedburner.com/booksreview":
                {"hosts": ["booksreview.example"], "resolved": True},
            "https://rss.dw.com/rdf":
                {"hosts": ["dw.com"], "resolved": False},
        })
        m.resolve_pending(fetch)
        self.assertEqual(fetched, ["https://rss.dw.com/rdf"])
        self.assertEqual(m.unresolved, [])

    def test_item_without_an_address_is_not_a_digest_item(self):
        m = fm.Membership(self.feeds(), {})
        self.assertFalse(m.is_digest_item(None))
        self.assertFalse(m.is_digest_item(""))

    def test_an_export_has_digest_hosts_for_every_feed(self):
        feeds = fm.digest_feeds(fm.parse_opml(FIXTURE))
        m = fm.Membership(feeds, {})
        # Before any lookup every feed already maps to its own site.
        self.assertIn("review.example.org", m.digest_hosts)
        self.assertIn("dailysample.example", m.digest_hosts)
        self.assertIn("wire.sample.example", m.digest_hosts)
        self.assertNotIn("science today", m.digest_hosts)
        self.assertNotIn("", m.digest_hosts)


if __name__ == "__main__":
    unittest.main()
