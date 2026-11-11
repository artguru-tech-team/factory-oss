#!/usr/bin/env python3
"""Setup for the Assign-an-agent browser test: a temp FACTORY_HOME and T3 DB, a stub relay webhook,
the dispatcher configured, then the built page served. Prints one JSON line with the URL."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from factory import serve, store, t3
from tests import datasource_env

WS = "/w/acme"


def iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


class Webhook(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")


def setup(temp_dir):
    db = os.path.join(temp_dir, "statev2.sqlite")
    now = datetime.now(timezone.utc)
    con = sqlite3.connect(db)
    for table, cols in t3.COLUMNS.items():
        con.execute(f"create table {table} ({', '.join(cols)})")
    con.execute("insert into projection_projects values (?,?,?)", ("p1", WS, None))
    for tid, title, status in (("disp", "Dispatcher: launches threads", "completed"),
                               ("free1", "acme/chore/sentry-noise", "completed"),
                               ("free2", "Inbox router", "completed"),
                               ("busy", "acme-rel/ship the release", "running")):
        t = iso(now - timedelta(minutes=10))
        con.execute("insert into orchestration_v2_projection_threads values (?,?,?,?,?,?,?,?)",
                    (tid, "p1", title, t, t, None, None, json.dumps({"lineage": {"rootThreadId": tid}})))
        con.execute("insert into orchestration_v2_projection_runs values (?,?,?,?,?,?)",
                    (tid, status, t, t, json.dumps({"modelSelection": {"model": "claude-opus-5-5"}}), "claudeAgent"))
    con.commit()
    con.close()

    hook = HTTPServer(("127.0.0.1", 0), Webhook)
    threading.Thread(target=hook.serve_forever, daemon=True).start()

    env = {**os.environ, "FACTORY_HOME": temp_dir, **datasource_env(temp_dir, FACTORY_T3_DB=db)}
    bin_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "factory"))

    def fy(*args):
        subprocess.check_call([bin_path, *args], env=env, stdout=subprocess.DEVNULL)

    fy("product", "set", "bk", "--name", "Acme", "--repo", "o/r", "--workspace", WS)
    fy("project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "Checkout: save the receipt and payer details",
       "--feat", "FEAT-42")
    fy("project", "add", "rel", "--product", "bk", "--kind", "build", "--name", "Release", "--feat", "FEAT-1")
    fy("project", "set", "ds", "--next", "Release: run the migration before the app ships",
       "--next", "Claude: start PR2 (payer details at checkout)")
    fy("project", "set", "rel", "--match", "acme-rel/")
    fy("update", "ds", "--health", "on-track", "--agent", "claude",
       "PR #12 (receipt record) merged to develop. Next: PR2 payer details.")
    fy("relay", "set", "--webhook", f"http://127.0.0.1:{hook.server_address[1]}/hook")
    fy("config", "set", "assign.dispatcher", "disp")
    os.environ["FACTORY_HOME"] = temp_dir
    os.environ.update(datasource_env(temp_dir, FACTORY_T3_DB=db))
    for pid in ("ds", "rel"):
        p = store.find_project(pid)
        p.update(phases=[{"name": "A", "total": 4, "start": "2026-10-20", "due": "2026-12-31",
                          "merged": 2, "inPR": 1, "blocked": 0}])
        store.save_project(p)
    fy("build")
    return store.key()


def main():
    temp_dir = tempfile.mkdtemp(prefix="factory-assign-")
    key = setup(temp_dir)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    sys.stdout.write(json.dumps({"url": f"http://127.0.0.1:{port}/?k={key}", "factoryHome": temp_dir}) + "\n")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
