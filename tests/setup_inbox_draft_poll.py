#!/usr/bin/env python3
"""Setup script for inbox draft poll Chromium test.

Builds a temp FACTORY_HOME with temp FACTORY_T3_DB and fake datasource paths,
creates product bk and project ds, seeds items A and B, runs factory build,
and optionally starts factory serve on an ephemeral port.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from factory import store, serve, t3
from tests import datasource_env


def setup_temp_factory(base_dir=None):
    temp_dir = base_dir or tempfile.mkdtemp(prefix="factory-draft-poll-")
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

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    bin_path = os.path.join(repo_root, "bin", "factory")

    subprocess.check_call([bin_path, "product", "set", "bk", "--name", "Acme", "--repo", "o/r", "--workspace", "/w/acme"], env=env, stdout=subprocess.DEVNULL)
    subprocess.check_call([bin_path, "project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "Design System"], env=env, stdout=subprocess.DEVNULL)

    os.environ["FACTORY_HOME"] = temp_dir
    for k, v in datasource_env(temp_dir, FACTORY_T3_DB=db_path).items():
        os.environ[k] = v

    p = store.find_project("ds")
    p.update(prs=[], phases=[{"name": "A", "total": 4, "start": "2026-10-20", "due": "2026-10-31", "merged": 2, "inPR": 1, "blocked": 0}])
    store.save_project(p)

    item_a = "d-poll-a"
    item_b = "d-poll-b"
    reset_inbox(temp_dir, item_a, item_b)

    subprocess.check_call([bin_path, "build"], env=env, stdout=subprocess.DEVNULL)
    key = store.key()

    return {
        "factoryHome": temp_dir,
        "t3Db": db_path,
        "itemA": item_a,
        "itemB": item_b,
        "key": key,
        "env": env,
        "bin": bin_path,
    }


def reset_inbox(temp_dir, item_a="d-poll-a", item_b="d-poll-b"):
    os.environ["FACTORY_HOME"] = temp_dir
    store.write("inbox.json", {
        "seq": 2,
        "items": [
            {"id": item_a, "project": "ds", "kind": "urgent", "title": "Decision A to resolve",
             "options": [["a", "Choice A"], ["b", "Choice B"]], "at": "2026-10-30T10:00:00Z", "target": "no session",
             "context": "Context for decision A", "reco": "a"},
            {"id": item_b, "project": "ds", "kind": "action", "title": "Decision B with draft",
             "options": [["1", "Choice 1"], ["2", "Choice 2"]], "at": "2026-10-30T10:00:00Z", "target": "no session",
             "context": "Context for decision B", "reco": "1"},
        ],
        "resolved": [],
    })
    store.write("answers.json", {})


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--reset":
        if len(sys.argv) < 3:
            print("usage: setup_inbox_draft_poll.py --reset <factory_home> [itemA] [itemB]", file=sys.stderr)
            sys.exit(1)
        home = sys.argv[2]
        item_a = sys.argv[3] if len(sys.argv) > 3 else "d-poll-a"
        item_b = sys.argv[4] if len(sys.argv) > 4 else "d-poll-b"
        reset_inbox(home, item_a, item_b)
        db_path = os.path.join(home, "statev2.sqlite")
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        bin_path = os.path.join(repo_root, "bin", "factory")
        env = {
            **os.environ,
            "FACTORY_HOME": home,
            **datasource_env(home, FACTORY_T3_DB=db_path),
        }
        subprocess.check_call([bin_path, "build"], env=env, stdout=subprocess.DEVNULL)
        print("ok: reset")
        return

    info = setup_temp_factory()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/?k={info['key']}"

    out = {
        "url": url,
        "factoryHome": info["factoryHome"],
        "itemA": info["itemA"],
        "itemB": info["itemB"],
        "key": info["key"],
        "port": port,
        "bin": info["bin"],
    }
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
