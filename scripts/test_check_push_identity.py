#!/usr/bin/env python3
"""
Tests for check-push-identity.py.

Run: python3 scripts/test_check_push_identity.py

Every test builds a real git repository in a temporary directory, configures
it the way a real runner would, and runs the real script against it as a
subprocess. Nothing here touches the network: the credential-helper leg is a
helper script on disk, which is what `git credential fill` wants anyway.

Two questions are worth answering, and they are the reason the interesting
tests are the ones about precedence:

  * does it agree with git about which credential wins? A check that says
    "clean" when a push would go up as somebody else is worse than no check,
    because it is the thing you would point at afterwards.
  * does it ever print a token? It runs inside a job whose transcript is kept
    as a public artifact, so a test asserts the token's absence from stdout
    and stderr in every single case, not just the ones that mention it.

What it cannot cover: whether git's precedence is what this asserts. That was
established against a live remote — from a checkout carrying the runner's
persisted header, a private repo that token cannot see is `Repository not
found`, and it lists refs once the header is reset — and it is the one fact
here that a unit test can only restate rather than prove.
"""

import base64
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check-push-identity.py"

OURS = "ghs_ours_" + "o" * 40
THEIRS = "ghs_theirs_" + "t" * 40


def basic(token, user="x-access-token"):
    payload = base64.b64encode(f"{user}:{token}".encode()).decode()
    return f"AUTHORIZATION: basic {payload}"


class PushIdentityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        # A plain https remote, no credentials in it, as actions/checkout
        # leaves one with persist-credentials: false.
        self.git("remote", "add", "origin", "https://github.com/committed-nightly/ops")

    def tearDown(self):
        self.tmp.cleanup()

    # --- helpers -------------------------------------------------------------

    def git(self, *args):
        done = subprocess.run(
            ["git", "-C", str(self.repo)] + list(args),
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def set_helper(self, token):
        """A credential helper on disk, answering with `token`."""
        helper = Path(self.tmp.name) / "helper.sh"
        helper.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "get" ]; then\n'
            "  echo username=x-access-token\n"
            f"  echo password={token}\n"
            "fi\n"
        )
        helper.chmod(0o755)
        self.git("config", "credential.helper", str(helper))

    def run_script(self, *args, token=OURS, cwd=None):
        env = dict(os.environ)
        env.pop("GH_TOKEN", None)
        # The real runner has gh installed as a global helper. Tests must not
        # inherit it, or the no-credential case quietly finds one.
        env["HOME"] = self.tmp.name
        env["GIT_CONFIG_GLOBAL"] = str(Path(self.tmp.name) / "nonexistent-gitconfig")
        env["GIT_CONFIG_SYSTEM"] = str(Path(self.tmp.name) / "nonexistent-gitconfig")
        if token is not None:
            env["GH_TOKEN"] = token
        done = subprocess.run(
            [sys.executable, str(SCRIPT), *(args or (str(self.repo),))],
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=60,
        )
        # Applies to every case, so it lives here rather than in one test.
        for stream_name, stream in (("stdout", done.stdout), ("stderr", done.stderr)):
            for secret in (OURS, THEIRS):
                self.assertNotIn(
                    secret,
                    stream,
                    f"a credential reached {stream_name}",
                )
        return done

    # --- the credential that wins -------------------------------------------

    def test_helper_with_our_token_passes(self):
        self.set_helper(OURS)
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ok", done.stdout)
        self.assertIn("credential helper", done.stdout)

    def test_helper_with_someone_elses_token_fails(self):
        self.set_helper(THEIRS)
        done = self.run_script()
        self.assertEqual(done.returncode, 1)
        self.assertIn("would NOT be this shift", done.stderr)

    def test_url_credentials_are_found(self):
        self.git(
            "remote",
            "set-url",
            "origin",
            f"https://x-access-token:{OURS}@github.com/committed-nightly/ops",
        )
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("URL of remote", done.stdout)

    def test_url_credentials_from_someone_else_fail(self):
        self.git(
            "remote",
            "set-url",
            "origin",
            f"https://x-access-token:{THEIRS}@github.com/committed-nightly/ops",
        )
        done = self.run_script()
        self.assertEqual(done.returncode, 1)
        self.assertIn("URL of remote", done.stderr)

    def test_a_url_with_credentials_is_not_echoed_back(self):
        self.git(
            "remote",
            "set-url",
            "origin",
            f"https://x-access-token:{THEIRS}@gitlab.com/somewhere/else",
        )
        done = self.run_script()
        self.assertEqual(done.returncode, 2)
        self.assertIn("<credentials>", done.stderr)

    # --- precedence, which is the whole point -------------------------------

    def test_an_extraheader_beats_a_helper(self):
        self.set_helper(OURS)
        self.git("config", "http.https://github.com/.extraheader", basic(THEIRS))
        done = self.run_script()
        self.assertEqual(done.returncode, 1)
        self.assertIn("extraheader", done.stderr)
        self.assertIn("persist-credentials: false", done.stderr)

    def test_an_extraheader_beats_credentials_in_the_url(self):
        self.git(
            "remote",
            "set-url",
            "origin",
            f"https://x-access-token:{OURS}@github.com/committed-nightly/ops",
        )
        self.git("config", "http.https://github.com/.extraheader", basic(THEIRS))
        done = self.run_script()
        self.assertEqual(done.returncode, 1)
        self.assertIn("extraheader", done.stderr)

    def test_credentials_in_the_url_beat_a_helper(self):
        self.set_helper(THEIRS)
        self.git(
            "remote",
            "set-url",
            "origin",
            f"https://x-access-token:{OURS}@github.com/committed-nightly/ops",
        )
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_an_empty_extraheader_resets_the_ones_before_it(self):
        """The documented one-command workaround must not read as a failure."""
        self.set_helper(OURS)
        self.git("config", "http.https://github.com/.extraheader", basic(THEIRS))
        self.git("config", "--add", "http.https://github.com/.extraheader", "")
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("credential helper", done.stdout)

    def test_a_header_after_a_reset_still_counts(self):
        self.set_helper(OURS)
        self.git("config", "http.https://github.com/.extraheader", basic(OURS))
        self.git("config", "--add", "http.https://github.com/.extraheader", "")
        self.git("config", "--add", "http.https://github.com/.extraheader", basic(THEIRS))
        done = self.run_script()
        self.assertEqual(done.returncode, 1)
        self.assertIn("extraheader", done.stderr)

    def test_our_own_token_in_a_header_passes_but_says_so(self):
        self.git("config", "http.https://github.com/.extraheader", basic(OURS))
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("note", done.stdout)
        self.assertIn("overriding", done.stdout)

    # --- headers it should ignore -------------------------------------------

    def test_a_header_for_another_host_is_ignored(self):
        self.set_helper(OURS)
        self.git("config", "http.https://ghe.example.com/.extraheader", basic(THEIRS))
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_header_that_is_not_a_credential_is_ignored(self):
        self.set_helper(OURS)
        self.git("config", "http.https://github.com/.extraheader", "X-Trace-Id: 7")
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_bare_extraheader_applies_everywhere(self):
        self.set_helper(OURS)
        self.git("config", "http.extraheader", basic(THEIRS))
        done = self.run_script()
        self.assertEqual(done.returncode, 1)

    def test_a_bearer_header_is_read_too(self):
        self.git("config", "http.https://github.com/.extraheader", f"Authorization: Bearer {THEIRS}")
        done = self.run_script()
        self.assertEqual(done.returncode, 1)

    # --- when it must refuse to answer --------------------------------------

    def test_two_conflicting_authorization_headers_are_not_guessed_at(self):
        self.git("config", "http.https://github.com/.extraheader", basic(OURS))
        self.git("config", "--add", "http.extraheader", basic(THEIRS))
        done = self.run_script()
        self.assertEqual(done.returncode, 2)
        self.assertIn("two or more", done.stderr)

    def test_an_unreadable_authorization_header_is_not_a_pass(self):
        self.set_helper(OURS)
        self.git("config", "http.https://github.com/.extraheader", "Authorization: Negotiate abc")
        done = self.run_script()
        self.assertEqual(done.returncode, 2)
        self.assertIn("does not read", done.stderr)

    def test_undecodable_basic_payload_is_not_a_pass(self):
        self.set_helper(OURS)
        self.git("config", "http.https://github.com/.extraheader", "Authorization: basic !!!!")
        done = self.run_script()
        self.assertEqual(done.returncode, 2)
        self.assertIn("base64", done.stderr)

    def test_no_credential_anywhere_is_not_a_pass(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 2)
        self.assertIn("gh auth setup-git", done.stderr)

    def test_a_missing_gh_token_is_not_a_pass(self):
        self.set_helper(OURS)
        done = self.run_script(token=None)
        self.assertEqual(done.returncode, 2)
        self.assertIn("GH_TOKEN", done.stderr)

    def test_an_empty_gh_token_is_not_a_pass(self):
        self.set_helper(OURS)
        done = self.run_script(token="")
        self.assertEqual(done.returncode, 2)
        self.assertIn("GH_TOKEN", done.stderr)

    def test_somewhere_that_is_not_a_repository(self):
        done = self.run_script(self.tmp.name)
        self.assertEqual(done.returncode, 2)
        self.assertIn("not a git working copy", done.stderr)

    def test_a_missing_remote(self):
        self.git("remote", "remove", "origin")
        done = self.run_script()
        self.assertEqual(done.returncode, 2)
        self.assertIn("no remote named", done.stderr)

    def test_a_named_remote(self):
        self.set_helper(OURS)
        self.git("remote", "remove", "origin")
        self.git("remote", "add", "upstream", "https://github.com/committed-nightly/ops")
        done = self.run_script(str(self.repo), "--remote", "upstream")
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_remote_flag_without_a_name(self):
        done = self.run_script(str(self.repo), "--remote")
        self.assertEqual(done.returncode, 2)

    def test_two_directories(self):
        done = self.run_script(str(self.repo), str(self.repo))
        self.assertEqual(done.returncode, 2)
        self.assertIn("usage", done.stderr)

    def test_the_default_directory_is_the_current_one(self):
        self.set_helper(OURS)
        done = self.run_script("--remote", "origin", cwd=str(self.repo))
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
