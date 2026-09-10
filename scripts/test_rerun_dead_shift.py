#!/usr/bin/env python3
"""
Tests for rerun-dead-shift.py.

Run: python3 scripts/test_rerun_dead_shift.py

Same shape as test_notify_shift_outcome.py: the real script runs as a
subprocess against real HTTP servers standing in for GitHub and for Slack.
The two questions worth answering here are "does it re-run the runs it should
and only those" and "does it ever announce a re-run that didn't happen", and
the second one is only interesting when a real request really is refused.

The one thing these cannot cover: whether GitHub lets the workflow's own
GITHUB_TOKEN re-run a shift, and whether the second attempt's completion comes
back round to the watcher. That needs a shift to fail on `main`.
"""

import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "rerun-dead-shift.py"

FAILED_RUN = {
    "id": 33819369404,
    "name": "Richmond",
    "conclusion": "failure",
    "event": "schedule",
    "head_branch": "main",
    "run_attempt": 1,
    "run_started_at": "2026-09-03T23:50:00Z",
    "updated_at": "2026-09-03T23:59:01Z",
    "html_url": "https://github.com/committed-nightly/ops/actions/runs/33819369404",
}


class FakeApis(BaseHTTPRequestHandler):
    """Slack on /api/*, GitHub on /repos/*, told what to say by each test."""

    slack_status = 200
    slack_body = '{"ok": true, "ts": "1788450759.477099"}'
    github_status = 201
    github_body = ""
    slack_received = []
    github_received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        record = {
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": json.loads(raw) if raw else {},
        }
        if self.path.startswith("/repos/"):
            FakeApis.github_received.append(record)
            status, body = FakeApis.github_status, FakeApis.github_body
        else:
            FakeApis.slack_received.append(record)
            status, body = FakeApis.slack_status, FakeApis.slack_body

        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass


class RerunTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeApis)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeApis.slack_status = 200
        FakeApis.slack_body = '{"ok": true, "ts": "1788450759.477099"}'
        FakeApis.github_status = 201
        FakeApis.github_body = ""
        FakeApis.slack_received = []
        FakeApis.github_received = []

    def run_script(self, run, **overrides):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_INCIDENTS": "C0BUB0WCBJ5",
            "SLACK_API_BASE": f"{self.base}/api",
            "GITHUB_API_BASE": self.base,
            "GITHUB_TOKEN": "ghs-test",
            "GITHUB_REPOSITORY": "committed-nightly/ops",
            "INCIDENT_TS": "1788450759.477099",
            "WORKFLOW_RUN": run if isinstance(run, str) else json.dumps(run),
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def slack_text(self):
        self.assertEqual(len(FakeApis.slack_received), 1)
        return FakeApis.slack_received[0]["body"]["text"]

    # --- the run it exists for ---------------------------------------------

    def test_first_failure_is_rerun(self):
        result = self.run_script(FAILED_RUN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(FakeApis.github_received), 1)
        sent = FakeApis.github_received[0]
        self.assertEqual(
            sent["path"],
            "/repos/committed-nightly/ops/actions/runs/33819369404/rerun",
        )
        self.assertEqual(sent["auth"], "Bearer ghs-test")
        self.assertIn("Re-running", self.slack_text())
        self.assertIn("attempt 2", self.slack_text())

    def test_the_reply_is_threaded_under_the_incident(self):
        self.run_script(FAILED_RUN)
        body = FakeApis.slack_received[0]["body"]
        self.assertEqual(body["thread_ts"], "1788450759.477099")
        self.assertEqual(body["channel"], "C0BUB0WCBJ5")

    def test_no_incident_ts_still_posts(self):
        """A notifier that fell over must not also suppress the re-run."""
        result = self.run_script(FAILED_RUN, INCIDENT_TS=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("thread_ts", FakeApis.slack_received[0]["body"])
        self.assertEqual(len(FakeApis.github_received), 1)

    # --- the runs it refuses to touch ---------------------------------------

    def test_second_attempt_is_not_rerun(self):
        result = self.run_script({**FAILED_RUN, "run_attempt": 2})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(FakeApis.github_received, [])
        self.assertIn("attempt 2", self.slack_text())
        self.assertIn("Not re-running", self.slack_text())

    def test_a_long_chain_of_attempts_is_not_rerun(self):
        self.run_script({**FAILED_RUN, "run_attempt": 7})
        self.assertEqual(FakeApis.github_received, [])

    def test_cancelled_is_not_rerun(self):
        result = self.run_script({**FAILED_RUN, "conclusion": "cancelled"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(FakeApis.github_received, [])
        self.assertIn("on purpose", self.slack_text())

    def test_timed_out_is_not_rerun(self):
        self.run_script({**FAILED_RUN, "conclusion": "timed_out"})
        self.assertEqual(FakeApis.github_received, [])
        self.assertIn("time budget", self.slack_text())

    def test_conclusions_nobody_enumerated_are_not_rerun(self):
        """New GitHub conclusions must default to leaving it alone."""
        for conclusion in ("startup_failure", "stale", "action_required", "neutral"):
            with self.subTest(conclusion=conclusion):
                self.setUp()
                result = self.run_script({**FAILED_RUN, "conclusion": conclusion})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(FakeApis.github_received, [])
                self.assertIn("Not re-running", self.slack_text())

    def test_null_conclusion_is_not_rerun(self):
        self.run_script({**FAILED_RUN, "conclusion": None})
        self.assertEqual(FakeApis.github_received, [])
        self.assertIn("no conclusion", self.slack_text())

    def test_every_declined_reply_carries_the_manual_command(self):
        for conclusion in ("cancelled", "timed_out", "startup_failure"):
            with self.subTest(conclusion=conclusion):
                self.setUp()
                self.run_script({**FAILED_RUN, "conclusion": conclusion})
                self.assertIn(
                    "gh run rerun 33819369404 --repo committed-nightly/ops",
                    self.slack_text(),
                )

    # --- the switch ----------------------------------------------------------

    def test_off_switch_stops_the_rerun_but_not_the_reply(self):
        for value in ("off", "OFF", " off ", "0", "false", "no"):
            with self.subTest(value=value):
                self.setUp()
                result = self.run_script(FAILED_RUN, SHIFT_AUTO_RERUN=value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(FakeApis.github_received, [])
                self.assertIn("switched off", self.slack_text())

    def test_unset_switch_means_on(self):
        result = self.run_script(FAILED_RUN, SHIFT_AUTO_RERUN=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(FakeApis.github_received), 1)

    def test_anything_that_is_not_off_means_on(self):
        self.run_script(FAILED_RUN, SHIFT_AUTO_RERUN="yes please")
        self.assertEqual(len(FakeApis.github_received), 1)

    # --- staying quiet -------------------------------------------------------

    def test_a_clean_run_says_nothing(self):
        """The watcher didn't post, so there is no thread to reply in."""
        result = self.run_script({**FAILED_RUN, "conclusion": "success"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(FakeApis.slack_received, [])
        self.assertEqual(FakeApis.github_received, [])

    def test_a_recovered_rerun_says_nothing(self):
        result = self.run_script(
            {**FAILED_RUN, "conclusion": "success", "run_attempt": 2}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(FakeApis.slack_received, [])

    def test_skipped_says_nothing(self):
        self.run_script({**FAILED_RUN, "conclusion": "skipped"})
        self.assertEqual(FakeApis.slack_received, [])

    def test_a_quiet_run_needs_no_credentials(self):
        result = self.run_script(
            {**FAILED_RUN, "conclusion": "success"},
            SLACK_BOT_TOKEN=None,
            GITHUB_TOKEN=None,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    # --- never claiming a re-run that didn't happen ---------------------------

    def test_github_refusing_is_reported_not_announced(self):
        """403 here is the missing actions:write permission, the likeliest fault."""
        FakeApis.github_status = 403
        FakeApis.github_body = '{"message": "Resource not accessible by integration"}'
        result = self.run_script(FAILED_RUN)
        self.assertEqual(result.returncode, 1)
        text = self.slack_text()
        self.assertIn("could not", text)
        self.assertIn("403", text)
        self.assertIn("Resource not accessible by integration", text)
        self.assertNotIn("Re-running", text)
        self.assertIn("gh run rerun 33819369404", text)

    def test_unreachable_github_is_reported_not_announced(self):
        result = self.run_script(FAILED_RUN, GITHUB_API_BASE="http://127.0.0.1:1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not reach GitHub", self.slack_text())
        self.assertNotIn("Re-running", self.slack_text())

    def test_github_conflict_is_reported(self):
        FakeApis.github_status = 409
        FakeApis.github_body = '{"message": "This run is already in progress"}'
        result = self.run_script(FAILED_RUN)
        self.assertEqual(result.returncode, 1)
        self.assertIn("409", self.slack_text())

    # --- failing loudly ------------------------------------------------------

    def test_slack_refusing_with_http_200_is_a_failure(self):
        FakeApis.slack_body = '{"ok": false, "error": "channel_not_found"}'
        result = self.run_script(FAILED_RUN)
        self.assertEqual(result.returncode, 1)
        self.assertIn("channel_not_found", result.stderr)

    def test_slack_non_json_body_is_a_failure(self):
        FakeApis.slack_body = "<html>proxy says no</html>"
        result = self.run_script(FAILED_RUN)
        self.assertEqual(result.returncode, 1)
        self.assertIn("non-JSON", result.stderr)

    def test_missing_github_token_is_a_failure(self):
        result = self.run_script(FAILED_RUN, GITHUB_TOKEN=None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("GITHUB_TOKEN", result.stderr)
        self.assertEqual(FakeApis.github_received, [])

    def test_missing_slack_channel_is_a_failure(self):
        result = self.run_script({**FAILED_RUN, "conclusion": "cancelled"},
                                 SLACK_INCIDENTS=None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("SLACK_INCIDENTS", result.stderr)

    def test_malformed_event_is_a_failure(self):
        result = self.run_script("not json at all")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not valid JSON", result.stderr)

    def test_missing_event_is_a_failure(self):
        result = self.run_script(FAILED_RUN, WORKFLOW_RUN=None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("WORKFLOW_RUN", result.stderr)

    # --- degrading rather than crashing --------------------------------------

    def test_a_sparse_event_still_decides(self):
        result = self.run_script({"conclusion": "failure", "id": 1})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(FakeApis.github_received), 1)
        self.assertIn("The shift", self.slack_text())

    def test_a_non_integer_attempt_is_not_rerun(self):
        result = self.run_script({**FAILED_RUN, "run_attempt": "2"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(FakeApis.github_received, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
