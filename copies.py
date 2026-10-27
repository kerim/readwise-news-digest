"""Linking a full-length copy you saved to the feed item that was tagged.

Paywalled and teaser feeds (NYT, FT, WSJ and others) deliver a few lines. You
open the article in the browser and save it to Reader, which creates a second,
separate Reader document for the same article. The tagged feed item and your
saved copy are then one article, and your reading of the copy is the real
signal.

A copy is matched to a feed item on the same site, in three steps, stopping at
the first that fits:

  1. the same address (host without www + path; query and fragment dropped;
     for wsj.com the trailing 8-character article id, since the path differs);
  2. a shared article id in the path (a UUID, a run of 6+ hex characters
     containing a digit, or a 5+ digit number that is not a date);
  3. the same headline (case, punctuation and " | Author" / " - Site" endings
     ignored; exact for short headlines, difflib ratio >= 0.9 for headlines of
     30+ characters), and only when exactly one item fits.
"""

import difflib
import re
from urllib.parse import unquote, urlsplit

from feeds_membership import host_of

COPY_CATEGORIES = ("article", "pdf", "epub", "email")
HEADLINE_THRESHOLD = 0.9
MIN_HEADLINE_CHARS = 10          # shorter titles are too generic to compare
FUZZY_MIN_CHARS = 30             # only headlines this long may differ a little
SUGGESTIONS_HOST = "readwise-news-digest.invalid"   # our own notes (reports, suggestions)

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
WSJ_ID_RE = re.compile(r"-([0-9a-f]{8})/?$")
TOKEN_SPLIT_RE = re.compile(r"[/\-_.]+")
HEX_RE = re.compile(r"[0-9a-f]+")
DATE_LIKE_RE = re.compile(r"(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])?")
SITE_SEPARATOR_RE = re.compile(r"\s+[|–—-]\s+")
NON_WORD_RE = re.compile(r"[\W_]+")


def is_copy_candidate(doc):
    """A document that could be a saved copy: one of yours, not a feed item, a
    highlight or note, or this script's own suggestions document."""
    if doc.get("parent_id") or doc.get("category") not in COPY_CATEGORIES:
        return False
    return host_of(doc.get("source_url")) not in ("", SUGGESTIONS_HOST)


# --- Keys ------------------------------------------------------------------


def address_key(url):
    host = host_of(url)
    path = unquote(urlsplit(url or "").path).lower()
    if host == "wsj.com":
        found = WSJ_ID_RE.search(path)
        if found:
            return f"wsj.com#{found.group(1)}"
    return f"{host}{path.rstrip('/')}"


def _is_article_id(token):
    if token.isdigit():
        return len(token) >= 5 and not DATE_LIKE_RE.fullmatch(token)
    return (len(token) >= 6 and HEX_RE.fullmatch(token) is not None
            and any(c.isdigit() for c in token))


def article_ids(url):
    path = unquote(urlsplit(url or "").path).lower()
    ids = set(UUID_RE.findall(path))
    ids.update(t for t in TOKEN_SPLIT_RE.split(path) if _is_article_id(t))
    return ids


def _normalize(text):
    return NON_WORD_RE.sub(" ", text.lower()).strip()


def headline_forms(title):
    """The headline as written, and without a trailing ' | Author' / ' - Site'."""
    title = title or ""
    forms = {_normalize(title)}
    parts = SITE_SEPARATOR_RE.split(title)
    if len(parts) > 1:
        forms.add(_normalize(parts[0]))
        forms.add(_normalize(" ".join(parts[:-1])))
    return {f for f in forms if len(f) >= MIN_HEADLINE_CHARS}


def headline_ratio(title_a, title_b):
    best = 0.0
    for a in headline_forms(title_a):
        for b in headline_forms(title_b):
            best = max(best, difflib.SequenceMatcher(None, a, b).ratio())
    return best


def headlines_match(title_a, title_b):
    """The same headline, ignoring case, punctuation and a trailing author or
    site name. A short headline must match exactly (one character is a tenth
    of "Article 29" and would make "Article 59" look the same); only a headline
    of FUZZY_MIN_CHARS or more may differ slightly (ratio >= 0.9)."""
    for a in headline_forms(title_a):
        for b in headline_forms(title_b):
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            if ratio == 1.0:
                return True
            if ratio >= HEADLINE_THRESHOLD and min(len(a), len(b)) >= FUZZY_MIN_CHARS:
                return True
    return False


def make_key(url, title):
    return {
        "host": host_of(url),
        "address": address_key(url),
        "ids": sorted(article_ids(url)),
        "title": title or "",
    }


# --- Matching ----------------------------------------------------------------


def find_match(target, candidates):
    """Match one key against {id: key}. Returns (id, how) or (None, None).

    `how` is "address", "id" or "headline". Only same-site candidates count.
    """
    host = target["host"]
    if not host:
        return None, None
    same_site = {i: k for i, k in candidates.items() if k["host"] == host}

    has_path = "/" in target["address"] or "#" in target["address"]
    if has_path:
        for cid, key in same_site.items():
            if key["address"] == target["address"]:
                return cid, "address"

    ids = set(target["ids"])
    if ids:
        hits = [cid for cid, key in same_site.items() if ids & set(key["ids"])]
        if hits:
            best = max(hits, key=lambda cid: headline_ratio(
                target["title"], same_site[cid]["title"]))
            return best, "id"

    hits = [cid for cid, key in same_site.items()
            if headlines_match(target["title"], key["title"])]
    if len(hits) == 1:     # two candidates means we cannot tell: no link
        return hits[0], "headline"
    return None, None


def best_headline(target, candidates):
    """(id, ratio) of the closest same-site headline, for the unmatched report."""
    best_id, best = None, 0.0
    for cid, key in candidates.items():
        if key["host"] != target["host"]:
            continue
        ratio = headline_ratio(target["title"], key["title"])
        if ratio > best:
            best_id, best = cid, ratio
    return best_id, best
