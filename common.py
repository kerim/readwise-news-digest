"""Shared utilities for Readwise News Digest."""

import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = Path.home() / "Downloads"

# Optional: qmd (a local search tool) adds context when the profile is built from
# an Obsidian vault of Readwise highlights. Without it that step is simply skipped.
QMD_BIN = shutil.which("qmd") or "qmd"
QMD_COLLECTION = "readwise"

MODEL = "claude-haiku-4-5-20251001"
API_TIMEOUT = 90

# Where the two secrets live in the macOS Keychain (see the README, Install).
ANTHROPIC_KEYCHAIN_ACCOUNT = "newsdigest"
ANTHROPIC_KEYCHAIN_SERVICE = "newsdigest-anthropic-api-key"
READWISE_KEYCHAIN_ACCOUNT = "readwise-news-digest"
READWISE_KEYCHAIN_SERVICE = "readwise-news-digest-token"

# Article form taxonomy. The LLM picks one per item during scoring; prefs use
# them as form-mute values.
FORMS = [
    "criticism", "analysis", "essay", "investigation",
    "interview", "memoir", "op-ed", "recap", "news", "explainer",
]

# Per-domain reporting-style tier. Biases the form_score rubric. Unknown
# domains default to "unknown" (no bias). A short, general starting list: add
# your own sites under "source_tiers" in prefs.json (see the README).
SOURCE_TIERS = {
    # Weekly longform / essay journals
    "newyorker.com": "weekly_longform",
    "nybooks.com": "weekly_longform",
    "lrb.co.uk": "weekly_longform",
    "lareviewofbooks.org": "weekly_longform",
    "bostonreview.net": "weekly_longform",
    "foreignaffairs.com": "weekly_longform",
    # Magazines — high-form bias but also publish shorter pieces
    "theatlantic.com": "magazine",
    # Dailies — mixed, let text + URL hints decide.
    "nytimes.com": "daily_mixed",
    "theguardian.com": "daily_mixed",
    "washingtonpost.com": "daily_mixed",
    "ft.com": "daily_mixed",
    "vox.com": "daily_mixed",
    "aljazeera.com": "daily_mixed",
    "npr.org": "daily_mixed",
    "arstechnica.com": "daily_mixed",
    "wired.com": "daily_mixed",
    "france24.com": "daily_mixed",
    "dw.com": "daily_mixed",
    # Wire services
    "apnews.com": "wire",
    "reuters.com": "wire",
    "bloomberg.com": "wire",
}

log = logging.getLogger("rnd")
log.addHandler(logging.NullHandler())   # silent until setup_logging adds a file


class ConfigError(RuntimeError):
    """Something the user has to set up (token, key, profile, feed list)."""


class StateError(RuntimeError):
    """A state file exists but cannot be read."""


class APINetworkError(RuntimeError):
    """Network-level failure calling the Anthropic API (DNS, connect, timeout).

    Distinguished from a generic RuntimeError so callers fail fast instead of
    retrying — a stalled network won't be fixed by a smaller batch.
    """


class APIAuthError(RuntimeError):
    """The Anthropic API rejected the key (401/403). Retrying cannot help."""


class Paths:
    """Every file the script reads or writes, rooted at one folder.

    Tests build one on a temporary folder so nothing touches the real state.
    """

    def __init__(self, base_dir=BASE_DIR):
        base = Path(base_dir)
        self.base = base
        self.state = base / "state.json"
        self.tracked = base / "tracked.json"
        self.prefs = base / "prefs.json"
        self.profile = base / "interest-profile.json"
        self.learned_weights = base / "learned-weights.json"
        self.feed_sites = base / "feed-sites.json"
        self.weekly = base / "weekly.json"   # the daily job's memory
        self.reports = base / "reports.json"   # each delivered month's numbers
        self.classifications = base / "classifications.json"   # topic/region per article
        self.opml = base / "Reader_Feeds.opml"
        self.log = base / "readwise-news-digest.log"
        self.run_log = base / "run.log"
        self.lock = base / "run.lock"
        self.status = base / "run.status"


def setup_logging(paths):
    logger = logging.getLogger("rnd")
    logger.setLevel(logging.INFO)
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return logger
    fh = logging.FileHandler(paths.log)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    return logger


# --- Secrets ---------------------------------------------------------------


def _keychain_secret(account, service):
    result = subprocess.run(
        ["security", "find-generic-password", "-a", account, "-s", service, "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def get_readwise_token():
    token = _keychain_secret(READWISE_KEYCHAIN_ACCOUNT, READWISE_KEYCHAIN_SERVICE)
    if not token:
        raise ConfigError(
            "No Readwise token in the Keychain. Get one at "
            "readwise.io/access_token, then run (it asks you to paste the token): "
            f"security add-generic-password -a {READWISE_KEYCHAIN_ACCOUNT} "
            f"-s {READWISE_KEYCHAIN_SERVICE} -U -w")
    return token


def get_api_key():
    key = _keychain_secret(ANTHROPIC_KEYCHAIN_ACCOUNT, ANTHROPIC_KEYCHAIN_SERVICE)
    if not key:
        raise ConfigError(
            "No Anthropic API key in the Keychain. Get one at console.anthropic.com, "
            "then run (it asks you to paste the key): security add-generic-password "
            f"-a {ANTHROPIC_KEYCHAIN_ACCOUNT} -s {ANTHROPIC_KEYCHAIN_SERVICE} -U -w")
    return key


# --- Files -----------------------------------------------------------------


def atomic_write(path, data):
    """Write JSON atomically — temp file in the same directory, then rename."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_json(path, default):
    """Read a JSON file; `default` only when the file does not exist.

    A file that exists but cannot be parsed is an error, not an empty file:
    silently starting over would forget every tagged item.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        raise StateError(f"{path} is not valid JSON: {e}") from e


def run_qmd(*args):
    """Run a QMD CLI command. Returns stdout on success, '' on failure."""
    try:
        result = subprocess.run([QMD_BIN, *args], capture_output=True, text=True,
                                timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning(f"QMD command error: {e}")
        return ""
    if result.returncode != 0:
        log.warning(f"QMD command failed: {' '.join(args)}: {result.stderr.strip()}")
        return ""
    return result.stdout


def load_weekly(paths):
    """What the daily profile/suggestions job remembers between runs: when the
    profile and the suggestions last succeeded, and the sites already suggested.
    Kept apart from state.json so this job never races the hourly run."""
    weekly = load_json(paths.weekly, {})
    weekly.setdefault("suggested", {})
    return weekly


def save_weekly(paths, weekly):
    atomic_write(paths.weekly, weekly)


# --- Dates -----------------------------------------------------------------
#
# Calendar dates are LOCAL (what the user lives by); instants are UTC-aware
# (arithmetic, and the `updatedAfter` cursor Reader compares against).


def local_today():
    return datetime.now().strftime("%Y-%m-%d")


def local_date(dt):
    """Local calendar date of an aware datetime."""
    return dt.astimezone().strftime("%Y-%m-%d")


def local_date_days_ago(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def days_between(earlier, later):
    """Whole days from one 'YYYY-MM-DD' local date to another."""
    return (date.fromisoformat(later) - date.fromisoformat(earlier)).days


def date_minus_days(day, days):
    """The 'YYYY-MM-DD' date `days` before another."""
    return (date.fromisoformat(day) - timedelta(days=days)).isoformat()


def parse_iso(text):
    """Parse a Reader timestamp ('…+00:00', '…Z' or a bare date) to an aware
    datetime; naive values are taken as UTC. None for empty or unparseable."""
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def iso_utc(dt):
    return dt.astimezone(timezone.utc).isoformat()


# --- Notifications ---------------------------------------------------------


NOTIFIER = (Path(__file__).resolve().parent / "bin" / "Readwise News Digest.app"
            / "Contents" / "MacOS" / "readwise-news-digest-notify")


def notify(title, message, notifier=NOTIFIER):
    """Show a macOS notification, from the program's own app (see
    notifier/notifier.m) so it is listed as "Readwise News Digest"; osascript
    (listed as Script Editor) if that app is missing or not allowed. Never
    raises: a failed notification must not turn a problem report into a second
    problem."""
    if notifier.exists():
        try:
            done = subprocess.run([str(notifier), title, message],
                                  capture_output=True, text=True, timeout=150)
            if done.returncode == 0:
                return
            log.warning(f"Notification app failed: {done.stderr.strip()}")
        except (OSError, subprocess.SubprocessError) as e:
            log.warning(f"Notification app failed: {e}")
    script = (f"display notification {json.dumps(message, ensure_ascii=False)} "
              f"with title {json.dumps(title, ensure_ascii=False)}")
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True,
                       timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f"Could not show notification: {e}")


# --- Anthropic -------------------------------------------------------------


def call_anthropic(api_key, messages, system=None, max_tokens=4096):
    """Call the Anthropic API and return the response text."""
    body = {"model": MODEL, "max_tokens": max_tokens, "messages": messages}
    if system:
        body["system"] = system

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
            response = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        if e.code in (401, 403):
            raise APIAuthError(f"API error {e.code}: {error_body}") from e
        raise RuntimeError(f"API error {e.code}: {error_body}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise APINetworkError(f"API network error: {e}") from e

    if response.get("stop_reason") == "max_tokens":
        log.warning("API response was truncated (max_tokens reached).")

    text = response["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        text = text.rsplit("```", 1)[0]
    return text


def eprint(*args):
    print(*args, file=sys.stderr, flush=True)
