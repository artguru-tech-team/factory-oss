#!/usr/bin/env python3
"""Setup for the Roadmap browser test: a temp FACTORY_HOME with seeded roadmap items (full Now),
then serve the built page. Prints one JSON line with the URL, FACTORY_HOME, bin path and env;
then serves until killed.
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
from factory import serve, store, t3
from tests import datasource_env


def setup_temp_factory():
    temp_dir = tempfile.mkdtemp(prefix="factory-roadmap-")
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

    env = {
        **os.environ,
        "FACTORY_HOME": temp_dir,
        "PATH": bin_dir + os.pathsep + os.environ.get("PATH", ""),
        **datasource_env(temp_dir, FACTORY_T3_DB=db_path),
    }
    bin_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "factory"))

    subprocess.check_call([bin_path, "product", "set", "bk", "--name", "Acme", "--repo", "o/r"],
                          env=env, stdout=subprocess.DEVNULL)

    # Seed roadmap items with 7 in Now (FULL), 2 in Next, 2 in Later, 2 in Done
    items = [
        {"id": f"FEAT-{i}", "product": "bk", "title": f"Now Feature {i}", "lane": "now", "rank": i, "source": "factory"}
        for i in range(1, 8)
    ]
    items += [
        {"id": "FEAT-8", "product": "bk", "title": "Next Feature 8", "lane": "next", "rank": 1, "source": "factory"},
        {"id": "FEAT-9", "product": "bk", "title": "Next Feature 9", "lane": "next", "rank": 2, "source": "factory"},
        {"id": "FEAT-10", "product": "bk", "title": "Later Feature 10", "lane": "later", "rank": 1, "source": "factory"},
        {"id": "FEAT-11", "product": "bk", "title": "Later Feature 11", "lane": "later", "rank": 2, "source": "factory"},
        {"id": "FEAT-12", "product": "bk", "title": "Done Feature 12", "lane": "done", "rank": 1, "done": "2026-11-01", "source": "factory"},
        {"id": "FEAT-13", "product": "bk", "title": "Done Feature 13", "lane": "done", "rank": 2, "done": "2026-10-15", "source": "factory"},
    ]
    store.write("roadmap.json", {"items": items, "sources": {}})

    subprocess.check_call([bin_path, "build"], env=env, stdout=subprocess.DEVNULL)

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
    finally:
        srv.server_close()
        shutil.rmtree(info["factoryHome"], ignore_errors=True)


if __name__ == "__main__":
    main()
