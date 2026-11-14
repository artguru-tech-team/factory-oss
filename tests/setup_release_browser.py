#!/usr/bin/env python3
"""Setup for the Releases tab browser test: a temp FACTORY_HOME with three products
(one shipping, one on a rollback hold, one whose GitHub read failed), served on a free port.
Pass "empty" to seed no products. Prints one JSON line with url and key, then serves until killed.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from factory import release, serve, store, t3
from tests import datasource_env


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(empty):
    home = tempfile.mkdtemp(prefix="factory-release-")
    os.environ["FACTORY_HOME"] = home
    db = os.path.join(home, "statev2.sqlite")
    con = sqlite3.connect(db)
    for table, cols in t3.COLUMNS.items():
        con.execute(f"create table {table} ({', '.join(cols)})")
    con.commit()
    con.close()
    os.environ.update(datasource_env(home, FACTORY_T3_DB=db))
    cli = [os.path.join(os.path.dirname(__file__), "..", "bin", "factory")]
    for name in ("web", "app", "store"):  # the page needs at least one project; the empty case keeps no release product
        subprocess.check_call(cli + ["product", "set", name, "--name", name.title(), "--repo", "acme/" + name], stdout=subprocess.DEVNULL)
    now = datetime.now(timezone.utc)
    today = now.astimezone(release.TUNIS).strftime("%Y-%m-%d")
    rel = release.release()
    if not empty:
        for name in ("web", "app", "store"):
            subprocess.check_call(cli + ["release", "product", "add", name, "--repo", "acme/" + name, "--flow", "pr", "--project", name,
                                         "--display-name", name.title(), "--flow-label", "develop -> main"], stdout=subprocess.DEVNULL)
        rel = release.release()
        rel["products"]["web"]["mode"] = rel["products"]["app"]["mode"] = "live"  # store stays shadow
        rel["verdicts"][today] = {
            "web": {"verdict": "solo-red", "ship": [1289], "held": [{"number": 1292, "reason": "second red: only the oldest red ships solo"}],
                    "reasons": ["solo red: #1289 (adds migration)"], "at": iso(now)},
            "app": {"verdict": "wait", "ship": [], "held": [], "reasons": ["on hold: db connection leak"], "at": iso(now)},
            "store": {"verdict": "wait", "ship": [], "held": [], "at": iso(now),
                      "reasons": ["could not read: GitHub API 502 Bad Gateway for acme/store"]},
        }
        rel["holds"]["app"] = {"since": iso(now - timedelta(hours=8)), "issue": "db connection leak"}
        rel["journal"] = [
            {"at": iso(now - timedelta(days=1)), "product": "web", "event": "started", "prs": [1280], "sha": "9c01bf2aa", "note": ""},
            {"at": iso(now - timedelta(days=1)), "product": "web", "event": "shipped", "prs": [1280], "sha": "9c01bf2aa", "note": ""},
            {"at": iso(now - timedelta(hours=8)), "product": "app", "event": "rolled-back", "prs": [892], "sha": "4be1f3d00", "note": "db connection leak"},
        ]
        release.save_release(rel)
    subprocess.check_call(cli + ["build"], stdout=subprocess.DEVNULL)
    return store.key()


def main():
    key = seed("empty" in sys.argv[1:])
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    sys.stdout.write(json.dumps({"url": f"http://127.0.0.1:{port}/?k={key}", "key": key, "port": port,
                                 "factoryHome": os.environ["FACTORY_HOME"]}) + "\n")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
