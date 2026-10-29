import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import health
from tests.support import NOW, RunTestCase


def at(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


def hourly(text, tagged=0, problem=None):
    fields = {"tagged": str(tagged)}
    if problem:
        fields["problem"] = problem
    return (at(text), "hourly", fields)


def daily(text, problem=None):
    return (at(text), "daily", {"problem": problem} if problem else {})


def hourly_all_day(day, start=0, end=23, tagged_at=7):
    return [hourly(f"{day} {h:02d}:07", tagged=28 if h == tagged_at else 0)
            for h in range(start, end + 1)]


class ReadLogTests(unittest.TestCase):
    def test_tells_the_two_jobs_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.log"
            path.write_text(
                "2026-11-01 07:07 tagged=28 scored=300 graded=3 learned=1.0\n"
                "2026-11-01 08:07 tagged=0 scored=0 graded=3 learned=1.0 problem=readwise-api\n"
                "2026-11-02 03:00 daily ok\n"
                "2026-11-02 03:01 profile topics=14 notes=3235\n"
                "not a log line\n")
            rows = health.read_log(path)
        self.assertEqual([k for _, k, _ in rows], ["hourly", "hourly", "daily", "other"])
        self.assertEqual(rows[1][2]["problem"], "readwise-api")

    def test_a_missing_log_is_empty(self):
        self.assertEqual(health.read_log(Path("/nonexistent/run.log")), [])


class HourlyStoppedTests(unittest.TestCase):
    def test_fine_when_the_hourly_job_ran_since_the_last_daily_run(self):
        rows = [daily("2026-11-01 03:00"), *hourly_all_day("2026-11-01")]
        self.assertIsNone(health.hourly_job_stopped(rows))

    def test_first_daily_run_has_nothing_to_compare(self):
        self.assertIsNone(health.hourly_job_stopped([]))

    def test_woken_together_the_hourly_may_finish_first(self):
        rows = [hourly("2026-11-01 09:14"), daily("2026-11-01 09:15")]
        self.assertIsNone(health.hourly_job_stopped(rows))

    def test_says_so_when_the_hourly_job_stopped(self):
        rows = [*hourly_all_day("2026-11-01", end=10), daily("2026-11-02 03:00")]
        key, message = health.hourly_job_stopped(rows)
        self.assertEqual(key, "hourly-stopped")
        self.assertIn("1 November 10:07", message)

    def test_a_mac_switched_off_for_a_week_is_not_a_problem(self):
        rows = [daily("2026-11-01 03:00"), *hourly_all_day("2026-11-01")]
        self.assertIsNone(health.hourly_job_stopped(rows))   # next daily run is a week later


class DailyStoppedTests(unittest.TestCase):
    def test_fine_when_the_daily_job_ran_today(self):
        rows = [*hourly_all_day("2026-11-01"), daily("2026-11-02 03:00"),
                *hourly_all_day("2026-11-02", end=12)]
        self.assertIsNone(health.daily_job_stopped(rows, at("2026-11-02 12:07")))

    def test_waits_for_a_few_hourly_runs_after_three(self):
        rows = [*hourly_all_day("2026-11-01"), daily("2026-11-01 03:00"),
                *hourly_all_day("2026-11-02", end=4)]
        self.assertIsNone(health.daily_job_stopped(rows, at("2026-11-02 04:07")))

    def test_says_so_when_the_daily_job_has_not_run_today(self):
        rows = [daily("2026-11-01 03:00"), *hourly_all_day("2026-11-01"),
                *hourly_all_day("2026-11-02", end=6)]
        key, message = health.daily_job_stopped(rows, at("2026-11-02 06:07"))
        self.assertEqual(key, "daily-stopped")
        self.assertIn("1 November 03:00", message)

    def test_install_day_is_not_a_problem(self):
        rows = hourly_all_day("2026-11-02", start=14)
        self.assertIsNone(health.daily_job_stopped(rows, at("2026-11-02 23:07")))

    def test_mac_asleep_at_three_runs_it_on_wake(self):
        rows = [*hourly_all_day("2026-11-01", end=22), daily("2026-11-02 09:00"),
                *hourly_all_day("2026-11-02", start=9, end=12)]
        self.assertIsNone(health.daily_job_stopped(rows, at("2026-11-02 12:07")))


class PicksStoppedTests(unittest.TestCase):
    def test_fine_with_a_pick_today(self):
        rows = hourly_all_day("2026-11-01") + hourly_all_day("2026-11-02")
        self.assertIsNone(health.picks_stopped(rows))

    def test_one_quiet_day_is_not_enough(self):
        rows = hourly_all_day("2026-11-01") + hourly_all_day("2026-11-02", tagged_at=None)
        self.assertIsNone(health.picks_stopped(rows))

    def test_two_quiet_days_are(self):
        rows = (hourly_all_day("2026-11-01") + hourly_all_day("2026-11-02", tagged_at=None)
                + hourly_all_day("2026-11-03", tagged_at=None, end=9))
        key, message = health.picks_stopped(rows)
        self.assertEqual(key, "picks-stopped")
        self.assertIn("since 1 November", message)

    def test_days_the_mac_was_off_do_not_count(self):
        rows = hourly_all_day("2026-11-01") + hourly_all_day("2026-11-09", tagged_at=None)
        self.assertIsNone(health.picks_stopped(rows))


def entry(i, tagged, grade=None, grade_since=None):
    return {"id": f"e{i}", "tagged_date": tagged, "grade": grade,
            "grade_since": grade_since}


class GradesFrozenTests(unittest.TestCase):
    def test_too_early_to_tell(self):
        tracked = {"e1": entry(1, "2026-11-01")}
        self.assertIsNone(health.grades_frozen(tracked, "2026-11-10"))

    def test_fine_with_a_recent_change(self):
        tracked = {"e1": entry(1, "2026-10-01", 3, "2026-11-05")}
        self.assertIsNone(health.grades_frozen(tracked, "2026-11-10"))

    def test_says_so_after_fourteen_days_without_a_change(self):
        tracked = {"e1": entry(1, "2026-10-01", 3, "2026-10-20")}
        key, message = health.grades_frozen(tracked, "2026-11-03")
        self.assertEqual(key, "grades-frozen")
        self.assertIn("since 2026-10-20", message)

    def test_says_so_when_nothing_was_ever_graded(self):
        tracked = {"e1": entry(1, "2026-10-01")}
        self.assertEqual(health.grades_frozen(tracked, "2026-10-20")[0], "grades-frozen")


class LearningEmptyTests(unittest.TestCase):
    OLD = {f"e{i}": entry(i, "2026-10-01", 3) for i in range(3)}

    def test_says_so_when_old_grades_give_no_nudges(self):
        self.assertEqual(health.learning_empty(self.OLD, {"topics": {}}, "2026-11-05")[0],
                         "learning-empty")

    def test_fine_with_nudges(self):
        self.assertIsNone(health.learning_empty(self.OLD, {"topics": {"A": 0.2}},
                                                "2026-11-05"))

    def test_fine_while_grades_are_young(self):
        self.assertIsNone(health.learning_empty(self.OLD, {}, "2026-10-20"))


class MonthSummaryTests(unittest.TestCase):
    def test_counts_days_and_lists_problems(self):
        rows = (hourly_all_day("2026-11-01") + [daily("2026-11-02 03:00")]
                + [hourly("2026-11-02 08:07", problem="readwise-api"),
                   daily("2026-11-03 03:00", problem="hourly-stopped"),
                   hourly("2026-12-01 07:07")])
        text = health.month_summary(rows, "2026-11")
        self.assertIn("hourly job ran on 2 days", text)
        self.assertIn("daily job on 2 days", text)
        self.assertIn("hourly-stopped, readwise-api", text)

    def test_a_clean_month(self):
        rows = hourly_all_day("2026-11-01") + [daily("2026-11-02 03:00")]
        self.assertIn("No problems were reported", health.month_summary(rows, "2026-11"))


class HourlyJobAlarmTests(RunTestCase):
    def test_a_stopped_daily_job_is_notified_once_a_day_and_logged(self):
        stale = NOW - timedelta(days=2)
        self.paths.run_log.write_text(
            f"{stale:%Y-%m-%d} 03:00 daily ok\n"
            + "".join(f"{stale:%Y-%m-%d} {h:02d}:07 tagged=28 scored=0 graded=0 "
                      "learned=0.0\n" for h in range(4, 24)))
        for _ in range(4):          # four runs after 03:00 today
            self.go()
        alarms = [m for _, m in self.notices if "daily 03:00 job" in m]
        self.assertEqual(len(alarms), 1)
        self.assertIn("problem=daily-stopped", self.run_log()[-1])


if __name__ == "__main__":
    unittest.main()
