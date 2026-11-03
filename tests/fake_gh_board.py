#!/usr/bin/env python3
"""Fake `gh` for the board browser test: canned responses, never the network.

Used by setup_board_browser.py, which copies this file to <FACTORY_HOME>/bin/gh and prepends that
directory to PATH so `factory build` (and the board's gh calls) read canned data. Set
FACTORY_BOARD_FAIL=1 to make every board data command fail, exercising the "GitHub unreachable"
error state. Set FACTORY_BOARD_EMPTY=1 to return no issues/PRs, exercising the empty state.
Mutations (issue edit/close/comment, pr comment, label create) are appended to gh-actions.log.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

REPO = "o/r"
ONE_HOUR_AGO = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat()

ISSUES_OPEN = [
    {"number": 142, "title": "Stripe checkout 500 on missing postal code in FR address",
     "state": "open", "created_at": "2026-11-02T08:00:00Z", "updated_at": "2026-11-02T10:00:00Z",
     "closed_at": None, "html_url": f"https://github.com/{REPO}/issues/142",
     "repository_url": f"https://api.github.com/repos/{REPO}",
     "body": "When the address has no postal code the checkout call 500s.",
     "labels": [{"name": "intake"}, {"name": "source:sentry"}]},
    {"number": 139, "title": "Auth token refresh race condition causes random logouts",
     "state": "open", "created_at": "2026-11-02T07:00:00Z", "updated_at": "2026-11-02T09:00:00Z",
     "closed_at": None, "html_url": f"https://github.com/{REPO}/issues/139",
     "repository_url": f"https://api.github.com/repos/{REPO}",
     "body": "Refreshing the token races the request, sometimes logging the user out.",
     "labels": [{"name": "factory"}, {"name": "source:sentry"}]},
    {"number": 22, "title": "Terraform state lock timeout in staging deploy workflow",
     "state": "open", "created_at": "2026-11-01T15:00:00Z", "updated_at": "2026-11-02T08:00:00Z",
     "closed_at": None, "html_url": f"https://github.com/{REPO}/issues/22",
     "repository_url": f"https://api.github.com/repos/{REPO}",
     "body": "",
     "labels": [{"name": "building"}, {"name": "source:manual"}]},
    {"number": 130, "title": "Board cards drag and drop ghosting on Firefox 128",
     "state": "open", "created_at": "2026-11-01T10:00:00Z", "updated_at": "2026-11-02T09:00:00Z",
     "closed_at": None, "html_url": f"https://github.com/{REPO}/issues/130",
     "repository_url": f"https://api.github.com/repos/{REPO}",
     "body": "",
     "labels": [{"name": "intake"}, {"name": "source:manual"}]},
    {"number": 135, "title": "Webhook retry storm on 429 response from supplier API",
     "state": "open", "created_at": "2026-11-01T09:00:00Z", "updated_at": "2026-11-02T08:00:00Z",
     "closed_at": None, "html_url": f"https://github.com/{REPO}/issues/135",
     "repository_url": f"https://api.github.com/repos/{REPO}",
     "body": "",
     "labels": [{"name": "needs-decision"}, {"name": "source:sentry"}]},
]

ISSUES_CLOSED = [
    {"number": 79, "title": "Video duration reported as 0:00 on webm upload from Chrome on Android",
     "state": "closed", "created_at": "2026-11-01T08:00:00Z", "updated_at": ONE_HOUR_AGO,
     "closed_at": ONE_HOUR_AGO, "html_url": f"https://github.com/{REPO}/issues/79",
     "repository_url": f"https://api.github.com/repos/{REPO}",
     "body": "",
     "labels": [{"name": "intake"}, {"name": "source:bug"}]},
]

GRAPHQL_DATA = {
    "data": {
        "open": {"nodes": [
            {"number": 301, "isDraft": False, "url": f"https://github.com/{REPO}/pull/301",
             "closingIssuesReferences": {"nodes": [
                 {"number": 130, "repository": {"nameWithOwner": REPO}}]}},
        ]},
        "merged": {"nodes": [
            {"number": 89, "mergedAt": ONE_HOUR_AGO, "url": f"https://github.com/{REPO}/pull/89",
             "closingIssuesReferences": {"nodes": [
                 {"number": 79, "repository": {"nameWithOwner": REPO}}]}},
        ]},
    }
}

TIMELINE = [
    {"event": "cross-referenced", "created_at": "2026-11-02T08:05:00Z",
     "source": {"type": "issue", "issue": {"number": 301, "repository_url": f"https://api.github.com/repos/{REPO}",
                                           "pull_request": {"merged_at": None}}}},
    {"event": "labeled", "created_at": "2026-11-02T08:01:00Z", "label": {"name": "intake"}},
]


def fail_if_asked():
    if os.environ.get("FACTORY_BOARD_FAIL") == "1":
        sys.stderr.write("gh: GitHub unreachable (fake)\n")
        sys.exit(1)


def empty_if_asked():
    return os.environ.get("FACTORY_BOARD_EMPTY") == "1"


def log_action():
    path = os.path.join(os.environ.get("FACTORY_HOME", "."), "gh-actions.log")
    with open(path, "a") as f:
        f.write(json.dumps(sys.argv[1:]) + "\n")


def arg_value(argv, flag):
    try:
        return argv[argv.index(flag) + 1]
    except (ValueError, IndexError):
        return None


def main():
    argv = sys.argv[1:]
    if not argv:
        return
    cmd = argv[0]
    if cmd == "api" and len(argv) >= 2:
        sub = argv[1]
        if sub == "graphql":
            fail_if_asked()
            if empty_if_asked():
                print(json.dumps({"data": {"open": {"nodes": []}, "merged": {"nodes": []}}}))
            else:
                print(json.dumps(GRAPHQL_DATA))
            return
        if sub.startswith("search/issues?"):
            fail_if_asked()
            q = unquote(sub)
            if empty_if_asked():
                print(json.dumps({"total_count": 0, "items": []}))
            elif "is:closed" in q:
                print(json.dumps({"total_count": len(ISSUES_CLOSED), "items": ISSUES_CLOSED}))
            else:
                print(json.dumps({"total_count": len(ISSUES_OPEN), "items": ISSUES_OPEN}))
            return
        if sub.startswith("repos/") and "/timeline" in sub:
            fail_if_asked()
            print(json.dumps(TIMELINE))
            return
    if cmd == "issue" and len(argv) >= 2:
        if argv[1] == "view":
            fail_if_asked()
            js = arg_value(argv, "--json") or ""
            if "state" in js:
                # promote's write check reads the issue's GitHub state and labels
                print(json.dumps({"state": "open", "labels": [{"name": "intake"}, {"name": "source:sentry"}]}))
            else:
                print(json.dumps({"createdAt": "2026-11-02T08:00:00Z"}))
            return
        fail_if_asked()
        log_action()
        return
    if cmd == "label" and len(argv) >= 2:
        if argv[1] == "list":
            fail_if_asked()
            print(json.dumps([{"name": "intake"}, {"name": "factory"}, {"name": "needs-decision"},
                              {"name": "building"}]))
            return
        # create: succeed (the label may already exist)
        fail_if_asked()
        log_action()
        return
    if cmd == "pr" and len(argv) >= 2 and argv[1] == "comment":
        fail_if_asked()
        log_action()
        return
    # doctor / auth status / anything else: succeed silently
    return


if __name__ == "__main__":
    main()
