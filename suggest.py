#!/usr/bin/env python3
"""Suggest sites to follow, based only on the articles you have saved.

Every two months it puts a note in Reader, titled "Feed suggestions — <date>",
listing sites you keep saving articles from but do not follow. The note is
tagged `newsdigest` (so it shows up in your newsdigest view) and
`newsdigest-suggestions`, and saved to Later.

The evidence is only your own reading: at least three articles saved from a
site in the last six months that match no feed in your OPML (any folder), ranked
by how many you saved, highlighted and read at length. No AI and no web search.
Every feed address offered has been fetched and parsed first, and a site is
never suggested twice.

The job that calls this runs daily and the work is due every two months, so a
failed attempt is retried the next day; a success is remembered.

    python3 suggest.py --dry-run   # print what would be delivered; write nothing
    python3 suggest.py --force     # deliver now even if not due
"""

import argparse
import logging
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html import escape

import copies
import feed_discovery
import feeds_membership
from common import (
    DOWNLOADS_DIR,
    Paths,
    days_between,
    get_readwise_token,
    load_json,
    load_weekly,
    local_today,
    notify,
    parse_iso,
    save_weekly,
    setup_logging,
)
from reader_api import ReaderClient
from tracking import COPY_TAG, tag_names

log = logging.getLogger("rnd")

SUGGEST_EVERY_DAYS = 55       # about two months; the monthly report that carries them
                              # comes 59-62 days after the last one, so 60 would slip a month
ACTIVITY_DAYS = 180           # look back this far at what you saved
MIN_SAVES = 3                 # a site needs this many saved articles to count
MAX_SUGGESTIONS = 5           # most sites in one note
MAX_FEED_CHECKS = 40          # most sites tried to find five with a working feed
READ_FRACTION = 0.5
HIGHLIGHT_CAP_PER_DOC = 5
FEED_CHECK_WORKERS = 8
# The note is tagged `newsdigest` so it appears in the newsdigest view, plus its own
# tag so it can be told apart. Reader's Inbox is not enabled for everyone, so it is
# saved to Later, which always is.
NOTE_TAGS = ("newsdigest", "newsdigest-suggestions")
NOTE_LOCATION = "later"

# Not publications: social sites, short-link and archive services, Reader itself.
IGNORED_HOSTS = {
    "twitter.com", "x.com", "t.co", "facebook.com", "instagram.com", "threads.net",
    "youtube.com", "youtu.be", "reddit.com", "bsky.app", "linkedin.com",
    "tiktok.com", "mastodon.social", "patreon.com",
    "archive.ph", "archive.is", "archive.fo", "archive.li", "archive.md",
    "archive.vn", "archive.today", "archive.org", "web.archive.org",
    "readwise.io", "read.readwise.io", copies.SUGGESTIONS_HOST,
}


# --- When ----------------------------------------------------------------------


def due(weekly, today, force=False):
    last = weekly.get("last_suggestions_date")
    return force or not last or days_between(last, today) >= SUGGEST_EVERY_DAYS


def already_suggested(weekly):
    """Sites that have been suggested before. A site is never suggested twice."""
    return set(weekly.get("suggested", {}))


# --- What you follow ---------------------------------------------------------------


def followed_hosts(paths, downloads, fetch_links, write):
    """Every site that any feed in your OPML (any folder) links to, plus each
    feed's own site, so nothing you already follow is suggested."""
    if write:
        feeds_membership.refresh_opml(paths, downloads)
        opml = paths.opml
    else:
        opml, _ = feeds_membership.locate_opml(paths, downloads)
    feeds = feeds_membership.parse_opml(opml)
    membership = feeds_membership.Membership(feeds, load_json(paths.feed_sites, {}))
    if membership.unresolved:
        membership.resolve_pending(fetch_links)
    if write:
        feeds_membership.save_membership(paths, membership)
    return set(membership.digest_hosts) | {
        feeds_membership.own_host(f.url) for f in feeds}


# --- Evidence from your own reading ----------------------------------------------------


def gather_activity(client, now):
    """Documents you saved yourself, and your highlights, in the last six months."""
    since = now - timedelta(days=ACTIVITY_DAYS)
    saved = []
    for category in copies.COPY_CATEGORIES:
        saved += client.list_documents(updated_after=since, category=category)
    highlights = client.list_documents(updated_after=since, category="highlight")
    return saved, highlights


def rank_activity(saved, highlights, excluded, since):
    """Sites you save from that you don't follow, strongest first.

    Only articles *saved* since `since` count, however recently one was touched.
    A site needs MIN_SAVES of them. Strength is saves, plus each highlight on
    them (up to 5 per article), plus 1 for each read at least half. Left out:
    sites in `excluded`, social and archive sites, forwarded emails (no web
    address), this script's own notes, and saved copies of tagged items.
    """
    rows = {}
    owner = {}      # saved document id -> site
    for doc in saved:
        created = parse_iso(doc.get("created_at"))
        if doc.get("parent_id") or created is None or created < since:
            continue
        host = feeds_membership.host_of(doc.get("source_url"))
        if (not host or "." not in host or host in IGNORED_HOSTS or host in excluded
                or COPY_TAG in {t.lower() for t in tag_names(doc)}):
            continue
        row = rows.setdefault(host, {"host": host, "saves": 0, "highlights": 0,
                                     "read": 0})
        row["saves"] += 1
        if (doc.get("reading_progress") or 0) >= READ_FRACTION:
            row["read"] += 1
        owner[doc["id"]] = host

    per_parent = defaultdict(int)
    for hl in highlights:
        if hl.get("parent_id") in owner:
            per_parent[hl["parent_id"]] += 1
    for parent, count in per_parent.items():
        rows[owner[parent]]["highlights"] += min(count, HIGHLIGHT_CAP_PER_DOC)

    ranked = []
    for row in rows.values():
        if row["saves"] >= MIN_SAVES:
            row["score"] = row["saves"] + row["highlights"] + row["read"]
            ranked.append(row)
    return sorted(ranked, key=lambda r: (-r["score"], r["host"]))


def activity_reason(row):
    def count(n, noun):
        return f"{n} {noun}{'s' if n != 1 else ''}"

    bits = [f"saved {count(row['saves'], 'article')}"]
    if row["highlights"]:
        bits.append(f"highlighted {count(row['highlights'], 'passage')}")
    if row["read"]:
        bits.append(f"read {row['read']} at length")
    text = ", ".join(bits[:-1]) + " and " + bits[-1] if len(bits) > 1 else bits[0]
    return f"You {text} from this site in the last {ACTIVITY_DAYS // 30} months."


def activity_items(ranked, find_feed, limit=MAX_SUGGESTIONS,
                   max_checks=MAX_FEED_CHECKS):
    """The strongest sites that have a working feed, with that feed's address.

    Goes down the ranking in small batches until `limit` sites have a feed or
    `max_checks` sites have been tried: plenty of strong sites block scripts or
    publish their feed somewhere unusual, and they should not use up the list.
    """
    items = []
    with ThreadPoolExecutor(max_workers=FEED_CHECK_WORKERS) as executor:
        for start in range(0, min(len(ranked), max_checks), FEED_CHECK_WORKERS):
            batch = ranked[start:start + FEED_CHECK_WORKERS]
            feeds = executor.map(lambda row: find_feed(row["host"]), batch)
            for row, feed_url in zip(batch, feeds, strict=True):
                if feed_url and len(items) < limit:
                    items.append({"name": row["host"], "host": row["host"],
                                  "feed_url": feed_url,
                                  "reason": activity_reason(row)})
            if len(items) >= limit:
                break
    return items


# --- The note --------------------------------------------------------------------------


def document_url(today):
    """Reader de-duplicates saved documents by address, so each note gets its own
    made-up address; a repeat on the same day finds the note already there."""
    return f"https://{copies.SUGGESTIONS_HOST}/suggestions/{today}"


INTRO = ("Sites you keep saving articles from but don't follow yet. To follow "
         "one, add its feed address in Reader (Feeds → Add feed). If you want "
         "it in the digest, file it in the “News Digest feeds” folder and "
         "export your feeds as OPML again.")


def items_html(items):
    """The list of suggested sites, as the HTML both the standalone note and the
    monthly report use."""
    parts = ["<ul>"]
    for item in items:
        url = escape(item["feed_url"], quote=True)
        parts.append(
            f"<li><strong>{escape(item['name'])}</strong><br>{escape(item['reason'])}"
            f"<br>Feed: <a href=\"{url}\">{url}</a></li>")
    parts.append("</ul>")
    return "".join(parts)


def describe_items(items):
    lines = []
    for item in items:
        lines += [item["name"], f"  {item['reason']}", f"  Feed: {item['feed_url']}"]
    return "\n".join(lines)


def build_document(today, items):
    title = f"Feed suggestions — {today}"
    return title, f"<div><p>{escape(INTRO)}</p>{items_html(items)}</div>"


def describe(title, items):
    """The note as plain text, for --dry-run."""
    return "\n".join([title, "", describe_items(items)])


# --- The whole thing ----------------------------------------------------------------------


def prepare(paths, client, today, now, weekly,
            discover=feed_discovery.discover_feed_for_domain,
            validate=feed_discovery.validate_feed_url,
            fetch_links=feeds_membership.fetch_feed_links, downloads=DOWNLOADS_DIR,
            dry_run=False, force=False, say=print):
    """Work out the suggestions if they are due: None when they are not, else the
    list of sites (empty when nothing qualifies). Delivering and remembering are
    the caller's job (see `record`); a dry run also leaves the feed-site cache
    alone."""
    if not due(weekly, today, force):
        say(f"Feed suggestions not due (last delivered "
            f"{weekly['last_suggestions_date']}).")
        return None

    def find_feed(host):
        return feed_discovery.find_working_feed(host, discover, validate)

    say("Reading your feed list…")
    followed = followed_hosts(paths, downloads, fetch_links, write=not dry_run)
    excluded = followed | already_suggested(weekly)

    say(f"Looking at what you saved in the last {ACTIVITY_DAYS // 30} months…")
    saved, highlights = gather_activity(client, now)
    ranked = rank_activity(saved, highlights, excluded,
                           now - timedelta(days=ACTIVITY_DAYS))
    items = activity_items(ranked, find_feed)
    say(f"{len(ranked)} sites you saved {MIN_SAVES}+ articles from aren't in your "
        f"feeds; {len(items)} chosen (the strongest with a working feed).")
    return items


def record(weekly, items, today):
    """Remember that the suggestions were dealt with: each site in `items` has now
    been suggested (never again), and the next ones are due in about two months.
    Pass no items when nothing new reached Reader. The caller saves `weekly`."""
    for item in items:
        weekly["suggested"].setdefault(item["host"], []).append(today)
    weekly["last_suggestions_date"] = today


def run(paths, client, today, now, weekly,
        discover=feed_discovery.discover_feed_for_domain,
        validate=feed_discovery.validate_feed_url,
        fetch_links=feeds_membership.fetch_feed_links, downloads=DOWNLOADS_DIR,
        dry_run=False, force=False, say=print):
    """Deliver the suggestions as a note of their own (the monthly report does this
    for you, with the suggestions inside it). Returns a short result dict.

    Nothing is remembered unless the note was delivered (or already was), so a
    failed attempt is simply tried again tomorrow. A dry run writes nothing,
    including the feed-site cache and the Reader library.
    """
    items = prepare(paths, client, today, now, weekly, discover, validate, fetch_links,
                    downloads, dry_run, force, say)
    if items is None:
        return {"status": "not due"}
    if not items:
        say("No new suggestions this time.")
        if not dry_run:
            record(weekly, [], today)
            save_weekly(paths, weekly)
        return {"status": "nothing to suggest"}

    title, html = build_document(today, items)
    if dry_run:
        say("")
        say(describe(title, items))
        return {"status": "dry run", "items": items}

    created, doc_id = client.save_document(
        document_url(today), title, html, tags=NOTE_TAGS, location=NOTE_LOCATION)
    record(weekly, items if created else [], today)
    save_weekly(paths, weekly)
    say(f"Delivered “{title}” to your Reader Later list." if created
        else f"“{title}” was already in your Reader library; left as it is.")
    return {"status": "delivered" if created else "already delivered",
            "id": doc_id, "items": items}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be delivered; write nothing")
    parser.add_argument("--force", action="store_true",
                        help="deliver now even if not due")
    args = parser.parse_args(argv)
    paths = Paths()
    setup_logging(paths)
    try:
        client = ReaderClient(get_readwise_token())
        weekly = load_weekly(paths)
        run(paths, client, local_today(), datetime.now(timezone.utc), weekly,
            dry_run=args.dry_run, force=args.force)
    except Exception as exc:
        log.error(f"Feed suggestions failed: {exc}", exc_info=True)
        print(f"Problem: feed suggestions failed: {exc}", file=sys.stderr)
        if not args.dry_run:
            notify("Readwise News Digest", f"Feed suggestions failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
