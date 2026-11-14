#!/usr/bin/env python3
"""Setup for the product manager browser test: a temp FACTORY_HOME seeded with projects that cover every
sweep group (finished, validated prototype not built, quiet, late) plus a closed one, a Now lane, support digest
runs, an owner message, an old inbox item, two release products (GitHub read faked), project PRs at each lifecycle
stage (open, merged, shipped and announced), the built page, then a server. Prints one JSON line with the URL; serves until killed.
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
from factory import comms, digest, release, serve, store, t3
from tests import datasource_env

NOW = datetime.now(timezone.utc)


def ago(days):
    return store.iso(NOW - timedelta(days=days))


def day(offset):
    return (NOW + timedelta(days=offset)).date().isoformat()


def phase(name, merged, total, due):
    return {"name": name, "merged": merged, "inPR": 0, "blocked": 0, "total": total, "start": day(-40), "due": due}


def project(pid, name, phases, created=1, **kw):
    store.save_project({"id": pid, "product": "acme", "kind": "build", "name": name, "feat": "", "prio": None, "next": [],
                        "prs": [], "match": [], "createdAt": ago(created), "phases": phases, "scopes": [], "added": 0,
                        "history": {NOW.date().isoformat(): [sum(p["total"] for p in phases), sum(p["merged"] for p in phases)]},
                        **kw})


def seed():
    store.write("products.json", {"acme": {"name": "Acme", "workspaces": [], "repos": ["acme/app"], "run_labels": []},
                                  "store": {"name": "Store", "workspaces": [], "repos": [], "run_labels": []},
                                  "tools": {"name": "Tools", "workspaces": [], "repos": [], "run_labels": []}})
    project("fee-engine", "New fee engine: VAT and commission", [phase("Build", 17, 17, day(-9))])
    project("landing", "Switch the landing page to the new design", [phase("Build", 16, 16, day(-3))])
    project("release-panel", "Release manager panel", [phase("Build", 6, 6, day(-5))])
    project("kyc-lock", "Lock KYC: block withdrawals without KYC", [phase("Build", 1, 4, day(30))], created=13, history={})
    project("usdc", "USDC payouts", [phase("Build", 0, 3, day(45))], created=13, history={})
    project("disputes", "Dispute evidence: payer receipt, capture", [phase("Build", 2, 4, day(-3))])
    project("healthy", "Payment-link API for merchants", [phase("Build", 5, 14, day(10))])
    project("old-launch", "Creator profile redesign", [phase("Build", 16, 16, day(-30))], created=40, closedAt=ago(2), closeNote="in prod")
    store.append_line("updates/healthy.jsonl", {"at": ago(1), "agent": "ab", "health": "onTrack", "text": "units merged"})
    store.write("autopilot.json", {"paused": False, "pausedAt": None, "daily_cap": 12, "projects": {"disputes": {
        "thread": "t-disputes", "goal": "finish the evidence capture", "every_hours": 4, "max_per_day": 2, "enabled": True,
        "history": []}}})
    store.write_text("protos/acme-release-panel/v1.html", "<!doctype html><title>Release panel</title><p>Prototype</p>")
    store.save_protos([{"id": "acme-release-panel", "product": "acme", "feature": "Release manager panel", "project": "release-panel",
                        "versions": [{"v": 1, "status": "validated", "date": ago(7), "file": "protos/acme-release-panel/v1.html",
                                      "note": "", "look_at": [], "decisions": None, "sent_back_reason": None, "pr": None,
                                      "agent": None, "thread": None}]}])
    # phase 2: a Now lane, support digest runs, an owner message and an inbox item waiting for 2 days
    now_rows = ["Faster search page", "Team invites by email", "Bulk export for large workspaces", "Saved filters"]
    store.write("roadmap.json", {"sources": {}, "items": [
        {"id": f"ACME-{k}", "product": "acme", "title": t, "lane": "now", "rank": k, "source": "factory"} for k, t in enumerate(now_rows, 1)]})
    store.write("digest.json", dict(digest.DEFAULT_DIGEST, history=[
        {"at": ago(2), "conversations": 40, "cost_usd": 0.01, "result": "Dark mode for the dashboard (4 tickets)", "error": None, "posted": True},
        {"at": ago(1), "conversations": 52, "cost_usd": 0.01, "result": "Bulk export times out on large workspaces (9 tickets)",
         "error": None, "posted": True}]))
    store.write("relay.json", [{"id": "m-1", "thread": "t-disputes", "text": "x", "raw": "please speed up the search page loading",
                                "kind": "message", "state": "sent", "tries": 1, "at": ago(1)}])
    store.add_ask_item("healthy", "decision", "Pick an email provider for team invites", "Email provider", "Two offers.",
                       [["a", "Provider A"], ["b", "Provider B"]], "a", "", "agent", None, at=ago(2))


def seed_release():
    """Two release products for "Merged, not in prod": one with PRs, one whose GitHub read fails (the read is faked).
    After launch: each shipped a release; shop's error check fails (the check and the PR titles are faked)."""
    errors = [{"name": "sentry", "kind": "errors", "cmd": ["errors-now"], "baseline_cmd": ["errors-baseline"]}]
    prod = lambda repo: {"repo": repo, "flow": "pr", "base": "develop", "head": "main", "thread": None, "red_paths": [],
                         "project": "healthy", "mode": "live", "checks": errors}
    acme = dict(prod("acme/app"), comms={"langs": ["en"], "audiences": ["users"]})
    shop = dict(prod("acme/shop"), checks=[dict(errors[0], cmd=["shop-errors"])])
    store.write("release.json", dict(release.DEFAULT_RELEASE, products={"acme": acme, "shop": shop}, shipped_prs={"acme": [1490, 1491]}, journal=[
        {"at": ago(8), "product": "shop", "event": "shipped", "prs": [310], "sha": "5e1f00d", "note": ""},
        {"at": ago(2), "product": "acme", "event": "shipped", "prs": [1490, 1491], "sha": "a1b2c3d", "note": ""}]))
    store.write("comms.json", {"changelogs": {}, "since": {}, "deferred": {}, "relayed": {}, "jobs": {
        "f-acme-1490": {"id": "f-acme-1490", "product": "acme", "kind": "feature", "prs": [{"n": 1490}], "status": "sent",
                        "sent_at": ago(1)}}})
    ok = {"name": "sentry", "kind": "errors", "ok": True, "value": 2, "baseline": 3, "detail": ""}
    # Lifecycle: project PRs shipped and announced, merged, open (set after the build: its fake gh knows no acme PR), and
    # two Now rows first seen in the Now lane 20 and 16 days ago (stuck past the 14-day limit)
    for pid, prs in (("fee-engine", [1490, 1491]), ("landing", [1498]), ("disputes", [1510])):
        p = store.find_project(pid)
        store.save_project(dict(p, prs=[f"acme/app#{n}" for n in prs]))
    stages = {"acme:ACME-1": {"stage": "Roadmap", "at": ago(20)}, "acme:ACME-4": {"stage": "Roadmap", "at": ago(16)}}
    store.write("pm.json", {"snoozed": {}, "lastSweep": {}, "stages": stages, "launches": {"shop:5e1f00d": {"at": ago(8), "checks": {"d1": {
        "at": ago(7), "chips": [{"name": "Comms sent", "state": "w", "detail": "no comms configured"},
                                {"name": "Errors since launch", "state": "y", "detail": "sentry: 2 (baseline 3)"},
                                {"name": "Support tickets", "state": "y", "detail": "0 mention it"}]}}}},
        # Roadmaps: store has an agent's proposed first roadmap, tools has none (acme's is in the Roadmap view)
        "roadmaps": {"store": {"at": ago(1), "sources": "12 open issues, 30 days of orders", "rows": [
            {"id": f"store-r{n}", "lane": lane, "title": t, "goal": g, "evidence": ev, "status": "proposed"} for n, (lane, t, g, ev) in enumerate([
                ("now", "Gift cards at checkout", "Gift card orders above 5% of sales", "#21 #24: 14 buyers asked"),
                ("next", "Wishlist sharing links", "1 in 10 wishlists shared", "#30: share button requested"),
                ("next", "Order history filters", "Support chats about old orders down 30%", "#33 #35"),
                ("later", "Loyalty points", "Repeat buyers up 10%", "#41")], 1)]}}})
    titles = {310: "Checkout: retry a declined card", 1490: "Bulk export as CSV", 1491: "Team invites: resend the link"}
    comms.pr_facts = lambda repo, ns: {n: {"title": titles[n]} for n in ns if n in titles}
    release._run_signal_check = lambda c, judge: dict(ok, **({"ok": False, "value": 41, "detail": "41 > 11 (baseline 3)"}
                                                             if c["cmd"] == ["shop-errors"] else {}))

    def fake(cfg):
        if cfg["repo"] == "acme/shop":
            raise store.FactoryError("gh: HTTP 502 from the GitHub API")
        return [{"number": 1498, "title": "Search: highlight matches"}, {"number": 1502, "title": "Export as CSV"}], "sha", []
    release.unreleased_prs = fake


def setup_temp_factory():
    temp_dir = tempfile.mkdtemp(prefix="factory-pm-")
    os.environ["FACTORY_HOME"] = temp_dir
    bin_dir = os.path.join(temp_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    gh = os.path.join(bin_dir, "gh")
    shutil.copyfile(os.path.join(os.path.dirname(__file__), "fake_gh_board.py"), gh)
    os.chmod(gh, 0o755)
    db_path = os.path.join(temp_dir, "statev2.sqlite")
    con = sqlite3.connect(db_path)
    for table, cols in t3.COLUMNS.items():
        con.execute(f"create table {table} ({', '.join(cols)})")
    con.commit()
    con.close()
    env = {**os.environ, "FACTORY_HOME": temp_dir, "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
           **datasource_env(temp_dir, FACTORY_T3_DB=db_path)}
    bin_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "factory"))
    seed()
    subprocess.check_call([bin_path, "build"], env=env, stdout=subprocess.DEVNULL)
    return {"factoryHome": temp_dir, "key": store.key(), "bin": bin_path, "env": env}


def main():
    info = setup_temp_factory()
    os.environ["FACTORY_HOME"] = info["factoryHome"]
    for k, v in info["env"].items():
        if k not in os.environ or k in ("FACTORY_HOME", "PATH"):
            os.environ[k] = v
    seed_release()  # after the build, which never reads it: the server process gets the faked GitHub read
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    sys.stdout.write(json.dumps({"url": f"http://127.0.0.1:{port}/?k={info['key']}", "factoryHome": info["factoryHome"],
                                 "port": port}) + "\n")
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
