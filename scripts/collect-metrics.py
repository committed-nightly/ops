#!/usr/bin/env python3
"""
Collect a weekly snapshot of the org into metrics/data/<date>.json and
append the headline series to metrics/series.csv.

This is deliberately dumb. It counts things and extracts candidate
excerpts; it makes no judgements. Moss does the judging, and keeps his
classifications in metrics/labels.csv so a number can always be traced
back to the thing it came from.

Why a script and not the agent: a metric computed by reading Slack each
week is not the same metric week to week. Definitions live here, in
version control, where changing one is a visible diff.

Env: GH_TOKEN, SLACK_BOT_TOKEN, SLACK_* channel ids.
"""

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ORG = "committed-nightly"
# Overridable so the tests can point the script at a real local server.
# Nothing else sets these; in CI and on a shift they are the real hosts.
GH = os.environ.get("GH_API", "https://api.github.com")
SLACK = os.environ.get("SLACK_API", "https://slack.com/api")

GH_HEADERS = {
    "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]

NOW = datetime.now(timezone.utc)
WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))
SINCE = NOW - timedelta(days=WINDOW_DAYS)

# Heuristic only. These surface candidates for Moss to read and classify;
# they are never reported as counts on their own. A regex cannot tell
# disagreement from politeness.
DISAGREEMENT_PATTERNS = [
    r"\bI think I'?m right\b", r"\bI deviated\b", r"\bdisagree\b",
    r"\bpushing back\b", r"\bnot convinced\b", r"\bI'?d argue\b",
    r"\brather than quietly\b", r"\byour number\b",
]
SELF_CORRECTION_PATTERNS = [
    r"\bI was wrong\b", r"\bstruck? through\b", r"\bmy mistake\b",
    r"\bI got .{0,20} wrong\b", r"\bcorrection\b", r"\bI'?d been wrong\b",
]


# ------------------------------------------------------ shift clock-outs
#
# Both shifts clock out into #shift-log with a bold-label skeleton, and the
# outcome is one field of it -- not a word loose in the prose. Reading the
# prose is what logbook#65 was: the old parser substring-matched seven words
# against the whole message, so "nothing here was abandoned" tagged a clean
# shift `abandoned`. Measured over the 100 messages Slack still holds, it
# found an outcome in 27, and every one of those 27 took it from a prose
# line -- `*Why*`, `*Left for next time*`, a bullet list -- because the
# night-shift template has no outcome field for it to read.
#
# Labels are matched with one or two asterisks either side: Slack mrkdwn
# wants one, both shifts write Markdown's two about a fifth of the time, and
# the day-shift template is a blockquote so a leading '>' is normal.

NIGHT_LABELS = frozenset({
    "built", "revised", "why", "pr", "rough edges", "left for next time",
})
DAY_LABELS = frozenset({"verdict", "late verdict", "checked", "asks", "honestly"})

# A label owns its line: it starts it (after any blockquote marker) and is
# followed by the template's em dash, or a colon or hyphen if someone typed
# one. Without that trailing separator this matches any bold phrase that
# happens to open a line, of which the corpus has plenty.
LABEL_RE = re.compile(
    r"^[ \t]*>?[ \t]*\*{1,2}([A-Za-z][^*\n]{0,40}?)\*{1,2}[ \t]*(?:\u2014|--|[-:])",
    re.M,
)

# The ledger's vocabulary, longest first so "sent back" is tried before a
# bare word inside it could win. `ledger.py` holds the canonical list and
# refuses anything off it; these are the same seven, plus the spaced spelling
# of `sent-back` that the day-shift template actually prints.
VERDICT_TERMS = (
    ("shipped-for-review", "shipped-for-review"),
    ("sent-back", "sent-back"),
    ("sent back", "sent-back"),
    ("abandoned", "abandoned"),
    ("no-work", "no-work"),
    ("revised", "revised"),
    ("merged", "merged"),
    ("closed", "closed"),
)


def labels(text):
    """The bold field labels this message uses, lowercased.

    "Late verdict, posted today" is the day shift's backfill spelling, so a
    label is cut at its first comma before being looked up.
    """
    out = set()
    for raw in LABEL_RE.findall(text or ""):
        out.add(raw.split(",")[0].strip().lower())
    return out


def classify(text):
    """'night', 'day' or None -- which shift's clock-out this message is.

    Structure only. A message that talks about a shift is not a clock-out,
    which is the whole of logbook#65.
    """
    seen = labels(text)
    if seen & {"verdict", "late verdict"} or {"checked", "honestly"} <= seen:
        return "day"
    # Two fields of the skeleton. One is a bold line that happens to be
    # called "Why"; the template never prints fewer than four.
    if len(seen & NIGHT_LABELS) >= 2:
        return "night"
    return None


def verdict_outcome(text):
    """(outcome, unparsed_line) from the `*Verdict*` field alone.

    Scans that one field, not the message -- and only as far as the end of
    its first clause, because the template is `**Verdict** - merged / sent
    back / closed` and the outcome is what the field opens with. Anything
    after the first `. ` or `; ` is the day shift saying what else it did:
    one real verdict reads "merged. ... logbook#12 closed.", and reading the
    whole field makes that two outcomes and no answer.

    If the clause names exactly one outcome it is that outcome. If it names
    none or several the field comes back as `unparsed` so a changed format
    shows up in the report as a number and a quotable line, instead of as a
    quiet wrong answer. On the corpus that is 3 of 77 -- "nothing to
    review", "nothing to merge", "no change" -- all of them days on which
    the answer genuinely was not one of the three.
    """
    for line in (text or "").split("\n"):
        found = LABEL_RE.match(line)
        if not found or found.group(1).split(",")[0].strip().lower() not in (
            "verdict", "late verdict"
        ):
            continue
        low = re.split(r"(?:\.|;)[ \t]", line.lower(), maxsplit=1)[0]
        hits, spans = [], []
        for term, outcome in VERDICT_TERMS:
            for m in re.finditer(rf"(?<![\w-]){re.escape(term)}(?![\w-])", low):
                # "sent-back" also contains "back"-less matches of shorter
                # terms; keep the first term to claim a span.
                if any(s < m.end() and m.start() < e for s, e in spans):
                    continue
                spans.append((m.start(), m.end()))
                if outcome not in hits:
                    hits.append(outcome)
        if len(hits) == 1:
            return hits[0], None
        return None, line.strip()[:200]
    return None, None


# ----------------------------------------------------------- the ledger
#
# A night shift's clock-out has no outcome field at all -- the outcome of a
# night is written in `SHIFTS.md`, in a column, from a closed vocabulary. So
# that is where it is read from, by the date column, rather than guessed at
# from the post. The two sources are deliberately not joined row to row: the
# ledger's Slack permalink points at whichever message the shift posted last,
# which for 8 of 79 nights is a follow-up and not the clock-out.

LEDGER_OUTCOMES = frozenset({
    "shipped-for-review", "revised", "merged", "sent-back", "closed",
    "abandoned", "no-work",
})
LEDGER_ROW = re.compile(r"^(\d{4}-\d\d-\d\d)\s*\|[^|]*\|[^|]*\|\s*([^|\s]+)\s*\|")


def read_ledger(path):
    """Outcomes from the ledger rows dated inside the window.

    An outcome that is not in `ledger.py`'s list is not silently dropped --
    it means one of the two files has drifted, and that is a finding.
    """
    out = {"rows": 0, "outcomes": {}, "unknown": [], "error": None}
    try:
        text = Path(path).read_text()
    except OSError as e:
        out["error"] = str(e)
        return out
    for line in text.splitlines():
        m = LEDGER_ROW.match(line)
        if not m:
            continue
        day = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if day.date() < SINCE.date():
            continue
        out["rows"] += 1
        outcome = m.group(2)
        if outcome not in LEDGER_OUTCOMES:
            out["unknown"].append(line[:160])
            continue
        out["outcomes"][outcome] = out["outcomes"].get(outcome, 0) + 1
    return out


def gh(path, **params):
    """Paginated GitHub GET."""
    out, url = [], f"{GH}{path}"
    while url:
        r = requests.get(url, headers=GH_HEADERS, params=params, timeout=30)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        body = r.json()
        out.extend(body if isinstance(body, list) else [body])
        url = r.links.get("next", {}).get("url")
        params = {}
        time.sleep(0.05)
    return out


def slack(method, **params):
    r = requests.get(
        f"{SLACK}/{method}",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        params=params, timeout=30,
    )
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        # A missing scope here is a real finding, not a crash. Record it.
        return {"ok": False, "error": body.get("error", "unknown")}
    return body


def iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def hours(a, b):
    return round((b - a).total_seconds() / 3600, 2) if a and b else None


def find(patterns, text):
    hits = []
    for p in patterns:
        for m in re.finditer(p, text or "", re.I):
            start = max(0, m.start() - 120)
            hits.append(text[start:m.end() + 200].strip())
    return hits


# ---------------------------------------------------------------- GitHub

def collect_github():
    repos, prs, excerpts = [], [], []

    for repo in gh(f"/orgs/{ORG}/repos", per_page=100, type="all"):
        name = repo["name"]
        pushed = iso(repo["pushed_at"])
        repos.append({
            "name": name,
            "created_at": repo["created_at"],
            "pushed_at": repo["pushed_at"],
            "days_since_push": (NOW - pushed).days if pushed else None,
            "archived": repo["archived"],
            "private": repo["private"],
            "has_description": bool(repo.get("description")),
            "has_license": bool(repo.get("license")),
            "topics": repo.get("topics", []),
            "open_issues": repo.get("open_issues_count", 0),
            "created_this_window": iso(repo["created_at"]) >= SINCE,
        })

        for pr in gh(f"/repos/{ORG}/{name}/pulls", state="all", per_page=100):
            created, merged = iso(pr["created_at"]), iso(pr.get("merged_at"))
            closed = iso(pr.get("closed_at"))

            reviews = gh(f"/repos/{ORG}/{name}/pulls/{pr['number']}/reviews")
            comments = gh(f"/repos/{ORG}/{name}/pulls/{pr['number']}/comments",
                          per_page=100)

            first_review = iso(reviews[0]["submitted_at"]) if reviews else None
            states = [r["state"] for r in reviews]

            texts = []
            if created >= SINCE:
                texts.append((pr.get("body") or "", pr["user"]["login"]))
            for r in reviews:
                submitted = r.get("submitted_at")
                if submitted and iso(submitted) >= SINCE:
                    texts.append((r.get("body") or "", r["user"]["login"]))
            for c in comments:
                if iso(c["created_at"]) >= SINCE:
                    texts.append((c.get("body") or "", c["user"]["login"]))

            for kind, pats in (("disagreement", DISAGREEMENT_PATTERNS),
                               ("self_correction", SELF_CORRECTION_PATTERNS)):
                for text, author in texts:
                    for hit in find(pats, text):
                        excerpts.append({
                            "kind": kind, "source": f"{name}#{pr['number']}",
                            "author": author, "excerpt": hit,
                        })

            prs.append({
                "repo": name,
                "number": pr["number"],
                "author": pr["user"]["login"],
                "created_at": pr["created_at"],
                "merged": bool(merged),
                "state": pr["state"],
                "review_count": len(reviews),
                "review_states": states,
                "sent_back": states.count("CHANGES_REQUESTED"),
                "rounds": max(1, len(states)),
                "line_comments": len(comments),
                "hours_to_first_review": hours(created, first_review),
                "hours_to_merge": hours(created, merged),
                "hours_open": hours(created, closed or NOW),
                "in_window": created >= SINCE,
            })

    runs = gh(f"/repos/{ORG}/ops/actions/runs", per_page=100).__iter__()
    workflow_runs = []
    for page in runs:
        for run in page.get("workflow_runs", []) if isinstance(page, dict) else []:
            if iso(run["created_at"]) >= SINCE:
                workflow_runs.append({
                    "name": run["name"],
                    "conclusion": run["conclusion"],
                    "created_at": run["created_at"],
                    "url": run["html_url"],
                })

    issues = [
        {"number": i["number"], "title": i["title"], "state": i["state"],
         "age_days": (NOW - iso(i["created_at"])).days,
         "labels": [l["name"] for l in i.get("labels", [])]}
        for i in gh(f"/repos/{ORG}/logbook/issues", state="all", per_page=100)
        if "pull_request" not in i
    ]

    return {"repos": repos, "prs": prs, "workflow_runs": workflow_runs,
            "logbook_issues": issues, "excerpts": excerpts}


# ----------------------------------------------------------------- Slack

# A day shift reviews the night before, so its verdict can land inside the
# window under a parent that is not. Three days is enough for a Friday night
# reviewed on Monday, and cheap: it buys a handful of extra thread fetches.
THREAD_LOOKBACK_DAYS = 3


def thread_replies(cid, parents):
    """Replies to `parents`, filtered to the window. Returns (msgs, errors)."""
    got, errs = [], []
    for parent in parents:
        if not parent.get("reply_count"):
            continue
        body = slack("conversations.replies", channel=cid, ts=parent["ts"],
                     limit=200)
        if not body.get("ok"):
            errs.append({"channel": "shift-log", "thread": parent["ts"],
                         "error": body["error"]})
            continue
        for m in body.get("messages", []):
            if m["ts"] == parent["ts"]:
                continue
            if float(m["ts"]) < SINCE.timestamp():
                continue
            got.append(m)
        time.sleep(0.05)
    return got, errs


def collect_slack():
    channels = {
        "shift-log": os.environ.get("SLACK_SHIFT_LOG"),
        "orders": os.environ.get("SLACK_ORDERS"),
        "incidents": os.environ.get("SLACK_INCIDENTS"),
        "general": os.environ.get("SLACK_GENERAL"),
        "memes": os.environ.get("SLACK_MEMES"),
    }
    out, excerpts, errors = {}, [], []
    unparsed_verdicts = []

    for label, cid in channels.items():
        if not cid:
            errors.append({"channel": label, "error": "no channel id configured"})
            continue

        # shift-log is read with a lookback, so a verdict inside the window
        # that hangs under a night post just outside it is still found. The
        # replies themselves are filtered to the window; the extra parents
        # are dropped below.
        oldest = SINCE - timedelta(days=THREAD_LOOKBACK_DAYS if label == "shift-log" else 0)
        body = slack("conversations.history", channel=cid, limit=200,
                     oldest=str(oldest.timestamp()))
        if not body.get("ok"):
            errors.append({"channel": label, "error": body["error"]})
            continue

        raw = body.get("messages", [])

        # The day shift clocks out *in the thread* under the night post it is
        # reviewing -- "one thread is one full cycle", per its own template.
        # conversations.history never returns thread replies, so every day
        # verdict the company has ever written was invisible here: 87 replies
        # across the 90 days Slack holds, 78 of them Jen's.
        if label == "shift-log":
            got, errs = thread_replies(cid, raw)
            errors.extend(errs)
            # Now drop the lookback parents. They were fetched to find
            # threads, not to be counted -- leaving them in would add up to
            # three extra days of night shifts to every window.
            raw = [m for m in raw
                   if float(m["ts"]) >= SINCE.timestamp()] + got

        msgs = []
        for m in raw:
            text = m.get("text", "")
            kind = classify(text)
            outcome, unparsed = (verdict_outcome(text) if kind == "day"
                                 else (None, None))
            if unparsed:
                unparsed_verdicts.append({
                    "ts": m["ts"], "author": m.get("username", "?"),
                    "line": unparsed,
                })
            msgs.append({
                "ts": m["ts"],
                "at": datetime.fromtimestamp(float(m["ts"]), timezone.utc).isoformat(),
                "author": m.get("username") or m.get("user") or "?",
                "chars": len(text),
                "threaded": bool(m.get("thread_ts") and m["thread_ts"] != m["ts"]),
                "reply_count": m.get("reply_count", 0),
                # Which shift's clock-out this is, from the skeleton of bold
                # labels -- not from whether the prose happens to use one of
                # the outcome words (logbook#65). `outcome` is the day
                # shift's `*Verdict*` field; a night's outcome is not in
                # Slack at all and comes from the ledger.
                "clock_out": kind,
                "outcome": outcome,
            })
            for ekind, pats in (("disagreement", DISAGREEMENT_PATTERNS),
                                ("self_correction", SELF_CORRECTION_PATTERNS)):
                for hit in find(pats, text):
                    excerpts.append({
                        "kind": ekind, "source": f"slack/{label}",
                        "author": m.get("username", "?"), "excerpt": hit,
                    })

        out[label] = msgs

    return {"channels": out, "excerpts": excerpts, "errors": errors,
            "unparsed_verdicts": unparsed_verdicts}


# ---------------------------------------------------------------- output

def series_row(gh_data, slack_data, ledger_data):
    prs = [p for p in gh_data["prs"] if p["in_window"]]
    merged = [p for p in prs if p["merged"]]
    log = slack_data["channels"].get("shift-log", [])
    # A shift is a clock-out post, identified by its skeleton. It used to be
    # "a message with one of seven words in it anywhere", which over the
    # eleven weeks already in series.csv never once got this right: it read
    # 3, 3, 2, 1, 3, 5, 0, 3, 1, 4, 0 for weeks that each held about seven
    # night shifts and about as many day verdicts, and the two zeroes were
    # not quiet weeks (logbook#65).
    nights = [m for m in log if m["clock_out"] == "night"]
    days = [m for m in log if m["clock_out"] == "day"]
    shifts = nights + days
    ledger = ledger_data["outcomes"]
    ttm = [p["hours_to_merge"] for p in merged if p["hours_to_merge"]]
    ttr = [p["hours_to_first_review"] for p in prs if p["hours_to_first_review"]]

    return {
        "date": NOW.date().isoformat(),
        "shifts_logged": len(shifts),
        "night_clock_outs": len(nights),
        "day_clock_outs": len(days),
        # From the ledger: a night's clock-out has no outcome field, so this
        # is not in Slack to be read. `—` in the ledger's PR column covers
        # both a no-work night and an `ops` push, so the outcome word is the
        # only thing that tells them apart.
        "no_work_shifts": ledger.get("no-work", 0),
        "ledger_lines": ledger_data["rows"],
        "ledger_merged": ledger.get("merged", 0),
        "ledger_sent_back": ledger.get("sent-back", 0),
        "ledger_shipped_for_review": ledger.get("shipped-for-review", 0),
        # Both of these should be 0. A verdict the parser cannot read, or a
        # ledger outcome that is not in `ledger.py`'s list, means a format
        # moved -- and the point of logbook#65 is that it should cost a
        # number here rather than going unnoticed.
        "unparsed_verdicts": len(slack_data.get("unparsed_verdicts", [])),
        "ledger_unknown_outcomes": len(ledger_data["unknown"]),
        "prs_opened": len(prs),
        "prs_merged": len(merged),
        "prs_sent_back": sum(1 for p in prs if p["sent_back"]),
        "review_rounds_mean": round(
            sum(p["rounds"] for p in prs) / len(prs), 2) if prs else 0,
        "line_comments_total": sum(p["line_comments"] for p in prs),
        "repos_total": len([r for r in gh_data["repos"] if not r["archived"]]),
        "repos_created": sum(1 for r in gh_data["repos"] if r["created_this_window"]),
        "repos_stale_14d": sum(
            1 for r in gh_data["repos"]
            if not r["archived"] and (r["days_since_push"] or 0) > 14),
        "hours_to_merge_mean": round(sum(ttm) / len(ttm), 1) if ttm else None,
        "hours_to_review_mean": round(sum(ttr) / len(ttr), 1) if ttr else None,
        "workflow_failures": sum(
            1 for r in gh_data["workflow_runs"] if r["conclusion"] == "failure"),
        "logbook_issues_open": sum(
            1 for i in gh_data["logbook_issues"] if i["state"] == "open"),
        # Candidates, not findings. Moss classifies these by hand.
        "disagreement_candidates": sum(
            1 for e in gh_data["excerpts"] + slack_data["excerpts"]
            if e["kind"] == "disagreement"),
        "self_correction_candidates": sum(
            1 for e in gh_data["excerpts"] + slack_data["excerpts"]
            if e["kind"] == "self_correction"),
    }


def write_series_row(csv_path, row):
    """Append `row`, rewriting the header if this run added columns.

    The old code appended under whatever header was already there. Add a
    field and every row after it is written one column to the left of its
    name, which `csv.DictReader` in make-charts.py reads without complaint
    and charts. Growing the header is a migration, so do it as one: keep the
    old column order, put new columns on the end, and give the rows already
    in the file an empty cell rather than inventing a value for a week
    nobody measured.
    """
    old_rows, old_cols = [], []
    if csv_path.exists():
        with csv_path.open(newline="") as f:
            reader = csv.DictReader(f)
            old_cols = reader.fieldnames or []
            old_rows = list(reader)

    cols = old_cols + [c for c in row if c not in old_cols]
    if cols == old_cols:
        with csv_path.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=cols).writerow(row)
        return

    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, restval="")
        w.writeheader()
        for old in old_rows:
            w.writerow({k: v for k, v in old.items() if k in cols})
        w.writerow(row)
    added = [c for c in cols if c not in old_cols]
    if old_cols and added:
        print(f"series.csv: added columns {', '.join(added)}; "
              f"{len(old_rows)} existing rows left empty there", file=sys.stderr)


def main():
    out_dir = Path(os.environ.get("METRICS_DIR", "logbook/metrics"))
    if not out_dir.parent.exists():
        sys.exit(f"{out_dir.parent} not found — is logbook cloned?")
    (out_dir / "data").mkdir(parents=True, exist_ok=True)

    gh_data = collect_github()
    slack_data = collect_slack()
    ledger_data = read_ledger(out_dir.parent / "SHIFTS.md")

    snapshot = {
        "collected_at": NOW.isoformat(),
        "window_days": WINDOW_DAYS,
        "github": gh_data,
        "slack": slack_data,
        "ledger": ledger_data,
    }

    snap_path = out_dir / "data" / f"{NOW.date().isoformat()}.json"
    snap_path.write_text(json.dumps(snapshot, indent=2, default=str))

    row = series_row(gh_data, slack_data, ledger_data)
    write_series_row(out_dir / "series.csv", row)

    if slack_data["errors"]:
        print("SLACK ERRORS:", json.dumps(slack_data["errors"]), file=sys.stderr)
    if ledger_data["error"]:
        print("LEDGER UNREADABLE:", ledger_data["error"], file=sys.stderr)
    for u in slack_data.get("unparsed_verdicts", []):
        print(f"UNPARSED VERDICT {u['ts']} {u['author']}: {u['line']}",
              file=sys.stderr)
    for u in ledger_data["unknown"]:
        print(f"LEDGER OUTCOME NOT IN VOCABULARY: {u}", file=sys.stderr)

    print(json.dumps(row, indent=2))
    print(f"\nsnapshot: {snap_path}")


if __name__ == "__main__":
    main()
