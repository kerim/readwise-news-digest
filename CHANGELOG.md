# Changelog

## 1.2.0 — 2026-10-29

### Added

- **👍 on any article counts**, not only on picks: a way to point the digest at
  subjects it doesn't pick yet. Once a day the program looks up every Reader
  article tagged 👍 that isn't one of its picks. The scoring AI is shown them as
  interests (so similar articles rank higher even when they fit no profile topic),
  their sites get a boost, and the weekly profile rebuild sees them first. Each
  counts for 120 days after the 👍.

## 1.1.0 — 2026-10-29

### Added

- **Vote with 👍 and 👎.** Tag a pick (or your saved copy of it) 👍 or 👎.
  - 👍 grades the article 5, counts double in the learning, and counts straight
    away instead of after a month. It also gives the article's topic, site, type and
    angle a direct boost from the next daily pick, fading by half each month.
  - 👎 counts only with a reason, written in the article's **document note**. The
    scoring AI reads your noted 👎s and 👍s at every daily pick, as rules for
    similar articles ("an event announcement, not an article"). A 👎 without a note
    changes nothing, and a 👎 never lowers the site or topic by itself.
- **Warnings when the program quietly stops working**, not only when something
  fails. The hourly and daily jobs check on each other, and you are told when no
  articles have been picked for two days, when no grade has changed for 14 days, or
  when the learning produces nothing. Each monthly report ends with a line on how
  the jobs ran.
- **Notifications under their own name.** They now come from a small
  "Readwise News Digest" app that `./install.sh` builds, so you can set them to
  stay on screen (System Settings → Notifications → Readwise News Digest →
  Persistent). `check_setup.py` reports whether the app is there.
- A suggested digest view in the README that shows only today's unopened picks.

### Changed

- **Learning starts at once.** There is no longer a minimum of 50 graded articles
  before scores are adjusted; your interest profile sets the starting scores.
- `run.log` lines end in `learned=<n>` instead of `learned=<n>/50`, and the daily
  job adds a `daily ok` (or `daily problem=…`) line.

### Fixed

- Running `./install.sh` again after an update left both background jobs unable to
  start until they were reloaded by hand. It now reloads jobs that are already
  loaded.
- A 👎 tag (like any tag of your own) used to grade an article 5, the strongest
  "liked it". Votes are now handled on their own.

After updating, run `./install.sh` again. It builds the notification app (macOS may
ask you to allow its notifications) and, since your jobs are already loaded,
reloads them itself: rebuilding changes the jobs' launcher programs, and macOS
refuses to start them until they are reloaded.

## 1.0.0 — 2026-10-27

First public release.
