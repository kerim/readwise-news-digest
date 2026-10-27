#!/usr/bin/env python3
"""Hourly entry point for Readwise News Digest.

Every run first looks at what you did with the tagged items (opened, read,
highlighted, shortlisted, archived, tagged) and turns it into a 1-5 grade. Then,
once per local day (first run at or after 05:00), it scores the new items from
the "News Digest feeds" folder and tags the best 25, plus 3 random near-misses,
with `newsdigest` in Reader.

    python3 run.py --dry-run   # print the picks, write nothing
    python3 run.py             # the real run
    python3 run.py --grades    # grade now, then list every item's grade and why
"""

import argparse
import fcntl
import logging
import os
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import copies
import feeds_membership
import grading
import learning
import scoring
import selection
import tracking
from common import (
    DOWNLOADS_DIR,
    APIAuthError,
    ConfigError,
    Paths,
    StateError,
    atomic_write,
    call_anthropic,
    get_api_key,
    get_readwise_token,
    iso_utc,
    load_json,
    local_date,
    local_date_days_ago,
    notify,
    parse_iso,
    setup_logging,
)
from reader_api import (
    ReaderAPIError,
    ReaderAuthError,
    ReaderClient,
    ReaderNetworkError,
)
from tracking import TAG_NAME, make_snapshot, tag_names

log = logging.getLogger("rnd")

TAG_AFTER_HOUR = 5               # local time of the day's first tagging run
LOOKBACK_CAP_HOURS = 36          # never look further back than this
FIRST_RUN_LOOKBACK_HOURS = 28    # first run ever
SCORED_MEMORY_DAYS = 14          # items scored but not picked are not rescored
RELIST_MARGIN_SECONDS = 60       # slack for clock differences with Reader
LOCK_POLL_SECONDS = 3.0
NEAR_MISS_RATIO = 0.6            # --grades flags saved articles this close to a tagged one


class ScoringIncomplete(RuntimeError):
    """Some batches could not be scored; the day is retried next hour."""


# --- Progress and the single-run lock ----------------------------------------


class Reporter:
    """Prints progress for a person at a terminal and keeps the lock holder's
    current stage in a small file so a second run can show it."""

    def __init__(self, paths=None, out=print):
        self._paths = paths
        self._out = out
        self._started = datetime.now().strftime("%H:%M")
        self._writing_status = False

    def say(self, message):
        self._out(message)
        log.info(message)

    def stage(self, message):
        self.say(message)
        if self._writing_status:
            atomic_write(self._paths.status,
                         {"started": self._started, "stage": message})

    def enable_status(self):
        self._writing_status = True
        self.stage("starting")


class RunLock:
    """An OS lock that keeps a scheduled and a manual run from overlapping.

    flock is released by the kernel if the holder dies, so it can never get
    stuck. A second run shows what the first is doing, then goes ahead.
    """

    def __init__(self, paths, reporter, sleep=time.sleep):
        self._paths = paths
        self._reporter = reporter
        self._sleep = sleep
        self._fd = None

    def acquire(self):
        self._fd = os.open(self._paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
        announced = None
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                status = load_json_quiet(self._paths.status)
                if announced is None:
                    started = status.get("started", "earlier")
                    self._reporter.say(f"Waiting for the run that started at "
                                       f"{started} to finish…")
                stage = status.get("stage")
                if stage and stage != announced:
                    self._reporter.say(f"  now: {stage}")
                    announced = stage
                elif announced is None:
                    announced = ""
                self._sleep(LOCK_POLL_SECONDS)

    def release(self):
        if self._fd is not None:
            os.close(self._fd)   # closing drops the lock
            self._fd = None


def load_json_quiet(path):
    try:
        return load_json(path, {})
    except StateError:
        return {}


# --- Dependencies ------------------------------------------------------------


@dataclass
class Deps:
    """Everything the run talks to, so tests can replace each piece."""

    client: object = None
    client_factory: object = None   # builds the client inside the error handling
    api_key: object = get_api_key
    score_call: object = call_anthropic
    fetch_links: object = feeds_membership.fetch_feed_links
    notify: object = notify
    downloads: Path = DOWNLOADS_DIR
    rng: random.Random = field(default_factory=random.Random)
    sleep: object = time.sleep
    now: object = lambda: datetime.now(timezone.utc)   # noqa: E731


# --- State -------------------------------------------------------------------


def load_state(paths):
    state = load_json(paths.state, {})
    state.setdefault("scored_ids", {})
    state.setdefault("pending_scores", {})
    state.setdefault("notified", {})
    state.setdefault("copy_keys", {})
    state.setdefault("highlight_parents", {})
    return state


def title_key(host, title):
    """(site, lower-cased title) to recognise the same article at two
    addresses; None when there is no real title to compare."""
    words = " ".join((title or "").lower().split())
    return (host, words) if host and words and words != "untitled" else None


def new_tracked_entry(item, doc, tags, tagged_date, tagged_at):
    return {
        "id": item["id"],
        "title": item["title"],
        "source_url": item["url"],
        "host": item["source"],
        "tagged_date": tagged_date,
        "tagged_at": tagged_at,
        "near_miss": item["near_miss"],
        "rank": item["rank"],
        "primary_topic": item["primary_topic"],
        "form": item["form"],
        "frame": item["frame"],
        "raw_topic_score": item["raw_topic_score"],
        "raw_form_score": item["raw_form_score"],
        "topic_score": item["topic_score"],
        "form_score": item["form_score"],
        "score": item["score"],
        "snapshot": make_snapshot(doc, tags),
        "copy_id": None,
        "copy_snapshot": None,
        "grade": None,
        "grade_reason": None,
        "grade_since": None,
        "frozen": False,
    }


# --- The tag step ------------------------------------------------------------


def prepare_membership(deps, paths, reporter, dry_run):
    """Read the feed list and work out which sites are digest sites."""
    if dry_run:
        # Writes nothing: read the newest export where it is, cache or no cache.
        opml, _ = feeds_membership.locate_opml(paths, deps.downloads)
    else:
        feeds_membership.refresh_opml(paths, deps.downloads)
        opml = paths.opml
    feeds = feeds_membership.digest_feeds(feeds_membership.parse_opml(opml))
    membership = feeds_membership.Membership(
        feeds, load_json(paths.feed_sites, {}))
    pending = membership.unresolved
    if pending:
        reporter.stage(f"Looking up the sites of {len(pending)} feeds…")
        membership.resolve_pending(deps.fetch_links)
    if not dry_run:
        feeds_membership.save_membership(paths, membership)
    still = membership.unresolved
    reporter.say(f"{len(feeds)} digest feeds → {len(membership.digest_hosts)} sites"
                 + (f" ({len(still)} feeds not readable yet, using their own site)"
                    if still else ""))
    return membership


def find_candidates(deps, state, tracked, membership, since, reporter,
                    extra_tiers=None):
    """Feed items from digest sites, created since `since`, not seen before.

    Returns (candidate dicts, documents by id, instant the listing began).
    """
    listing_started = deps.now()
    reporter.stage("Listing new feed items…")
    docs = deps.client.list_documents(
        updated_after=since, category="rss",
        on_page=lambda page, total: reporter.stage(
            f"Listing new feed items… page {page} ({total} so far)"))
    by_id, candidates = {}, []
    # An article can reach Reader twice: through two feeds of one publisher, as a feed item Reader re-created hours later, or at two
    # addresses (a site fixing a slug). Same address, or same site and title,
    # counts as one article -- including one tagged on an earlier day.
    recent = local_date_days_ago(SCORED_MEMORY_DAYS)
    seen_urls = {e.get("source_url") for e in tracked.values()}
    seen_titles = {title_key(e.get("host"), e.get("title"))
                   for e in tracked.values() if e.get("tagged_date", "") >= recent}
    for doc in docs:
        created = parse_iso(doc.get("created_at"))
        if (doc.get("category") != "rss" or doc.get("parent_id")
                or created is None or created < since
                or doc["id"] in tracked or doc["id"] in state["scored_ids"]
                or not membership.is_digest_item(doc.get("source_url"))):
            continue
        item = scoring.candidate_from_document(doc, extra_tiers)
        if item is None:
            continue
        key = title_key(item["source"], item["title"])
        if item["url"] in seen_urls or (key and key in seen_titles):
            continue
        seen_urls.add(item["url"])
        seen_titles.add(key)
        by_id[doc["id"]] = doc
        candidates.append(item)
    reporter.say(f"{len(docs)} feed items changed since {since:%Y-%m-%d %H:%M}; "
                 f"{len(candidates)} new digest items to score")
    return candidates, by_id, listing_started


def score_candidates(deps, paths, state, tracked, candidates, today, reporter,
                     dry_run):
    """Haiku scores for every candidate, reusing scores kept from a failed try
    earlier today. Returns the scored items."""
    pending = state["pending_scores"]
    if pending.get("date") != today:
        pending = state["pending_scores"] = {"date": today, "items": {}}
    kept = pending["items"]
    todo = [c for c in candidates if c["id"] not in kept]
    if kept and len(todo) < len(candidates):
        reporter.say(f"Reusing {len(candidates) - len(todo)} scores kept from "
                     f"an earlier try today")

    if todo:
        profile = scoring.load_profile(paths)
        profile_context = scoring.build_profile_context(profile)
        topic_names = scoring.profile_topic_names(profile)
        api_key = deps.api_key()

        def keep(batch, batch_num, total):
            reporter.stage(f"Scoring batch {batch_num}/{total}")
            if not dry_run:
                for item in batch:
                    kept[item["id"]] = item
                atomic_write(paths.state, state)

        reporter.stage(f"Scoring {len(todo)} items…")
        scored, failed = scoring.score_items(
            todo, profile_context, topic_names,
            scoring.recent_frames(tracked), api_key,
            on_batch=keep, call=deps.score_call, sleep=deps.sleep)
        if failed:
            raise ScoringIncomplete(
                f"{len(failed)} of {len(todo)} items could not be scored")
        for item in scored:
            kept[item["id"]] = item
    return [kept[c["id"]] for c in candidates]


def choose_picks(deps, paths, scored, reporter):
    weights = scoring.load_learned_weights(paths)
    scoring.apply_learned_weights(scored, weights)
    prefs = scoring.load_prefs(paths)
    kept, drops = scoring.apply_prefs_filter(scored, prefs) if prefs else (scored, {})
    if any(drops.values()):
        reporter.say(f"Your mutes removed {sum(drops.values())} items")
    pool = scoring.above_junk_floor(kept)
    reporter.say(f"{len(pool)} of {len(scored)} items are above the junk floor")
    return selection.select_digest(pool, deps.rng)


def print_picks(reporter, picks, stats):
    reporter.say("")
    reporter.say(f"Would tag {len(picks)} items "
                 f"({stats['picks']} picks + {stats['near_misses']} near-misses):")
    for n, item in enumerate(picks, start=1):
        mark = "near-miss" if item["near_miss"] else "pick"
        reporter.say(f"{n:>3}. {item['score']:>4.1f}  {item['source']}  "
                     f"[{mark}, rank {item['rank']}]")
        reporter.say(f"       {item['title']}")
        reporter.say(f"       {item['primary_topic']} · {item['form']}"
                     + (f" · {item['frame']}" if item['frame'] else ""))
    sites = Counter(item["source"] for item in picks)
    reporter.say("")
    reporter.say("Sites (all from the News Digest folder): "
                 + ", ".join(f"{site} ×{n}" for site, n in sites.most_common()))


def write_tags(deps, picks, docs_by_id, listing_started, reporter):
    """Add `newsdigest` to each pick, keeping every tag it already has.

    Scoring takes minutes, so the tags read at the start are stale by now.
    Re-listing what changed since then gives the current tags: a tag you added
    meanwhile is kept, and the window for a clash shrinks to about a second.
    """
    reporter.stage("Checking for changes while scoring…")
    since = listing_started - timedelta(seconds=RELIST_MARGIN_SECONDS)
    fresh = {d["id"]: d for d in deps.client.list_documents(
        updated_after=since, category="rss")}
    updates, tags_after, docs = [], {}, {}
    for item in picks:
        doc = fresh.get(item["id"]) or docs_by_id[item["id"]]
        tags = tag_names(doc)
        if TAG_NAME not in tags:
            tags.append(TAG_NAME)
        updates.append((item["id"], tags))
        tags_after[item["id"]] = tags
        docs[item["id"]] = doc
    reporter.stage(f"Tagging {len(updates)} items in Reader…")
    confirmed, failed = deps.client.set_tags(updates)
    for doc_id, error in failed.items():
        log.warning(f"Could not tag {doc_id}: {error}")
    return confirmed, tags_after, docs


def run_tag_step(deps, paths, state, tracked, reporter, dry_run):
    """Returns {"tagged": n, "scored": n}."""
    now = deps.now()
    today = local_date(now)
    result = {"tagged": 0, "scored": 0}

    if not dry_run:
        if state.get("last_tag_date") == today:
            at = parse_iso(state.get("last_tag_at"))
            when = at.astimezone().strftime("%H:%M") if at else "earlier"
            reporter.say(f"Already tagged today at {when}, "
                         f"{state.get('last_tag_count', 0)} items.")
            return result
        if now.astimezone().hour < TAG_AFTER_HOUR:
            reporter.say(f"Too early to tag; tagging starts at "
                         f"{TAG_AFTER_HOUR:02d}:00.")
            return result

    membership = prepare_membership(deps, paths, reporter, dry_run)

    last = parse_iso(state.get("last_tag_at"))
    cap = now - timedelta(hours=LOOKBACK_CAP_HOURS)
    since = max(last, cap) if last else now - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS)

    candidates, docs_by_id, listing_started = find_candidates(
        deps, state, tracked, membership, since, reporter,
        scoring.load_prefs(paths).get("source_tiers"))
    if not candidates:
        reporter.say("Nothing new to tag yet.")
        return result

    scored = score_candidates(deps, paths, state, tracked, candidates, today,
                              reporter, dry_run)
    result["scored"] = len(scored)
    picks, stats = choose_picks(deps, paths, scored, reporter)
    if not picks:
        reporter.say("No item cleared the junk floor; nothing to tag yet.")
        return result

    if dry_run:
        print_picks(reporter, picks, stats)
        return result

    confirmed, tags_after, docs = write_tags(
        deps, picks, docs_by_id, listing_started, reporter)
    if not confirmed:
        raise ReaderAPIError("Readwise confirmed none of the tag updates")

    # Record only what Reader confirmed. Tracked first: if the script dies
    # between the two writes, the items are tracked and never tagged twice.
    tagged_at = iso_utc(now)
    for item in picks:
        if item["id"] in confirmed:
            tracked[item["id"]] = new_tracked_entry(
                item, docs[item["id"]], tags_after[item["id"]], today, tagged_at)
    atomic_write(paths.tracked, tracked)

    for candidate in candidates:
        state["scored_ids"][candidate["id"]] = today
    cutoff = local_date_days_ago(SCORED_MEMORY_DAYS)
    state["scored_ids"] = {k: v for k, v in state["scored_ids"].items() if v >= cutoff}
    state["pending_scores"] = {}
    state["last_tag_date"] = today
    state["last_tag_at"] = iso_utc(listing_started)
    state["last_tag_count"] = len(confirmed)
    atomic_write(paths.state, state)

    result["tagged"] = len(confirmed)
    reporter.say(f"Tagged {len(confirmed)} items with “{TAG_NAME}”.")

    # Best effort, now that the day is safely recorded: link full-length copies
    # you saved *before* these were tagged. A failure is retried next hour.
    try:
        tracking.apply_highlights(tracked, state)
        linked = tracking.link_copies(tracked, state, today)
        if linked:
            reporter.say(f"Linked {len(linked)} full-length copies you had "
                         f"already saved.")
        atomic_write(paths.tracked, tracked)
        tracking.tag_copies(deps.client, tracked)
        atomic_write(paths.tracked, tracked)
    except ReaderAPIError as exc:
        log.warning(f"Could not finish linking saved copies: {exc}")
    return result


# --- The grade step ------------------------------------------------------------


def grade_summary(tracked, today):
    return {"graded": sum(1 for e in tracked.values() if e.get("grade") is not None),
            "learned": learning.decayed_total(tracked, today)}


def run_grade_step(deps, paths, state, tracked, reporter):
    """Look at what you did with the tagged items and regrade them.

    Returns {"graded": n, "learned": decayed counted items}.
    """
    now = deps.now()
    today = local_date(now)
    if not tracked:
        reporter.say("Nothing tagged yet, so nothing to grade.")
        return grade_summary(tracked, today)

    cursor = parse_iso(state.get("cursor"))
    if cursor is None:
        # First time: start where tagging started, so a highlight made on the
        # very first picks is not lost.
        cursor = min(parse_iso(e["tagged_at"]) for e in tracked.values())
    since = cursor - timedelta(seconds=RELIST_MARGIN_SECONDS)
    listing_started = now

    def page(number, total):
        reporter.stage(f"Checking what you did with tagged items… "
                       f"page {number} ({total} changed)")

    reporter.stage("Checking what you did with tagged items…")
    docs = deps.client.list_documents(updated_after=since, on_page=page)

    tracking.record_highlights(state, docs, today)
    tracking.record_copies(state, docs, today)
    tracking.apply_snapshots(tracked, docs)
    refresh_due = state.get("last_refresh_date") != today
    if refresh_due:
        reporter.stage("Refreshing every tagged item (once a day)…")
        tracking.daily_refresh(deps.client, tracked, state, today, now,
                               on_page=page)
    # Two passes: link saved copies first, then apply highlights, so a copy
    # saved together with its highlights counts.
    linked = tracking.link_copies(tracked, state, today)
    tracking.apply_highlights(tracked, state)
    changed = grading.regrade(tracked, today, daily=refresh_due)
    learning.update_learned_weights(paths, tracked, today)
    tracking.prune(state, today)

    # Snapshots first, then the cursor: if the script dies in between, the
    # next run reads the same listing again, which is harmless.
    atomic_write(paths.tracked, tracked)
    state["cursor"] = iso_utc(listing_started)
    atomic_write(paths.state, state)

    reporter.say(f"{len(docs)} documents changed; {len(linked)} saved copies "
                 f"linked; {len(changed)} grades changed.")
    for entry in changed:
        shown = "no grade" if entry["grade"] is None else f"{entry['grade']:g}"
        reporter.say(f"  {shown:>8}  {entry['title'][:70]} — {entry['grade_reason']}")

    try:
        if tracking.tag_copies(deps.client, tracked):
            atomic_write(paths.tracked, tracked)
    except ReaderAPIError as exc:
        log.warning(f"Could not tag saved copies yet: {exc}")
    return grade_summary(tracked, today)


def print_grades(paths, state, tracked, reporter, today):
    """`--grades`: every item's grade and why, the learning gate, the biggest
    nudges, and saved documents from digest sites that matched nothing."""
    graded = sorted((e for e in tracked.values() if e.get("grade") is not None),
                    key=lambda e: (-e["grade"], e["tagged_date"]))
    waiting = [e for e in tracked.values() if e.get("grade") is None]
    reporter.say("")
    reporter.say(f"{len(tracked)} tagged items; {len(graded)} graded, "
                 f"{len(waiting)} not graded yet.")
    for entry in graded + waiting:
        shown = "—" if entry["grade"] is None else f"{entry['grade']:g}"
        flags = (" [+copy]" if entry.get("copy_id") else "") + (
            " [frozen]" if entry.get("frozen") else "") + (
            " [near-miss]" if entry.get("near_miss") else "")
        reporter.say(f"{shown:>4}  {entry['title'][:62]} ({entry['host']}){flags}")
        reporter.say(f"      {entry['grade_reason'] or 'not looked at yet'}")

    weights = learning.compute_learned_weights(tracked, today)
    reporter.say("")
    reporter.say(f"Learning: {weights['counted']} graded items are 30-120 days old "
                 f"(decayed total {weights['decayed_total']:.1f} of "
                 f"{learning.GATE:.0f} needed before any nudge).")
    for dim, value, delta in learning.biggest_nudges(weights):
        reporter.say(f"  nudge {delta:+.2f}  {dim[:-1]}: {value}")

    near_misses, others = unmatched_copies(paths, state, tracked)
    if near_misses:
        reporter.say("")
        reporter.say("Saved articles that look like a tagged item but were not "
                     "linked (a gap in the matching?):")
        for record, title, ratio in near_misses:
            reporter.say(f"  {record['created']}  {record['host']}  "
                         f"“{record['title'][:55]}” ~ “{title[:55]}” ({ratio:.2f})")
    if others:
        reporter.say("")
        reporter.say(f"{others} other saved articles from digest sites match no "
                     f"tagged item (normal: you also save things that were "
                     f"never picked).")


def unmatched_copies(paths, state, tracked):
    """Saved documents from digest sites that no tagged item claimed, so a
    matching pattern we missed is visible instead of silent.

    Returns (near misses, how many others). A near miss is a saved document
    whose headline is close (>= NEAR_MISS_RATIO) to a not-yet-linked tagged item
    on the same site, which is what a missed pattern would look like. Most
    saves are simply of articles that were never picked, so those are counted,
    not listed.
    """
    if not paths.opml.exists():
        return [], 0
    hosts = feeds_membership.load_membership(paths).digest_hosts
    claimed = {e["copy_id"] for e in tracked.values() if e.get("copy_id")}
    open_items = {e["id"]: copies.make_key(e["source_url"], e["title"])
                  for e in tracked.values()
                  if not e.get("copy_id") and not e.get("frozen")}
    near, others = [], 0
    for cid, record in sorted(state["copy_keys"].items(),
                              key=lambda kv: kv[1]["created"]):
        if cid in claimed or record["host"] not in hosts:
            continue
        item_id, ratio = copies.best_headline(record, open_items)
        if item_id and ratio >= NEAR_MISS_RATIO:
            near.append((record, tracked[item_id]["title"], ratio))
        else:
            others += 1
    return near, others


# --- Problems ----------------------------------------------------------------


def classify(exc):
    """(problem key, what to tell the user) for an exception."""
    if isinstance(exc, ReaderAuthError):
        return ("readwise-auth", "Readwise rejected the access token. Store a "
                "new one in the Keychain (see the README).")
    if isinstance(exc, ReaderNetworkError):
        return ("readwise-network", "Could not reach Readwise. The next hourly "
                "run will try again.")
    if isinstance(exc, APIAuthError):
        return ("anthropic-auth", "Anthropic rejected the API key used for "
                "scoring.")
    if isinstance(exc, ScoringIncomplete):
        return ("anthropic-scoring", f"Scoring was incomplete ({exc}). The next "
                "hourly run will retry without paying for what already scored.")
    if isinstance(exc, ReaderAPIError):
        return ("readwise-api", f"Readwise refused the request: {exc}")
    if isinstance(exc, ConfigError):
        return ("config", str(exc))
    if isinstance(exc, StateError):
        return ("state", str(exc))
    return (f"crash-{type(exc).__name__}", f"Unexpected error: {exc}")


def report_problem(deps, state, exc, reporter, notify_user=True):
    """Print the problem; notify at most once per distinct problem per day."""
    key, message = classify(exc)
    log.error(f"{key}: {message}", exc_info=not isinstance(
        exc, (ReaderAPIError, APIAuthError, ConfigError, ScoringIncomplete,
              StateError)))
    reporter.say(f"Problem: {message}")
    today = local_date(deps.now())
    if notify_user and state["notified"].get(key) != today:
        deps.notify("Readwise News Digest", message)
        state["notified"][key] = today
        return True
    return False


# --- Entry point -------------------------------------------------------------


def append_run_log(paths, now, result, problems=()):
    local = now.astimezone()
    line = (f"{local:%Y-%m-%d %H:%M} tagged={result['tagged']} "
            f"scored={result['scored']} graded={result['graded']} "
            f"learned={result['learned']:.1f}/{learning.GATE:.0f}")
    if problems:
        line += " problem=" + ",".join(problems)
    with open(paths.run_log, "a") as f:
        f.write(line + "\n")


def run_once(deps, paths, reporter, dry_run, grades_only=False):
    """One run: grade, then (once a day) tag. Returns the exit code.

    A failure in one step does not stop the other: the daily tagging still
    happens if grading breaks, and vice versa.
    """
    result = {"tagged": 0, "scored": 0, "graded": 0, "learned": 0.0}
    state = {"scored_ids": {}, "pending_scores": {}, "notified": {},
             "copy_keys": {}, "highlight_parents": {}}
    problems = []

    def fail(exc):
        problems.append(classify(exc)[0])
        report_problem(deps, state, exc, reporter, notify_user=not dry_run)

    try:
        state = load_state(paths)
        if deps.client is None:
            deps.client = deps.client_factory()
        tracked = load_json(paths.tracked, {})
    except Exception as exc:
        fail(exc)
        if not dry_run:   # never overwrite a state file we could not read
            append_run_log(paths, deps.now(), result, problems)
        return 1

    if not dry_run:
        try:
            result.update(run_grade_step(deps, paths, state, tracked, reporter))
        except Exception as exc:
            fail(exc)
    if not grades_only:
        try:
            result.update(run_tag_step(deps, paths, state, tracked, reporter,
                                       dry_run))
            result.update(grade_summary(tracked, local_date(deps.now())))
        except Exception as exc:
            fail(exc)
    if grades_only and not problems:
        print_grades(paths, state, tracked, reporter, local_date(deps.now()))

    if not dry_run:
        if problems:
            atomic_write(paths.state, state)   # remember what we notified about
        append_run_log(paths, deps.now(), result, problems)
    return 1 if problems else 0


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="score and print the picks; write no state, no tags")
    parser.add_argument("--grades", action="store_true",
                        help="grade now, then list every item's grade and why")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    paths = Paths()
    setup_logging(paths)
    reporter = Reporter(paths)
    lock = None
    if not args.dry_run:
        # A dry run writes nothing, so it never needs to wait for a real run.
        lock = RunLock(paths, reporter)
        lock.acquire()
        reporter.enable_status()
    def make_client():
        return ReaderClient(
            get_readwise_token(),
            on_wait=lambda seconds, why: reporter.stage(
                f"Waiting {seconds}s ({why})…"))

    try:
        return run_once(Deps(client_factory=make_client), paths, reporter,
                        args.dry_run, grades_only=args.grades)
    finally:
        if lock:
            lock.release()


if __name__ == "__main__":
    sys.exit(main())
