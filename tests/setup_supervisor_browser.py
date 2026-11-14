#!/usr/bin/env python3
"""Setup for the Supervisor browser test: the Autopilot test's temp FACTORY_HOME, plus a supervisor.json
with findings and a facts snapshot, then serve the built page."""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from factory import serve, store
from tests.setup_autopilot_browser import iso, setup_temp_factory


def rec(sig, status, sev, title, evidence, kind, seen, **kw):
    return {"signature": sig, "status": status, "severity": sev, "title": title, "evidence": evidence, "check": "demo",
            "action": {"kind": kind}, "attempts": 1, "actions_at": [], "first_seen": iso(seen), "last_seen": iso(seen), **kw}


def main():
    info = setup_temp_factory()
    now = datetime.now(timezone.utc)
    recs = [
        rec("acme:health-mismatch", "open", "warn", "acme: reported onTrack, facts say atRisk", "no update since 2026-11-01", "record", now - timedelta(minutes=40), would="would record"),
        rec("vps:disk", "open", "crit", "VPS disk is filling up", "disk / over 90% used", "infra", now - timedelta(minutes=10), would="would infra"),
        rec("job:digest:stale", "resolved", "warn", "job digest looks stalled", "digest not written for over 60 minutes", "rerun", now - timedelta(hours=2)),
        rec("job:old:stale", "resolved", "warn", "job old looks stalled", "old", "rerun", now - timedelta(hours=30)),
        # an old action on a closed finding must not show; a long result must not widen a phone screen
        rec("acme:done", "resolved", "warn", "acme: closed one", "e", "infra", now - timedelta(hours=1), did="would infra"),
        rec('acme:"q"<x>', "open", "warn", "acme: <i>quoted</i> signature", "e", "dispatch", now - timedelta(minutes=50),
            did="error: RuntimeError: the relay webhook answered 503 Service Unavailable after three retries",
            actions_at=[iso(now - timedelta(minutes=50))]),
        rec("acme:escalated", "escalated", "crit", "acme: escalated one", "e", "escalate", now - timedelta(minutes=20),
            did="quiet", last_result="inbox a42", actions_at=[iso(now - timedelta(hours=3))]),
    ]
    snap = {"at": iso(now), "jobs": [{"name": "chatscan", "max_min": 15, "last": iso(now - timedelta(minutes=3)), "stale": False},
                                      {"name": "digest", "max_min": 60, "last": iso(now - timedelta(hours=2)), "stale": True}],
            "health": [{"project": "acme", "reported": "onTrack", "computed": "atRisk", "why": "no update since 2026-11-01"},
                       {"project": "web", "reported": "onTrack", "computed": "onTrack", "why": "on schedule with a recent update"}],
            "vps": {"disk_pct": 92.1, "mem_pct": 55.0, "thresholds": {"disk_warn": 80, "disk_crit": 90, "mem_warn": 90}}}
    store.write("supervisor.json", {"last_run": iso(now - timedelta(minutes=5)), "findings": {r["signature"]: r for r in recs}, "snapshot": snap})
    subprocess.check_call([info["bin"], "build"], env=info["env"], stdout=subprocess.DEVNULL)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    sys.stdout.write(json.dumps({"url": f"http://127.0.0.1:{port}/?k={info['key']}", "factoryHome": info["factoryHome"]}) + "\n")
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
