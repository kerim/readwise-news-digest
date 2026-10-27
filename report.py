#!/usr/bin/env python3
"""Reading reports: one every month and, each January, one for the whole year.

The monthly note is called "News Digest Monthly Report — <Month Year>". It says
what you read in Reader that month: the sources you used most, a split by topic
and by part of the world, how the digest's picks did, and when you read. When the
feed suggestions are due (about every two months) they ride along in the same
note. In January a second note, "News Digest Annual Report — <Year>", adds up the
year from the monthly numbers kept in reports.json.

What counts as reading is everything in your Reader library, not only the digest:
an article you opened, saved yourself or highlighted in that month. Topic and
region come from one Haiku call per batch of headlines, remembered in
classifications.json so a retry never pays twice.

    python3 report.py --dry-run                 # print the report that is due; write nothing
    python3 report.py --month 2026-10 --html /tmp/oct.html   # draw one month to a file
    python3 report.py --year 2026 --dry-run     # the annual report, from the kept months

The daily job calls run_monthly and run_annual; a failed attempt is retried the
next day.
"""

import argparse
import calendar
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from html import escape

import copies
import feeds_membership
import suggest
from common import (
    APIAuthError,
    APINetworkError,
    Paths,
    atomic_write,
    call_anthropic,
    get_api_key,
    get_readwise_token,
    load_json,
    load_weekly,
    local_date,
    local_today,
    notify,
    parse_iso,
    save_weekly,
    setup_logging,
)
from grading import LONG_WORDS, READ_FRACTION
from reader_api import SAVED_USING, ReaderClient

log = logging.getLogger("rnd")

REPORT_TAGS = ("newsdigest", "newsdigest-report")
REPORT_LOCATION = "later"      # not every account has a Reader Inbox (see suggest.py)
READING_CATEGORIES = ("rss", "article", "email", "pdf", "epub")
SAVED_CATEGORIES = ("article", "pdf", "epub")    # an email arrives; it is not a save
WORDS_PER_MINUTE = 238
TOP_SOURCES = 10               # rows in the monthly source table
ANNUAL_TOP_SOURCES = 15
KEPT_SOURCES = 80              # sources kept per month for the annual sums
TOP_HIGHLIGHTED = 5
CLASSIFY_BATCH = 50
MIN_SPLIT = 10                 # a failed batch is halved down to this size
BAR_WIDTH = 20

# Fixed on purpose: the profile's own topics are renamed from time to time, which
# would split a year of counts. These are the defaults; replace them with
# "report_topics" and "report_regions" in prefs.json (see the README).
TOPICS = [
    "Politics and government",
    "International relations and diplomacy",
    "War, security and conflict",
    "Economy, business and labor",
    "Technology and the internet",
    "Media and journalism",
    "Science and health",
    "Climate and environment",
    "Law, rights and justice",
    "Society, identity and migration",
    "Culture, books and ideas",
    "History",
    "Other",
]
GLOBAL = "Global or no single place"
REGIONS = [
    "United States", "Canada", "Europe", "Russia and Central Asia",
    "Middle East and North Africa", "Sub-Saharan Africa", "South Asia", "East Asia",
    "Southeast Asia", "Latin America and the Caribbean", "Oceania", GLOBAL,
]


def classify_system(topics, regions):
    return (
        "You label news and essay headlines for a personal reading report. For each "
        "item choose exactly one TOPIC and exactly one REGION from the lists below, "
        "spelled exactly as written.\n\n"
        "TOPICS: " + "; ".join(topics) + "\n\n"
        "REGIONS: " + "; ".join(regions) + "\n\n"
        "The region is the place the piece is mainly ABOUT, not where the publication "
        "is based. If it is not mainly about one of the listed places (an idea, an "
        "industry, the whole world), use '" + GLOBAL + "'. Use 'Other' for a topic "
        "only when nothing else fits.\n\n"
        'Answer with only a JSON array: [{"index": 0, "topic": "...", "region": "..."}]'
    )


def label_lists(prefs):
    """The topic and region lists to use: prefs.json's, else the defaults. The
    catch-all 'Other' and the global bucket are always there."""
    topics = list(prefs.get("report_topics") or TOPICS)
    regions = list(prefs.get("report_regions") or REGIONS)
    return (topics + ["Other"] * ("Other" not in topics),
            regions + [GLOBAL] * (GLOBAL not in regions))


class ReportError(RuntimeError):
    """Something went wrong that retrying tomorrow may fix."""


# --- Months ----------------------------------------------------------------------


def previous_month(day):
    """'YYYY-MM' of the month before the one `day` ('YYYY-MM-DD') is in."""
    year, month = int(day[:4]), int(day[5:7])
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def month_days(month):
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]


def month_bounds(month):
    """Local midnight at the start of the month and of the next one (aware)."""
    year, number = int(month[:4]), int(month[5:7])
    start = datetime(year, number, 1).astimezone()
    following = datetime(year + 1, 1, 1) if number == 12 else datetime(year, number + 1, 1)
    return start, following.astimezone()


def month_name(month):
    return f"{calendar.month_name[int(month[5:7])]} {month[:4]}"


# --- What you read -----------------------------------------------------------------


def is_own(doc):
    """Notes this script saved to Reader are not reading."""
    return (doc.get("source") == SAVED_USING
            or feeds_membership.host_of(doc.get("source_url")) == copies.SUGGESTIONS_HOST)


def readings(docs, start, end):
    """One record per article you touched in [start, end).

    Touched means opened, saved by you, or highlighted in the period. The same
    article that reached Reader twice (a feed item and its saved full-length copy)
    counts once, with the strongest of the two.
    """
    highlights = defaultdict(int)
    for doc in docs:
        if doc.get("category") == "highlight" and doc.get("parent_id"):
            made = parse_iso(doc.get("created_at"))
            if made is not None and start <= made < end:
                highlights[doc["parent_id"]] += 1

    merged = {}
    for doc in docs:
        if (doc.get("parent_id") or doc.get("category") not in READING_CATEGORIES
                or is_own(doc)):
            continue
        host = feeds_membership.host_of(doc.get("source_url"))
        if not host:
            continue
        opened_at = parse_iso(doc.get("first_opened_at"))
        opened = opened_at is not None and start <= opened_at < end
        created = parse_iso(doc.get("created_at"))
        saved = (doc.get("category") in SAVED_CATEGORIES
                 and created is not None and start <= created < end)
        marks = highlights.get(doc["id"], 0)
        if not (opened or saved or marks):
            continue
        progress = min(float(doc.get("reading_progress") or 0), 1.0)
        record = {
            "id": doc["id"], "host": host, "label": doc.get("site_name") or "",
            "title": doc.get("title") or "", "summary": (doc.get("summary") or "")[:200],
            "words": int(doc.get("word_count") or 0), "progress": progress,
            "opened": opened, "opened_at": doc.get("first_opened_at") if opened else None,
            "saved": saved, "highlights": marks,
        }
        key = (host, copies.NON_WORD_RE.sub("", record["title"].lower())) \
            if record["title"] else (host, doc["id"])
        have = merged.get(key)
        if have is None:
            merged[key] = record
            continue
        have["opened"] = have["opened"] or opened
        if opened and (not have["opened_at"] or record["opened_at"] < have["opened_at"]):
            have["opened_at"] = record["opened_at"]
        have["saved"] = have["saved"] or saved
        have["highlights"] += marks
        have["progress"] = max(have["progress"], progress)
        have["words"] = max(have["words"], record["words"])
        have["label"] = have["label"] or record["label"]
    return list(merged.values())


# --- Topic and region ---------------------------------------------------------------


def _items_prompt(batch):
    rows = [f'<item index="{n}"><title>{escape(item["title"])}</title>'
            f'<site>{escape(item["label"] or item["host"])}</site>'
            f'<summary>{escape(item["summary"])}</summary></item>'
            for n, item in enumerate(batch)]
    return "Label these items.\n\n" + "\n".join(rows)


def label_batch(batch, api_key, call, topics=TOPICS, regions=REGIONS):
    """Haiku's topic and region for each item of the batch, in order. A reply that
    cannot be read is retried on halves; a batch that still fails raises."""
    try:
        text = call(api_key, [{"role": "user", "content": _items_prompt(batch)}],
                    system=classify_system(topics, regions), max_tokens=4096)
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("items", data.get("labels", []))
        by_index = {d["index"]: d for d in data if isinstance(d, dict) and "index" in d}
    except (APIAuthError, APINetworkError):
        raise
    except (json.JSONDecodeError, RuntimeError, KeyError, TypeError) as exc:
        if len(batch) <= MIN_SPLIT:
            raise ReportError(f"Could not label {len(batch)} headlines: {exc}") from exc
        log.warning(f"Labelling {len(batch)} headlines failed ({exc}); trying halves")
        middle = len(batch) // 2
        return (label_batch(batch[:middle], api_key, call, topics, regions)
                + label_batch(batch[middle:], api_key, call, topics, regions))
    labels = []
    for n in range(len(batch)):
        entry = by_index.get(n) or {}
        topic = entry.get("topic")
        region = entry.get("region")
        labels.append({"topic": topic if topic in topics else "Other",
                       "region": region if region in regions else GLOBAL})
    return labels


def classify(items, cache, api_key, call, save_cache, topics=TOPICS, regions=REGIONS):
    """Make sure every item has a topic and region in `cache` (document id ->
    labels). The cache is saved after each batch, so a failure part-way costs
    only the batches not yet done."""
    todo = [item for item in items if item["id"] not in cache]
    for start in range(0, len(todo), CLASSIFY_BATCH):
        batch = todo[start:start + CLASSIFY_BATCH]
        for item, labels in zip(batch, label_batch(batch, api_key, call, topics, regions),
                                strict=True):
            cache[item["id"]] = labels
        save_cache()
    return cache


# --- Numbers -------------------------------------------------------------------------


def summarize(items, labels, days):
    """The month's numbers. Everything in here is plain JSON so it can be kept in
    reports.json and added up for the annual report."""
    weekdays = [0] * 7
    open_days = set()
    per_host = defaultdict(lambda: {"items": 0, "read": 0, "highlights": 0, "saved": 0,
                                    "labels": Counter()})
    topics = Counter()
    regions = Counter()
    minutes = 0.0
    for item in items:
        row = per_host[item["host"]]
        row["items"] += 1
        row["read"] += item["progress"] >= READ_FRACTION
        row["highlights"] += item["highlights"]
        row["saved"] += item["saved"]
        if item["label"]:
            row["labels"][item["label"]] += 1
        found = labels.get(item["id"])
        if found:
            topics[found["topic"]] += 1
            regions[found["region"]] += 1
        minutes += item["words"] * item["progress"] / WORDS_PER_MINUTE
        if item["opened_at"]:
            moment = parse_iso(item["opened_at"])
            open_days.add(local_date(moment))
            weekdays[moment.astimezone().weekday()] += 1

    ranked = sorted(per_host.items(),
                    key=lambda kv: (-kv[1]["items"], -kv[1]["read"], kv[0]))
    sources = {host: {"label": row["labels"].most_common(1)[0][0] if row["labels"] else host,
                      "items": row["items"], "read": row["read"],
                      "highlights": row["highlights"], "saved": row["saved"]}
               for host, row in ranked[:KEPT_SOURCES]}
    read = [item for item in items if item["progress"] >= READ_FRACTION]
    top = sorted((item for item in items if item["highlights"]),
                 key=lambda item: (-item["highlights"], item["title"]))[:TOP_HIGHLIGHTED]
    return {
        "days": days, "items": len(items), "sources_total": len(per_host),
        "opened": sum(1 for item in items if item["opened"]), "read": len(read),
        "long_reads": sum(1 for item in read if item["words"] >= LONG_WORDS),
        "saved": sum(1 for item in items if item["saved"]),
        "highlights": sum(item["highlights"] for item in items),
        "highlighted_items": sum(1 for item in items if item["highlights"]),
        "minutes": round(minutes), "active_days": len(open_days), "weekdays": weekdays,
        "sources": sources, "topics": dict(topics), "regions": dict(regions),
        "top_highlighted": [{"title": item["title"], "label": item["label"] or item["host"],
                             "highlights": item["highlights"]} for item in top],
    }


def digest_stats(tracked, first_day, last_day):
    """How the digest's own picks did in the period, from what is already tracked."""
    picks = [t for t in tracked.values() if first_day <= (t.get("tagged_date") or "") <= last_day]

    def snapshots(pick):
        return [s for s in (pick.get("snapshot"), pick.get("copy_snapshot")) if s]

    grades = [p["grade"] for p in picks if p.get("grade") is not None]
    near = [p for p in picks if p.get("near_miss")]
    return {
        "picks": len(picks), "near_misses": len(near),
        "opened": sum(1 for p in picks if any(s.get("first_opened_at") for s in snapshots(p))),
        "near_misses_opened": sum(1 for p in near
                                  if any(s.get("first_opened_at") for s in snapshots(p))),
        "read": sum(1 for p in picks
                    if any((s.get("reading_progress") or 0) >= READ_FRACTION
                           for s in snapshots(p))),
        "graded": len(grades), "grade_sum": round(sum(grades), 2),
    }


def combine(months):
    """Add several months' numbers together (the annual report)."""
    total = {"months": len(months), "days": 0, "items": 0, "opened": 0, "read": 0,
             "long_reads": 0, "saved": 0, "highlights": 0, "highlighted_items": 0,
             "minutes": 0, "active_days": 0, "weekdays": [0] * 7, "sources": {},
             "topics": Counter(), "regions": Counter(), "top_highlighted": [],
             "digest": Counter()}
    labels = defaultdict(Counter)
    for stats in months:
        for key in ("days", "items", "opened", "read", "long_reads", "saved", "highlights",
                    "highlighted_items", "minutes", "active_days"):
            total[key] += stats.get(key, 0)
        total["weekdays"] = [a + b for a, b in zip(total["weekdays"], stats["weekdays"],
                                                   strict=True)]
        total["topics"].update(stats.get("topics", {}))
        total["regions"].update(stats.get("regions", {}))
        total["digest"].update(stats.get("digest", {}))
        total["top_highlighted"] += stats.get("top_highlighted", [])
        for host, row in stats.get("sources", {}).items():
            mine = total["sources"].setdefault(
                host, {"items": 0, "read": 0, "highlights": 0, "saved": 0})
            for key in mine:
                mine[key] += row[key]
            labels[host][row["label"]] += row["items"]
    for host, row in total["sources"].items():
        row["label"] = labels[host].most_common(1)[0][0]
    total["sources_total"] = len(total["sources"])
    total["top_highlighted"] = sorted(
        total["top_highlighted"], key=lambda t: (-t["highlights"], t["title"]))[:TOP_HIGHLIGHTED]
    return total


# --- Words -----------------------------------------------------------------------------


def pct(part, whole):
    return round(100 * part / whole) if whole else 0


def bar(share):
    filled = round(share * BAR_WIDTH)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def duration(minutes):
    if minutes < 90:
        return f"about {minutes} minutes"
    return f"about {round(minutes / 60)} hours"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def source_table(sources, limit, total_items):
    ranked = sorted(sources.items(), key=lambda kv: (-kv[1]["items"], -kv[1]["read"], kv[0]))
    rows = [[row["label"], str(row["items"]), str(row["read"]), str(row["highlights"]),
             f"{pct(row['items'], total_items)}%"] for _, row in ranked[:limit]]
    return {"head": ["Source", "Articles", "Read at least half", "Highlights", "Share"],
            "rows": rows}


def breakdown(counts, total):
    """'Topic — 42 (31%) ████░░░' lines, biggest first, skipping empty ones."""
    return [f"{name} — {n} ({pct(n, total)}%)  {bar(n / total)}"
            for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if n]


def rhythm_bullets(stats):
    names = list(calendar.day_abbr)
    weekdays = stats["weekdays"]
    bullets = [f"You opened something on {stats['active_days']} of {stats['days']} days."]
    if any(weekdays):
        busiest = max(range(7), key=lambda d: weekdays[d])
        bullets.append(f"Busiest day of the week: {calendar.day_name[busiest]} "
                       f"({plural(weekdays[busiest], 'article')} opened).")
        bullets.append("By weekday: " + " · ".join(f"{names[d]} {weekdays[d]}" for d in range(7)))
    return bullets


def digest_bullets(digest):
    if not digest.get("picks"):
        return []
    bullets = [f"The digest tagged {plural(digest['picks'], 'article')} "
               f"({digest['near_misses']} of them random near-misses). You opened "
               f"{digest['opened']} ({pct(digest['opened'], digest['picks'])}%) and read "
               f"{digest['read']} at least halfway."]
    if digest.get("graded"):
        bullets.append(f"Average grade of the graded picks: "
                       f"{digest['grade_sum'] / digest['graded']:.1f} out of 5 "
                       f"({digest['graded']} graded so far).")
    return bullets


def _sections(stats, head, source_limit, comparison=None, extra_notes=()):
    """The sections shared by the monthly and the annual report."""
    items = stats["items"]
    glance = [
        f"You touched {plural(items, 'article')} from {plural(stats['sources_total'], 'source')}: "
        f"{stats['opened']} opened, {stats['read']} read at least halfway "
        f"({pct(stats['read'], items)}%), {stats['long_reads']} of those long reads "
        f"(over {LONG_WORDS} words).",
        f"You saved {plural(stats['saved'], 'article')} yourself and highlighted "
        f"{plural(stats['highlights'], 'passage')} in {plural(stats['highlighted_items'], 'article')}.",
        f"Reading time, counting the part of each article you got through: "
        f"{duration(stats['minutes'])}.",
    ]
    if comparison:
        glance.append(comparison)
    top = sorted(stats["sources"].values(), key=lambda r: -r["items"])[:source_limit]
    share = pct(sum(row["items"] for row in top), items)
    sections = [
        {"heading": head, "bullets": glance},
        {"heading": "Sources you used most",
         "table": source_table(stats["sources"], source_limit, items),
         "notes": [f"{stats['sources_total']} sources in all; these {len(top)} are {share}% of "
                   "everything you touched."]},
    ]
    labelled = sum(stats["topics"].values())
    if labelled:
        sections += [
            {"heading": "What you read about", "bullets": breakdown(stats["topics"], labelled),
             "notes": ["Topics and regions are guessed by an AI from each headline and "
                       "summary, so read them as a rough picture."]},
            {"heading": "Where in the world", "bullets": breakdown(stats["regions"], labelled)},
        ]
    digest = digest_bullets(stats.get("digest") or {})
    if digest:
        sections.append({"heading": "How the digest did", "bullets": digest})
    sections.append({"heading": "When you read", "bullets": rhythm_bullets(stats)})
    if stats["top_highlighted"]:
        sections.append({"heading": "Most highlighted", "bullets": [
            f"{t['title']} — {t['label']} ({plural(t['highlights'], 'highlight')})"
            for t in stats["top_highlighted"]]})
    for note in extra_notes:
        sections.append({"heading": "", "notes": [note]})
    return sections


def monthly_sections(month, stats, previous=None, preview_to=None, suggestions=None):
    comparison = None
    if previous:
        comparison = (f"Last month: {previous['items']} articles touched, {previous['read']} "
                      f"read at least halfway.")
    heading = f"{month_name(month)} at a glance"
    notes = ()
    if preview_to:
        notes = (f"Preview: this covers 1–{int(preview_to[8:10])} {month_name(month)} only, so "
                 "far. The real report comes after the month ends.",)
    sections = _sections(stats, heading, TOP_SOURCES, comparison)
    if suggestions:
        sections.append({"heading": "Sites to consider following",
                         "paragraphs": [suggest.INTRO],
                         "raw_html": suggest.items_html(suggestions),
                         "plain": suggest.describe_items(suggestions)})
    for note in notes:
        sections.insert(0, {"heading": "", "notes": [note]})
    return sections


def annual_sections(year, per_month, total):
    first, last = month_name(min(per_month)), month_name(max(per_month))
    span = f"{first} to {last}" if first != last else first
    biggest = max(per_month.values(), key=lambda s: s["items"])["items"] or 1
    months = [f"{calendar.month_name[int(m[5:7])]} — {s['items']} articles, {s['read']} read  "
              f"{bar(s['items'] / biggest)}" for m, s in sorted(per_month.items())]
    sections = _sections(total, f"{year} at a glance", ANNUAL_TOP_SOURCES)
    sections.insert(1, {"heading": "Month by month", "bullets": months})
    if len(per_month) < 12:
        sections.insert(0, {"heading": "", "notes": [
            f"This report covers {span}; the digest has no earlier months to add up."]})
    return sections


# --- Drawing ------------------------------------------------------------------------------


def render_html(sections):
    parts = []
    for section in sections:
        if section.get("heading"):
            parts.append(f"<h2>{escape(section['heading'])}</h2>")
        for paragraph in section.get("paragraphs", []):
            parts.append(f"<p>{escape(paragraph)}</p>")
        if section.get("bullets"):
            parts.append("<ul>" + "".join(f"<li>{escape(b)}</li>" for b in section["bullets"])
                         + "</ul>")
        table = section.get("table")
        if table and table["rows"]:
            head = "".join(f"<th>{escape(h)}</th>" for h in table["head"])
            body = "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in row) + "</tr>"
                           for row in table["rows"])
            parts.append(f"<table><tr>{head}</tr>{body}</table>")
        if section.get("raw_html"):
            parts.append(section["raw_html"])
        for note in section.get("notes", []):
            parts.append(f"<p><em>{escape(note)}</em></p>")
    return "<div>" + "".join(parts) + "</div>"


def render_text(title, sections):
    lines = [title, "=" * len(title), ""]
    for section in sections:
        if section.get("heading"):
            lines += [section["heading"], "-" * len(section["heading"])]
        lines += section.get("paragraphs", [])
        lines += [f"  • {b}" for b in section.get("bullets", [])]
        table = section.get("table")
        if table and table["rows"]:
            widths = [max(len(str(row[n])) for row in [table["head"]] + table["rows"])
                      for n in range(len(table["head"]))]
            for row in [table["head"]] + table["rows"]:
                lines.append("  " + "  ".join(str(c).ljust(w) for c, w in zip(row, widths,
                                                                               strict=True)))
        if section.get("plain"):
            lines.append(section["plain"])
        lines += [f"  ({n})" for n in section.get("notes", [])]
        lines.append("")
    return "\n".join(lines)


# --- When --------------------------------------------------------------------------------------


def due_month(weekly, today):
    """The month whose report is due (the one that just ended), or None."""
    target = previous_month(today)
    last = weekly.get("last_report_month")
    return target if not last or last < target else None


def due_year(weekly, today, stored):
    """The year whose annual report is due, or None. It waits for December's
    monthly report, so the year is never added up short."""
    year = int(today[:4]) - 1
    if (weekly.get("last_annual_year") or 0) >= year:
        return None
    if (weekly.get("last_report_month") or "") < f"{year}-12":
        return None
    return year if any(m.startswith(f"{year}-") for m in stored) else None


def document_url(name):
    """Reader de-duplicates saved documents by address, so each report has its own
    made-up one; a repeat finds the report already there."""
    return f"https://{copies.SUGGESTIONS_HOST}/report/{name}"


# --- The whole thing ---------------------------------------------------------------------------


def _write_html(path, title, html):
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!doctype html><meta charset='utf-8'><title>{escape(title)}</title>"
                f"<style>body{{font:16px/1.5 -apple-system,sans-serif;max-width:46em;"
                f"margin:2em auto;padding:0 1em}}td,th{{padding:2px 10px;text-align:left}}"
                f"</style><h1>{escape(title)}</h1>{html}")


def run_monthly(paths, client, today, now, weekly, api_key=get_api_key,
                call=call_anthropic, prepare_suggestions=suggest.prepare,
                dry_run=False, month=None, html_path=None, say=print):
    """Make the monthly report if one is due (or for `month`, when given).

    A month that is not over yet is only a preview: nothing is delivered or
    remembered. A real delivery is remembered only after Reader has the note, so
    a failed attempt is simply tried again tomorrow.
    """
    target = month or due_month(weekly, today)
    if target is None:
        say(f"Monthly report not due (last delivered for {weekly.get('last_report_month')}).")
        return {"status": "not due"}
    start, end = month_bounds(target)
    preview = end > now
    through = today if preview else f"{target}-{month_days(target):02d}"

    say(f"Reading your Reader library for {month_name(target)}…")
    docs = client.list_documents(updated_after=start)
    items = readings(docs, start, end)
    if not items:
        say(f"Nothing read in {month_name(target)}; no report.")
        if not (dry_run or preview):
            weekly["last_report_month"] = max(weekly.get("last_report_month") or "", target)
            save_weekly(paths, weekly)
        return {"status": "nothing read"}

    say(f"{len(items)} articles; labelling topics and regions…")
    cache = load_json(paths.classifications, {})
    topics, regions = label_lists(load_json(paths.prefs, {}))
    classify(items, cache, api_key(), call, lambda: atomic_write(paths.classifications, cache),
             topics, regions)

    stats = summarize(items, cache, int(through[8:10]) if preview else month_days(target))
    stats["digest"] = digest_stats(load_json(paths.tracked, {}), f"{target}-01", through)
    stored = load_json(paths.reports, {})
    previous = stored.get(previous_month(f"{target}-01"))

    suggestions = None
    if not preview:
        suggestions = prepare_suggestions(paths, client, today, now, weekly,
                                          dry_run=dry_run, say=say)
    sections = monthly_sections(target, stats, previous, through if preview else None,
                                suggestions or None)
    title = f"News Digest Monthly Report — {month_name(target)}"
    html = render_html(sections)
    if html_path:
        _write_html(html_path, title, html)
        say(f"Wrote {html_path}")
    if dry_run:
        say("")
        say(render_text(title, sections))
        return {"status": "dry run", "stats": stats}
    if preview:
        say(f"{month_name(target)} is not over, so this was only a preview; nothing delivered.")
        return {"status": "preview", "stats": stats}

    tags = REPORT_TAGS + (suggest.NOTE_TAGS[1:] if suggestions else ())
    created, doc_id = client.save_document(document_url(target), title, html, tags=tags,
                                           location=REPORT_LOCATION)
    stored[target] = stats
    atomic_write(paths.reports, stored)
    weekly["last_report_month"] = max(weekly.get("last_report_month") or "", target)
    if suggestions is not None:
        suggest.record(weekly, suggestions if created else [], today)
    save_weekly(paths, weekly)
    say(f"Delivered “{title}” to your Reader Later list." if created
        else f"“{title}” was already in your Reader library; left as it is.")
    return {"status": "delivered" if created else "already delivered", "id": doc_id,
            "stats": stats}


def run_annual(paths, client, today, weekly, dry_run=False, year=None, html_path=None,
               say=print):
    """Make the annual report if one is due (or for `year`), from the kept months."""
    stored = load_json(paths.reports, {})
    target = year or due_year(weekly, today, stored)
    if target is None:
        say("Annual report not due.")
        return {"status": "not due"}
    per_month = {m: s for m, s in stored.items() if m.startswith(f"{target}-")}
    if not per_month:
        say(f"No monthly reports kept for {target}; no annual report.")
        return {"status": "nothing kept"}
    total = combine(list(per_month.values()))
    sections = annual_sections(target, per_month, total)
    title = f"News Digest Annual Report — {target}"
    html = render_html(sections)
    if html_path:
        _write_html(html_path, title, html)
        say(f"Wrote {html_path}")
    if dry_run:
        say("")
        say(render_text(title, sections))
        return {"status": "dry run"}
    created, doc_id = client.save_document(document_url(str(target)), title, html,
                                           tags=REPORT_TAGS, location=REPORT_LOCATION)
    weekly["last_annual_year"] = max(weekly.get("last_annual_year") or 0, target)
    save_weekly(paths, weekly)
    say(f"Delivered “{title}” to your Reader Later list." if created
        else f"“{title}” was already in your Reader library; left as it is.")
    return {"status": "delivered" if created else "already delivered", "id": doc_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print the report; deliver and remember nothing")
    parser.add_argument("--month", help="report on this month (YYYY-MM) instead of the due one")
    parser.add_argument("--year", type=int, help="the annual report for this year")
    parser.add_argument("--html", metavar="FILE", help="also draw the report into this file")
    args = parser.parse_args(argv)
    paths = Paths()
    setup_logging(paths)
    try:
        client = ReaderClient(get_readwise_token())
        weekly = load_weekly(paths)
        today = local_today()
        if args.year:
            run_annual(paths, client, today, weekly, args.dry_run, args.year, args.html)
        else:
            run_monthly(paths, client, today, datetime.now(timezone.utc), weekly,
                        dry_run=args.dry_run, month=args.month, html_path=args.html)
            if not args.month:
                run_annual(paths, client, today, weekly, args.dry_run)
    except Exception as exc:
        log.error(f"Report failed: {exc}", exc_info=True)
        print(f"Problem: report failed: {exc}", file=sys.stderr)
        if not args.dry_run:
            notify("Readwise News Digest", f"The reading report failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
