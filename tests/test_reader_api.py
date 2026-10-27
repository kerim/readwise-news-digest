import json
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from reader_api import (
    RateLimiter,
    ReaderAPIError,
    ReaderAuthError,
    ReaderClient,
    ReaderNetworkError,
)


class FakeTransport:
    """Plays back scripted replies: (status, data, headers) or an exception."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": json.loads(body) if body else None})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, data, *rest = reply
        raw = json.dumps(data).encode() if data is not None else b""
        return status, (rest[0] if rest else {}), raw


def make_client(replies, **kwargs):
    transport = FakeTransport(replies)
    sleeps = []
    client = ReaderClient("tok", transport=transport, sleep=sleeps.append,
                          **kwargs)
    return client, transport, sleeps


def query(call):
    return parse_qs(urlsplit(call["url"]).query)


class ListDocumentsTests(unittest.TestCase):
    def test_follows_every_page_and_sends_filters(self):
        client, transport, _ = make_client([
            (200, {"results": [{"id": "a"}], "nextPageCursor": "c1"}),
            (200, {"results": [{"id": "b"}], "nextPageCursor": "c2"}),
            (200, {"results": [{"id": "c"}], "nextPageCursor": None}),
        ])
        when = datetime(2026, 10, 27, 5, 0, tzinfo=timezone.utc)
        docs = client.list_documents(updated_after=when, category="rss",
                                     location="feed")
        self.assertEqual([d["id"] for d in docs], ["a", "b", "c"])
        first, second = query(transport.calls[0]), query(transport.calls[1])
        self.assertEqual(first["updatedAfter"], ["2026-10-27T05:00:00+00:00"])
        self.assertEqual(first["category"], ["rss"])
        self.assertEqual(first["location"], ["feed"])
        self.assertNotIn("pageCursor", first)
        self.assertEqual(second["pageCursor"], ["c1"])
        self.assertEqual(transport.calls[0]["headers"]["Authorization"],
                         "Token tok")

    def test_repeated_tag_parameters(self):
        client, transport, _ = make_client([(200, {"results": []})])
        client.list_documents(tag=["newsdigest", "newsdigest-copy"])
        self.assertEqual(query(transport.calls[0])["tag"],
                         ["newsdigest", "newsdigest-copy"])

    def test_a_repeated_cursor_ends_the_listing(self):
        client, _, _ = make_client([
            (200, {"results": [{"id": "a"}], "nextPageCursor": "c1"}),
            (200, {"results": [{"id": "b"}], "nextPageCursor": "c1"}),
        ])
        self.assertEqual(len(client.list_documents()), 2)

    def test_a_failing_page_raises_instead_of_returning_half(self):
        client, _, _ = make_client([
            (200, {"results": [{"id": "a"}], "nextPageCursor": "c1"}),
            (400, {"detail": "bad"}),
        ])
        with self.assertRaises(ReaderAPIError):
            client.list_documents()

    def test_page_callback(self):
        client, _, _ = make_client([
            (200, {"results": [{"id": "a"}], "nextPageCursor": "c1"}),
            (200, {"results": [{"id": "b"}]}),
        ])
        seen = []
        client.list_documents(on_page=lambda page, total: seen.append((page, total)))
        self.assertEqual(seen, [(1, 1), (2, 2)])


class RetryTests(unittest.TestCase):
    def test_rate_limit_waits_retry_after_then_retries(self):
        client, transport, sleeps = make_client([
            (429, {}, {"retry-after": "7"}),
            (200, {"results": []}),
        ])
        client.list_documents()
        self.assertEqual(len(transport.calls), 2)
        self.assertIn(7, sleeps)

    def test_rate_limit_gives_up_eventually(self):
        client, _, _ = make_client([(429, {}, {"retry-after": "1"})] * 10)
        with self.assertRaises(ReaderAPIError) as ctx:
            client.list_documents()
        self.assertEqual(ctx.exception.status, 429)

    def test_network_failure_is_retried_then_succeeds(self):
        client, transport, sleeps = make_client([
            ReaderNetworkError("down"),
            ReaderNetworkError("down"),
            (200, {"results": [{"id": "a"}]}),
        ])
        self.assertEqual(len(client.list_documents()), 1)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(sleeps, [5, 15])

    def test_network_failure_raises_after_about_a_minute_of_retries(self):
        client, transport, sleeps = make_client([ReaderNetworkError("down")] * 5)
        with self.assertRaises(ReaderNetworkError):
            client.list_documents()
        self.assertEqual(sum(sleeps), 60)
        self.assertEqual(len(transport.calls), 4)

    def test_server_errors_are_retried(self):
        client, _, _ = make_client([(503, {}), (200, {"results": []})])
        self.assertEqual(client.list_documents(), [])

    def test_bad_token_is_an_auth_error_and_not_retried(self):
        client, transport, _ = make_client([(401, {"detail": "nope"})])
        with self.assertRaises(ReaderAuthError):
            client.list_documents()
        self.assertEqual(len(transport.calls), 1)

    def test_check_token(self):
        client, _, _ = make_client([(204, None)])
        self.assertTrue(client.check_token())
        client, _, _ = make_client([(401, {})])
        self.assertFalse(client.check_token())


class SetTagsTests(unittest.TestCase):
    def test_sends_the_whole_tag_list_and_confirms_all_on_200(self):
        client, transport, _ = make_client([
            (200, {"results": [{"id": "a", "success": True},
                               {"id": "b", "success": True}]}),
        ])
        confirmed, failed = client.set_tags([("a", ["x", "newsdigest"]),
                                             ("b", ["newsdigest"])])
        self.assertEqual(confirmed, {"a", "b"})
        self.assertEqual(failed, {})
        call = transport.calls[0]
        self.assertEqual(call["method"], "PATCH")
        self.assertTrue(call["url"].endswith("/v3/bulk_update/"))
        self.assertEqual(call["body"], {"updates": [
            {"id": "a", "tags": ["x", "newsdigest"]},
            {"id": "b", "tags": ["newsdigest"]}]})

    def test_207_records_only_the_successes(self):
        client, _, _ = make_client([
            (207, {"results": [{"id": "a", "success": True},
                               {"id": "b", "success": False,
                                "error": "Document not found"}]}),
        ])
        confirmed, failed = client.set_tags([("a", ["t"]), ("b", ["t"])])
        self.assertEqual(confirmed, {"a"})
        self.assertEqual(failed, {"b": "Document not found"})

    def test_207_without_a_per_item_answer_is_not_vouched_for(self):
        client, _, _ = make_client([
            (207, {"results": [{"id": "a", "success": True}]}),
        ])
        confirmed, failed = client.set_tags([("a", ["t"]), ("b", ["t"])])
        self.assertEqual(confirmed, {"a"})
        self.assertIn("b", failed)

    def test_more_than_fifty_go_in_chunks_of_fifty(self):
        updates = [(f"d{i}", ["t"]) for i in range(120)]
        replies = [(200, {"results": []})] * 3
        client, transport, _ = make_client(replies)
        confirmed, _ = client.set_tags(updates)
        self.assertEqual(len(confirmed), 120)
        sizes = [len(c["body"]["updates"]) for c in transport.calls]
        self.assertEqual(sizes, [50, 50, 20])


class SaveDocumentTests(unittest.TestCase):
    def test_a_new_address_creates_a_document(self):
        client, transport, _ = make_client([(201, {"id": "d1", "url": "u"})])
        created, doc_id = client.save_document(
            "https://x.invalid/s/1", "Title", "<p>hi</p>", tags=["t"], location="new")
        self.assertEqual((created, doc_id), (True, "d1"))
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].endswith("/v3/save/"))
        self.assertEqual(call["body"]["url"], "https://x.invalid/s/1")
        self.assertEqual(call["body"]["title"], "Title")
        # Reader answers 400 without both an author and a title on a note like this.
        self.assertEqual(call["body"]["author"], "Readwise News Digest")
        self.assertEqual(call["body"]["html"], "<p>hi</p>")
        self.assertEqual(call["body"]["tags"], ["t"])
        self.assertEqual(call["body"]["location"], "new")
        self.assertFalse(call["body"]["should_clean_html"])

    def test_an_address_that_already_exists_is_reported_not_overwritten(self):
        client, _, _ = make_client([(200, {"id": "d1", "url": "u"})])
        created, doc_id = client.save_document("https://x.invalid/s/1", "T", "<p/>")
        self.assertEqual((created, doc_id), (False, "d1"))


class RateLimiterTests(unittest.TestCase):
    def test_waits_when_the_window_is_full(self):
        now = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        limiter = RateLimiter(max_calls=3, period=60, clock=lambda: now[0],
                              sleep=sleep)
        for _ in range(3):
            limiter.wait()
            now[0] += 1
        limiter.wait()   # the 4th call must wait for the 1st to age out
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 57, delta=1)

    def test_no_wait_under_the_limit(self):
        limiter = RateLimiter(max_calls=3, period=60, clock=lambda: 0.0,
                              sleep=lambda s: self.fail("should not sleep"))
        limiter.wait()
        limiter.wait()


if __name__ == "__main__":
    unittest.main()
