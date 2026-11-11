#!/usr/bin/env python3
"""Setup for the Develop queue browser test: a temp FACTORY_HOME with canned queue data
built by calling the real queue.queue() with queue.open_prs and release.unreleased_prs mocked.
Prints JSON with url, factoryHome, key, and serves until killed.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from factory import queue, release, serve, store, t3
from tests import datasource_env


def setup_temp_factory():
    temp_dir = tempfile.mkdtemp(prefix="factory-queue-")
    os.environ["FACTORY_HOME"] = temp_dir

    db_path = os.path.join(temp_dir, "statev2.sqlite")
    con = sqlite3.connect(db_path)
    for table, cols in t3.COLUMNS.items():
        con.execute(f"create table {table} ({', '.join(cols)})")
    con.commit()
    con.close()

    env = {
        **os.environ,
        "FACTORY_HOME": temp_dir,
        **datasource_env(temp_dir, FACTORY_T3_DB=db_path),
    }
    bin_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "factory"))

    # Add products
    subprocess.check_call([bin_path, "product", "set", "web", "--name", "Web", "--repo", "acme/web"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "product", "set", "app", "--name", "App", "--repo", "acme/app"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "product", "set", "store", "--name", "Store", "--repo", "acme/store"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "product", "set", "factory", "--name", "Factory", "--repo", "acme/factory"],
                          env=env, stdout=subprocess.DEVNULL)

    subprocess.check_call([bin_path, "release", "product", "add", "web", "--repo", "acme/web", "--flow", "pr", "--project", "web",
                           "--display-name", "Web", "--color", "#4670CF", "--comms-langs", "fr", "en", "--comms-audiences", "creators", "--short", "web", "--flow-label", "develop → main · 07:30"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "release", "product", "add", "app", "--repo", "acme/app", "--flow", "pr", "--project", "app",
                           "--display-name", "App", "--color", "#17C964", "--comms-langs", "en", "fr", "--comms-audiences", "creators", "brands", "--short", "app", "--flow-label", "develop → main · 07:30"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "release", "product", "add", "store", "--repo", "acme/store", "--flow", "pr", "--project", "store",
                           "--display-name", "Store", "--color", "#5433eb", "--comms-langs", "fr", "en", "--comms-audiences", "customers", "--short", "sto", "--flow-label", "develop → main · 07:30"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "release", "product", "add", "factory", "--repo", "acme/factory", "--flow", "factory", "--project", "factory",
                           "--display-name", "Factory", "--short", "fac", "--flow-label", "PR → main · 11:00"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "build"], env=env, stdout=subprocess.DEVNULL)

    # Realistic PRs:
    # 1. web:
    #    - fix: #1392 (CI green, review clean, ready to merge)
    #    - feat needing OK: #1155
    #    - review PR: #1422
    #    - conflicting PR: #221
    #    - held PR: #999
    #    - bot bumps: #1358, #1359 (clean dependency bumps)
    # 2. app:
    #    - red bot set: 5 bot PRs all failing CI -> dev_red blocked!
    # 3. store:
    #    - open_prs error (GitHub 502)
    # 4. factory:
    #    - release PR into main: #1431
    now = datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)  # Tuesday morning

    web_prs = [
        {
            "number": 1392,
            "title": "fix(feed): add Contact to the mobile More sheet",
            "baseRefName": "develop",
            "headRefName": "web/fix/mobile-contact-link",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-11-05T12:00:00Z",
            "updatedAt": "2026-11-05T12:00:00Z",
            "author": "dev",
            "is_bot": False,
            "labels": [],
            "failing_contexts": 0,
            "has_pending": False,
            "review_state": "SUCCESS",
        },
        {
            "number": 1155,
            "title": "feat(feed): post the share video or card on my page in one tap",
            "baseRefName": "develop",
            "headRefName": "web/feat/post-to-page",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-10-12T12:00:00Z",
            "updatedAt": "2026-10-12T12:00:00Z",
            "author": "dev",
            "is_bot": False,
            "labels": [],
            "failing_contexts": 0,
            "has_pending": False,
            "review_state": "SUCCESS",
        },
        {
            "number": 1422,
            "title": "[agent] fix: Fee switch rollback charges net-credited tips twice",
            "baseRefName": "develop",
            "headRefName": "agent/web-1331",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-11-07T12:00:00Z",
            "updatedAt": "2026-11-07T12:00:00Z",
            "author": "dev",
            "is_bot": False,
            "labels": [],
            "failing_contexts": 0,
            "has_pending": False,
            "review_state": "none",
        },
        {
            "number": 221,
            "title": "Feat/add moderator steps",
            "baseRefName": "develop",
            "headRefName": "feat/add-moderator-steps",
            "isDraft": False,
            "mergeable": "CONFLICTING",
            "createdAt": "2025-09-27T12:00:00Z",
            "updatedAt": "2026-08-15T12:00:00Z",
            "author": "dev",
            "is_bot": False,
            "labels": [],
            "failing_contexts": 0,
            "has_pending": False,
            "review_state": "none",
        },
        {
            "number": 999,
            "title": "refactor(core): old held experiment",
            "baseRefName": "develop",
            "headRefName": "held/experiment",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-10-01T12:00:00Z",
            "updatedAt": "2026-10-01T12:00:00Z",
            "author": "dev",
            "is_bot": False,
            "labels": [],
            "failing_contexts": 0,
            "has_pending": False,
            "review_state": "SUCCESS",
        },
        {
            "number": 1358,
            "title": "chore(deps): bump lodash from 4.17.23 to 4.18.1",
            "baseRefName": "develop",
            "headRefName": "dependabot/npm_and_yarn/lodash-4.18.1",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-11-04T12:00:00Z",
            "updatedAt": "2026-11-04T12:00:00Z",
            "author": "dependabot[bot]",
            "is_bot": True,
            "labels": [],
            "failing_contexts": 0,
            "has_pending": False,
            "review_state": "none",
        },
        {
            "number": 1359,
            "title": "chore(deps): bump axios from 1.6.0 to 1.6.1",
            "baseRefName": "develop",
            "headRefName": "dependabot/npm_and_yarn/axios-1.6.1",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-11-04T12:00:00Z",
            "updatedAt": "2026-11-04T12:00:00Z",
            "author": "dependabot[bot]",
            "is_bot": True,
            "labels": [],
            "failing_contexts": 0,
            "has_pending": False,
            "review_state": "none",
        },
        {
            "number": 1431,
            "title": "release: develop -> main (2026-11-07)",
            "baseRefName": "main",
            "headRefName": "release/2026-11-07",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-11-07T12:00:00Z",
            "updatedAt": "2026-11-07T12:00:00Z",
            "author": "dev",
            "is_bot": False,
            "labels": [],
            "failing_contexts": 0,
            "has_pending": False,
            "review_state": "SUCCESS",
        },
    ]

    # app: 5 bot PRs failing into develop -> dev_red True
    app_prs = [
        {
            "number": 100 + i,
            "title": f"build(deps): bump package-{i} from 1.0 to 1.1",
            "baseRefName": "develop",
            "headRefName": f"dependabot/npm/pkg-{i}",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-11-06T12:00:00Z",
            "updatedAt": "2026-11-06T12:00:00Z",
            "author": "dependabot[bot]",
            "is_bot": True,
            "labels": [],
            "failing_contexts": 1,
            "has_pending": False,
            "review_state": "none",
        }
        for i in range(5)
    ]

    factory_prs = [
        {
            "number": 55,
            "title": "fix(cli): add quiet flag to build command",
            "baseRefName": "main",
            "headRefName": "factory/fix/quiet-flag",
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "createdAt": "2026-11-08T12:00:00Z",
            "updatedAt": "2026-11-08T12:00:00Z",
            "author": "dev",
            "is_bot": False,
            "labels": [],
            "failing_contexts": 0,
            "has_pending": False,
            "review_state": "SUCCESS",
        }
    ]

    def mock_open_prs(prod_cfg):
        repo = prod_cfg.get("repo", "")
        if "web" in repo:
            return web_prs
        elif "app" in repo:
            return app_prs
        elif "store" in repo:
            raise Exception("GitHub returned 502 for acme/store")
        elif "factory" in repo:
            return factory_prs
        return []

    def mock_unreleased(prod_cfg):
        repo = prod_cfg.get("repo", "")
        if "web" in repo:
            return (
                [
                    {"number": 101, "title": "feat(payments): store the capture id", "mergedAt": "2026-11-04T18:08:10Z"},
                    {"number": 102, "title": "fix(feed): small readiness fixes", "mergedAt": "2026-11-04T18:14:59Z"},
                ],
                "sha0",
                [],
            )
        return ([], "sha0", [])

    # Hold #999
    queue.record_queue_action("acme/web#999", "hold", now=now)

    # Set journal for prod entry
    rel = release.release()
    rel["journal"] = [
        {"product": "web", "event": "shipped", "at": "2026-11-05T10:00:00Z", "sha": "33dfe87"},
    ]
    # Set verdict for app so health = [] (green checks pass)
    today_tunis = now.astimezone(release.TUNIS).date().isoformat()
    rel["verdicts"] = {
        today_tunis: {
            "app": {"verdict": "ship-all", "ship": [], "reasons": []}
        }
    }
    release.save_release(rel)

    with mock.patch.object(queue, "open_prs", side_effect=mock_open_prs), \
         mock.patch.object(release, "unreleased_prs", side_effect=mock_unreleased):
        snapshot, _ = queue.queue(now=now)

    key = store.key()
    return {"factoryHome": temp_dir, "t3Db": db_path, "key": key, "bin": bin_path, "env": env}


def main():
    info = setup_temp_factory()
    os.environ["FACTORY_HOME"] = info["factoryHome"]
    for k, v in info["env"].items():
        if k not in os.environ or k == "FACTORY_HOME" or k == "PATH":
            os.environ[k] = v
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    out = {"url": f"http://127.0.0.1:{port}/?k={info['key']}", "factoryHome": info["factoryHome"],
           "key": info["key"], "port": port, "bin": info["bin"], "env": info["env"]}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
