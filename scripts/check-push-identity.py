#!/usr/bin/env python3
"""
Work out which credential a push to github.com will actually carry, and check
that it is this shift's.

Every shift already checks its commits before they go up:

    git log -1 --format='%an <%ae>'

That catches nothing here. A commit's author is baked into the object; the
identity a *push* lands as comes from the credential git sends with the
request. Those are two different facts. A shift can commit correctly as
`richmond-avenal[bot]`, push with the runner's own `GITHUB_TOKEN`, and have
every one of those commits appear on GitHub as `github-actions[bot]` — which
is logbook#18, and which is the thing Dan asked us to stop doing.

`actions/checkout` is how it happens. By default it leaves its token behind in
a file that an `includeIf.gitdir` pulls into the checkout it just created:

    http.https://github.com/.extraheader=AUTHORIZATION: basic <base64 of
                                          x-access-token:GITHUB_TOKEN>

An `extraheader` is attached to the request itself, so it beats a credential
helper *and* it beats credentials embedded in the remote URL. Observed, not
assumed: from inside such a checkout, with a good token in the remote URL, a
private repo the shift can read and `github-actions[bot]` cannot comes back

    remote: Repository not found.

and the same command with the header cleared for one invocation lists its
refs. Today that presents as a 403 on push, because `github-actions[bot]` has
no write access here. Add `contents: write` to a shift's `permissions:` for
some unrelated reason and the same push starts succeeding, quietly, as the
wrong identity.

So the check worth having is not "is `persist-credentials` off in the YAML" —
that is a proxy, and it goes stale the moment someone adds a second checkout
step. It is: resolve the credential that actually wins, and compare it to the
token we know is ours. That is this script. It resolves in git's order,

  1. an `Authorization` `extraheader` applying to https://github.com/,
  2. credentials embedded in the remote's URL,
  3. whatever the credential helpers hand back for github.com,

and compares the winner against `$GH_TOKEN` — the token the shift minted, and
the one its own preflight has already proved works.

Tokens are never printed, by this or by anything it calls. Everything it says
about a credential it says with a truncated SHA-256, which is enough to tell
two credentials apart and no use to anyone who reads the artifact.

Three things it deliberately does not do:

  * It does not implement git's URL-matching rules for `http.<url>.*` in
    full. It takes any `extraheader` whose URL component plausibly covers
    https://github.com/ — and when it cannot tell, it says so and exits 2
    rather than reporting a pass.
  * It does not check push *permission*. A credential that is ours but cannot
    write is a 403, which is loud, and not the failure this is for.
  * It does not look at the workflow files. It inspects the working copy it
    was pointed at, which is the only thing a push actually consults.

Usage:

    python3 scripts/check-push-identity.py [repo-dir] [--remote NAME]

Exit codes:

    0  the credential that wins is $GH_TOKEN
    1  a different credential wins — a push would be attributed to someone else
    2  cannot tell (no $GH_TOKEN, not a git repo, no credential resolvable,
       an Authorization header this cannot read, two of them, …)
"""

import base64
import hashlib
import hmac
import os
import re
import subprocess
import sys
from urllib.parse import urlsplit

EXIT_OK = 0
EXIT_WRONG_IDENTITY = 1
EXIT_CANNOT_TELL = 2

TARGET = "https://github.com/"
TARGET_HOST = "github.com"

REMEDY = """\
An extraheader travels with the request, so it overrides both the credential
helper and any credentials in the remote URL. It is almost always
actions/checkout's persisted token, i.e. github-actions[bot].

In the workflow:

    - uses: actions/checkout@v6
      with:
        persist-credentials: false

For one command only:

    git -c http.https://github.com/.extraheader= push origin main

See logbook#18."""


def fingerprint(secret):
    """A truncated digest. Enough to compare two credentials, no use as one."""
    return "sha256:" + hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def git(args, cwd, stdin=None, timeout=20):
    """Run git, returning (returncode, stdout). Never raises on a git failure."""
    env = dict(os.environ)
    # Anything that wants to ask a human must fail instead. Without this,
    # `credential fill` on a box with no helper blocks until the job times
    # out. `/bin/false` rather than `/bin/echo`: an askpass program is handed
    # the prompt as its argument, so `echo` answers with the prompt text and
    # git accepts that as the password — a credential invented by the check
    # itself, which then fails to match and reports a problem that isn't one.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/bin/false"
    try:
        done = subprocess.run(
            ["git", "-C", cwd] + args,
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 128, ""
    except FileNotFoundError:
        print("check-push-identity: no git on PATH", file=sys.stderr)
        sys.exit(EXIT_CANNOT_TELL)
    return done.returncode, done.stdout


def config_pairs(cwd, regexp):
    """Config keys matching regexp, as a list of (key, value).

    `-z` because a config value may contain anything at all, newlines
    included: records are NUL-terminated and the key is separated from the
    value by a newline.
    """
    code, out = git(["config", "--get-regexp", "-z", regexp], cwd)
    if code != 0:
        return []
    pairs = []
    for record in out.split("\0"):
        if not record:
            continue
        key, _, value = record.partition("\n")
        pairs.append((key.strip(), value))
    return pairs


def config_origins(cwd, regexp):
    """Best-effort "which file set this", for the error message only."""
    code, out = git(["config", "--show-origin", "--get-regexp", regexp], cwd)
    if code != 0:
        return {}
    origins = {}
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        origin, rest = parts
        key = rest.split(" ", 1)[0]
        origins.setdefault(key, origin)
    return origins


def header_applies_to_github(key):
    """Does an `http.<url>.extraheader` key cover https://github.com/?

    Returns True, False, or None for "there is a URL here I can't judge".
    `http.extraheader` with no URL at all applies to every request.
    """
    inner = key[len("http.") : -len(".extraheader")]
    if inner == "":
        return True
    url = inner.rstrip("/")
    if "://" not in url:
        return None
    split = urlsplit(url)
    if split.scheme not in ("http", "https"):
        return False
    host = split.hostname or ""
    if host.lower() != TARGET_HOST:
        return False
    # A path component narrows the header to that prefix. Anything under
    # github.com/ is a prefix of some push target, so treat a bare host as
    # applying and a path as applying too — it cannot be ruled out without
    # knowing the push URL, and over-reporting here is the safe direction.
    return True


def credential_from_authorization(value):
    """Pull the secret out of an `AUTHORIZATION: basic|bearer …` header value.

    Returns (token, None) or (None, why-not).
    """
    name, _, rest = value.partition(":")
    if name.strip().lower() != "authorization":
        return None, "header is not an Authorization header"
    rest = rest.strip()
    scheme, _, payload = rest.partition(" ")
    scheme = scheme.lower()
    payload = payload.strip()
    if not payload:
        return None, "Authorization header has no credential in it"
    if scheme == "bearer" or scheme == "token":
        return payload, None
    if scheme == "basic":
        try:
            decoded = base64.b64decode(payload, validate=True).decode("utf-8")
        except Exception:
            return None, "Authorization basic payload is not valid base64"
        _, sep, password = decoded.partition(":")
        if not sep:
            return None, "Authorization basic payload has no colon in it"
        return password, None
    return None, f"Authorization scheme {scheme!r} is one this does not read"


def from_extraheader(cwd):
    """Leg 1. Returns (token, description) or (None, None), or exits 2."""
    pairs = [
        (key, value)
        for key, value in config_pairs(cwd, r"^http\..*extraheader$")
        if key.startswith("http.") and key.endswith(".extraheader")
    ]
    if not pairs:
        return None, None

    origins = config_origins(cwd, r"^http\..*extraheader$")

    # Resets first, before judging anything. An empty value clears the
    # accumulated list — git documents that for http.extraHeader, and it is
    # what makes the `git -c http.….extraheader= push` workaround work. Config
    # is walked in order and command-line `-c` is read last, so everything set
    # before an empty value is gone. Verified against a live remote rather
    # than taken from the docs: with an empty value appended, a private repo
    # the checkout's own token cannot see becomes readable again.
    surviving = []
    for key, value in pairs:
        if not value.strip():
            surviving = []
        else:
            surviving.append((key, value))

    candidates = []
    for key, value in surviving:
        applies = header_applies_to_github(key)
        if applies is None:
            cannot_tell(
                f"{key} is set, and this cannot work out whether it applies to "
                f"{TARGET}. Resolve it by hand before trusting a push from here."
            )
        if applies:
            candidates.append((key, value))

    authorizations = []
    for key, value in candidates:
        token, why_not = credential_from_authorization(value)
        if token is None:
            if why_not == "header is not an Authorization header":
                continue  # a header, but not a credential. Fine.
            cannot_tell(f"{key} ({origins.get(key, 'origin unknown')}): {why_not}")
        authorizations.append((key, token))

    if len(authorizations) > 1:
        keys = ", ".join(key for key, _ in authorizations)
        cannot_tell(
            f"two or more Authorization headers apply to {TARGET} ({keys}). "
            "Which one the server honours is not ours to decide."
        )
    if not authorizations:
        return None, None
    key, token = authorizations[0]
    return token, f"{key} ({origins.get(key, 'origin unknown')})"


def from_remote_url(cwd, remote):
    """Leg 2. Returns (token, description) or (None, None), or exits 2."""
    code, out = git(["remote", "get-url", remote], cwd)
    if code != 0 or not out.strip():
        cannot_tell(f"no remote named {remote!r} in {cwd}")
    url = out.strip()
    split = urlsplit(url)
    if not split.scheme:
        cannot_tell(f"remote {remote!r} is {url!r}, which is not an https remote")
    if (split.hostname or "").lower() != TARGET_HOST:
        cannot_tell(f"remote {remote!r} is not on {TARGET_HOST}: {redact_url(url)}")
    if split.password:
        return split.password, f"credentials embedded in the URL of remote {remote!r}"
    return None, None


def redact_url(url):
    return re.sub(r"//[^/@]*@", "//<credentials>@", url)


def from_helper(cwd):
    """Leg 3. Returns (token, description) or (None, None)."""
    request = f"protocol=https\nhost={TARGET_HOST}\n\n"
    code, out = git(["credential", "fill"], cwd, stdin=request)
    if code != 0:
        return None, None
    password = None
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == "password":
            password = value
    if not password:
        return None, None
    return password, "a credential helper (`git credential fill`)"


def cannot_tell(message):
    print(f"push identity: cannot tell — {message}", file=sys.stderr)
    sys.exit(EXIT_CANNOT_TELL)


def main(argv):
    args = [a for a in argv[1:]]
    remote = "origin"
    if "--remote" in args:
        i = args.index("--remote")
        try:
            remote = args[i + 1]
        except IndexError:
            print("check-push-identity: --remote needs a name", file=sys.stderr)
            return EXIT_CANNOT_TELL
        del args[i : i + 2]
    if len(args) > 1:
        print(
            "usage: check-push-identity.py [repo-dir] [--remote NAME]",
            file=sys.stderr,
        )
        return EXIT_CANNOT_TELL
    cwd = args[0] if args else "."

    expected = os.environ.get("GH_TOKEN") or ""
    if not expected:
        cannot_tell("$GH_TOKEN is empty, so there is nothing to compare against")

    code, _ = git(["rev-parse", "--git-dir"], cwd)
    if code != 0:
        cannot_tell(f"{cwd} is not a git working copy")

    # In git's order, and lazily: a leg that cannot answer is only a problem
    # if nothing above it already has.
    winner = where = mechanism = None
    for mechanism, resolve in (
        ("header", lambda: from_extraheader(cwd)),
        ("url", lambda: from_remote_url(cwd, remote)),
        ("helper", lambda: from_helper(cwd)),
    ):
        winner, where = resolve()
        if winner is not None:
            break

    if winner is None:
        cannot_tell(
            f"no credential for {TARGET} could be resolved from {cwd} — no "
            "applicable header, nothing in the remote URL, and no helper "
            "answered. A push from here would not get as far as being "
            "misattributed; it would fail. `gh auth setup-git` is the usual fix."
        )

    if not hmac.compare_digest(winner, expected):
        print(
            "push identity: a push to github.com from "
            f"{cwd} would NOT be this shift.\n\n"
            f"  what wins:      {where}\n"
            f"  it carries:     {fingerprint(winner)}\n"
            f"  $GH_TOKEN is:   {fingerprint(expected)}\n",
            file=sys.stderr,
        )
        if mechanism == "header":
            print(REMEDY, file=sys.stderr)
        return EXIT_WRONG_IDENTITY

    print(
        f"push identity: ok — $GH_TOKEN ({fingerprint(winner)}) is what wins, "
        f"from {where}"
    )
    if mechanism == "header":
        # Right credential, wrong mechanism. Not a failure, but the next person
        # to change the workflow's token will not know this header exists.
        print(
            f"push identity: note — {where} is what supplies it, overriding "
            "any helper. Whoever changes the token this job checks out with "
            "changes what pushes from here, silently. See logbook#18.",
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
