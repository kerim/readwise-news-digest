import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GENERIC = {"bash", "sh", "zsh", "env", "python", "python3", "node", "uv",
           "caffeinate"}

JOBS = {
    "com.readwisenewsdigest.run": {
        "name": "readwise-news-digest-run", "script": "run.py"},
    "com.readwisenewsdigest.profile": {
        "name": "readwise-news-digest-profile", "script": "profile_builder.py"},
}


PYTHON = "/venv/bin/python"


def load(label):
    """The job file as ./install.sh writes it, for a project in ROOT."""
    template = (ROOT / "launchd" / f"{label.rsplit('.', 1)[1]}.plist.template").read_text()
    text = template.replace("@PROJECT_DIR@", str(ROOT)).replace("@PYTHON@", PYTHON)
    return plistlib.loads(text.encode())


class PlistTests(unittest.TestCase):
    def test_each_job_starts_with_a_launcher_named_for_the_job(self):
        # macOS lists a background job under the basename of ProgramArguments[0];
        # a generic name ("bash", "python3") says nothing about what it is.
        for label, job in JOBS.items():
            first = Path(load(label)["ProgramArguments"][0])
            self.assertEqual(first.name, job["name"], label)
            self.assertNotIn(first.name, GENERIC)
            self.assertEqual(first.parent, ROOT / "bin", label)

    def test_the_label_matches_the_file_name(self):
        for label in JOBS:
            self.assertEqual(load(label)["Label"], label)

    def test_the_real_command_follows_the_launcher_and_exists(self):
        for label, job in JOBS.items():
            args = load(label)["ProgramArguments"]
            self.assertEqual(args[1:4], ["/usr/bin/caffeinate", "-i", PYTHON])
            self.assertEqual(Path(args[4]), ROOT / job["script"])
            self.assertTrue((ROOT / job["script"]).exists())

    def test_jobs_run_from_the_project_folder_and_log_there(self):
        for label in JOBS:
            plist = load(label)
            self.assertEqual(Path(plist["WorkingDirectory"]), ROOT)
            self.assertEqual(plist["StandardOutPath"], plist["StandardErrorPath"])
            self.assertTrue(plist["StandardOutPath"].startswith(str(ROOT)))

    def test_the_run_job_is_hourly_and_runs_at_login(self):
        plist = load("com.readwisenewsdigest.run")
        self.assertEqual(plist["StartCalendarInterval"], {"Minute": 7})
        self.assertTrue(plist["RunAtLoad"])

    def test_the_profile_job_runs_daily_so_a_failed_week_is_retried_tomorrow(self):
        # The script does the "weekly" part itself (profile every 7 days, feed
        # suggestions when due); the schedule must not be weekly or a failure
        # would wait a week.
        plist = load("com.readwisenewsdigest.profile")
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 3, "Minute": 0})
        self.assertNotIn("RunAtLoad", plist)

    def test_the_templates_leave_no_machine_specific_path_behind(self):
        for label in JOBS:
            text = (ROOT / "launchd" / f"{label.rsplit('.', 1)[1]}.plist.template").read_text()
            self.assertNotIn("/Users/", text)
            self.assertNotIn("/opt/homebrew", text.split("<key>EnvironmentVariables")[0])
            self.assertIn("@PROJECT_DIR@", text)
            self.assertIn("@PYTHON@", text)

    def test_the_build_script_builds_both_launchers(self):
        text = (ROOT / "build-launchers.sh").read_text()
        for job in JOBS.values():
            self.assertIn(job["name"], text)

    def test_the_reload_script_handles_both_jobs(self):
        text = (ROOT / "reload-launchers.sh").read_text()
        for label in JOBS:
            self.assertIn(label, text)


@unittest.skipUnless(shutil.which("clang"), "clang not installed")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.binary = Path(self._tmp.name) / "readwise-news-digest-run"
        subprocess.run(["clang", "-O2", "-Wall", "-Wextra", "-Werror",
                        "-o", str(self.binary), str(ROOT / "newsdigest-launcher.c")],
                       check=True, capture_output=True)

    def test_it_runs_the_command_after_it_with_the_arguments_untouched(self):
        out = subprocess.run([str(self.binary), "/bin/echo", "a", "b c"],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout, "a b c\n")

    def test_the_command_runs_under_the_launchers_name(self):
        # `ps` shows the command line a process was started with, so it agrees
        # with what Login Items shows for the job.
        # "; true" stops sh replacing itself with ps, so $$ is still the shell.
        out = subprocess.run(
            [str(self.binary), "/bin/sh", "-c", "ps -o args= -p $$; true"],
            capture_output=True, text=True)
        if "not permitted" in out.stdout + out.stderr:
            self.skipTest("ps is not allowed in this sandbox")
        self.assertTrue(out.stdout.startswith("readwise-news-digest-run"),
                        out.stdout)

    def test_no_command_is_a_usage_error(self):
        out = subprocess.run([str(self.binary)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("usage", out.stderr)

    def test_a_missing_command_fails_clearly(self):
        out = subprocess.run([str(self.binary), "/no/such/program"],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 127)
        self.assertIn("cannot exec", out.stderr)


if __name__ == "__main__":
    unittest.main()
