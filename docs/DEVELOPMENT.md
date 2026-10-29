# Developing Readwise News Digest

For the overview and install steps see the [README](../README.md). This page is for
changing the code.

## Commands

```bash
.venv/bin/python -m unittest discover -s tests   # all tests; a few seconds, nothing touches Reader
.venv/bin/ruff check .                    # lint (pip install ruff first; E501 is off on purpose: the scoring prompt)
.venv/bin/python run.py --dry-run        # score and print the picks; writes nothing
.venv/bin/python run.py                  # a real run: grade, then (once a day) tag
.venv/bin/python run.py --grades         # grade now, then list every item's grade and why
.venv/bin/python profile_builder.py      # the daily job: profile (if a vault is set), then the reports if due
.venv/bin/python report.py --dry-run [--month YYYY-MM] [--html FILE]   # a report; delivers nothing (it keeps topic labels in classifications.json)
.venv/bin/python suggest.py --dry-run    # the feed suggestions list; writes nothing
.venv/bin/python check_setup.py          # is everything in place?
./install.sh                             # venv, launchers, job files for this folder
./reload-launchers.sh                    # (re)load both LaunchAgents -- see below
```

## Where things are

| File | Role |
|---|---|
| `run.py` | hourly entry point: lock, grade step, daily tag step, problems, `--grades` |
| `reader_api.py` | Reader API client: paged listing, bulk tag update, saving a note, 429/retry handling |
| `feeds_membership.py` | OPML → which sites are digest sites |
| `scoring.py`, `selection.py` | Haiku scoring; 25 + 3 selection |
| `tracking.py` | snapshots, saved-copy linking, highlight memory, the daily refresh |
| `grading.py`, `copies.py`, `learning.py` | the grade table; copy matching; learned score nudges |
| `profile_builder.py` | the daily 03:00 job: optional weekly profile from a highlights vault (+ qmd + Haiku, plus grade signals), then the monthly and annual reports |
| `report.py` | the monthly and annual reading reports: what was read (the whole library), top sources, topic and region split, digest results |
| `suggest.py`, `feed_discovery.py` | the feed suggestions (from the user's saved articles only), shown in every other monthly report; finding and checking a site's feed |
| `check_setup.py` | the "is everything in place?" check |
| `health.py` | the checks for problems that raise no error (a job that stopped, no picks, frozen grades, empty learning), read from `run.log` |
| `common.py` | paths, Keychain, dates, Anthropic call, notifications |
| `launchd/*.plist.template`, `install.sh`, `build-launchers.sh`, `newsdigest-launcher.c` | the background jobs |
| `notifier/` | the "Readwise News Digest" notification app (Objective-C, built by `build-launchers.sh` into `bin/`); `common.notify` uses it, falling back to osascript |

State is gitignored JSON next to the code: `tracked.json`, `state.json`,
`feed-sites.json`, `prefs.json` (the user's settings), `learned-weights.json`,
`interest-profile.json`, `weekly.json` (the daily job's memory: when the profile,
suggestions and reports last succeeded, and which sites were suggested),
`reports.json` (each delivered month's numbers, added up for the annual report) and
`classifications.json` (Haiku's topic and region per article). `weekly.json` is kept
apart from `state.json` on purpose so the daily job never races the hourly run.
Nothing personal is committed: settings and state are ignored, and the checked-in
defaults are neutral.

## Rules that cost time to learn

- **Calendar dates are local; instants are UTC.** Tagged dates, "21 days", "30 days"
  and the once-a-day gate use local dates (`local_date`, `days_between`). The
  `updatedAfter` cursor and `tagged_at` are UTC-aware instants. Mixing them
  misfiles a run east of UTC.
- **Never send tag keys to Reader, only names.** A document's `tags` is keyed by the
  lower-cased key (`done`) with the real name inside (`DONE`); `bulk_update`
  replaces the whole list. Use `tracking.tag_names`.
- **Never overwrite a state file that can't be read.** `load_json` raises `StateError`
  for a corrupt file; silently starting over would forget every tagged item.
- **A real run writes to Reader** (tags on 25–28 items a day, `newsdigest-copy` on
  linked saved copies, report notes). Use `--dry-run` to look first; tests use a fake
  library (`tests/support.py`) and never touch Reader.
- **A fake Reader accepts things the real one refuses.** Reader answers 400 to a saved
  note without an author. After any change to what is sent to Reader, do one real
  delivery before calling it done.
- **Some environments refuse the network.** If you run this under a sandbox that
  blocks outgoing connections, anything calling Readwise, Anthropic or an RSS feed
  fails, and feedparser then quietly returns zero entries instead of erroring.
- **Notes are delivered to Reader, never to a file.** They are saved to **Later**
  and tagged `newsdigest` plus their own tag: not every account has a Reader Inbox,
  and Reader silently moves a save to the default location if the one asked for isn't
  enabled. Reader de-duplicates saved documents by address, so each note has its own
  made-up address (`readwise-news-digest.invalid/...`); a 200 reply means it already
  exists and is left alone. Suggested sites are recorded only after a new note is
  actually created, and never suggested twice. Notes this program saved are
  recognised by `source = readwise-news-digest` and left out of every count.
- **Reports count the whole library, not just the digest, and are never half-made.**
  A month is read from one `updatedAfter` listing (about a minute for a month); what
  counts is opened, saved-by-the-user or highlighted *in that month* (by
  `first_opened_at`, `created_at`, and highlight `created_at`), so the same code reports
  any past month. Topic and region are a fixed list labelled by Haiku (a profile's own
  topics get renamed, which would split a year of counts). Nothing is remembered until
  Reader has the note, so a failure is retried by the next daily run; a month still in
  progress is only previewed. The annual report waits for December's.
- **Personal choices live in `prefs.json`, never in the code.** The checked-in
  defaults (source tiers, report topics and regions) are deliberately generic.
- **Every silent failure needs a check in `health.py`.** The old app lost five
  months of clicks because a failed write was swallowed and nothing noticed.
  Errors notify; anything that can stop without an error (a job, a signal, a
  feed) gets a health check. Both jobs write a line to `run.log` on every run
  (`tagged=...` hourly, `daily ok|problem=...` daily) and read the other's.
- **👍/👎 are votes, not tags of your own** (`grading.VOTE_TAGS`). 👎 counts only
  through its document note, which reaches the scorer's prompt; it never feeds the
  numeric learning. Notes live in each snapshot (`notes`).
- **Notifications come from our own app, never a temp folder.** osascript's
  notifications are filed under Script Editor, shared with every script, so
  their style can't be set for this program alone. `bin/Readwise News Digest.app`
  (ad-hoc signed, registered with `lsregister`) posts through UserNotifications.
  macOS refuses that app outright ("Notifications are not allowed for this
  application", no prompt) when it sits in a temporary folder; from `bin/` it
  works whether started directly or with `open`.
- **Branch flow:** work lands on an integration branch after it has been checked by
  hand; the default branch gets a merge only when a feature is complete.

## Background jobs (launchd)

Two LaunchAgents: `com.readwisenewsdigest.run` (hourly at :07, `RunAtLoad`) and
`com.readwisenewsdigest.profile` (daily 03:00; the script itself rebuilds the profile
when a week has passed and makes the monthly/annual reports when they are due, so a
failed part is retried the next day). Both wrap the work in `caffeinate -i` so the Mac
doesn't idle-sleep mid-run; the Mac is **not** woken at night -- launchd runs a missed
job once when it wakes, and tagging happens on the first run of the day.

The checked-in files are templates (`launchd/*.plist.template`); `./install.sh` fills
in this folder's path and the venv's Python and writes the real
`com.readwisenewsdigest.*.plist` files (ignored by git).

**`ProgramArguments[0]` is always a compiled launcher, never a script or an
interpreter.** Two separate reasons:

1. macOS Tahoe's AppleSystemPolicy kills a user-directory shell script started
   directly by launchd (`OS_REASON_CODESIGNING`). A script listed as an *argument*
   is fine; a locally built, ad-hoc-signed binary is fine.
2. The name System Settings → Login Items & Extensions shows is the basename of
   `ProgramArguments[0]`, fixed at registration. A plist that starts with
   `/bin/bash` or `python3` is listed as "bash" or "python3". So
   `newsdigest-launcher.c` is compiled once per job under the job's own name
   (`readwise-news-digest-run`, `readwise-news-digest-profile`) and `exec()`s the
   rest unchanged. Never ship a generic name.

**After editing a template (or first install) run `./install.sh` and then
`./reload-launchers.sh`, not a plain reload.** Background Task Management appears to
key its cached name to the plist file's own identity; editing the file in place and
reloading (even rebooting) leaves the old name. The script recreates the
`~/Library/LaunchAgents/` symlink from scratch, then `bootout` + `bootstrap`.

`launchctl bootstrap` of LaunchAgents on Tahoe needs the calling app to have
**App Management** permission (System Settings → Privacy & Security); if it can't
be granted, log out and back in -- macOS re-bootstraps everything in
`~/Library/LaunchAgents/` at login.

Done means verified: after loading, the Login Items list shows
`readwise-news-digest-run` / `-profile`, and `launchctl kickstart` of the run job adds a
new line to `run.log`.
