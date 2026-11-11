#!/usr/bin/env python3
"""Setup for the Waiting on you messaging browser test: the Assign-an-agent fixture (sessions free1, free2,
busy; relay stub) plus, per viewport, two questions asked outside T3 and one asked by the busy session.
Prints one JSON line with the URL, FACTORY_HOME and the item ids."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from factory import serve, store
from tests import setup_assign_browser as sab


def main():
    temp_dir = tempfile.mkdtemp(prefix="factory-session-msg-")
    key = sab.setup(temp_dir)
    items = {}
    for view in ("desktop", "mobile"):
        free = store.add_ask_item("ds", "urgent", f"Expire the old claim tokens? ({view})", "Decision", "1,204 tokens left.",
                                  [["exp", "Expire after 30 days"], ["keep", "Keep them"]], "exp", "", "claude", None)
        known = store.add_ask_item("ds", "urgent", f"Pick the retry window ({view})", "Decision", "42 retries last week.",
                                   [["6h", "6 h"], ["1h", "1 h"]], "6h", "", "claude", "busy")
        race = store.add_ask_item("ds", "urgent", f"Ship the hotfix tonight? ({view})", "Decision", "3 users hit it.",
                                  [["yes", "Yes"], ["no", "No"]], "yes", "", "claude", None)
        items[view] = {"free": free["id"], "known": known["id"], "race": race["id"]}
    bin_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "factory"))
    subprocess.check_call([bin_path, "build"], stdout=subprocess.DEVNULL)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/?k={key}"
    sys.stdout.write(json.dumps({"url": url, "factoryHome": temp_dir, "items": items}) + "\n")
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
