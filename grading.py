"""Turn what you did with a tagged article into a 1-5 grade.

Reader only reports an item's current state, never where it has been, so every
tagged item carries a *snapshot* of its last-seen state (see tracking.py) and
the grade is recomputed from that snapshot. The current state decides, except
for two sticky flags (highlighted, shortlisted) and the freezes (day 90,
deleted), which are never undone.

Rows are checked top to bottom and the first match wins:

    5    you gave it 👍, ever highlighted, or carries a tag of your own
    4    long article (>= 1,500 words) read >= 50%, in any location
    3    ever shortlisted; or medium article (500-1,499 words) read >= 50%
    2    long/medium article opened, read < 50%, then archived;
         or in Later/Inbox, unopened, 21+ days after you moved it there
    2    (at the 90-day freeze) long/medium article still in Later/Inbox/Feed,
         opened, but none of the above
    1.5  in Feed, unopened, 30+ days after tagging
    1    archived without being opened
    none anything else

👍 and 👎 are votes, not tags of your own: 👎 never raises a grade (the
reason you write in its note is what counts, see scoring.reader_feedback).

A saved full-length copy makes the pair worth at least 3, or more if the copy
itself earns more.
"""

from common import days_between, local_date, parse_iso

LONG_WORDS = 1500
MEDIUM_WORDS = 500
READ_FRACTION = 0.5          # "read" means at least this much
LATER_UNOPENED_DAYS = 21     # unopened in Later/Inbox this long: a soft no
FEED_UNOPENED_DAYS = 30      # unopened in Feed this long after tagging
FREEZE_DAYS = 90             # tracking stops this long after tagging
COPY_FLOOR = 3               # keeping a full copy is worth at least this

# The script's own tags never count as yours.
OWN_TAGS = frozenset({"newsdigest", "newsdigest-copy", "newsdigest-suggestions"})

THUMBS_UP = "👍"
THUMBS_DOWN = "👎"
VOTE_TAGS = frozenset({THUMBS_UP, THUMBS_DOWN})


def word_class(word_count):
    """'long', 'medium', 'short' or 'unknown' (no word count)."""
    if word_count is None:
        return "unknown"
    if word_count >= LONG_WORDS:
        return "long"
    if word_count >= MEDIUM_WORDS:
        return "medium"
    return "short"


def user_tags(snap):
    """Tags on a snapshot that are yours: not ours, not added by a Reader rule
    or Ghostreader (type 'auto')."""
    auto = {t.lower() for t in snap.get("auto_tags") or []}
    return [t for t in snap.get("tags") or []
            if t.lower() not in OWN_TAGS and t.lower() not in auto
            and t not in VOTE_TAGS]


def vote(snap):
    """THUMBS_UP, THUMBS_DOWN or None for one snapshot (👍 wins if both)."""
    tags = (snap or {}).get("tags") or []
    if THUMBS_UP in tags:
        return THUMBS_UP
    return THUMBS_DOWN if THUMBS_DOWN in tags else None


def voted_up(entry):
    """True when you gave the feed item or its saved copy a 👍."""
    return any(vote(entry.get(k)) == THUMBS_UP
               for k in ("snapshot", "copy_snapshot"))


def _days_since(iso_or_none, today, fallback_date):
    moved = parse_iso(iso_or_none)
    return days_between(local_date(moved) if moved else fallback_date, today)


def _read_text(progress, words, cls):
    return f"read {progress:.0%} of a {cls} article ({words:,} words)"


def grade_document(snap, tagged_date, today, as_archived=False, freezing=False):
    """(grade or None, reason) for one snapshot (a feed item or a saved copy).

    `as_archived` grades a deleted item as if it had been archived in its last
    snapshot. `freezing` applies the rule that only exists at the 90-day freeze.
    """
    location = "archive" if as_archived else snap.get("location")
    progress = snap.get("reading_progress") or 0
    words = snap.get("word_count")
    cls = word_class(words)
    opened = bool(snap.get("first_opened_at"))

    if vote(snap) == THUMBS_UP:
        return 5, f"you gave it {THUMBS_UP}"
    if snap.get("highlighted"):
        return 5, "you highlighted it"
    mine = user_tags(snap)
    if mine:
        return 5, f"you tagged it “{mine[0]}”"

    if cls == "long" and progress >= READ_FRACTION:
        return 4, _read_text(progress, words, cls)

    if snap.get("shortlisted"):
        return 3, "you shortlisted it"
    if cls == "medium" and progress >= READ_FRACTION:
        return 3, _read_text(progress, words, cls)

    long_or_medium = cls in ("long", "medium")
    if location == "archive" and opened and long_or_medium and progress < READ_FRACTION:
        return 2, "opened, read less than half, then archived"
    if location in ("later", "new") and not opened:
        waited = _days_since(snap.get("last_moved_at"), today, tagged_date)
        if waited >= LATER_UNOPENED_DAYS:
            return 2, f"still unopened {waited} days after you moved it to Later"
    if (freezing and long_or_medium and opened
            and location in ("later", "new", "feed")):
        return 2, "opened but never finished in 90 days"

    if (location == "feed" and not opened
            and days_between(tagged_date, today) >= FEED_UNOPENED_DAYS):
        return 1.5, f"unopened in Feed {days_between(tagged_date, today)} days after tagging"
    if location == "archive" and not opened:
        return 1, "archived without opening"

    if opened and long_or_medium:
        return None, "in progress"
    if opened:
        return None, "opened a short piece (no grade)"
    return None, "not opened yet"


def grade_entry(entry, today, freezing=False):
    """(grade or None, reason) for a tracked item and its saved copy, if any."""
    tagged = entry["tagged_date"]
    item_grade, item_reason = grade_document(
        entry["snapshot"], tagged, today,
        as_archived=bool(entry.get("item_gone")), freezing=freezing)
    copy_snap = entry.get("copy_snapshot")
    if copy_snap is None:
        return item_grade, item_reason

    copy_grade, copy_reason = grade_document(copy_snap, tagged, today,
                                             freezing=freezing)
    best_grade, best_reason = COPY_FLOOR, "you saved a full-length copy"
    for grade, reason, source in ((item_grade, item_reason, "feed item"),
                                  (copy_grade, copy_reason, "your saved copy")):
        if grade is not None and grade > best_grade:
            best_grade, best_reason = grade, f"{reason} ({source})"
        elif grade == best_grade and source == "your saved copy":
            best_reason = f"{reason} (your saved copy)"
    return best_grade, best_reason


def regrade(tracked, today, daily=False):
    """Recompute every live entry's grade. Returns the entries that changed.

    On the daily check an entry 90 or more days old is graded once more with
    the freeze rule and frozen; an entry whose feed item is gone (and has no
    saved copy), or whose saved copy is gone, is frozen too. Freezes are applied
    after the grades, so the final state is what gets frozen.
    """
    changed = []
    for entry in tracked.values():
        if entry.get("frozen"):
            continue
        age = days_between(entry["tagged_date"], today)
        due = daily and age >= FREEZE_DAYS
        grade, reason = grade_entry(entry, today, freezing=due)
        if grade != entry.get("grade"):
            entry["grade_since"] = today if grade is not None else None
            changed.append(entry)
        entry["grade"], entry["grade_reason"] = grade, reason
        if due or entry.get("copy_gone") or (
                entry.get("item_gone") and not entry.get("copy_id")):
            entry["frozen"] = True
    return changed
