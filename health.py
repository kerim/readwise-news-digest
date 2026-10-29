"""Notice when the program has quietly stopped doing its job, and say so.

Errors already raise a notification (run.py, profile_builder.py). These checks
catch what raises no error: a background job that no longer runs, no articles
being picked, grades that stop changing, learning that produces nothing. Both
jobs write a line to run.log every time they run, and each reads the other's
lines. Every check returns (key, message) or None; the caller notifies.
"""

import re
from datetime import datetime, time, timedelta

from common import days_between

GRACE_HOURS = 2          # a job woken together with the other may finish first
DAILY_HOUR = 3           # the daily job's slot (launchd runs a missed one on wake)
HOURLY_RUNS_BEFORE_ALARM = 3   # hourly runs after 03:00 before the daily one is overdue
TAG_HOUR = 6             # by now the day's picks should have been tagged
QUIET_PICK_DAYS = 2      # days with runs but no picks before saying so
FROZEN_GRADE_DAYS = 14   # days with no grade changing before saying so
LEARNING_AGE_DAYS = 31   # by this age a graded item is being learned from

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}) (.*)$")


def read_log(path):
    """[(local naive datetime, kind, fields)] from run.log. kind is "hourly"
    (a run.py line), "daily" (profile_builder's line) or "other"."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    rows = []
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        rest = match[2]
        fields = dict(part.split("=", 1) for part in rest.split() if "=" in part)
        kind = ("hourly" if "tagged" in fields
                else "daily" if rest.startswith("daily") else "other")
        rows.append((datetime.strptime(match[1], "%Y-%m-%d %H:%M"), kind, fields))
    return rows


def _times(rows, kind):
    return [when for when, k, _ in rows if k == kind]


def _when(moment):
    return f"{moment.day} {moment:%B %H:%M}"


def hourly_job_stopped(rows):
    """The daily job's check: has the hourly job run since the daily job last
    did? (Run before the daily job writes its own line.)"""
    daily = _times(rows, "daily")
    if not daily:
        return None             # first daily run: nothing to compare with
    hourly = _times(rows, "hourly")
    last = max(hourly) if hourly else None
    if last is not None and last >= max(daily) - timedelta(hours=GRACE_HOURS):
        return None
    since = f"since {_when(last)}" if last else "at all"
    return ("hourly-stopped",
            f"The hourly job has not run {since}, so nothing is being picked "
            "or graded. Run ./reload-launchers.sh in the program's folder.")


def daily_job_stopped(rows, now):
    """The hourly job's check: once it has run a few times after 03:00 today,
    the daily job should have run today too. `rows` includes this run."""
    three = datetime.combine(now.date(), time(DAILY_HOUR))
    hourly = _times(rows, "hourly")
    if not hourly or min(hourly) >= three:
        return None             # installed today: no 03:00 to have missed
    if sum(1 for when in hourly if when >= three) < HOURLY_RUNS_BEFORE_ALARM:
        return None
    daily = _times(rows, "daily")
    if daily and max(daily).date() == now.date():
        return None
    since = f"since {_when(max(daily))}" if daily else "yet"
    return ("daily-stopped",
            f"The daily 03:00 job has not run {since}, so your profile and "
            "monthly reports are not being made. Run ./reload-launchers.sh in "
            "the program's folder.")


def picks_stopped(rows):
    """No articles tagged on QUIET_PICK_DAYS days on which the hourly job ran
    after TAG_HOUR. `rows` includes this run."""
    hourly = [(when, fields) for when, kind, fields in rows if kind == "hourly"]
    tagged = [when for when, fields in hourly if fields.get("tagged", "0") != "0"]
    last = max(tagged) if tagged else None
    quiet_days = {when.date() for when, _ in hourly
                  if when.hour >= TAG_HOUR and (last is None or when.date() > last.date())}
    if len(quiet_days) < QUIET_PICK_DAYS:
        return None
    since = f"since {last.day} {last:%B}" if last else "yet"
    return ("picks-stopped",
            f"No articles have been picked {since}. Your digest feeds may be "
            "returning nothing; run check_setup.py in the program's folder.")


def grades_frozen(tracked, today):
    """No grade has changed for FROZEN_GRADE_DAYS while picks are being made."""
    dates = [e["tagged_date"] for e in tracked.values()]
    if not dates or days_between(min(dates), today) < FROZEN_GRADE_DAYS:
        return None
    changes = [e["grade_since"] for e in tracked.values() if e.get("grade_since")]
    last = max(changes) if changes else None
    if last is not None and days_between(last, today) < FROZEN_GRADE_DAYS:
        return None
    since = f"since {last}" if last else "since the first picks"
    return ("grades-frozen",
            f"No article's grade has changed {since}. Either you have not "
            "opened, saved or archived any picks in that time, or the program "
            "can no longer see what you do in Reader.")


def learning_empty(tracked, weights, today):
    """Graded items old enough to learn from, but no nudges at all."""
    old = [e for e in tracked.values() if e.get("grade") is not None
           and days_between(e["tagged_date"], today) >= LEARNING_AGE_DAYS]
    if len(old) < 2 or any(weights.get(dim) for dim in
                           ("topics", "sources", "forms", "frames")):
        return None
    return ("learning-empty",
            f"{len(old)} graded articles are old enough to learn from, but the "
            "learning has produced no score adjustments.")


def hourly_checks(rows, now, tracked, weights, today):
    """Everything the hourly job checks, as [(key, message)]."""
    found = [daily_job_stopped(rows, now), picks_stopped(rows),
             grades_frozen(tracked, today), learning_empty(tracked, weights, today)]
    return [f for f in found if f]


def month_summary(rows, month):
    """One sentence on how the jobs ran in `month` (YYYY-MM), for the report."""
    in_month = [(w, k, f) for w, k, f in rows if f"{w:%Y-%m}" == month]
    hourly_days = {w.date() for w, k, _ in in_month if k == "hourly"}
    daily_days = {w.date() for w, k, _ in in_month if k == "daily"}
    problems = sorted({p for _, _, f in in_month
                       for p in f.get("problem", "").split(",") if p})
    def days(n):
        return f"{n} day" if n == 1 else f"{n} days"
    text = (f"How the program ran: the hourly job ran on {days(len(hourly_days))} "
            f"and the daily job on {days(len(daily_days))} this month. ")
    if problems:
        return text + "Problems reported: " + ", ".join(problems) + "."
    return text + "No problems were reported."
