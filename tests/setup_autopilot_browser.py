#!/usr/bin/env python3
"""Setup for the Autopilot browser test: a temp FACTORY_HOME, a temp T3 DB with a few threads
in different states, autopilot entries, then serve the built page.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from factory import store, serve, t3
from tests import datasource_env

WS = "/w/acme"


def iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def setup_temp_factory(base_dir=None):
    temp_dir = base_dir or tempfile.mkdtemp(prefix="factory-autopilot-")
    db_path = os.path.join(temp_dir, "statev2.sqlite")
    now = datetime.now(timezone.utc)

    con = sqlite3.connect(db_path)
    for table, cols in t3.COLUMNS.items():
        con.execute(f"create table {table} ({', '.join(cols)})")
    con.execute("insert into projection_projects values (?,?,?)", ("p1", WS, None))

    def thread(tid, status, updated, waiting=False):
        payload = json.dumps({"lineage": {"rootThreadId": tid}})
        con.execute("insert into orchestration_v2_projection_threads values (?,?,?,?,?,?,?,?)",
                    (tid, "p1", "task " + tid, iso(updated), iso(updated), None, None, payload))
        if status:
            con.execute("insert into orchestration_v2_projection_runs values (?,?,?,?,?,?)",
                        (tid, status, iso(updated), iso(updated),
                         json.dumps({"modelSelection": {"model": "claude-opus-5-5"}}), "claudeAgent"))
        if waiting:
            con.execute("insert into orchestration_v2_projection_runtime_requests values (?,?)", (tid, "pending"))

    thread("idle1", "running", now - timedelta(hours=3))                    # silent 3 h: idle (stalled)
    thread("wait1", "running", now - timedelta(hours=1), waiting=True)      # waiting on you
    thread("run1", "running", now - timedelta(minutes=10))                  # working
    con.commit()
    con.close()

    env = {**os.environ, "FACTORY_HOME": temp_dir, **datasource_env(temp_dir, FACTORY_T3_DB=db_path)}
    bin_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "factory"))

    subprocess.check_call([bin_path, "product", "set", "bk", "--name", "Acme", "--repo", "o/r", "--workspace", WS],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "Design System"],
                          env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "project", "add", "rel", "--product", "bk", "--kind", "build", "--name", "Release"],
                          env=env, stdout=subprocess.DEVNULL)

    os.environ["FACTORY_HOME"] = temp_dir
    for k, v in datasource_env(temp_dir, FACTORY_T3_DB=db_path).items():
        os.environ[k] = v

    for pid in ("ds", "rel"):
        p = store.find_project(pid)
        p.update(phases=[{"name": "A", "total": 4, "start": "2026-10-20", "due": "2026-10-31",
                          "merged": 2, "inPR": 1, "blocked": 0}])
        store.save_project(p)

    cfg = store.autopilot()
    cfg["projects"]["ds"] = {"thread": "idle1", "goal": "Migrate Button and Tag to DS tokens",
                             "every_hours": 3, "enabled": True, "max_per_day": 4,
                             "last_nudge_at": None, "history": []}
    cfg["projects"]["rel"] = {"thread": "wait1", "goal": "Wait for merge of #95 before continuing",
                              "every_hours": 6, "enabled": True, "max_per_day": 4,
                              "last_nudge_at": None, "history": []}
    store.save_autopilot(cfg)

    subprocess.check_call([bin_path, "build"], env=env, stdout=subprocess.DEVNULL)
    key = store.key()
    return {"factoryHome": temp_dir, "t3Db": db_path, "key": key, "env": env, "bin": bin_path}


def main():
    info = setup_temp_factory()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    out = {"url": f"http://127.0.0.1:{port}/?k={info['key']}", "factoryHome": info["factoryHome"],
           "key": info["key"], "port": port, "bin": info["bin"]}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        shutil.rmtree(info["factoryHome"], ignore_errors=True)


if __name__ == "__main__":
    main()
