import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from check_isolation_update_due import update_due


class UpdateDueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "summary.json"
        self.now = datetime(2026, 10, 27, 3, 20, tzinfo=timezone.utc)

    def timestamp(self, value):
        self.path.write_text(json.dumps({"created_at": value}), encoding="utf-8")

    def due(self, event="schedule"):
        return update_due(self.path, event, self.now)[0]

    def test_recent_output_skips_scheduled_analysis(self):
        self.timestamp((self.now - timedelta(minutes=15)).isoformat())
        self.assertFalse(self.due())

    def test_refresh_becomes_due_at_the_age_boundary(self):
        for age, expected in [(timedelta(minutes=50) - timedelta(seconds=1), False),
                              (timedelta(minutes=50), True),
                              (timedelta(hours=4), True)]:
            with self.subTest(age=age):
                self.timestamp((self.now - age).isoformat())
                self.assertEqual(self.due(), expected)

    def test_manual_run_always_refreshes_recent_output(self):
        self.timestamp(self.now.isoformat())
        self.assertTrue(self.due("workflow_dispatch"))

    def test_missing_output_refreshes(self):
        self.assertTrue(self.due())

    def test_invalid_output_does_not_suppress_recovery(self):
        for raw in ['bad json', '{}', '[]', '{"created_at":null}', '{"created_at":"bad"}']:
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                self.assertTrue(self.due())

    def test_future_timestamp_does_not_suppress_recovery(self):
        self.timestamp((self.now + timedelta(hours=2)).isoformat())
        self.assertTrue(self.due())

    def test_equivalent_timezone_offsets_have_the_same_age(self):
        self.timestamp("2026-10-27T12:20:00+10:00")
        self.assertTrue(self.due())
        self.timestamp("2026-10-27T13:10:00+10:00")
        self.assertFalse(self.due())

    def test_legacy_naive_utc_timestamp_is_supported(self):
        self.timestamp("2026-10-27T03:10:00")
        self.assertFalse(self.due())

    def test_command_writes_the_output_used_by_github_actions(self):
        self.timestamp(datetime.now(timezone.utc).isoformat())
        output = Path(self.directory.name) / "github-output"
        summary = Path(self.directory.name) / "step-summary"
        for event, expected in [("schedule", "false"), ("workflow_dispatch", "true")]:
            with self.subTest(event=event):
                output.write_text("", encoding="utf-8")
                env = dict(os.environ, GITHUB_EVENT_NAME=event,
                           GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(summary))
                subprocess.run([sys.executable, str(Path(__file__).with_name("check_isolation_update_due.py")),
                                "--summary", str(self.path)], env=env, check=True, capture_output=True)
                self.assertEqual(output.read_text(encoding="utf-8"), f"run_required={expected}\n")
        self.assertIn("Isolation update check", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
