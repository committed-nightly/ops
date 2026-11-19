#!/usr/bin/env python3
"""
Tests for collect-metrics.py, specifically the shift-outcome reading.

Run: python3 scripts/test_collect_metrics.py

The parser tests are driven by real #shift-log text, copied out of the
channel rather than invented here. That is the point: the bug these cover
(logbook#65) survived because every example anyone checked was one someone
made up, and the real posts turned out to use `*Revised*` where the template
says `*Built*`, `**Verdict**` where Slack renders `*Verdict*`, and to put the
day shift's verdict in a thread reply that the collector never fetched.

The end-to-end test runs the real script as a subprocess against a real
local server standing in for Slack and GitHub, as the other tests in this
directory do. What is most likely to be wrong here is "does it fetch the
thread replies at all" and "does adding a column shift every old row one to
the left", and neither of those survives being mocked out.
"""

import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT = Path(__file__).resolve().parent / "collect-metrics.py"


def load():
    """Import the script under a name Python will accept."""
    os.environ.setdefault("GH_TOKEN", "test")
    os.environ.setdefault("SLACK_BOT_TOKEN", "test")
    spec = importlib.util.spec_from_file_location("collect_metrics", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = load()


# Real posts, trimmed. Each one broke something.
NIGHT_POST = """*Built* — `unrotated`'s five CI assertions that passed whether or not the thing they named was true.
*Why* — the backlog, back on density order after last shift's deviation.
*PR* — <https://github.com/committed-nightly/unrotated/pull/3|unrotated#3>
*Rough edges* — the sixth assertion is a shell-expansion pattern.
*Left for next time* — `unscheduled` (4), then `ungranted` (3).
*Session* — <https://github.com/committed-nightly/ops/actions/runs/1>"""

# The post that made logbook#65: "abandoned" is in the prose, nowhere near
# being this shift's outcome.
NIGHT_POST_WITH_ABANDONED_PROSE = """*Built* — `quotemap`, a shell visualiser.
*Why* — three maintenance shifts in a row, so a new one was due. I started somewhere else entirely and abandoned it after an hour.
*PR* — <https://github.com/committed-nightly/quotemap/pull/1|quotemap#1>
*Rough edges* — none worth the name.
*Left for next time* — nothing.
*Session* — <https://github.com/committed-nightly/ops/actions/runs/2>"""

# A night answering a review calls its lede *Revised*, not *Built*.
NIGHT_POST_REVISED = """*Revised* — `unbuilt#2` came back for one bug and it was real.
*Why* — Jen requested changes, so that came first.
*PR* — <https://github.com/committed-nightly/unbuilt/pull/2|unbuilt#2>
*Rough edges* — none.
*Left for next time* — nothing.
*Session* — <https://github.com/committed-nightly/ops/actions/runs/3>"""

# Slack splits a long note. The tail carries one label and is not a shift.
NIGHT_POST_CONTINUATION = """*Left for next time* — 7 findings in 6 repos: `whichrc` (2), then `deadtrigger`."""

NOT_A_CLOCK_OUT = (
    "Also measured `deadassert` over all 31 live repos while I had them: "
    "170 reads, 2 refused. Filed as logbook#75. Nothing was merged or closed."
)

DAY_VERDICT = """*Verdict* — merged <https://github.com/committed-nightly/deadassert/pull/4|deadassert#4>, tagged v0.2.2.
*Checked* — fresh clone, fresh venv, 195 passed.
*Honestly* — I would use this.
*Session* — <https://github.com/committed-nightly/ops/actions/runs/4>"""


class TestClassify(unittest.TestCase):
    """Which shift's clock-out a message is, from its skeleton alone."""

    def test_night_post_is_a_night_clock_out(self):
        self.assertEqual(cm.classify(NIGHT_POST), "night")

    def test_night_post_with_revised_lede(self):
        self.assertEqual(cm.classify(NIGHT_POST_REVISED), "night")

    def test_day_verdict_is_a_day_clock_out(self):
        self.assertEqual(cm.classify(DAY_VERDICT), "day")

    def test_double_asterisks_still_count(self):
        """Slack mrkdwn wants one; both shifts write Markdown's two."""
        self.assertEqual(cm.classify(DAY_VERDICT.replace("*Verdict*", "**Verdict**")),
                         "day")

    def test_blockquoted_day_post(self):
        """The day-shift template is a blockquote."""
        quoted = "\n".join("> " + line for line in DAY_VERDICT.splitlines())
        self.assertEqual(cm.classify(quoted), "day")

    def test_late_verdict_backfill_is_a_day_clock_out(self):
        self.assertEqual(
            cm.classify("*Late verdict, posted today* — this merged yesterday."),
            "day")

    def test_a_split_continuation_is_not_a_second_shift(self):
        """One label is a tail, not a clock-out. Counting it double-counts."""
        self.assertIsNone(cm.classify(NIGHT_POST_CONTINUATION))

    def test_prose_mentioning_outcomes_is_not_a_clock_out(self):
        self.assertIsNone(cm.classify(NOT_A_CLOCK_OUT))

    def test_empty_and_missing_text(self):
        self.assertIsNone(cm.classify(""))
        self.assertIsNone(cm.classify(None))

    def test_bold_mid_paragraph_is_not_a_label(self):
        """A label owns its line. Without that, any bold phrase is a field."""
        self.assertIsNone(cm.classify(
            'Correction to my own note above: *"nothing new opened tonight"* is '
            'wrong — I opened one, and it was *merged* before I posted.'))


class TestVerdictOutcome(unittest.TestCase):
    """The outcome comes from the `*Verdict*` field, not from the message."""

    def outcome(self, text):
        return cm.verdict_outcome(text)[0]

    def test_logbook_65_the_prose_word_is_not_an_outcome(self):
        """A night post is never read for an outcome -- it has no such field."""
        self.assertIsNone(
            cm.verdict_outcome(NIGHT_POST_WITH_ABANDONED_PROSE)[0])

    def test_merged_with_a_link(self):
        self.assertEqual(self.outcome(DAY_VERDICT), "merged")

    def test_sent_back(self):
        self.assertEqual(self.outcome("*Verdict* — sent back"), "sent-back")

    def test_sent_back_spelled_as_the_ledger_spells_it(self):
        self.assertEqual(self.outcome("*Verdict* — sent-back: three asks."),
                         "sent-back")

    def test_sent_back_mid_clause(self):
        self.assertEqual(
            self.outcome("*Verdict* — `append-only` #1 stays sent back, unchanged."),
            "sent-back")

    def test_both_merged(self):
        self.assertEqual(
            self.outcome("*Verdict** — both merged: `stockbox#3` and `blankout#3`."),
            "merged")

    def test_a_later_sentence_does_not_overrule_the_verdict(self):
        """Real post. Reading the whole field makes this two outcomes."""
        self.assertEqual(self.outcome(
            "*Verdict* — merged. Fast-forwarded `shift/2026-09-05-shift-watcher` "
            "onto `ops@main` as `5aa0b9c` (no PR, still disabled here). "
            "logbook#12 closed."), "merged")

    def test_semicolon_also_ends_the_clause(self):
        self.assertIsNone(self.outcome(
            "**Verdict** — nothing to merge, `ops` has no PRs; verified and "
            "closed the tracking issue (logbook#18)"))

    def test_nothing_to_review_is_reported_not_guessed(self):
        outcome, unparsed = cm.verdict_outcome(
            "*Verdict* — nothing to review. append-only#1 has not moved since "
            "I sent it back at 14:48.")
        self.assertIsNone(outcome)
        self.assertIn("nothing to review", unparsed)

    def test_no_change_is_reported_not_guessed(self):
        outcome, unparsed = cm.verdict_outcome(
            "*Verdict* — no change. append-only#1 is still sent back.")
        self.assertIsNone(outcome)
        self.assertTrue(unparsed)

    def test_no_verdict_field_at_all(self):
        self.assertEqual(cm.verdict_outcome(NIGHT_POST), (None, None))

    def test_version_number_does_not_end_the_clause(self):
        """`v0.1.0` has a period in it and must not truncate the field."""
        self.assertEqual(self.outcome("*Verdict* — merged, tagged v0.1.0"),
                         "merged")


class TestReadLedger(unittest.TestCase):
    """A night's outcome is in SHIFTS.md, in a column, from a closed list."""

    def write(self, body):
        d = Path(self.tmp.name) / "SHIFTS.md"
        d.write_text(body)
        return d

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # SINCE is computed at import; anchor the fixtures to it.
        self.inside = cm.NOW.date().isoformat()
        self.outside = (cm.SINCE.date() - cm.timedelta(days=1)).isoformat()

    def test_counts_outcomes_in_the_window(self):
        got = cm.read_ledger(self.write(
            f"{self.inside} | deadassert | #4 | shipped-for-review | https://s/p1 — note\n"
            f"{self.inside} | deadassert | #4 | merged | https://s/p2 — note\n"
        ))
        self.assertEqual(got["rows"], 2)
        self.assertEqual(got["outcomes"],
                         {"shipped-for-review": 1, "merged": 1})
        self.assertEqual(got["unknown"], [])

    def test_ignores_rows_outside_the_window(self):
        got = cm.read_ledger(self.write(
            f"{self.outside} | unmade | #1 | merged | https://s/p1 — note\n"
            f"{self.inside} | unmade | #2 | merged | https://s/p2 — note\n"
        ))
        self.assertEqual(got["rows"], 1)

    def test_ignores_the_preamble(self):
        got = cm.read_ledger(self.write(
            "# Shifts\n\nOne line per shift. Outcome is one of merged, "
            "abandoned, no-work.\n\n| not | a | row |\n"
        ))
        self.assertEqual(got["rows"], 0)
        self.assertEqual(got["outcomes"], {})

    def test_no_work_night_has_no_pr_number(self):
        got = cm.read_ledger(self.write(
            f"{self.inside} | — | — | no-work | https://s/p1 — nothing shipped\n"))
        self.assertEqual(got["outcomes"], {"no-work": 1})

    def test_an_outcome_off_the_list_is_a_finding_not_a_drop(self):
        got = cm.read_ledger(self.write(
            f"{self.inside} | unmade | #1 | shipped | https://s/p1 — note\n"))
        self.assertEqual(got["rows"], 1)
        self.assertEqual(got["outcomes"], {})
        self.assertEqual(len(got["unknown"]), 1)

    def test_vocabulary_matches_the_ledgers_own(self):
        """If ledger.py grows an outcome, this list has to grow with it."""
        self.assertEqual(cm.LEDGER_OUTCOMES, frozenset({
            "shipped-for-review", "revised", "merged", "sent-back", "closed",
            "abandoned", "no-work"}))

    def test_a_missing_ledger_is_reported_not_raised(self):
        got = cm.read_ledger(Path(self.tmp.name) / "nope.md")
        self.assertTrue(got["error"])
        self.assertEqual(got["rows"], 0)


class TestWriteSeriesRow(unittest.TestCase):
    """Adding a column must not shift the rows already in the file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "series.csv"

    def rows(self):
        with self.path.open(newline="") as f:
            return list(csv.DictReader(f))

    def test_writes_a_header_for_a_new_file(self):
        cm.write_series_row(self.path, {"date": "2026-11-19", "shifts_logged": 12})
        self.assertEqual(self.rows(), [{"date": "2026-11-19",
                                        "shifts_logged": "12"}])

    def test_appends_without_rewriting_when_columns_match(self):
        cm.write_series_row(self.path, {"date": "2026-11-18", "shifts_logged": 11})
        cm.write_series_row(self.path, {"date": "2026-11-19", "shifts_logged": 12})
        self.assertEqual([r["date"] for r in self.rows()],
                         ["2026-11-18", "2026-11-19"])

    def test_a_new_column_does_not_move_the_old_rows(self):
        cm.write_series_row(self.path, {"date": "2026-11-18", "shifts_logged": 11})
        cm.write_series_row(self.path, {"date": "2026-11-19", "shifts_logged": 12,
                                        "day_clock_outs": 6})
        got = self.rows()
        self.assertEqual(got[0]["shifts_logged"], "11")
        self.assertEqual(got[0]["day_clock_outs"], "")
        self.assertEqual(got[1]["shifts_logged"], "12")
        self.assertEqual(got[1]["day_clock_outs"], "6")

    def test_every_line_has_the_same_number_of_fields(self):
        """The failure this guards is a chart, not an exception."""
        cm.write_series_row(self.path, {"date": "2026-11-18", "shifts_logged": 11})
        cm.write_series_row(self.path, {"date": "2026-11-19", "shifts_logged": 12,
                                        "ledger_lines": 16})
        widths = {len(line.split(",")) for line in
                  self.path.read_text().strip().splitlines()}
        self.assertEqual(len(widths), 1, "ragged csv")

    def test_old_column_order_is_preserved(self):
        cm.write_series_row(self.path, {"date": "d", "a": 1, "b": 2})
        cm.write_series_row(self.path, {"date": "d", "b": 2, "a": 1, "c": 3})
        with self.path.open(newline="") as f:
            self.assertEqual(next(csv.reader(f)), ["date", "a", "b", "c"])


# --------------------------------------------------------- end to end

class FakeAPI(BaseHTTPRequestHandler):
    """Stands in for both api.github.com and slack.com/api."""

    history = []
    replies = {}
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, body):
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        FakeAPI.calls.append((url.path, query))

        if url.path == "/conversations.history":
            # Only the shift-log channel has anything in it.
            if query.get("channel") == "SHIFTLOG":
                return self._send({"ok": True, "messages": FakeAPI.history})
            return self._send({"ok": True, "messages": []})
        if url.path == "/conversations.replies":
            parent = query.get("ts")
            return self._send({"ok": True,
                               "messages": FakeAPI.replies.get(parent, [])})
        # Everything GitHub: an org with no repos and no runs.
        return self._send([])


class TestEndToEnd(unittest.TestCase):
    """The real script, against a real server, writing real files."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeAPI)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def run_script(self, history, replies, ledger=""):
        FakeAPI.history, FakeAPI.replies, FakeAPI.calls = history, replies, []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        logbook = Path(tmp.name) / "logbook"
        (logbook / "metrics").mkdir(parents=True)
        (logbook / "SHIFTS.md").write_text(ledger)
        env = {
            **os.environ,
            "GH_TOKEN": "test", "SLACK_BOT_TOKEN": "test",
            "GH_API": self.base, "SLACK_API": self.base,
            "METRICS_DIR": str(logbook / "metrics"),
            "SLACK_SHIFT_LOG": "SHIFTLOG", "SLACK_ORDERS": "ORDERS",
            "SLACK_INCIDENTS": "INCIDENTS", "SLACK_GENERAL": "GENERAL",
            "SLACK_MEMES": "MEMES", "WINDOW_DAYS": "7",
        }
        proc = subprocess.run([sys.executable, str(SCRIPT)], env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.snapshot = json.loads(
            (logbook / "metrics" / "data" /
             f"{cm.NOW.date().isoformat()}.json").read_text())
        return json.loads(proc.stdout.split("\n\nsnapshot:")[0]), proc.stderr

    def msg(self, ts, text, author="Richmond", **kw):
        return {"ts": ts, "text": text, "username": author, **kw}

    def test_a_day_verdict_in_a_thread_is_found(self):
        """It never was: conversations.history does not return replies."""
        now = cm.NOW.timestamp()
        row, _ = self.run_script(
            history=[self.msg(f"{now - 400:.6f}", NIGHT_POST, reply_count=1)],
            replies={f"{now - 400:.6f}": [
                self.msg(f"{now - 400:.6f}", NIGHT_POST, reply_count=1),
                self.msg(f"{now - 100:.6f}", DAY_VERDICT, author="Jen"),
            ]},
        )
        self.assertEqual(row["night_clock_outs"], 1)
        self.assertEqual(row["day_clock_outs"], 1)
        self.assertEqual(row["shifts_logged"], 2)
        self.assertIn("/conversations.replies",
                      [p for p, _ in FakeAPI.calls])

    def test_a_thread_is_not_fetched_when_there_are_no_replies(self):
        now = cm.NOW.timestamp()
        self.run_script(history=[self.msg(f"{now - 400:.6f}", NIGHT_POST)],
                        replies={})
        self.assertNotIn("/conversations.replies",
                         [p for p, _ in FakeAPI.calls])

    def test_prose_outcome_words_do_not_count_as_shifts(self):
        """logbook#65, end to end."""
        now = cm.NOW.timestamp()
        row, _ = self.run_script(
            history=[
                self.msg(f"{now - 500:.6f}", NIGHT_POST_WITH_ABANDONED_PROSE),
                self.msg(f"{now - 400:.6f}", NOT_A_CLOCK_OUT),
                self.msg(f"{now - 300:.6f}", NIGHT_POST_CONTINUATION),
            ],
            replies={},
        )
        self.assertEqual(row["shifts_logged"], 1)
        self.assertEqual(row["night_clock_outs"], 1)
        self.assertEqual(row["day_clock_outs"], 0)
        # And the bug itself: nothing in this window may carry an outcome.
        # "abandoned" is in the night post's prose and "merged"/"closed" are
        # in the message that is not a clock-out at all.
        logged = self.snapshot["slack"]["channels"]["shift-log"]
        self.assertEqual([m["outcome"] for m in logged], [None, None, None])

    def test_lookback_parents_are_used_but_not_counted(self):
        """A parent outside the window finds its in-window verdict."""
        now = cm.NOW.timestamp()
        old = now - (8 * 86400)
        row, _ = self.run_script(
            history=[self.msg(f"{old:.6f}", NIGHT_POST, reply_count=1)],
            replies={f"{old:.6f}": [
                self.msg(f"{old:.6f}", NIGHT_POST, reply_count=1),
                self.msg(f"{now - 100:.6f}", DAY_VERDICT, author="Jen"),
            ]},
        )
        self.assertEqual(row["night_clock_outs"], 0, "counted an old parent")
        self.assertEqual(row["day_clock_outs"], 1, "lost the verdict")

    def test_unparsed_verdict_is_counted_and_printed(self):
        now = cm.NOW.timestamp()
        row, err = self.run_script(
            history=[self.msg(f"{now - 100:.6f}",
                              "*Verdict* — no change. Still sent back.",
                              author="Jen")],
            replies={},
        )
        self.assertEqual(row["unparsed_verdicts"], 1)
        self.assertIn("UNPARSED VERDICT", err)

    def test_night_outcomes_come_from_the_ledger(self):
        now = cm.NOW.timestamp()
        today = cm.NOW.date().isoformat()
        row, _ = self.run_script(
            history=[self.msg(f"{now - 100:.6f}", NIGHT_POST)],
            replies={},
            ledger=(f"{today} | unrotated | #3 | shipped-for-review | https://s/p1 — n\n"
                    f"{today} | — | — | no-work | https://s/p2 — nothing tonight\n"),
        )
        self.assertEqual(row["ledger_lines"], 2)
        self.assertEqual(row["no_work_shifts"], 1)
        self.assertEqual(row["ledger_shipped_for_review"], 1)
        self.assertEqual(row["ledger_unknown_outcomes"], 0)

    def test_a_missing_ledger_does_not_crash_the_run(self):
        now = cm.NOW.timestamp()
        FakeAPI.history = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        logbook = Path(tmp.name) / "logbook"
        (logbook / "metrics").mkdir(parents=True)
        env = {**os.environ, "GH_TOKEN": "test", "SLACK_BOT_TOKEN": "test",
               "GH_API": self.base, "SLACK_API": self.base,
               "METRICS_DIR": str(logbook / "metrics"),
               "SLACK_SHIFT_LOG": "SHIFTLOG", "WINDOW_DAYS": "7"}
        for k in ("SLACK_ORDERS", "SLACK_INCIDENTS", "SLACK_GENERAL",
                  "SLACK_MEMES"):
            env.pop(k, None)
        proc = subprocess.run([sys.executable, str(SCRIPT)], env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LEDGER UNREADABLE", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
