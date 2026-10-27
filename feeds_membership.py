"""Which Reader items belong to the "News Digest feeds" folder.

Reader's API does not say which folder or feed an item came from, only the
article address. The OPML export does list every feed with its folders, so:

  1. read the newest `Reader_Feeds*.opml` from Downloads (copied into the
     project folder, so cleaning Downloads changes nothing);
  2. keep the feeds filed under "News Digest feeds";
  3. resolve each feed to the article hosts its entries link to (a feed on
     feedburner or another host still links to the publisher's own site);
  4. an item is a digest item when its article host is one of those hosts.
"""

import logging
import shutil
import socket
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from common import ConfigError, atomic_write, load_json

log = logging.getLogger("rnd")

DIGEST_FOLDER = "News Digest feeds"
OPML_PATTERN = "Reader_Feeds*.opml"
LINK_BLOG_HOST_LIMIT = 5   # entries linking to more hosts than this: link blog
FEED_TIMEOUT = 10
FEED_FETCH_WORKERS = 8
FALLBACK_PREFIXES = ("feeds.", "rss.")

# Browser-like headers: many sites 403 anything that looks like a bot.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Feed:
    title: str
    url: str
    folders: tuple


def host_of(url):
    """Host of an address, lower-cased, without a leading 'www.'. '' if none."""
    try:
        host = (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def own_host(feed_url):
    """The feed's own site host: its host minus a feeds./rss. prefix."""
    host = host_of(feed_url)
    for prefix in FALLBACK_PREFIXES:
        if host.startswith(prefix):
            return host[len(prefix):]
    return host


def parse_opml(path):
    """All feeds in an OPML export, with the folders each is filed under."""
    text = Path(path).read_text(encoding="utf-8")
    # A real Reader export never declares entities. Refusing any that does
    # rules out entity-expansion tricks without needing a third-party parser.
    if "<!ENTITY" in text:
        raise ConfigError(f"{path} declares XML entities; not a Reader export.")
    root = ET.fromstring(text.encode("utf-8"))
    feeds = []
    for node in root.iter("outline"):
        url = node.get("xmlUrl")
        if not url:
            continue
        folders = tuple(part.strip() for part in
                        (node.get("category") or "").split(",") if part.strip())
        title = node.get("title") or node.get("text") or url
        feeds.append(Feed(title=title, url=url, folders=folders))
    return feeds


def digest_feeds(feeds):
    return [f for f in feeds if DIGEST_FOLDER in f.folders]


def newest_opml(downloads_dir):
    """The most recently modified Reader_Feeds*.opml in a folder, or None."""
    try:
        candidates = list(downloads_dir.glob(OPML_PATTERN))
    except OSError as e:
        log.warning(f"Cannot look for the OPML in {downloads_dir}: {e}")
        return None
    candidates = [p for p in candidates if p.is_file()]
    return max(candidates, key=lambda p: p.stat().st_mtime, default=None)


def locate_opml(paths, downloads_dir):
    """(file to read, file to copy it from or None).

    A newer export in Downloads wins over the project's copy. Raises
    ConfigError when there is no OPML anywhere, so tagging is skipped with a
    notification rather than quietly tagging nothing.
    """
    source = newest_opml(downloads_dir)
    if source is not None and (
            not paths.opml.exists()
            or source.stat().st_mtime > paths.opml.stat().st_mtime):
        return source, source
    if paths.opml.exists():
        return paths.opml, None
    raise ConfigError(
        "No feed list yet. In Reader, export feeds as OPML to Downloads "
        "(Reader_Feeds.opml), then run again.")


def refresh_opml(paths, downloads_dir):
    """Copy a newer OPML from Downloads into the project folder, so cleaning
    Downloads later changes nothing. Returns True when the copy changed."""
    _, source = locate_opml(paths, downloads_dir)
    if source is None:
        return False
    shutil.copy2(source, paths.opml)
    log.info(f"Read feed list from {source}")
    return True


def fetch_feed_links(feed_url):
    """Article links of a feed's current entries. [] when it fails or is empty."""
    import feedparser

    try:
        parsed = feedparser.parse(
            feed_url, agent=USER_AGENT,
            request_headers={"Accept-Language": "en-US,en;q=0.9"})
    except Exception as e:  # feedparser can raise on odd network states
        log.warning(f"Feed {feed_url} could not be read: {e}")
        return []
    return [entry.get("link", "") for entry in parsed.entries]


def hosts_from_links(links, feed_url):
    """(hosts, resolved) for one feed from its entries' article links."""
    counts = Counter(h for h in (host_of(link) for link in links) if h)
    if not counts:
        return [own_host(feed_url)], False   # failed or empty: try again later
    if len(counts) > LINK_BLOG_HOST_LIMIT:
        return [own_host(feed_url)], True    # link blog: only its own site
    return [host for host, _ in counts.most_common()], True


class Membership:
    """The set of hosts whose articles count as digest items."""

    def __init__(self, feeds, cache):
        self.feeds = feeds            # digest Feeds
        self.cache = cache            # {feed url: {"hosts": [...], "resolved": bool}}

    @property
    def digest_hosts(self):
        hosts = set()
        for feed in self.feeds:
            entry = self.cache.get(feed.url)
            hosts.update(entry["hosts"] if entry else [own_host(feed.url)])
        hosts.discard("")
        return hosts

    @property
    def unresolved(self):
        return [f for f in self.feeds
                if not self.cache.get(f.url, {}).get("resolved")]

    def is_digest_item(self, source_url):
        return host_of(source_url) in self.digest_hosts

    def resolve_pending(self, fetch=fetch_feed_links):
        """Look up the hosts of feeds not resolved yet. Returns how many
        resolved this time."""
        pending = self.unresolved
        if not pending:
            return 0
        old_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(FEED_TIMEOUT)
        try:
            with ThreadPoolExecutor(max_workers=FEED_FETCH_WORKERS) as pool:
                links = list(pool.map(lambda f: fetch(f.url), pending))
        finally:
            socket.setdefaulttimeout(old_timeout)
        resolved_now = 0
        for feed, feed_links in zip(pending, links, strict=True):
            hosts, resolved = hosts_from_links(feed_links, feed.url)
            self.cache[feed.url] = {"hosts": hosts, "resolved": resolved}
            resolved_now += resolved
            if not resolved:
                log.warning(f"Feed {feed.title} ({feed.url}) gave no entries; "
                            f"using {hosts[0]} for now")
        return resolved_now


def load_membership(paths):
    """Build the membership from the project's OPML copy and the host cache."""
    feeds = digest_feeds(parse_opml(paths.opml))
    cache = load_json(paths.feed_sites, {})
    return Membership(feeds, cache)


def save_membership(paths, membership):
    atomic_write(paths.feed_sites, membership.cache)
