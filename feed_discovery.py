"""Find and check a site's RSS/Atom feed.

Discovery looks for <link rel="alternate"> tags and a few common paths; validation
fetches the address and parses it, so only feeds that really work are offered.
"""

import logging
import ssl
import urllib.request
from html.parser import HTMLParser

log = logging.getLogger("rnd")

# A browser-like User-Agent: many sites 403 anything that looks like a bot. It
# does NOT defeat TLS-handshake fingerprinting (some Cloudflare sites), so those
# feeds stay unreachable from a script and can't be suggested.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 10
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "application/rss+xml,application/atom+xml,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}
FEED_CONTENT_TYPES = ("xml", "rss", "atom", "feed")


class LinkParser(HTMLParser):
    """Find <link rel="alternate"> feed addresses in a page."""

    def __init__(self):
        super().__init__()
        self.feeds = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        attrs_dict = dict(attrs)
        if attrs_dict.get("rel") == "alternate" and attrs_dict.get("type") in (
            "application/rss+xml", "application/atom+xml", "application/feed+json"
        ) and attrs_dict.get("href"):
            self.feeds.append(attrs_dict["href"])


def absolute(href, domain):
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return f"https://{domain}{href}"
    return href


def discover_feed_for_domain(domain):
    """Candidate feed addresses for a site: the usual paths, then the links in its
    home page. Returns a list (possibly empty); nothing here is checked to work."""
    candidates = [f"https://{domain}/{path}" for path in (
        "feed", "feed/", "rss", "rss.xml", "atom.xml", "feeds/posts/default",
        "index.xml")]
    ctx = ssl.create_default_context()

    for url in candidates:
        try:
            req = urllib.request.Request(url, headers=REQUEST_HEADERS)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if any(t in content_type for t in FEED_CONTENT_TYPES):
                    return [url]
        except Exception:
            continue

    try:
        req = urllib.request.Request(f"https://{domain}/", headers=REQUEST_HEADERS)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
            html = resp.read().decode("utf-8", errors="replace")[:50000]
        parser = LinkParser()
        parser.feed(html)
        return [absolute(href, domain) for href in parser.feeds]
    except Exception:
        return []


def validate_feed_url(url):
    """True if the address fetches and parses as a usable RSS/Atom feed. Never
    raises. Passes with at least one entry, or a well-formed feed with a title."""
    import feedparser
    try:
        parsed = feedparser.parse(
            url, agent=USER_AGENT,
            request_headers={"Accept-Language": "en-US,en;q=0.9"})
    except Exception:
        return False
    if getattr(parsed, "entries", None):
        return True
    feed = getattr(parsed, "feed", {}) or {}
    return not getattr(parsed, "bozo", False) and bool(feed.get("title"))


def find_working_feed(domain, discover=discover_feed_for_domain,
                      validate=validate_feed_url):
    """The first discovered feed address that really works, or None. A feed
    address is only ever offered after it has been fetched and parsed."""
    for url in discover(domain):
        if validate(url):
            return url
    return None
