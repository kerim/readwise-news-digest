import unittest

import copies


def key(url, title="A title that is long enough"):
    return copies.make_key(url, title)


def match(target_url, target_title, candidates):
    """candidates: [(id, url, title)] -> (id, how)"""
    pool = {cid: key(url, title) for cid, url, title in candidates}
    return copies.find_match(key(target_url, target_title), pool)


class AddressTests(unittest.TestCase):
    def test_query_fragment_trailing_slash_and_www_are_ignored(self):
        self.assertEqual(
            copies.address_key("https://www.nytimes.com/2026/10/18/opinion/x.html?smid=abc#top"),
            copies.address_key("https://nytimes.com/2026/10/18/opinion/x.html"))
        self.assertEqual(copies.address_key("https://a.com/p/"),
                         copies.address_key("https://a.com/p"))

    def test_nyt_smid_and_ft_syndication_query_match_by_address(self):
        self.assertEqual(match(
            "https://www.nytimes.com/2026/10/18/opinion/sample-story.html?smid=nytcore-share",
            "T", [("feed", "https://www.nytimes.com/2026/10/18/opinion/sample-story.html", "x")]),
            ("feed", "address"))
        self.assertEqual(match(
            "https://www.ft.com/content/0a1b2c3d-1111-4222-8333-444455556666",
            "FT", [("feed", "https://www.ft.com/content/0a1b2c3d-1111-4222-8333-444455556666?syn-abc123=1", "x")]),
            ("feed", "address"))

    def test_wsj_matches_on_the_trailing_id_when_the_path_differs(self):
        self.assertEqual(match(
            "https://www.wsj.com/world/africa/sample-story-one-c2e5d0a4",
            "Playboy",
            [("feed", "https://www.wsj.com/articles/sample-story-two-c2e5d0a4", "x")]),
            ("feed", "address"))

    def test_wsj_different_ids_do_not_match(self):
        self.assertEqual(match(
            "https://www.wsj.com/world/a-c2e5d0a4", "Alpha beta gamma delta",
            [("feed", "https://www.wsj.com/articles/a-00000000", "Epsilon zeta eta theta")]),
            (None, None))

    def test_a_bare_host_is_not_an_address(self):
        self.assertEqual(match("https://a.com/", "Some long headline here",
                               [("x", "https://a.com", "Another long headline there")]),
                         (None, None))


class ArticleIdTests(unittest.TestCase):
    def test_numeric_ids(self):
        self.assertEqual(copies.article_ids(
            "https://www.npr.org/2026/10/27/nx-s1-5550123/sample-story"), {"5550123"})
        self.assertEqual(copies.article_ids(
            "https://www.dw.com/en/story/a-70000001?maca=en-rss"), {"70000001"})
        self.assertEqual(copies.article_ids(
            "https://www.technologyreview.com/2026/10/26/1100001/sample-story/"),
            {"1100001"})

    def test_dates_and_short_numbers_are_not_ids(self):
        self.assertEqual(copies.article_ids(
            "https://www.france24.com/en/europe/20261026-sample-story"), set())
        self.assertEqual(copies.article_ids("https://a.com/2026/10/27/story"), set())

    def test_hex_ids_need_a_digit_so_words_like_decade_do_not_count(self):
        self.assertEqual(copies.article_ids("https://a.com/the-decade-ahead"), set())
        self.assertIn("c2e5d0a4", copies.article_ids("https://a.com/x-c2e5d0a4"))

    def test_uuids(self):
        ids = copies.article_ids(
            "https://www.ft.com/content/0a1b2c3d-1111-4222-8333-444455556666")
        self.assertIn("0a1b2c3d-1111-4222-8333-444455556666", ids)

    def test_a_shared_id_matches_when_the_slug_differs(self):
        self.assertEqual(match(
            "https://www.dw.com/en/new-slug/a-70000001",
            "Different words entirely",
            [("feed", "https://www.dw.com/en/old-slug/a-70000001?maca=en-rss",
              "Something else altogether")]), ("feed", "id"))

    def test_a_shared_id_on_another_site_never_matches(self):
        self.assertEqual(match(
            "https://other.com/a-70000001", "Same headline for sure here",
            [("feed", "https://www.dw.com/en/x/a-70000001", "Same headline for sure here")]),
            (None, None))


class HeadlineTests(unittest.TestCase):
    def test_ignores_case_punctuation_and_a_trailing_author_or_site(self):
        self.assertEqual(match(
            "https://www.newyorker.com/new-address/one",
            "How Tree Rings Changed Our Idea of an Old Forest | The Example Weekly",
            [("feed", "https://www.newyorker.com/old-address/two",
              "How tree rings changed our idea of an old forest")]),
            ("feed", "headline"))
        self.assertEqual(match(
            "https://x.com/s/1", "The Fall of 'Example Quarterly'",
            [("feed", "https://x.com/t/2", "The fall of “Example Quarterly” - A. Writer")]),
            ("feed", "headline"))

    def test_close_but_different_headlines_do_not_match(self):
        self.assertEqual(match(
            "https://x.com/s/1", "Mayor wins in Springfield, narrowly",
            [("feed", "https://x.com/t/2", "Mayor wins in Springfield, comfortably")]),
            (None, None))

    def test_a_short_headline_must_match_exactly(self):
        self.assertEqual(match("https://x.com/s/1", "Article 59",
                               [("feed", "https://x.com/t/2", "Article 29")]),
                         (None, None))
        self.assertEqual(match("https://x.com/s/1", "Power to the People",
                               [("feed", "https://x.com/t/2", "power to the people!")]),
                         ("feed", "headline"))

    def test_a_long_headline_may_differ_slightly(self):
        self.assertEqual(match(
            "https://x.com/s/1",
            "Why the Springfield Garden Club is so successful now",
            [("feed", "https://x.com/t/2",
              "Why the Springfield Garden Club is so successful today")]),
            ("feed", "headline"))

    def test_two_candidates_means_no_link(self):
        self.assertEqual(match(
            "https://x.com/s/1", "Same headline appears twice here",
            [("a", "https://x.com/t/2", "Same headline appears twice here"),
             ("b", "https://x.com/t/3", "Same headline appears twice here")]),
            (None, None))

    def test_never_across_sites(self):
        self.assertEqual(match(
            "https://one.com/s/1", "Same headline on two sites",
            [("feed", "https://two.com/t/2", "Same headline on two sites")]),
            (None, None))

    def test_a_title_that_is_just_the_site_name_is_too_generic(self):
        self.assertEqual(match(
            "https://www.ft.com/content/aaaa1111-0000-0000-0000-000000000001", "FT",
            [("feed", "https://www.ft.com/world/x", "FT")]), (None, None))

    def test_address_wins_over_headline_and_id_wins_over_headline(self):
        pool = {"by_title": key("https://x.com/a/1", "The very same headline here"),
                "by_address": key("https://x.com/p/q", "Unrelated words wholly")}
        self.assertEqual(copies.find_match(
            key("https://x.com/p/q", "The very same headline here"), pool),
            ("by_address", "address"))


class CandidateTests(unittest.TestCase):
    def doc(self, **kw):
        d = {"id": "c", "category": "article", "parent_id": None,
             "source_url": "https://www.nytimes.com/2026/a.html"}
        d.update(kw)
        return d

    def test_your_saved_articles_pdfs_books_and_emails_qualify(self):
        for category in ("article", "pdf", "epub", "email"):
            self.assertTrue(copies.is_copy_candidate(self.doc(category=category)))

    def test_feed_items_highlights_notes_tweets_and_videos_do_not(self):
        for category in ("rss", "highlight", "note", "tweet", "video"):
            self.assertFalse(copies.is_copy_candidate(self.doc(category=category)))
        self.assertFalse(copies.is_copy_candidate(self.doc(parent_id="x")))

    def test_our_own_suggestions_document_and_address_less_documents_do_not(self):
        self.assertFalse(copies.is_copy_candidate(self.doc(
            source_url="https://readwise-news-digest.invalid/suggestions/2026-10-30")))
        self.assertFalse(copies.is_copy_candidate(self.doc(source_url=None)))


if __name__ == "__main__":
    unittest.main()
