"""Snapshots of tagged items, saved-copy links, highlights and the daily refresh.

Reader's "changed since" listing only returns what changed, and a highlight
shows up in the hour it is made and never again. So every tagged item keeps a
snapshot of its last-seen state (location, reading progress, word count, when
opened and moved, tags, plus two sticky flags: highlighted and shortlisted).
grading.py reads only snapshots.

Each listing is processed in two passes: first saved copies are linked to
tagged items, then highlights are applied against tagged ids *and* linked copy
ids, so a copy saved together with its highlights (the web highlighter) counts.
"""

import logging
from datetime import timedelta

import copies
from common import date_minus_days, days_between, local_date, parse_iso

log = logging.getLogger("rnd")

TAG_NAME = "newsdigest"
COPY_TAG = "newsdigest-copy"
MEMORY_DAYS = 14        # copy keys and highlight parents are kept this long
COPY_WINDOW_DAYS = 14   # a tagged item can acquire a copy this long after tagging


# --- Snapshots ---------------------------------------------------------------


def tag_names(doc):
    """Names of a document's tags.

    Reader returns tags as an object keyed by the *lower-cased* key (`done`),
    with the real name inside (`DONE`). Names are what bulk_update expects
    back, so sending keys would silently change your tags' capitalisation.
    Falling back to the key keeps a tag even if the value is bare.
    """
    tags = doc.get("tags") or {}
    if isinstance(tags, dict):
        names = []
        for key, value in tags.items():
            name = value.get("name") if isinstance(value, dict) else None
            names.append(name or key)
        return names
    return [t.get("name") if isinstance(t, dict) else t for t in tags]


def auto_tag_names(doc):
    """Tags added by a Reader rule or Ghostreader (type 'auto'): never yours."""
    tags = doc.get("tags") or {}
    if not isinstance(tags, dict):
        return []
    return [(value.get("name") or key) for key, value in tags.items()
            if isinstance(value, dict) and value.get("type") == "auto"]


def make_snapshot(doc, tags=None):
    """The last-seen state of an item (or of its saved copy)."""
    return {
        "location": doc.get("location"),
        "reading_progress": doc.get("reading_progress"),
        "word_count": doc.get("word_count"),
        "first_opened_at": doc.get("first_opened_at"),
        "last_moved_at": doc.get("last_moved_at"),
        "tags": tags if tags is not None else tag_names(doc),
        "auto_tags": auto_tag_names(doc),
        "notes": doc.get("notes") or "",
        "highlighted": False,
        "shortlisted": doc.get("location") == "shortlist",
    }


def update_snapshot(old, doc):
    """A fresh snapshot that keeps the two sticky flags."""
    new = make_snapshot(doc)
    new["highlighted"] = bool(old.get("highlighted"))
    new["shortlisted"] = bool(old.get("shortlisted")) or new["shortlisted"]
    return new


# --- What a listing tells us ----------------------------------------------------


def record_highlights(state, docs, today):
    """Remember which documents were highlighted, for 14 days, so a highlight
    made before an item was tagged (or its copy linked) is credited later."""
    for doc in docs:
        parent = doc.get("parent_id")
        if parent and doc.get("category") != "note":
            state["highlight_parents"][parent] = today


def record_copies(state, docs, today):
    """Remember the match keys of documents that could be saved copies."""
    cutoff = date_minus_days(today, MEMORY_DAYS)
    for doc in docs:
        if not copies.is_copy_candidate(doc):
            continue
        created = parse_iso(doc.get("created_at"))
        if created is None or local_date(created) < cutoff:
            continue
        record = copies.make_key(doc.get("source_url"), doc.get("title"))
        record.update(created=local_date(created), seen=today,
                      snapshot=make_snapshot(doc))
        state["copy_keys"][doc["id"]] = record


def apply_snapshots(tracked, docs):
    """Refresh the snapshot of every tagged item, and of every linked copy,
    that appears in a listing."""
    owner_of_copy = {e["copy_id"]: e for e in tracked.values() if e.get("copy_id")}
    for doc in docs:
        entry = tracked.get(doc["id"])
        if entry and not entry.get("frozen"):
            entry["snapshot"] = update_snapshot(entry["snapshot"], doc)
        owner = owner_of_copy.get(doc["id"])
        if owner and not owner.get("frozen") and owner.get("copy_snapshot"):
            owner["copy_snapshot"] = update_snapshot(owner["copy_snapshot"], doc)


def link_copies(tracked, state, today):
    """Link each tagged item (up to 14 days after tagging) to a saved copy.

    Returns the entries that gained a copy this time. A copy saved *before* its
    feed item was tagged is found here too, because copy keys are remembered.
    """
    claimed = {e["copy_id"] for e in tracked.values() if e.get("copy_id")}
    available = {cid: rec for cid, rec in state["copy_keys"].items()
                 if cid not in claimed}
    linked = []
    for entry in tracked.values():
        if (entry.get("frozen") or entry.get("copy_id")
                or days_between(entry["tagged_date"], today) > COPY_WINDOW_DAYS):
            continue
        target = copies.make_key(entry.get("source_url"), entry.get("title"))
        copy_id, how = copies.find_match(target, available)
        if copy_id is None:
            continue
        record = available.pop(copy_id)
        snapshot = dict(record["snapshot"])
        snapshot["highlighted"] = copy_id in state["highlight_parents"]
        entry.update(copy_id=copy_id, copy_how=how, copy_snapshot=snapshot,
                     copy_tagged=False)
        log.info(f"Linked saved copy {copy_id} to “{entry['title']}” ({how})")
        linked.append(entry)
    return linked


def apply_highlights(tracked, state):
    """Mark items and linked copies that were highlighted (sticky)."""
    parents = state["highlight_parents"]
    for entry in tracked.values():
        if entry.get("frozen"):
            continue
        if entry["id"] in parents:
            entry["snapshot"]["highlighted"] = True
        if entry.get("copy_id") in parents and entry.get("copy_snapshot"):
            entry["copy_snapshot"]["highlighted"] = True


def tag_copies(client, tracked):
    """Add `newsdigest-copy` to each linked copy not yet tagged.

    The copy is fetched by id just before the write and its current tags are
    merged, so a tag you added a moment ago is kept. Failures stay pending and
    are retried by the next hourly run. Returns how many were tagged.
    """
    pending = [e for e in tracked.values()
               if e.get("copy_id") and not e.get("copy_tagged")
               and not e.get("copy_gone") and not e.get("frozen")]
    updates, owners = [], {}
    for entry in pending:
        doc = client.get_document(entry["copy_id"])
        if doc is None:                 # deleted before we could tag it
            entry["copy_gone"] = True
            continue
        names = tag_names(doc)
        if COPY_TAG not in names:
            names.append(COPY_TAG)
        updates.append((doc["id"], names))
        owners[doc["id"]] = (entry, names)
    if not updates:
        return 0
    confirmed, failed = client.set_tags(updates)
    for doc_id, error in failed.items():
        log.warning(f"Could not tag saved copy {doc_id}: {error}")
    for doc_id in confirmed:
        entry, names = owners[doc_id]
        entry["copy_tagged"] = True
        entry["copy_snapshot"]["tags"] = names
    return len(confirmed)


# --- Once a day -------------------------------------------------------------------


def daily_refresh(client, tracked, state, today, now, on_page=None):
    """Re-read every tagged item and linked copy, and notice deletions.

    Lists `newsdigest` and, separately, `newsdigest-copy` in full (Reader's tag
    filter means "has all of these tags", so one combined call would return
    nothing), plus the last 14 days of articles, PDFs, books, emails and
    highlights to rebuild the copy keys and the highlight memory (this is what
    credits a highlight made before an item was tagged). Only complete listings
    count: if any page fails the exception propagates and nothing changes, so a
    half listing can never make an item look deleted.
    """
    item_docs = client.list_documents(tag=TAG_NAME, on_page=on_page)
    copy_docs = client.list_documents(tag=COPY_TAG, on_page=on_page)
    since = now - timedelta(days=MEMORY_DAYS)
    recent = []
    for category in copies.COPY_CATEGORIES:
        recent += client.list_documents(updated_after=since, category=category,
                                        on_page=on_page)
    highlights = client.list_documents(updated_after=since, category="highlight",
                                       on_page=on_page)

    record_copies(state, recent, today)
    record_highlights(state, highlights, today)
    items = {d["id"]: d for d in item_docs}
    copy_by_id = {d["id"]: d for d in copy_docs}
    for entry in tracked.values():
        if entry.get("frozen"):
            continue
        doc = items.get(entry["id"])
        if doc:
            entry["snapshot"] = update_snapshot(entry["snapshot"], doc)
            entry.pop("item_gone", None)
        else:
            entry["item_gone"] = True      # deleted, or the tag was removed
        if entry.get("copy_id"):
            copy = copy_by_id.get(entry["copy_id"])
            if copy:
                entry["copy_snapshot"] = update_snapshot(
                    entry["copy_snapshot"], copy)
                entry["copy_tagged"] = True
                entry.pop("copy_gone", None)
            elif entry.get("copy_tagged"):
                entry["copy_gone"] = True  # only a copy we tagged can be "missing"
    state["last_refresh_date"] = today


def prune(state, today):
    """Forget copy keys and highlights older than 14 days."""
    cutoff = date_minus_days(today, MEMORY_DAYS)
    state["copy_keys"] = {k: v for k, v in state["copy_keys"].items()
                          if v.get("created", "") >= cutoff}
    state["highlight_parents"] = {k: v for k, v in state["highlight_parents"].items()
                                  if v >= cutoff}
