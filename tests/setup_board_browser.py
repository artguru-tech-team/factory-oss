#!/usr/bin/env python3
"""Setup for the Board browser test: a temp FACTORY_HOME, a fake `gh` on PATH (canned board data,
never the network), then serve the built page. Prints one JSON line with the URL, FACTORY_HOME,
the factory bin path and the build env; then serves until killed.
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
    temp_dir = tempfile.mkdtemp(prefix="factory-board-")
    os.environ["FACTORY_HOME"] = temp_dir

    # fake `gh` first on PATH: the board build reads canned data from it
    bin_dir = os.path.join(temp_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    gh = os.path.join(bin_dir, "gh")
    shutil.copyfile(os.path.join(os.path.dirname(__file__), "fake_gh_board.py"), gh)
    os.chmod(gh, 0o755)

    # a minimal empty T3 DB (the build always reads agents, but the board does not need any)
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
