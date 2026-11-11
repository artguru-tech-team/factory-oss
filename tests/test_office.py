import base64
import hashlib
import io
import json
import os
import re
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from factory import build, cli, config, office, serve, sources, store, t3


def make_t3_fixture(db_path):
    con = sqlite3.connect(db_path)
    for table, cols in t3.COLUMNS.items():
        con.execute(f"create table {table} ({', '.join(cols)})")
    con.executemany("insert into projection_projects values (?,?,?)",
                    [("p1", "/w/workspace", None)])

    def add_thread(tid, pid, title, updated):
        payload = {"lineage": {"rootThreadId": tid}}
        con.execute("insert into orchestration_v2_projection_threads values (?,?,?,?,?,?,?,?)",
                    (tid, pid, title, updated, updated, None, None, json.dumps(payload)))

    add_thread("orch-1", "p1", "Orchestrator session", "2026-10-30T10:00:00Z")
    add_thread("thread:delegated:2", "p1", "Delegate task", "2026-10-30T10:05:00Z")
    add_thread("thread:delegated-task:a%3Ab", "p1", "Delegate with a literal %", "2026-10-30T10:06:00Z")

    con.executemany(
        "insert into orchestration_v2_projection_messages values (?,?,?,?)",
        [
            ("orch-1", "user", "2026-10-30T09:00:00Z", json.dumps({"text": "First user message", "createdBy": "user"})),
            ("orch-1", "assistant", "2026-10-30T09:01:00Z", json.dumps({"text": "First assistant reply"})),
            ("orch-1", "user", "2026-10-30T09:05:00Z", json.dumps({"text": "Second user message"})),
            ("orch-1", "assistant", "2026-10-30T09:06:00Z", json.dumps({"content": [{"type": "text", "text": "A" * 5000}]})),
            ("orch-1", "system", "2026-10-30T09:07:00Z", json.dumps({"text": "System notice to ignore"})),
            ("thread:delegated-task:a%3Ab", "assistant", "2026-10-30T10:07:00Z", json.dumps({"text": "delegate reply"})),
            # malformed rows: valid JSON that is not an object, and a content block whose text is not a string
            ("orch-1", "assistant", "2026-10-30T08:00:00Z", json.dumps(["not", "an", "object"])),
            ("orch-1", "assistant", "2026-10-30T08:01:00Z", json.dumps({"content": [{"type": "text", "text": 7}]})),
        ]
    )
    con.commit()
    con.close()


class OfficeBuildAndDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_home = os.environ.get("FACTORY_HOME")
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if self.prev_home is not None:
            os.environ["FACTORY_HOME"] = self.prev_home
        else:
            os.environ.pop("FACTORY_HOME", None)
        self.tmp.cleanup()

    def test_office_json_written_and_done_agents_older_than_1h_dropped(self):
        now_dt = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
        data = {
            "builtAt": store.iso(now_dt),
            "products": {"prodA": {"name": "Product A"}},
            "projects": [{"id": "p1", "name": "Project 1"}],
            "agents": [
                # Running agent: kept regardless of age
                {"hc": "1A01", "who": "Agent 1", "state": "running", "since": "2026-10-30T08:00:00Z"},
                # Done agent finished 30 min ago: kept (<= 1h)
                {"hc": "1B02", "who": "Agent 2", "state": "done", "since": "2026-10-30T11:30:00Z"},
                # Done agent finished 2 hours ago: dropped (> 1h)
                {"hc": "1C03", "who": "Agent 3", "state": "done", "since": "2026-10-30T10:00:00Z"},
                # Done agent finished exactly 1 hour ago: kept
                {"hc": "1D04", "who": "Agent 4", "state": "done", "since": "2026-10-30T11:00:00Z"},
            ],
            "tokens": {"total": 12345},
            "provColors": {"Claude": "var(--mist)"},
            "relayConfigured": True,
            "warnings": [],
        }

        build.render(data)
        self.assertTrue(os.path.exists(store.path("site", "office.json")))

        with open(store.path("site", "office.json"), "r", encoding="utf-8") as f:
            office_doc = json.load(f)

        self.assertEqual(office_doc["builtAt"], data["builtAt"])
        self.assertEqual(office_doc["products"], data["products"])
        self.assertEqual(office_doc["projects"], data["projects"])
        self.assertEqual(office_doc["tokens"], data["tokens"])
        self.assertEqual(office_doc["provColors"], data["provColors"])
        self.assertTrue(office_doc["relayConfigured"])

        hcs = [a["hc"] for a in office_doc["agents"]]
        self.assertIn("1A01", hcs)
        self.assertIn("1B02", hcs)
        self.assertIn("1D04", hcs)
        self.assertNotIn("1C03", hcs)

    def test_machine_defaults_and_config_overrides(self):
        host = socket.gethostname()
        expected_slug = office.machine_slug(host)

        info = office.machine_info()
        self.assertEqual(info["id"], expected_slug)
        self.assertEqual(info["label"], host)

        config.set_value("machine.id", "custom-machine-id")
        config.set_value("machine.label", "Custom Machine Label")

        info_custom = office.machine_info()
        self.assertEqual(info_custom["id"], "custom-machine-id")
        self.assertEqual(info_custom["label"], "Custom Machine Label")

    def test_office_installed_in_collect_and_button_rendering(self):
        # When office.html is not installed
        self.assertFalse(os.path.exists(store.path("site", "office.html")))
        data_empty = {
            "builtAt": "2026-10-30T12:00:00Z",
            "products": {}, "projects": [], "agents": [], "tokens": {},
            "levers": [], "answers": {}, "prs": {}, "log": [], "provColors": {},
            "warnings": []
        }
        build.render(data_empty)
        with open(store.path("site", "factory.html"), "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn('id="officeBtn"', html)
        self.assertIn('DATA.officeInstalled', html)

        # Install office
        valid_html = b"<!doctype html><html><head><title>Office</title></head><body><h1>Office</h1></body></html>"
        store.write_bytes("site/office.html", valid_html)
        self.assertTrue(os.path.exists(store.path("site", "office.html")))

        # Check status helper
        st = office.status()
        self.assertTrue(st["installed"])
        self.assertEqual(st["size"], len(valid_html))
        self.assertIsNotNone(st["mtime"])

        # Check doctor status
        doc_st = office.doctor_status()
        self.assertTrue(doc_st.startswith("installed ("))
        self.assertIn("built", doc_st)


class OfficeCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_home = os.environ.get("FACTORY_HOME")
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if self.prev_home is not None:
            os.environ["FACTORY_HOME"] = self.prev_home
        else:
            os.environ.pop("FACTORY_HOME", None)
        self.tmp.cleanup()

    def test_install_refuses_nonexistent_file(self):
        with self.assertRaises(store.FactoryError) as cm:
            office.install(os.path.join(self.tmp.name, "nonexistent.html"))
        self.assertIn("file not found", str(cm.exception))

    def test_install_refuses_non_html(self):
        bad_file = os.path.join(self.tmp.name, "bad.txt")
        with open(bad_file, "wb") as f:
            f.write(b"Hello world, not html")
        with self.assertRaises(store.FactoryError) as cm:
            office.install(bad_file)
        self.assertIn("<!doctype html", str(cm.exception))

    def test_install_refuses_file_over_8mb(self):
        big_file = os.path.join(self.tmp.name, "big.html")
        with open(big_file, "wb") as f:
            f.write(b"<!doctype html>" + b"0" * (8 * 1024 * 1024 + 10))
        with self.assertRaises(store.FactoryError) as cm:
            office.install(big_file)
        self.assertIn("file too large", str(cm.exception))

    def test_install_and_status_cli_commands(self):
        valid_file = os.path.join(self.tmp.name, "office.html")
        with open(valid_file, "wb") as f:
            f.write(b"<!doctype html><html><title>Office</title></html>")

        # Before install: status is not installed
        res_pre, text_pre = cli.office_status(mock.MagicMock())
        self.assertFalse(res_pre["installed"])
        self.assertEqual(text_pre, "not installed")

        # Run install CLI
        args = mock.MagicMock(file=valid_file)
        res_inst, text_inst = cli.office_install(args)
        self.assertTrue(res_inst["installed"])
        self.assertTrue(os.path.exists(store.path("site", "office.html")))

        # After install: status is installed
        res_post, text_post = cli.office_status(mock.MagicMock())
        self.assertTrue(res_post["installed"])
        self.assertIn("installed:", text_post)


class OfficeServerAndCspTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_home = os.environ.get("FACTORY_HOME")
        os.environ["FACTORY_HOME"] = self.tmp.name

        self.db_path = os.path.join(self.tmp.name, "statev2.sqlite")
        make_t3_fixture(self.db_path)
        os.environ["FACTORY_T3_DB"] = self.db_path

        self.key = store.key()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.srv.shutdown()
        self.srv.server_close()
        if self.prev_home is not None:
            os.environ["FACTORY_HOME"] = self.prev_home
        else:
            os.environ.pop("FACTORY_HOME", None)
        os.environ.pop("FACTORY_T3_DB", None)
        self.tmp.cleanup()

    def req(self, path, headers=None):
        r = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_csp_hashes_module_script_and_refuses_src(self):
        page = (b"<!doctype html><html><head>"
                b"<script>console.log('inline standard');</script>"
                b"<script type=\"module\">console.log('inline module');</script>"
                b"<script src=\"external.js\"></script>"
                b"</head></html>")
        header = serve.csp(page)
        self.assertIn("'sha256-", header)
        # Verify both inline scripts are hashed
        h1 = base64.b64encode(hashlib.sha256(b"console.log('inline standard');").digest()).decode()
        h2 = base64.b64encode(hashlib.sha256(b"console.log('inline module');").digest()).decode()
        self.assertIn(f"'sha256-{h1}'", header)
        self.assertIn(f"'sha256-{h2}'", header)
        # External script is NOT hashed and src is not permitted
        self.assertNotIn("external.js", header)
        script_src = [part for part in header.split(";") if part.strip().startswith("script-src")][0]
        self.assertNotIn("'unsafe-inline'", script_src)

    def test_api_office_503_then_200_with_live_inbox(self):
        k = "?k=" + self.key

        # When office.json does not exist: 503
        status, body, _ = self.req("/api/office" + k)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "not built yet: run factory build")

        # Now write office.json and seed an inbox item
        office_data = {
            "builtAt": "2026-10-30T12:00:00Z",
            "machine": {"id": "box-1", "label": "Box 1"},
            "products": {}, "projects": [], "agents": [], "tokens": {},
            "provColors": {}, "relayConfigured": False
        }
        store.write("site/office.json", office_data)

        store.write("inbox.json", {
            "seq": 1,
            "resolved": [],
            "items": [{
                "id": "d1", "project": "proj-1", "kind": "urgent", "title": "Deploy now?",
                "context": "Context details", "options": [["a", "Yes"], ["b", "No"]],
                "reco": "a", "from": "agent-1", "thread": "orch-1", "at": "2026-10-30T12:01:00Z",
                "extra": "ignore-me"
            }]
        })

        status, body, _ = self.req("/api/office" + k)
        self.assertEqual(status, 200)
        res = json.loads(body)
        self.assertEqual(res["machine"]["id"], "box-1")
        self.assertEqual(len(res["inbox"]), 1)
        item = res["inbox"][0]
        self.assertEqual(item["id"], "d1")
        self.assertEqual(item["title"], "Deploy now?")
        self.assertEqual(item["thread"], "orch-1")
        self.assertNotIn("extra", item)
        self.assertEqual(res["answers"], {})

        # an answer saved but not yet resolved comes back with the item, so the Office shows it as answered
        store.write("answers.json", {"d1": {"choice": "a", "note": "", "at": "2026-10-30T12:02:00Z"}, "gone": {"choice": "x"}})
        res = json.loads(self.req("/api/office" + k)[1])
        self.assertEqual(res["answers"], {"d1": {"choice": "a", "note": "", "at": "2026-10-30T12:02:00Z"}})

    def test_office_page_404_then_200_after_install(self):
        k = "?k=" + self.key

        # Before install: 404 with HTML hint
        status, body, headers = self.req("/office" + k)
        self.assertEqual(status, 404)
        self.assertIn(b"Office not installed", body)
        self.assertIn("Content-Security-Policy", headers)

        # Install office HTML
        office_content = b"<!doctype html><html><script type=\"module\">console.log(1);</script><body>Office</body></html>"
        store.write_bytes("site/office.html", office_content)

        # After install: 200 with office HTML and CSP
        status, body, headers = self.req("/office" + k)
        self.assertEqual(status, 200)
        self.assertEqual(body, office_content)
        self.assertIn("Content-Security-Policy", headers)

    def test_office_trailing_slash_redirects_keeping_the_key(self):
        import http.client
        con = http.client.HTTPConnection(self.base.split("//")[1], timeout=5)
        con.request("GET", "/office/?k=" + self.key)
        resp = con.getresponse()
        resp.read()
        con.close()
        self.assertEqual(resp.status, 301)
        self.assertEqual(resp.getheader("Location"), "/office?k=" + self.key)

    def test_threads_messages_api(self):
        k = "?k=" + self.key

        # Invalid thread id format -> 404
        status, _, _ = self.req("/api/threads/bad%20thread%20id/messages" + k)
        self.assertEqual(status, 404)

        # Unknown thread id -> 404
        status, body, _ = self.req("/api/threads/nonexistent-thread/messages" + k)
        self.assertEqual(status, 404)
        self.assertIn("not found", json.loads(body)["error"])

        # Known thread orch-1 -> 200
        status, body, _ = self.req("/api/threads/orch-1/messages" + k)
        self.assertEqual(status, 200)
        msgs = json.loads(body)
        self.assertEqual(len(msgs), 4)  # system notice was filtered out
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["text"], "First user message")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["text"], "First assistant reply")
        self.assertEqual(msgs[2]["role"], "user")
        self.assertEqual(msgs[3]["role"], "assistant")
        # Text cut to 4000 chars
        self.assertEqual(len(msgs[3]["text"]), 4000)

        # A delegate id with a literal %, sent as encodeURIComponent output
        from urllib.parse import quote
        status, body, _ = self.req("/api/threads/" + quote("thread:delegated-task:a%3Ab", safe="") + "/messages" + k)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)[0]["text"], "delegate reply")

        # Test limit query param
        status, body, _ = self.req("/api/threads/orch-1/messages" + k + "&limit=2")
        self.assertEqual(status, 200)
        msgs_limit = json.loads(body)
        self.assertEqual(len(msgs_limit), 2)
        # Oldest first of the last 2
        self.assertEqual(msgs_limit[0]["text"], "Second user message")
        self.assertEqual(len(msgs_limit[1]["text"]), 4000)


class MachineCliAndRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_home = os.environ.get("FACTORY_HOME")
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if self.prev_home is not None:
            os.environ["FACTORY_HOME"] = self.prev_home
        else:
            os.environ.pop("FACTORY_HOME", None)
        self.tmp.cleanup()

    def test_machine_add_rotate_remove_and_list(self):
        # Invalid machine id rejected
        with self.assertRaises(store.FactoryError):
            cli.machine_add(mock.MagicMock(id="bad_id_!", label="Bad", rotate=False))

        # Local machine id refused
        loc_id = office.machine_info()["id"]
        with self.assertRaises(store.FactoryError):
            cli.machine_add(mock.MagicMock(id=loc_id, label="Local", rotate=False))
        with self.assertRaises(store.FactoryError):
            cli.machine_add(mock.MagicMock(id="vps", label="VPS", rotate=False))

        # Add new machine requires label
        with self.assertRaises(store.FactoryError):
            cli.machine_add(mock.MagicMock(id="laptop", label=None, rotate=False))

        # Machine add generates token and prints it once, stores only sha256
        res, token = cli.machine_add(mock.MagicMock(id="laptop", label="Work Laptop", rotate=False))
        self.assertEqual(res["id"], "laptop")
        self.assertEqual(res["label"], "Work Laptop")
        self.assertEqual(res["token"], token)
        self.assertEqual(token, token)
        self.assertGreater(len(token), 30)

        stored = store.read("machines.json", {})
        self.assertIn("laptop", stored)
        self.assertEqual(stored["laptop"]["label"], "Work Laptop")
        self.assertNotIn("token", stored["laptop"])
        self.assertEqual(stored["laptop"]["tokenHash"], hashlib.sha256(token.encode("utf-8")).hexdigest())

        # Adding again without --rotate refuses
        with self.assertRaises(store.FactoryError) as cm:
            cli.machine_add(mock.MagicMock(id="laptop", label="Work Laptop", rotate=False))
        self.assertIn("already exists", str(cm.exception))

        # Adding with --rotate succeeds and generates new token
        res_rot, token_rot = cli.machine_add(mock.MagicMock(id="laptop", label="Updated Laptop", rotate=True))
        self.assertNotEqual(token, token_rot)
        stored_rot = store.read("machines.json", {})
        self.assertEqual(stored_rot["laptop"]["tokenHash"], hashlib.sha256(token_rot.encode("utf-8")).hexdigest())
        self.assertEqual(stored_rot["laptop"]["label"], "Updated Laptop")

        # Machine list
        data, text = cli.machine_list(mock.MagicMock())
        self.assertIn("laptop", data)
        self.assertNotIn("tokenHash", data["laptop"])
        self.assertNotIn("token", data["laptop"])
        self.assertIn("laptop", text)
        self.assertIn("Updated Laptop", text)
        self.assertIn("never", text)  # push age never

        # Doctor report
        doc_msg = office.doctor_machines()
        self.assertEqual(doc_msg, "1 registered, 0 online")

        # Machine remove
        rem_res, rem_text = cli.machine_remove(mock.MagicMock(id="laptop"))
        self.assertEqual(rem_res["removed"], "laptop")
        self.assertIn("ok: removed laptop", rem_text)
        self.assertEqual(store.read("machines.json", {}), {})

        # List when empty
        data_empty, text_empty = cli.machine_list(mock.MagicMock())
        self.assertEqual(data_empty, {})
        self.assertEqual(text_empty, "no machines")


class MachinePushAndOfficeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_home = os.environ.get("FACTORY_HOME")
        os.environ["FACTORY_HOME"] = self.tmp.name

        self.db_path = os.path.join(self.tmp.name, "statev2.sqlite")
        make_t3_fixture(self.db_path)
        os.environ["FACTORY_T3_DB"] = self.db_path

        self.key = store.key()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.srv.shutdown()
        self.srv.server_close()
        if self.prev_home is not None:
            os.environ["FACTORY_HOME"] = self.prev_home
        else:
            os.environ.pop("FACTORY_HOME", None)
        os.environ.pop("FACTORY_T3_DB", None)
        self.tmp.cleanup()

    def req(self, path, method="GET", body=None, headers=None):
        data = body if (body is None or isinstance(body, bytes)) else json.dumps(body).encode("utf-8")
        r = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def _seed_office(self):
        store.write(os.path.join("site", "office.json"), {
            "builtAt": "2026-10-30T12:00:00Z", "machine": {"id": "box-1", "label": "Box 1"},
            "products": {}, "projects": [], "agents": [], "tokens": {}, "provColors": {},
            "relayConfigured": False})

    def _push(self, token, payload, raw=None):
        return self.req(f"/api/machines/desktop/agents?k={self.key}", method="POST",
                        body=raw if raw is not None else payload,
                        headers={"Content-Type": "application/json", "X-Factory-Machine-Token": token})

    def test_push_keeps_only_strings_and_never_breaks_the_office(self):
        self._seed_office()
        _, token = cli.machine_add(mock.MagicMock(id="desktop", label="Desktop", rotate=False))
        rows = [
            {"hc": "a1", "provider": 7, "project": [1], "parent": {"x": 1}, "since": 123,
             "tokens": {"x": "A" * 1000}, "thread": "t-1", "state": "running"},
            {"hc": 5, "state": "running"},          # no string handle: dropped
            {"provider": "claude"},                  # no handle: dropped
        ]
        status, body, _ = self._push(token, {"agents": rows})
        self.assertEqual((status, json.loads(body)["accepted"]), (200, 1))
        status, body, _ = self.req(f"/api/office?k={self.key}")
        self.assertEqual(status, 200)
        row = next(a for a in json.loads(body)["agents"] if a.get("machine") == "desktop")
        self.assertEqual(row["hc"], "desktop:a1")
        self.assertEqual(row["thread"], "desktop:t-1")  # never a local thread id
        for k in ("provider", "project", "parent", "since"):
            self.assertIsNone(row[k], k)
        self.assertNotIn("tokens", row)

    def test_machine_cli_refuses_a_corrupt_registry(self):
        store.write("machines.json", {"laptop": {"label": "laptop", "tokenHash": "h"}})
        with open(store.path("machines.json"), "a", encoding="utf-8") as f:
            f.write("{broken")
        with self.assertRaises(store.FactoryError):
            cli.machine_add(mock.MagicMock(id="box", label="Box", rotate=False))
        with open(store.path("machines.json"), encoding="utf-8") as f:
            self.assertIn("laptop", f.read())  # nothing overwritten

    def test_push_with_nan_is_a_400(self):
        _, token = cli.machine_add(mock.MagicMock(id="desktop", label="Desktop", rotate=False))
        status, _, _ = self._push(token, None, raw=b'{"agents": [{"hc": "a", "x": NaN}]}')
        self.assertEqual(status, 400)

    def test_malformed_machines_file_costs_only_the_remote_buildings(self):
        self._seed_office()
        for bad in ([1, 2], {"x": "str"}):
            store.write("machines.json", bad)
            status, body, _ = self.req(f"/api/office?k={self.key}")
            self.assertEqual(status, 200, bad)
            self.assertEqual(len(json.loads(body)["machines"]), 1)
            status, _, _ = self._push("tok", {"agents": []})
            self.assertEqual(status, 403)

    def test_push_good_token_and_rows_stored(self):
        # Register machine
        _, token = cli.machine_add(mock.MagicMock(id="desktop", label="Desktop", rotate=False))

        rows = [
            {
                "hc": "1D01",
                "who": "Thread",
                "provider": "Claude",
                "model": "claude-3-7-sonnet",
                "via": "T3 thread",
                "product": "my-prod",
                "project": "p1",
                "state": "running",
                "t3Status": "running",
                "since": "2026-10-30T10:00:00Z",
                "tokens": 500,
                "task": "Working on feature",
                "thread": "thread-123",
                "parent": None,
                "canReceive": True,  # Server must force to False
                "extraKey": "should_be_dropped",
            }
        ]
        payload = {"agents": rows, "sentAt": "2020-01-01T00:00:00Z"}
        status, body, _ = self.req(
            f"/api/machines/desktop/agents?k={self.key}",
            method="POST",
            body=payload,
            headers={
                "Content-Type": "application/json",
                "X-Factory-Machine-Token": token,
            },
        )
        self.assertEqual(status, 200)
        res = json.loads(body)
        self.assertTrue(res["ok"])
        self.assertEqual(res["accepted"], 1)

        # Verify stored in machines.json
        stored = store.read("machines.json", {})
        desk = stored["desktop"]
        self.assertIsNotNone(desk["lastPushAt"])
        self.assertNotEqual(desk["lastPushAt"], "2020-01-01T00:00:00Z")  # Server time used!
        self.assertEqual(len(desk["agents"]), 1)
        ag = desk["agents"][0]
        self.assertEqual(ag["hc"], "desktop:1D01")  # handles are prefixed with the machine id
        self.assertEqual(ag["machine"], "desktop")
        self.assertFalse(ag["canReceive"])
        self.assertNotIn("extraKey", ag)

    def test_bad_token_and_unknown_id_both_403_same_body(self):
        _, token = cli.machine_add(mock.MagicMock(id="laptop", label="Laptop", rotate=False))

        # Bad token on known machine
        st1, body1, _ = self.req(
            f"/api/machines/laptop/agents?k={self.key}",
            method="POST",
            body={"agents": []},
            headers={"Content-Type": "application/json", "X-Factory-Machine-Token": "wrong-token"},
        )
        # Unknown machine id with any token
        st2, body2, _ = self.req(
            f"/api/machines/unknown-machine/agents?k={self.key}",
            method="POST",
            body={"agents": []},
            headers={"Content-Type": "application/json", "X-Factory-Machine-Token": token},
        )
        self.assertEqual(st1, 403)
        self.assertEqual(st2, 403)
        self.assertEqual(body1, body2)
        self.assertEqual(json.loads(body1), {"error": "unknown machine or bad token"})

    def test_local_id_refused(self):
        loc_id = office.machine_info()["id"]
        # local machine id
        st1, body1, _ = self.req(
            f"/api/machines/{loc_id}/agents?k={self.key}",
            method="POST",
            body={"agents": []},
            headers={"Content-Type": "application/json", "X-Factory-Machine-Token": "tok"},
        )
        self.assertEqual(st1, 400)
        # 'vps'
        st2, body2, _ = self.req(
            f"/api/machines/vps/agents?k={self.key}",
            method="POST",
            body={"agents": []},
            headers={"Content-Type": "application/json", "X-Factory-Machine-Token": "tok"},
        )
        self.assertEqual(st2, 400)

    def test_validation_limits(self):
        _, token = cli.machine_add(mock.MagicMock(id="box", label="Box", rotate=False))
        headers = {"Content-Type": "application/json", "X-Factory-Machine-Token": token}

        # > 300 rows refused (400)
        over_rows = [{"hc": f"H{i}", "state": "running"} for i in range(301)]
        st, body, _ = self.req(
            f"/api/machines/box/agents?k={self.key}",
            method="POST",
            body={"agents": over_rows},
            headers=headers,
        )
        self.assertEqual(st, 400)
        self.assertIn("300", json.loads(body)["error"])

        # > 1 MB body refused (413) from Content-Length alone: send only the headers, so the early answer
        # cannot race the upload (a full 1 MB body made this test fail with a broken pipe under load)
        import http.client
        conn = http.client.HTTPConnection(*self.base.split("//")[1].split(":"), timeout=5)
        conn.putrequest("POST", f"/api/machines/box/agents?k={self.key}")
        for hk, hv in {**headers, "Content-Length": str(serve.MAX_MACHINE_BODY + 1)}.items():
            conn.putheader(hk, hv)
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

        # Bad JSON refused (400)
        st_bad, _, _ = self.req(
            f"/api/machines/box/agents?k={self.key}",
            method="POST",
            body=b"not json",
            headers=headers,
        )
        self.assertEqual(st_bad, 400)

        # Strings cut to 500 chars, unknown state mapped to 'unknown'
        long_str_row = {
            "hc": "LONG",
            "task": "A" * 600,
            "state": "unrecognized_state_foo",
        }
        st_ok, _, _ = self.req(
            f"/api/machines/box/agents?k={self.key}",
            method="POST",
            body={"agents": [long_str_row]},
            headers=headers,
        )
        self.assertEqual(st_ok, 200)
        stored_row = store.read("machines.json", {})["box"]["agents"][0]
        self.assertEqual(len(stored_row["task"]), 500)
        self.assertEqual(stored_row["state"], "unknown")

    def test_office_api_machines_online_offline_edge_and_no_registered_machines(self):
        # 1. First test with NO registered machines
        office_base = {
            "builtAt": "2026-10-30T12:00:00Z",
            "machine": office.machine_info(),
            "products": {}, "projects": [],
            "agents": [
                {"hc": "LOCAL1", "state": "running", "since": "2026-10-30T11:00:00Z"}
            ],
            "tokens": {}, "provColors": {}, "relayConfigured": False
        }
        store.write("site/office.json", office_base)

        status, body, _ = self.req(f"/api/office?k={self.key}")
        self.assertEqual(status, 200)
        doc = json.loads(body)
        loc_id = office.machine_info()["id"]
        # Exactly one machine entry
        self.assertEqual(len(doc["machines"]), 1)
        self.assertEqual(doc["machines"][0]["id"], loc_id)
        self.assertTrue(doc["machines"][0]["online"])
        self.assertEqual(doc["machines"][0]["lastSeen"], "2026-10-30T12:00:00Z")
        self.assertEqual(doc["machine"]["id"], loc_id)
        # Agent tagged with local machine id
        self.assertEqual(doc["agents"][0]["machine"], loc_id)

        # 2. Now register two machines: one online, one offline (3-minute edge)
        now_dt = datetime.now(timezone.utc)
        just_under_3min = store.iso(now_dt - timedelta(seconds=170))  # 2m 50s ago -> online
        just_over_3min = store.iso(now_dt - timedelta(seconds=190))   # 3m 10s ago -> offline

        machines = {
            "laptop-on": {
                "label": "Online Laptop",
                "tokenHash": "hash1",
                "createdAt": "2026-10-30T10:00:00Z",
                "lastPushAt": just_under_3min,
                "agents": [
                    {"hc": "REMOTE1", "state": "running", "since": store.iso(now_dt)}
                ]
            },
            "laptop-off": {
                "label": "Offline Laptop",
                "tokenHash": "hash2",
                "createdAt": "2026-10-30T10:00:00Z",
                "lastPushAt": just_over_3min,
                "agents": [
                    {"hc": "REMOTE2", "state": "running", "since": store.iso(now_dt)}
                ]
            }
        }
        store.write("machines.json", machines)

        status, body, _ = self.req(f"/api/office?k={self.key}")
        self.assertEqual(status, 200)
        doc = json.loads(body)

        machines_map = {m["id"]: m for m in doc["machines"]}
        self.assertTrue(machines_map[loc_id]["online"])
        self.assertTrue(machines_map["laptop-on"]["online"])
        self.assertFalse(machines_map["laptop-off"]["online"])

        agent_hcs = [a["hc"] for a in doc["agents"]]
        self.assertIn("LOCAL1", agent_hcs)
        self.assertIn("REMOTE1", agent_hcs)
        self.assertNotIn("REMOTE2", agent_hcs)  # offline machine contributes NO agents!

    def test_collector_push_and_secret_redaction(self):
        # Setup secrets for collector
        store.write_text("secrets/machine-push-token", "secret-push-token-12345\n")
        store.write_text("secrets/access-client-id", "my-access-client-id\n")
        store.write_text("secrets/access-client-secret", "my-access-client-secret\n")

        received_headers = {}

        class MockCollectorHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                for k, v in self.headers.items():
                    received_headers[k.lower()] = v
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true, "accepted": 0}')

        mock_srv = ThreadingHTTPServer(("127.0.0.1", 0), MockCollectorHandler)
        threading.Thread(target=mock_srv.serve_forever, daemon=True).start()
        mock_port = mock_srv.server_address[1]
        self.addCleanup(lambda: (mock_srv.shutdown(), mock_srv.server_close()))

        args = mock.MagicMock(
            url=f"http://127.0.0.1:{mock_port}",
            machine="desk",
            key=None
        )
        cli.collect_push(args)

        self.assertEqual(received_headers.get("x-factory-machine-token"), "secret-push-token-12345")
        self.assertEqual(received_headers.get("cf-access-client-id"), "my-access-client-id")
        self.assertEqual(received_headers.get("cf-access-client-secret"), "my-access-client-secret")

        # Test failure: mock server returns 403, verify error does not contain secrets
        class FailingHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "denied"}')

        fail_srv = ThreadingHTTPServer(("127.0.0.1", 0), FailingHandler)
        threading.Thread(target=fail_srv.serve_forever, daemon=True).start()
        fail_port = fail_srv.server_address[1]
        self.addCleanup(lambda: (fail_srv.shutdown(), fail_srv.server_close()))

        fail_args = mock.MagicMock(
            url=f"http://127.0.0.1:{fail_port}",
            machine="desk",
            key="secret-key-param"
        )
        with self.assertRaises(store.FactoryError) as cm:
            cli.collect_push(fail_args)

        err_msg = str(cm.exception)
        self.assertIn("failed (HTTP 403)", err_msg)
        self.assertIn(f"http://127.0.0.1:{fail_port}/api/machines/desk/agents", err_msg)
        # Verify secrets and key are NEVER in the error message
        self.assertNotIn("secret-push-token", err_msg)
        self.assertNotIn("my-access-client-id", err_msg)
        self.assertNotIn("my-access-client-secret", err_msg)
        self.assertNotIn("secret-key-param", err_msg)



if __name__ == "__main__":
    unittest.main()
