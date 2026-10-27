#!/usr/bin/env python3
"""Rebuild the interest profile, and make the reading reports.

The launchd job runs this every day at 03:00. The profile is rebuilt when a week
has passed; the monthly report (report.py, which carries the feed suggestions
every other month) is made on the first run of a new month, and the annual
report on the first run of a new year. Each is retried the next day if it failed.

This part is optional. The profile can be written by hand (see "Describe your
interests" in the README). If prefs.json names a "vault_dir" -- an Obsidian vault
that the Readwise plugin fills with your highlights -- this job rebuilds the
profile from it every week: it scans the notes for tags, source domains and
highlight counts, uses qmd (if installed) for semantic enrichment, then
synthesizes topic descriptors with a single Haiku call. Two signals taken from
the grades are added:

* Recent interests: titles of tagged items graded 4-5 in the last 60 days. This
  catches things you tagged or read in full that never reach the highlights
  vault.
* Less of this: titles graded 1 (archived unopened) in the last 60 days.

Output: interest-profile.json
"""

import argparse
import json
import logging
import math
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import report
from common import (
    QMD_COLLECTION,
    Paths,
    atomic_write,
    call_anthropic,
    date_minus_days,
    days_between,
    get_api_key,
    get_readwise_token,
    load_json,
    load_weekly,
    local_today,
    notify,
    run_qmd,
    save_weekly,
    setup_logging,
)
from reader_api import ReaderClient

log = logging.getLogger("rnd")

# --- Configuration ---
MIN_VAULT_FILES = 100
RECENCY_HALF_LIFE_DAYS = 180
TOP_DOMAINS = 30
TOP_TOPICS = 30
QMD_SNIPPETS_PER_TAG = 3
QMD_SNIPPET_MAX_CHARS = 200
QMD_WORKERS = 2
HEADER_MAX_LINES = 40   # Readwise notes always have metadata within the first ~20 lines
PROFILE_EVERY_DAYS = 7  # the profile is rebuilt when this many days have passed
SIGNAL_DAYS = 60        # "recent interests" / "less of this" look back this far
SIGNAL_CAP = 30         # ... and list at most this many titles each

WIKILINK_RE = re.compile(r"\[\[([^|\]]+)(?:\|[^\]]+)?\]\]")
TAG_RE = re.compile(r"#(>>[\w-]+|\w[\w-]*)")
HIGHLIGHT_COUNT_RE = re.compile(r"==(\d+)==")

EXCLUDED_DOMAINS = {
    "archive.ph", "archive.is", "archive.org",
    "readwise.io", "read.readwise.io",
    "web.archive.org",
    "t.co", "twitter.com", "x.com",
}


def strip_wikilinks(s):
    return WIKILINK_RE.sub(r"\1", s)


def recency_weight(date_str, now):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0.1
    days_ago = max(0, (now - dt).days)
    return math.exp(-0.693 * days_ago / RECENCY_HALF_LIFE_DAYS)


def parse_note(filepath):
    """Parse a Readwise vault note header for metadata. Reads only the first
    ~40 lines since Readwise places all metadata before the highlights section."""
    meta = {
        "path": str(filepath),
        "title": "",
        "author": "",
        "category": "",
        "date": "",
        "source_url": "",
        "tags": [],
        "num_highlights": 0,
    }
    updated_date = ""

    try:
        with open(filepath, encoding="utf-8") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= HEADER_MAX_LINES:
                    break
                lines.append(line)
    except (UnicodeDecodeError, OSError):
        return None

    for line in lines:
        line = line.rstrip("\n").strip()

        if line.startswith("Updated:"):
            updated_date = line[8:].strip()
        elif line.startswith("Title:"):
            meta["title"] = strip_wikilinks(line[6:].strip())
        elif line.startswith("Authors:"):
            meta["author"] = strip_wikilinks(line[8:].strip())
        elif line.startswith("Category:"):
            meta["category"] = line[9:].strip().lstrip("#")
        elif line.startswith("Date:"):
            meta["date"] = line[5:].strip().replace("[[", "").replace("]]", "")
        elif line.startswith("Source URL:"):
            meta["source_url"] = line[11:].strip()
        elif line.startswith("Tags:"):
            meta["tags"] = TAG_RE.findall(line[5:].strip())
        elif line.startswith("Number of Highlights:"):
            m = HIGHLIGHT_COUNT_RE.search(line)
            if m:
                meta["num_highlights"] = int(m.group(1))

    if not meta["date"] and updated_date:
        meta["date"] = updated_date

    return meta


def extract_domain(url):
    if not url or not url.startswith("http"):
        return None
    try:
        host = urlparse(url).netloc.replace("www.", "")
        return host or None
    except Exception:
        return None


def configured_vault(paths):
    """The vault folder named by prefs.json ("vault_dir"), or None."""
    named = (load_json(paths.prefs, {}) or {}).get("vault_dir")
    return Path(named).expanduser() if named else None


def topic_prefix(paths):
    """Which vault tags are topics: prefs.json's "vault_topic_prefix" (for example
    ">>"), or "" for every tag."""
    return (load_json(paths.prefs, {}) or {}).get("vault_topic_prefix") or ""


def scan_vault(vault_dir):
    notes = []
    for subdir in ["Readwise/Articles", "Readwise/Books", "Readwise/Tweets"]:
        dirpath = vault_dir / subdir
        if not dirpath.exists():
            log.warning(f"Vault subdirectory not found: {dirpath}")
            continue
        for filepath in dirpath.glob("*.md"):
            meta = parse_note(filepath)
            if meta:
                notes.append(meta)
    return notes


def query_qmd_for_tag(tag_name):
    """Run a QMD semantic search for one tag and return up to N snippets.

    Uses --no-rerank to skip local LLM reranking: we only need vaguely related
    context strings for the topic synthesis, not precise ranking, and loading
    the reranker model per query pegs the CPU.
    """
    output = run_qmd("query", tag_name,
                     "--collection", QMD_COLLECTION,
                     "--limit", str(QMD_SNIPPETS_PER_TAG),
                     "--no-rerank")
    if not output:
        return []
    snippets = []
    for line in output.split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("---"):
            snippets.append(line[:QMD_SNIPPET_MAX_CHARS])
            if len(snippets) >= QMD_SNIPPETS_PER_TAG:
                break
    return snippets


def build_topic_profile(notes, now, qmd_query=query_qmd_for_tag, prefix=""):
    tag_scores = defaultdict(float)
    tag_counts = Counter()

    for note in notes:
        weight = recency_weight(note["date"], now)
        engagement = min(note["num_highlights"] / 10.0, 3.0)
        combined = weight * max(engagement, 0.5)

        for tag in note["tags"]:
            if prefix and not tag.startswith(prefix):
                continue
            clean = tag[len(prefix):].replace("-", " ")
            tag_scores[clean] += combined
            tag_counts[clean] += 1

    ranked = sorted(tag_scores.items(), key=lambda x: x[1], reverse=True)[:TOP_TOPICS]

    # Parallelize QMD queries: each is a subprocess call with its own startup cost
    tag_names = [t for t, _ in ranked]
    with ThreadPoolExecutor(max_workers=QMD_WORKERS) as executor:
        snippets_by_tag = dict(zip(tag_names, executor.map(qmd_query, tag_names),
                                   strict=True))

    return [
        {
            "tag": tag_name,
            "score": round(score, 3),
            "count": tag_counts[tag_name],
            "snippets": snippets_by_tag.get(tag_name, []),
        }
        for tag_name, score in ranked
    ]


def build_domain_profile(notes, now):
    domain_scores = defaultdict(float)
    domain_counts = Counter()

    for note in notes:
        domain = extract_domain(note["source_url"])
        if not domain or domain in EXCLUDED_DOMAINS:
            continue
        domain_scores[domain] += recency_weight(note["date"], now)
        domain_counts[domain] += 1

    ranked = sorted(domain_scores.items(), key=lambda x: x[1], reverse=True)[:TOP_DOMAINS]
    return [
        {"domain": domain, "score": round(score, 3), "count": domain_counts[domain]}
        for domain, score in ranked
    ]


# --- Signals from the grades -----------------------------------------------------


def _recent_with_grade(tracked, today, keep):
    cutoff = date_minus_days(today, SIGNAL_DAYS)
    rows = [e for e in tracked.values()
            if e.get("grade") is not None and keep(e["grade"])
            and (e.get("grade_since") or "") >= cutoff]
    # Most recent first; the order within a day follows the tagging date.
    rows.sort(key=lambda e: (e["grade_since"], e["tagged_date"]), reverse=True)
    return rows[:SIGNAL_CAP]


def recent_interests(tracked, today):
    """'Title (site)' of items graded 4-5 within the last 60 days, newest first."""
    return [f"{e['title']} ({e['host']})"
            for e in _recent_with_grade(tracked, today, lambda g: g >= 4)]


def less_of_this(tracked, today):
    """Titles of items graded 1 (archived unopened) within the last 60 days."""
    return [e["title"] for e in _recent_with_grade(tracked, today, lambda g: g == 1)]


def build_prompt(enriched_topics, recent=(), less=()):
    topic_data = []
    for t in enriched_topics[:TOP_TOPICS]:
        entry = f"- {t['tag']} (score: {t['score']}, count: {t['count']})"
        if t["snippets"]:
            entry += "\n  Snippets: " + " | ".join(t["snippets"][:3])
        topic_data.append(entry)

    extra = ""
    if recent:
        lines = "\n".join(f"- {title}" for title in recent)
        extra += (
            "\n\n## Recent Interests\n"
            "The reader recently engaged strongly with these articles (highlighted, "
            "tagged, or read at length). Give the topics they touch more weight:\n"
            f"{lines}"
        )
    if less:
        lines = "\n".join(f"- {title}" for title in less)
        extra += (
            "\n\n## Less Of This\n"
            "The reader archived these articles without opening them. Avoid "
            "synthesizing topics that mostly resemble them:\n"
            f"{lines}"
        )

    return f"""Given these weighted interests extracted from a reading archive
(higher score = more recent/engaged), synthesize them into coherent topic descriptors.

## Raw Topics
{chr(10).join(topic_data)}{extra}

## Task
Return a JSON array of topic objects. Each object should have:
- "topic": A clear, natural-language description of the interest area (2-8 words)
- "tags": Array of the original tag names that map to this topic
- "score": Normalized score from 0 to 1 (highest-scored topic = 1.0)

Merge closely related tags into single topics where it makes sense.
Keep the list to 15-25 items. Return ONLY valid JSON."""


def synthesize_topics(enriched_topics, api_key, recent=(), less=(),
                      call=call_anthropic):
    prompt = build_prompt(enriched_topics, recent, less)
    text = call(api_key, [{"role": "user", "content": prompt}], max_tokens=4096)
    try:
        topics = json.loads(text)
        if isinstance(topics, dict) and "topics" in topics:
            topics = topics["topics"]
        return topics
    except json.JSONDecodeError as e:
        log.error(f"Failed to parse topic synthesis: {e}")
        log.error(f"Raw text: {text[:500]}")
        max_score = enriched_topics[0]["score"] if enriched_topics else 1
        return [
            {"topic": t["tag"], "tags": [t["tag"]],
             "score": round(t["score"] / max_score, 3)}
            for t in enriched_topics[:25]
        ]


def build_profile(notes, tracked, today, now, api_key, call=call_anthropic,
                  qmd_query=query_qmd_for_tag, prefix=""):
    """The whole profile, from notes already scanned and the tracked items."""
    log.info("Building topic profile...")
    enriched_topics = build_topic_profile(notes, now, qmd_query, prefix)
    log.info(f"Found {len(enriched_topics)} topic tags.")
    domain_profile = build_domain_profile(notes, now)
    recent = recent_interests(tracked, today)
    less = less_of_this(tracked, today)
    log.info(f"Signals from grades: {len(recent)} recent interests, "
             f"{len(less)} 'less of this'.")
    log.info("Synthesizing topics with LLM...")
    topics = synthesize_topics(enriched_topics, api_key, recent, less, call)
    return {
        "generated": today,
        "vault_notes_scanned": len(notes),
        "topics": topics,
        "sources": domain_profile,
    }


def profile_due(weekly, today, force=False):
    last = weekly.get("last_profile_date")
    return force or not last or days_between(last, today) >= PROFILE_EVERY_DAYS


def rebuild_profile(paths, weekly, today, vault_dir, prefix=""):
    """Scan the vault and write a new interest-profile.json. Raises on any
    problem, leaving the old profile in place."""
    if not vault_dir.exists():
        raise RuntimeError(f"Vault directory not found: {vault_dir}")
    now = datetime.now(timezone.utc)
    log.info("Scanning vault...")
    notes = scan_vault(vault_dir)
    if len(notes) < MIN_VAULT_FILES:
        raise RuntimeError(f"Only {len(notes)} notes found (minimum "
                           f"{MIN_VAULT_FILES}). Is the vault synced?")
    log.info(f"Scanned {len(notes)} notes.")
    profile = build_profile(notes, load_json(paths.tracked, {}), today, now,
                            get_api_key(), prefix=prefix)
    if not profile["topics"]:
        raise RuntimeError("The profile came out with no topics; keeping the "
                           "old one.")
    atomic_write(paths.profile, profile)
    log.info(f"Wrote interest profile to {paths.profile}")
    weekly["last_profile_date"] = today
    save_weekly(paths, weekly)
    with open(paths.run_log, "a") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M} profile "
                f"topics={len(profile['topics'])} notes={len(notes)}\n")


def main(argv=None):
    """The daily job: rebuild the profile (only if prefs.json names a vault, and a
    week has passed), then make the monthly and annual reports when due. Each part
    is retried the next day if it fails, and a failure in one never stops the
    others."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--force", action="store_true",
                        help="rebuild the profile even if it is not due")
    args = parser.parse_args(argv)
    paths = Paths()
    setup_logging(paths)
    today = local_today()
    problems = 0
    try:
        weekly = load_weekly(paths)
    except Exception as exc:
        log.error(f"Cannot read weekly.json: {exc}")
        notify("Readwise News Digest", f"Cannot read the weekly notes file: {exc}")
        return 1

    vault = configured_vault(paths)
    if vault is None:
        print("No vault_dir in prefs.json, so the profile is not rebuilt "
              "automatically (edit interest-profile.json by hand).")
    elif profile_due(weekly, today, args.force):
        try:
            rebuild_profile(paths, weekly, today, vault, topic_prefix(paths))
            print("Profile build complete.")
        except Exception as exc:
            problems += 1
            log.error(f"Profile build failed: {exc}", exc_info=True)
            print(f"Problem: profile build failed: {exc}", file=sys.stderr)
            notify("Readwise News Digest", f"The weekly profile build failed: {exc}")
    else:
        print(f"Profile is up to date (built {weekly['last_profile_date']}).")

    now = datetime.now(timezone.utc)
    jobs = (("The monthly report", report.run_monthly, (today, now)),
            ("The annual report", report.run_annual, (today,)))
    for name, job, args in jobs:
        try:
            job(paths, ReaderClient(get_readwise_token()), *args, weekly)
        except Exception as exc:
            problems += 1
            log.error(f"{name} failed: {exc}", exc_info=True)
            print(f"Problem: {name.lower()} failed: {exc}", file=sys.stderr)
            notify("Readwise News Digest", f"{name} failed: {exc}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
