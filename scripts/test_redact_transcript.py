#!/usr/bin/env python3
"""
Tests for redact-transcript.py.

Run: python3 scripts/test_redact_transcript.py

Like the shift-watcher tests next door, these run the real script as a
subprocess rather than importing it, because the behaviour worth pinning is
what ends up on disk under a given environment — and the environment is half
the logic here.

The tests that matter most are the negative ones. Anything can replace a string
it was handed. The questions that decide whether this is safe to point at a
public artifact are "does it ever write a file it hasn't finished cleaning"
and "does it ever quietly do nothing", so most of what follows is about the
output *not* existing.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "redact-transcript.py"

GH = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
SLACK = "xoxb-" + "9988776655443-2211003344556-" + "Zk3jQ1p0Rw8sT6uV4xY2aB7c"


def transcript(*texts):
    """A stripped-down shape of the action's execution file: a list of turns."""
    return json.dumps(
        [
            {"type": "system", "subtype": "init", "session_id": "aaa4060c"},
            *[
                {
                    "type": "user",
                    "message": {"content": [{"type": "tool_result", "content": text}]},
                }
                for text in texts
            ],
            {"type": "result", "subtype": "success", "is_error": False},
        ],
        indent=2,
    )


class RedactTranscript(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.source = Path(self.dir.name) / "claude-execution-output.json"
        self.target = Path(self.dir.name) / "transcript.json"

    def run_script(self, env=None, source=None, target=None):
        environment = {
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        environment.update(env or {})
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                str(self.source if source is None else source),
                str(self.target if target is None else target),
            ],
            capture_output=True,
            text=True,
            env=environment,
        )

    def redacted(self):
        return self.target.read_text(encoding="utf-8")

    # --- the happy path -------------------------------------------------

    def test_replaces_a_known_value_and_names_it(self):
        self.source.write_text(transcript(f"remote: token is {GH}"))
        result = self.run_script({"GH_TOKEN": GH})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(GH, self.redacted())
        self.assertIn("[redacted: GH_TOKEN]", self.redacted())

    def test_output_is_still_valid_json_with_the_structure_intact(self):
        self.source.write_text(transcript(f"one {GH}", f"two {SLACK}"))
        result = self.run_script({"GH_TOKEN": GH, "SLACK_BOT_TOKEN": SLACK})
        self.assertEqual(result.returncode, 0, result.stderr)
        turns = json.loads(self.redacted())
        self.assertEqual(len(turns), 4)
        self.assertEqual(turns[0]["session_id"], "aaa4060c")
        self.assertEqual(turns[-1]["subtype"], "success")

    def test_every_occurrence_goes_not_just_the_first(self):
        self.source.write_text(transcript(f"{GH} {GH}", f"and again {GH}"))
        result = self.run_script({"GH_TOKEN": GH})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(GH, self.redacted())
        self.assertEqual(self.redacted().count("[redacted: GH_TOKEN]"), 3)
        self.assertIn("GH_TOKEN", result.stdout)
        self.assertIn("3", result.stdout)

    def test_a_clean_transcript_says_so_rather_than_saying_nothing(self):
        self.source.write_text(transcript("gh repo clone committed-nightly/logbook"))
        result = self.run_script({"GH_TOKEN": GH})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no credentials found", result.stdout)
        self.assertEqual(json.loads(self.redacted()), json.loads(self.source.read_text()))

    # --- shapes nobody listed -------------------------------------------

    def test_catches_a_token_shape_with_no_matching_env_var(self):
        # The case this rule exists for: a credential belonging to something
        # outside this org's variable list, pasted in by a command's output.
        stranger = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
        self.source.write_text(transcript(f"found {stranger} in the fixture"))
        result = self.run_script({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(stranger, self.redacted())
        self.assertIn("[redacted: github-token]", self.redacted())

    def test_matches_the_long_structured_installation_token(self):
        # The token a shift actually runs on is not the 40-character
        # `ghs_[A-Za-z0-9]+` of the published scanning patterns: it is ~383
        # characters and contains `.`, `-` and `_`. A pattern built from the
        # docs matches the first few characters and leaves the rest of a live
        # credential sitting in the file, which reads as redacted.
        tail = "aB3-dE6_gH9.jK2" * 25
        token = "ghs_v1.0_" + tail
        self.source.write_text(transcript(f"remote: rejected, using {token}"))
        result = self.run_script({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("jK2", self.redacted())
        self.assertIn("[redacted: github-token]", self.redacted())

    def test_catches_credentials_embedded_in_a_remote_url(self):
        # logbook#10: the ops checkout authenticates with the token inline in
        # remote.origin.url, so `git remote -v` prints it.
        unknown = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
        self.source.write_text(
            transcript(f"origin\thttps://x-access-token:{unknown}@github.com/a/b.git")
        )
        result = self.run_script({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(unknown, self.redacted())
        self.assertIn("[redacted: url-credentials]@github.com", self.redacted())

    def test_a_known_token_in_a_url_is_named_not_just_blanked(self):
        self.source.write_text(
            transcript(f"origin\thttps://x-access-token:{GH}@github.com/a/b.git")
        )
        result = self.run_script({"GH_TOKEN": GH})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[redacted: GH_TOKEN]@github.com", self.redacted())

    def test_catches_a_pem_private_key_across_lines(self):
        key = (
            "-----BEGIN RSA PRIVATE KEY-----\\nMIIEow"
            "IBAAKCAQEA\\nwibblewibble\\n-----END RSA PRIVATE KEY-----"
        )
        self.source.write_text(transcript(key))
        result = self.run_script({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("MIIEow", self.redacted())
        self.assertIn("[redacted: private-key]", self.redacted())

    def test_leaves_ordinary_urls_alone(self):
        text = (
            "https://slack.com/api/chat.postMessage and "
            "https://github.com/committed-nightly/ops/actions/runs/34172073876"
        )
        self.source.write_text(transcript(text))
        result = self.run_script({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("chat.postMessage", self.redacted())
        self.assertIn("runs/34172073876", self.redacted())
        self.assertNotIn("redacted", self.redacted())

    def test_extra_names_can_be_supplied_at_the_workflow(self):
        secret = "not-a-recognised-shape-at-all-1234"
        self.source.write_text(transcript(f"printed {secret}"))
        result = self.run_script(
            {"REDACT_ENV_VARS": "SOME_OTHER_TOKEN", "SOME_OTHER_TOKEN": secret}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[redacted: SOME_OTHER_TOKEN]", self.redacted())

    def test_longer_secrets_are_replaced_before_their_prefixes(self):
        # A value that is a prefix of another must not eat the longer one and
        # leave its tail sitting in the file.
        short = "abcdefghij"
        long = short + "klmnopqrst"
        self.source.write_text(transcript(f"value {long} end"))
        result = self.run_script({"GH_TOKEN": short, "SLACK_BOT_TOKEN": long})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("klmnopqrst", self.redacted())
        self.assertIn("[redacted: SLACK_BOT_TOKEN]", self.redacted())

    # --- refusing to write ----------------------------------------------

    def test_missing_input_is_its_own_exit_code_and_writes_nothing(self):
        result = self.run_script({})
        self.assertEqual(result.returncode, 3)
        self.assertFalse(self.target.exists())
        self.assertIn("no transcript to redact", result.stderr)

    def test_input_that_is_not_json_is_refused_rather_than_uploaded(self):
        self.source.write_text("a truncated tra")
        result = self.run_script({"GH_TOKEN": GH})
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.target.exists())
        self.assertIn("not valid JSON", result.stderr)

    def test_an_unwritable_target_leaves_no_partial_file(self):
        directory = Path(self.dir.name) / "nope"
        self.source.write_text(transcript(f"token {GH}"))
        result = self.run_script({"GH_TOKEN": GH}, target=directory / "out.json")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(directory.exists())
        self.assertIn("cannot write", result.stderr)

    def test_no_partial_file_is_left_beside_the_target(self):
        self.source.write_text("{ not json")
        self.run_script({"GH_TOKEN": GH})
        leftovers = sorted(p.name for p in Path(self.dir.name).iterdir())
        self.assertEqual(leftovers, ["claude-execution-output.json"])

    def test_usage_error_is_distinguishable_from_a_dirty_transcript(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.source)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    # --- the loud skip ---------------------------------------------------

    def test_a_short_secret_is_skipped_loudly_rather_than_silently(self):
        # Redacting a 4-character value would replace it everywhere it happens
        # to occur and ruin the transcript, so it is skipped — but skipping is
        # the dangerous half, so it has to be said out loud in both places a
        # human might look.
        self.source.write_text(transcript("nothing interesting here"))
        result = self.run_script({"GH_TOKEN": "true"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GH_TOKEN", result.stdout)
        self.assertIn("NOT redacted", result.stdout)

    def test_an_unset_secret_is_not_a_problem(self):
        self.source.write_text(transcript("a quiet shift"))
        result = self.run_script({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.target.exists())

    # --- the summary -----------------------------------------------------

    def test_the_step_summary_gets_the_same_text_as_stdout(self):
        summary = Path(self.dir.name) / "summary.md"
        summary.write_text("")
        self.source.write_text(transcript(f"token {GH}"))
        result = self.run_script(
            {"GH_TOKEN": GH, "GITHUB_STEP_SUMMARY": str(summary)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GH_TOKEN", summary.read_text())
        self.assertIn("1 redaction", summary.read_text())

    def test_an_unwritable_step_summary_does_not_lose_the_transcript(self):
        self.source.write_text(transcript(f"token {GH}"))
        result = self.run_script(
            {
                "GH_TOKEN": GH,
                "GITHUB_STEP_SUMMARY": str(Path(self.dir.name) / "nope" / "s.md"),
            }
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.target.exists())
        self.assertIn("could not write the step summary", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
