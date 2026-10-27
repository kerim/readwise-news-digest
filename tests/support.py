"""Shared test helpers: a fake Reader library and a base class for run tests."""

import copy
import json
import random
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import run
from common import Paths, atomic_write, parse_iso

NOW = datetime(2026, 10, 28, 7, 12).astimezone()   # 07:12 local, tz-aware

OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="1.0"><body><outline text="Feeds" title="Feeds">
<outline title="Example Magazine" category="Politics,News Digest feeds" type="rss" xmlUrl="https://magazine.example/feed/" />
<outline title="New Yorker" category="News Digest feeds" type="rss" xmlUrl="https://www.newyorker.com/feed/everything" />
<outline title="Other" category="Misc" type="rss" xmlUrl="https://other.example/feed" />
</outline></body></opml>"""

FEED_LINKS = {
    "https://magazine.example/feed/": ["https://magazine.example/a"],
    "https://www.newyorker.com/feed/everything": ["https://www.newyorker.com/x"],
}


def tags_dict(*names, kind="manual"):
    """Tags the way Reader returns them: keyed by lower-cased key."""
    return {n.lower(): {"name": n, "type": kind, "created": 1} for n in names}


def make_doc(i, host="magazine.example", age_hours=3, **kw):
    created = (NOW - timedelta(hours=age_hours)).isoformat()
    doc = {
        "id": f"doc{i}", "source_url": f"https://www.{host}/2026/article-{i}",
        "title": f"Article {i}", "summary": "A summary.", "category": "rss",
        "location": "feed", "tags": {}, "parent_id": None, "word_count": 900,
        "created_at": created, "updated_at": created,
        "reading_progress": 0, "first_opened_at": None, "last_moved_at": created,
    }
    doc.update(kw)
    return doc


class FakeReader:
    """Stands in for ReaderClient: an in-memory library."""

    def __init__(self, docs):
        self.docs = {d["id"]: d for d in docs}
        self.list_calls = []
        self.set_calls = []
        self.save_calls = []
        self.fail_set = None          # exception to raise from set_tags
        self.fail_first_n = 0         # report the first N updates as failed
        self.fail_listing = None      # exception to raise from list_documents
        self.fail_tag_listing = None  # ... only from listings filtered by tag
        self.clock = NOW              # stamped on documents set_tags changes

    def list_documents(self, updated_after=None, location=None, category=None,
                       tag=None, doc_id=None, on_page=None):
        self.list_calls.append({"updated_after": updated_after,
                                "category": category, "tag": tag})
        if self.fail_listing:
            raise self.fail_listing
        if self.fail_tag_listing and tag:
            raise self.fail_tag_listing
        wanted = [t.lower() for t in ([tag] if isinstance(tag, str) else tag or [])]
        found = []
        for doc in self.docs.values():
            if doc_id and doc["id"] != doc_id:
                continue
            if category and doc["category"] != category:
                continue
            if wanted and not all(t in doc["tags"] for t in wanted):
                continue
            if updated_after and parse_iso(doc["updated_at"]) < updated_after:
                continue
            found.append(copy.deepcopy(doc))
        if on_page:
            on_page(1, len(found))
        return found

    def get_document(self, doc_id):
        found = self.list_documents(doc_id=doc_id)
        return found[0] if found else None

    def save_document(self, url, title, html, tags=(), location="new",
                      author="Readwise News Digest"):
        """Like Reader: a new address creates a document, a repeat finds it."""
        self.save_calls.append({"url": url, "title": title, "html": html,
                                "tags": list(tags), "location": location,
                                "author": author})
        for doc in self.docs.values():
            if doc.get("source_url") == url:
                return False, doc["id"]
        doc_id = f"saved{len(self.save_calls)}"
        self.docs[doc_id] = make_doc(doc_id, id=doc_id, source_url=url, title=title,
                                     category="article", location=location,
                                     source="readwise-news-digest",
                                     tags=tags_dict(*tags, kind="public_api"))
        return True, doc_id

    def set_tags(self, updates):
        if self.fail_set:
            raise self.fail_set
        self.set_calls.append(updates)
        confirmed, failed = set(), {}
        for n, (doc_id, tags) in enumerate(updates):
            if n < self.fail_first_n or doc_id not in self.docs:
                failed[doc_id] = "Document not found"
                continue
            old = self.docs[doc_id]["tags"]
            self.docs[doc_id]["tags"] = {
                t.lower(): old.get(t.lower(), {"name": t, "type": "public_api"})
                for t in tags}
            self.docs[doc_id]["updated_at"] = self.clock.isoformat()
            confirmed.add(doc_id)
        return confirmed, failed

    # -- things you do in Reader ------------------------------------------------

    def touch(self, doc_id, **changes):
        """Change a document the way using Reader would, and bump updated_at."""
        self.docs[doc_id].update(changes)
        self.docs[doc_id]["updated_at"] = self.clock.isoformat()

    def highlight(self, parent_id, hid=None):
        hid = hid or f"hl-{parent_id}-{len(self.docs)}"
        self.docs[hid] = {
            "id": hid, "parent_id": parent_id, "category": "highlight",
            "location": "new", "tags": {}, "source_url": None, "title": "a quote",
            "created_at": self.clock.isoformat(), "updated_at": self.clock.isoformat(),
        }

    def delete(self, doc_id):
        del self.docs[doc_id]


class RunTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.paths = Paths(root / "project")
        self.paths.base.mkdir()
        self.downloads = root / "Downloads"
        self.downloads.mkdir()
        (self.downloads / "Reader_Feeds.opml").write_text(OPML)
        atomic_write(self.paths.profile, {"topics": [
            {"topic": "Topic A", "score": 1.0, "tags": ["a"]}]})
        self.now = NOW
        self.reader = FakeReader([make_doc(i) for i in range(40)])
        self.score_calls = 0
        self.scored_titles = []
        self.score_error = None
        self.on_first_score = None
        self.notices = []
        self.out = []
        self.deps = run.Deps(
            client=self.reader, api_key=lambda: "key", score_call=self.fake_score,
            fetch_links=lambda url: FEED_LINKS.get(url, []),
            notify=lambda title, message: self.notices.append((title, message)),
            downloads=self.downloads, rng=random.Random(3),
            sleep=lambda seconds: None, now=lambda: self.now)
        self.reporter = run.Reporter(self.paths, out=self.out.append)

    def fake_score(self, api_key, messages, system=None, max_tokens=0):
        self.score_calls += 1
        if self.score_error:
            raise self.score_error
        if self.on_first_score and self.score_calls == 1:
            self.on_first_score()
        prompt = messages[0]["content"]
        scores = []
        for index, title in re.findall(
                r'<item index="(\d+)"><title>(.*?)</title>', prompt):
            self.scored_titles.append(title)
            digits = re.search(r"\d+", title)
            n = int(digits.group()) if digits else 0
            scores.append({"index": int(index), "topic_score": 4 + n % 6,
                           "form_score": 5 + n % 5, "primary_topic": "Topic A",
                           "form": "analysis", "frame": "angle", "reason": "r"})
        return json.dumps(scores)

    def go(self, dry_run=False, grades_only=False):
        return run.run_once(self.deps, self.paths, self.reporter, dry_run,
                            grades_only=grades_only)

    def set_time(self, moment):
        """Move the clock; the fake library's clock follows."""
        self.now = moment
        self.reader.clock = moment

    def state(self):
        return json.loads(self.paths.state.read_text())

    def tracked(self):
        return json.loads(self.paths.tracked.read_text())

    def run_log(self):
        return self.paths.run_log.read_text().splitlines()

    def said(self):
        return "\n".join(self.out)
