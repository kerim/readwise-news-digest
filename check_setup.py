#!/usr/bin/env python3
"""Check that everything the digest needs is in place, and say what is missing.

    .venv/bin/python check_setup.py

It asks Readwise whether your token works, sends Anthropic a five-word test
message (a fraction of a cent), and looks at your files. The only thing it
writes is a copy of your exported feed list into this folder, so the background
jobs never depend on reading your Downloads folder. Exit status 1 means at least
one thing needs fixing.
"""

import os
import subprocess
import sys
from dataclasses import dataclass

import feeds_membership
from common import (
    DOWNLOADS_DIR,
    APIAuthError,
    APINetworkError,
    ConfigError,
    Paths,
    StateError,
    call_anthropic,
    get_api_key,
    get_readwise_token,
    load_json,
)
from reader_api import ReaderAPIError, ReaderClient

JOB_LABELS = ("com.readwisenewsdigest.run", "com.readwisenewsdigest.profile")


@dataclass
class Check:
    ok: bool | None      # True: fine. False: fix this. None: fine for now, just so you know.
    name: str
    detail: str


def check_python(version=sys.version_info):
    found = f"{version[0]}.{version[1]}"
    if tuple(version[:2]) >= (3, 10):
        return Check(True, "Python", f"{found}")
    return Check(False, "Python", f"{found} is too old; 3.10 or newer is needed")


def check_feedparser(importer=lambda: __import__("feedparser")):
    try:
        importer()
    except ImportError:
        return Check(False, "feedparser",
                     "not installed; run ./install.sh, then use .venv/bin/python")
    return Check(True, "feedparser", "installed")


def check_readwise(get_token=get_readwise_token, make_client=ReaderClient):
    try:
        token = get_token()
    except ConfigError as exc:
        return Check(False, "Readwise token", str(exc))
    try:
        accepted = make_client(token).check_token()
    except ReaderAPIError as exc:
        return Check(False, "Readwise token", f"could not reach Readwise ({exc}); "
                     "are you online?")
    if not accepted:
        return Check(False, "Readwise token",
                     "Readwise refused it; copy a fresh one from readwise.io/access_token")
    return Check(True, "Readwise token", "stored in the Keychain and accepted")


def check_anthropic(get_key=get_api_key, call=call_anthropic):
    try:
        key = get_key()
    except ConfigError as exc:
        return Check(False, "Anthropic key", str(exc))
    try:
        call(key, [{"role": "user", "content": "Reply with the single word OK."}],
             max_tokens=5)
    except APIAuthError:
        return Check(False, "Anthropic key",
                     "Anthropic refused it; create a new key at console.anthropic.com")
    except (APINetworkError, RuntimeError) as exc:
        return Check(False, "Anthropic key", f"the test message failed ({exc})")
    return Check(True, "Anthropic key", "stored in the Keychain and accepted")


def check_feeds(paths, downloads=DOWNLOADS_DIR):
    try:
        opml, _ = feeds_membership.locate_opml(paths, downloads)
        feeds = feeds_membership.parse_opml(opml)
        feeds_membership.refresh_opml(paths, downloads)
    except ConfigError as exc:
        return Check(False, "Feed list", str(exc))
    digest = feeds_membership.digest_feeds(feeds)
    if not digest:
        return Check(False, "Feed list",
                     f"found {len(feeds)} feeds but none in a folder called "
                     f"'{feeds_membership.DIGEST_FOLDER}'; create that folder in Reader, "
                     "move your digest feeds into it, and export the feeds again")
    return Check(True, "Feed list", f"{len(digest)} feeds in '{feeds_membership.DIGEST_FOLDER}' "
                 f"(of {len(feeds)})")


def check_profile(paths):
    try:
        profile = load_json(paths.profile, None)
    except StateError as exc:
        return Check(False, "Interests", str(exc))
    topics = (profile or {}).get("topics") or []
    if not topics:
        return Check(False, "Interests", "no interest-profile.json yet; copy "
                     "interest-profile.example.json and edit it (README: 'Describe your "
                     "interests')")
    return Check(True, "Interests", f"{len(topics)} topics in interest-profile.json")


def job_is_loaded(label):
    result = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"],
                            capture_output=True, text=True)
    return result.returncode == 0


def check_jobs(is_loaded=job_is_loaded):
    missing = [label for label in JOB_LABELS if not is_loaded(label)]
    if missing:
        return Check(None, "Background jobs", "not loaded yet; when everything above is "
                     "ticked, run ./reload-launchers.sh")
    return Check(True, "Background jobs", "both loaded")


def run_checks(paths, downloads=DOWNLOADS_DIR, **overrides):
    """All the checks, in the order a person would fix them."""
    return [
        check_python(), check_feedparser(),
        check_readwise(**overrides.get("readwise", {})),
        check_anthropic(**overrides.get("anthropic", {})),
        check_feeds(paths, downloads), check_profile(paths),
        check_jobs(**overrides.get("jobs", {})),
    ]


def render(checks):
    marks = {True: "✓", False: "✗", None: "–"}
    lines = [f"{marks[c.ok]} {c.name}: {c.detail}" for c in checks]
    problems = sum(1 for c in checks if c.ok is False)
    lines.append("")
    lines.append("Everything needed is in place." if not problems else
                 f"{problems} thing{'s' if problems != 1 else ''} to fix (marked ✗ above).")
    return "\n".join(lines)


def main():
    checks = run_checks(Paths())
    print(render(checks))
    return 1 if any(c.ok is False for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
