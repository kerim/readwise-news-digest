"""Thin client for the Readwise Reader API (https://readwise.io/reader_api).

Only what the digest needs: list documents (all pages), replace tags in bulk,
check the token. Handles the rate limit (20 calls a minute per endpoint, with
429 + Retry-After as the backstop) and a few retries when the network is
briefly down.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

from common import iso_utc

log = logging.getLogger("rnd")

API_ROOT = "https://readwise.io/api"
USER_AGENT = "readwise-news-digest/1.0"
SAVED_USING = "readwise-news-digest"   # Reader shows this as `source` on our own notes
REQUEST_TIMEOUT = 60       # a stalled connection must not hold the run lock
BULK_UPDATE_LIMIT = 50     # items per bulk_update call
# Waits between retries of a failed network call: about a minute in all, which
# covers Wi-Fi coming back after the Mac wakes.
NETWORK_RETRY_DELAYS = (5, 15, 40)
MAX_RATE_LIMIT_RETRIES = 5
DEFAULT_RETRY_AFTER = 30
MAX_RETRY_AFTER = 120


class ReaderAPIError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class ReaderAuthError(ReaderAPIError):
    """Readwise rejected the token (401/403)."""


class ReaderNetworkError(ReaderAPIError):
    """Could not reach Readwise (DNS, connect, timeout, 5xx after retries)."""


class RateLimiter:
    """Sliding window: at most `max_calls` calls in any `period` seconds.

    Set a little under Reader's real limit (20/min) so a long listing paces
    itself instead of leaning on 429 replies.
    """

    def __init__(self, max_calls=18, period=60.0, clock=time.monotonic,
                 sleep=time.sleep):
        self.max_calls = max_calls
        self.period = period
        self._clock = clock
        self._sleep = sleep
        self._calls = deque()

    def wait(self):
        now = self._clock()
        while self._calls and now - self._calls[0] >= self.period:
            self._calls.popleft()
        if len(self._calls) >= self.max_calls:
            delay = self.period - (now - self._calls[0])
            if delay > 0:
                self._sleep(delay)
            now = self._clock()
            while self._calls and now - self._calls[0] >= self.period:
                self._calls.popleft()
        self._calls.append(now)


def urllib_transport(method, url, headers, body, timeout):
    """Send one request. Returns (status, lower-cased headers, body bytes).

    HTTP error statuses are returned, not raised; only a failure to get any
    reply raises ReaderNetworkError.
    """
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return (resp.status,
                    {k.lower(): v for k, v in resp.headers.items()},
                    resp.read())
    except urllib.error.HTTPError as e:
        return (e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ReaderNetworkError(f"Cannot reach Readwise: {e}") from e


class ReaderClient:
    def __init__(self, token, transport=urllib_transport, sleep=time.sleep,
                 clock=time.monotonic, network_retry_delays=NETWORK_RETRY_DELAYS,
                 on_wait=None):
        self._token = token
        self._transport = transport
        self._sleep = sleep
        self._delays = tuple(network_retry_delays)
        self._on_wait = on_wait or (lambda seconds, why: None)
        # One limiter per endpoint: Reader limits each separately.
        self._list_limiter = RateLimiter(clock=clock, sleep=sleep)
        self._bulk_limiter = RateLimiter(clock=clock, sleep=sleep)
        self._save_limiter = RateLimiter(max_calls=45, clock=clock, sleep=sleep)

    # -- plumbing -----------------------------------------------------------

    def _request(self, method, path, params=None, json_body=None, limiter=None):
        url = f"{API_ROOT}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {
            "Authorization": f"Token {self._token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        body = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        network_attempt = 0
        rate_attempt = 0
        while True:
            if limiter is not None:
                limiter.wait()
            try:
                status, reply_headers, raw = self._transport(
                    method, url, headers, body, REQUEST_TIMEOUT)
            except ReaderNetworkError as e:
                if network_attempt >= len(self._delays):
                    raise
                delay = self._delays[network_attempt]
                network_attempt += 1
                log.warning(f"{e} — retrying in {delay}s")
                self._on_wait(delay, "network")
                self._sleep(delay)
                continue

            if status == 429:
                if rate_attempt >= MAX_RATE_LIMIT_RETRIES:
                    raise ReaderAPIError("Readwise rate limit still exceeded "
                                         "after retries", status)
                rate_attempt += 1
                delay = _retry_after(reply_headers)
                log.info(f"Rate limited; waiting {delay}s")
                self._on_wait(delay, "rate limit")
                self._sleep(delay)
                continue

            if status in (401, 403):
                raise ReaderAuthError(
                    f"Readwise rejected the token (HTTP {status})", status)

            if status >= 500:
                if network_attempt >= len(self._delays):
                    raise ReaderNetworkError(
                        f"Readwise server error (HTTP {status})", status)
                delay = self._delays[network_attempt]
                network_attempt += 1
                log.warning(f"Readwise HTTP {status} — retrying in {delay}s")
                self._on_wait(delay, "server error")
                self._sleep(delay)
                continue

            if status >= 400:
                detail = raw.decode("utf-8", "replace")[:300]
                raise ReaderAPIError(f"Readwise HTTP {status}: {detail}", status)

            data = json.loads(raw) if raw else None
            return status, data

    # -- calls --------------------------------------------------------------

    def check_token(self):
        """True if the token is accepted. Raises on a network failure."""
        try:
            self._request("GET", "/v2/auth/")
        except ReaderAuthError:
            return False
        return True

    def list_documents(self, updated_after=None, location=None, category=None,
                       tag=None, doc_id=None, on_page=None):
        """Return every matching document, following all pages.

        Returns only a complete listing: if any page fails the exception
        propagates, so a caller can never mistake a half listing for a whole
        one. `tag` may be a name or a list of up to 5 names (all must match).
        """
        params = {}
        if updated_after is not None:
            params["updatedAfter"] = (updated_after if isinstance(updated_after, str)
                                      else iso_utc(updated_after))
        if location:
            params["location"] = location
        if category:
            params["category"] = category
        if tag:
            params["tag"] = [tag] if isinstance(tag, str) else list(tag)
        if doc_id:
            params["id"] = doc_id

        results = []
        seen_cursors = set()
        page = 0
        while True:
            _, data = self._request("GET", "/v3/list/", params=params,
                                    limiter=self._list_limiter)
            page += 1
            results.extend(data.get("results") or [])
            if on_page:
                on_page(page, len(results))
            cursor = data.get("nextPageCursor")
            if not cursor or cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            params["pageCursor"] = cursor
        return results

    def get_document(self, doc_id):
        found = self.list_documents(doc_id=doc_id)
        return found[0] if found else None

    def save_document(self, url, title, html, tags=(), location="new",
                      author="Readwise News Digest"):
        """Save a document of our own to Reader. Returns (created, document id).

        Reader de-duplicates by address: a 201 means a new document was created,
        a 200 means one with this address already exists (and is left alone).
        Reader insists on an author and a title when the html is not cleaned.
        """
        body = {"url": url, "title": title, "author": author, "html": html,
                "should_clean_html": False, "location": location,
                "tags": list(tags), "saved_using": SAVED_USING}
        status, data = self._request("POST", "/v3/save/", json_body=body,
                                     limiter=self._save_limiter)
        return status == 201, (data or {}).get("id")

    def set_tags(self, updates):
        """Replace the whole tag list of each document.

        `updates` is a list of (document_id, [tag names]). Returns
        (confirmed ids, {failed id: error text}). A 207 reply is not an error:
        the documents that succeeded are in `confirmed`.
        """
        confirmed = set()
        failed = {}
        for start in range(0, len(updates), BULK_UPDATE_LIMIT):
            chunk = updates[start:start + BULK_UPDATE_LIMIT]
            body = {"updates": [{"id": doc_id, "tags": list(tags)}
                                for doc_id, tags in chunk]}
            status, data = self._request("PATCH", "/v3/bulk_update/",
                                         json_body=body,
                                         limiter=self._bulk_limiter)
            reported = {r.get("id"): r for r in (data or {}).get("results") or []}
            for doc_id, _ in chunk:
                result = reported.get(doc_id)
                if result is None:
                    # No per-item answer: 200 means everything went through,
                    # 207 means we cannot vouch for this one.
                    if status == 200:
                        confirmed.add(doc_id)
                    else:
                        failed[doc_id] = "no result reported"
                elif result.get("success"):
                    confirmed.add(doc_id)
                else:
                    failed[doc_id] = result.get("error") or "unknown error"
        return confirmed, failed


def _retry_after(headers):
    try:
        seconds = int(float(headers.get("retry-after", DEFAULT_RETRY_AFTER)))
    except (TypeError, ValueError):
        seconds = DEFAULT_RETRY_AFTER
    return max(1, min(seconds, MAX_RETRY_AFTER))
