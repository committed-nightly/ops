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
GH = "https://api.github.com"
SLACK = "https://slack.com/api"

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

            body = pr.get("body") or ""
            for r in reviews:
                body += "\n" + (r.get("body") or "")
            for c in comments:
                body += "\n" + (c.get("body") or "")

            for kind, pats in (("disagreement", DISAGREEMENT_PATTERNS),
                               ("self_correction", SELF_CORRECTION_PATTERNS)):
                for hit in find(pats, body):
                    excerpts.append({
                        "kind": kind, "source": f"{name}#{pr['number']}",
                        "author": pr["user"]["login"], "excerpt": hit,
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

def collect_slack():
    channels = {
        "shift-log": os.environ.get("SLACK_SHIFT_LOG"),
        "orders": os.environ.get("SLACK_ORDERS"),
        "incidents": os.environ.get("SLACK_INCIDENTS"),
        "general": os.environ.get("SLACK_GENERAL"),
        "memes": os.environ.get("SLACK_MEMES"),
    }
    out, excerpts, errors = {}, [], []

    for label, cid in channels.items():
        if not cid:
            errors.append({"channel": label, "error": "no channel id configured"})
            continue

        body = slack("conversations.history", channel=cid, limit=200,
                     oldest=str(SINCE.timestamp()))
        if not body.get("ok"):
            errors.append({"channel": label, "error": body["error"]})
            continue

        msgs = []
        for m in body.get("messages", []):
            text = m.get("text", "")
            msgs.append({
                "ts": m["ts"],
                "at": datetime.fromtimestamp(float(m["ts"]), timezone.utc).isoformat(),
                "author": m.get("username") or m.get("user") or "?",
                "chars": len(text),
                "threaded": bool(m.get("thread_ts") and m["thread_ts"] != m["ts"]),
                "reply_count": m.get("reply_count", 0),
                # The clock-out format is structured; parse the outcome
                # rather than asking the model to eyeball it.
                "outcome": next(
                    (o for o in ("no-work", "shipped-for-review", "revised",
                                 "abandoned", "merged", "sent back", "closed")
                     if o in text.lower()), None),
            })
            for kind, pats in (("disagreement", DISAGREEMENT_PATTERNS),
                               ("self_correction", SELF_CORRECTION_PATTERNS)):
                for hit in find(pats, text):
                    excerpts.append({
                        "kind": kind, "source": f"slack/{label}",
                        "author": m.get("username", "?"), "excerpt": hit,
                    })

        out[label] = msgs

    return {"channels": out, "excerpts": excerpts, "errors": errors}


# ---------------------------------------------------------------- output

def series_row(gh_data, slack_data):
    prs = [p for p in gh_data["prs"] if p["in_window"]]
    merged = [p for p in prs if p["merged"]]
    log = slack_data["channels"].get("shift-log", [])
    shifts = [m for m in log if m["outcome"]]
    ttm = [p["hours_to_merge"] for p in merged if p["hours_to_merge"]]
    ttr = [p["hours_to_first_review"] for p in prs if p["hours_to_first_review"]]

    return {
        "date": NOW.date().isoformat(),
        "shifts_logged": len(shifts),
        "no_work_shifts": sum(1 for m in shifts if m["outcome"] == "no-work"),
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


def main():
    out_dir = Path("metrics")
    (out_dir / "data").mkdir(parents=True, exist_ok=True)

    gh_data = collect_github()
    slack_data = collect_slack()

    snapshot = {
        "collected_at": NOW.isoformat(),
        "window_days": WINDOW_DAYS,
        "github": gh_data,
        "slack": slack_data,
    }

    snap_path = out_dir / "data" / f"{NOW.date().isoformat()}.json"
    snap_path.write_text(json.dumps(snapshot, indent=2, default=str))

    row = series_row(gh_data, slack_data)
    csv_path = out_dir / "series.csv"
    exists = csv_path.exists()
    with csv_path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if not exists:
            w.writeheader()
        w.writerow(row)

    if slack_data["errors"]:
        print("SLACK ERRORS:", json.dumps(slack_data["errors"]), file=sys.stderr)

    print(json.dumps(row, indent=2))
    print(f"\nsnapshot: {snap_path}")


if __name__ == "__main__":
    main()
