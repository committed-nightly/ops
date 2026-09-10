#!/usr/bin/env python3
"""
Tests for notify-shift-outcome.py.

Run: python3 scripts/test_notify_shift_outcome.py

These run the real script as a subprocess against a real HTTP server standing
in for Slack, rather than importing it and mocking urllib. The two things most
likely to be wrong here are "does it actually send the request" and "does it
notice when Slack says no while saying HTTP 200", and neither survives being
mocked out.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "notify-shift-outcome.py"

FAILED_RUN = {
    "id": 33769793117,
    "name": "Richmond",
    "conclusion": "failure",
    "event": "workflow_dispatch",
    "head_branch": "main",
    "run_attempt": 1,
    "run_started_at": "2026-09-03T14:56:00Z",
    "updated_at": "2026-09-03T14:59:06Z",
    "html_url": "https://github.com/committed-nightly/ops/actions/runs/33769793117",
}


class FakeSlack(BaseHTTPRequestHandler):
    """Answers chat.postMessage with whatever the current test told it to."""

    status = 200
    body = '{"ok": true, "ts": "1788450759.477099"}'
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        FakeSlack.received.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": json.loads(raw),
            }
        )
        payload = FakeSlack.body.encode("utf-8")
        self.send_response(FakeSlack.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class NotifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeSlack)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeSlack.status = 200
        FakeSlack.body = '{"ok": true, "ts": "1788450759.477099"}'
        FakeSlack.received = []

    def run_script(self, run, **overrides):
        env = {
            "PATH": os.environ.get("PATH", ""),
            "SLACK_BOT_TOKEN": "xoxb-test",
            "SLACK_INCIDENTS": "C0BUB0WCBJ5",
            "SLACK_API_BASE": self.base,
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

    # --- what gets reported -------------------------------------------------

    def test_failed_run_is_reported(self):
        result = self.run_script(FAILED_RUN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(FakeSlack.received), 1)
        sent = FakeSlack.received[0]["body"]
        self.assertEqual(sent["channel"], "C0BUB0WCBJ5")
        self.assertEqual(FakeSlack.received[0]["auth"], "Bearer xoxb-test")
        self.assertIn("Richmond", sent["text"])
        self.assertIn("did not complete", sent["text"])
        self.assertIn("33769793117", sent["text"])
        self.assertIn("workflow_dispatch", sent["text"])
        self.assertIn("3m 6s", sent["text"])

    def test_cancelled_and_timed_out_are_reported(self):
        for conclusion in ("cancelled", "timed_out"):
            with self.subTest(conclusion=conclusion):
                FakeSlack.received = []
                result = self.run_script({**FAILED_RUN, "conclusion": conclusion})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(FakeSlack.received), 1)
                self.assertIn(conclusion, FakeSlack.received[0]["body"]["text"])

    def test_unlisted_conclusion_is_reported(self):
        """The denylist exists so conclusions nobody enumerated still land."""
        for conclusion in ("startup_failure", "stale", "action_required", "neutral"):
            with self.subTest(conclusion=conclusion):
                FakeSlack.received = []
                result = self.run_script({**FAILED_RUN, "conclusion": conclusion})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(FakeSlack.received), 1)

    def test_null_conclusion_is_reported(self):
        result = self.run_script({**FAILED_RUN, "conclusion": None})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no conclusion", FakeSlack.received[0]["body"]["text"])

    # --- what stays quiet ---------------------------------------------------

    def test_clean_success_says_nothing(self):
        result = self.run_script({**FAILED_RUN, "conclusion": "success"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(FakeSlack.received, [])

    def test_skipped_says_nothing(self):
        result = self.run_script({**FAILED_RUN, "conclusion": "skipped"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(FakeSlack.received, [])

    def test_success_on_a_retry_closes_the_loop(self):
        result = self.run_script(
            {**FAILED_RUN, "conclusion": "success", "run_attempt": 2}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(FakeSlack.received), 1)
        self.assertIn("recovered on attempt 2", FakeSlack.received[0]["body"]["text"])

    # --- handing the thread on ----------------------------------------------

    def test_the_message_ts_reaches_the_next_step(self):
        """rerun-dead-shift.py replies in this thread, so it needs the ts."""
        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as handle:
            output = handle.name
        try:
            result = self.run_script(FAILED_RUN, GITHUB_OUTPUT=output)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                Path(output).read_text(), "ts=1788450759.477099\n"
            )
        finally:
            os.unlink(output)

    def test_a_quiet_run_writes_no_output(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as handle:
            output = handle.name
        try:
            result = self.run_script(
                {**FAILED_RUN, "conclusion": "success"}, GITHUB_OUTPUT=output
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(Path(output).read_text(), "")
        finally:
            os.unlink(output)

    def test_an_unwritable_output_file_does_not_lose_the_incident(self):
        result = self.run_script(FAILED_RUN, GITHUB_OUTPUT="/nope/nowhere.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(FakeSlack.received), 1)

    # --- failing loudly -----------------------------------------------------

    def test_slack_refusing_with_http_200_is_a_failure(self):
        """The one that matters: {"ok": false} arrives as a 200."""
        FakeSlack.body = '{"ok": false, "error": "channel_not_found"}'
        result = self.run_script(FAILED_RUN)
        self.assertEqual(result.returncode, 1)
        self.assertIn("channel_not_found", result.stderr)

    def test_slack_http_error_is_a_failure(self):
        FakeSlack.status = 500
        FakeSlack.body = "upstream is having a day"
        result = self.run_script(FAILED_RUN)
        self.assertEqual(result.returncode, 1)

    def test_slack_non_json_body_is_a_failure(self):
        FakeSlack.body = "<html>proxy says no</html>"
        result = self.run_script(FAILED_RUN)
        self.assertEqual(result.returncode, 1)
        self.assertIn("non-JSON", result.stderr)

    def test_unreachable_slack_is_a_failure(self):
        result = self.run_script(FAILED_RUN, SLACK_API_BASE="http://127.0.0.1:1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("could not reach Slack", result.stderr)

    def test_missing_token_is_a_failure(self):
        result = self.run_script(FAILED_RUN, SLACK_BOT_TOKEN=None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("SLACK_BOT_TOKEN", result.stderr)
        self.assertEqual(FakeSlack.received, [])

    def test_missing_channel_is_a_failure(self):
        result = self.run_script(FAILED_RUN, SLACK_INCIDENTS=None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("SLACK_INCIDENTS", result.stderr)

    def test_missing_credentials_do_not_fail_a_quiet_run(self):
        """No credentials and nothing to say is not an error worth going red for."""
        result = self.run_script(
            {**FAILED_RUN, "conclusion": "success"}, SLACK_BOT_TOKEN=None
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_malformed_event_is_a_failure(self):
        result = self.run_script("not json at all")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not valid JSON", result.stderr)

    def test_non_object_event_is_a_failure(self):
        result = self.run_script("[1, 2, 3]")
        self.assertEqual(result.returncode, 1)

    def test_missing_event_is_a_failure(self):
        result = self.run_script(FAILED_RUN, WORKFLOW_RUN=None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("WORKFLOW_RUN", result.stderr)

    # --- degrading rather than crashing -------------------------------------

    def test_a_sparse_event_still_reports(self):
        """Missing fields cost a detail, never the message."""
        result = self.run_script({"conclusion": "failure"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("A shift", FakeSlack.received[0]["body"]["text"])

    def test_unparseable_timestamps_cost_only_the_duration(self):
        result = self.run_script(
            {**FAILED_RUN, "run_started_at": "not a date", "updated_at": "nor this"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        text = FakeSlack.received[0]["body"]["text"]
        self.assertIn("33769793117", text)
        self.assertNotIn("took", text)

    def test_duration_formats_hours(self):
        result = self.run_script(
            {
                **FAILED_RUN,
                "run_started_at": "2026-09-03T14:00:00Z",
                "updated_at": "2026-09-03T16:04:09Z",
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2h 4m 9s", FakeSlack.received[0]["body"]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
