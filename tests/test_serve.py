import base64
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
import urllib.error
import urllib.request
from unittest import mock
from http.server import ThreadingHTTPServer

from factory import access_jwt, build, cli, serve, store


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        store.write("inbox.json", {"seq": 1, "resolved": [], "items": [
            {"id": "d1", "project": "ds", "kind": "urgent", "title": "T", "options": [["a", "A"], ["b", "B"]]}]})
        store.write_text("site/factory.html", "<p>page</p>")
        self.key = store.key()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()
        del os.environ["FACTORY_HOME"]

    def req(self, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:  # as the page sends it
            headers = {"Content-Type": "application/json", **(headers or {})}
        r = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_gate(self):
        k = "?k=" + self.key
        self.assertEqual(self.req("/" + k), (200, b"<p>page</p>"))
        self.assertEqual(self.req("/")[0], 401)
        self.assertEqual(self.req("/?k=wrong")[0], 401)
        self.assertEqual(self.req("/?k=%C3%A9")[0], 401)
        self.assertEqual(self.req("/api/answers")[0], 403)
        self.assertEqual(self.req("/api/answers?k=")[0], 403)
        self.assertEqual(self.req("/api/answers" + k, {"d1": {"choice": "a"}}, {"Cf-Ray": "x"})[0], 403)
        self.assertEqual(self.req("/nope" + k)[0], 404)

    def test_key_rotate(self):
        old_key = self.key
        self.assertEqual(self.req("/?k=" + old_key)[0], 200)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["key", "rotate"]), 0)
        out_text = out.getvalue().strip()
        self.assertEqual(out_text, "ok: key rotated")

        # old key 401
        self.assertEqual(self.req("/?k=" + old_key)[0], 401)

        # new key 200
        new_key = store.key()
        self.assertNotEqual(new_key, old_key)
        self.assertNotIn(new_key, out_text)
        self.assertEqual(self.req("/?k=" + new_key)[0], 200)

        # file mode 600
        st = os.stat(store.path(".key"))
        self.assertEqual(st.st_mode & 0o777, 0o600)

        # --json returns {"rotated": true}
        out_json = io.StringIO()
        with contextlib.redirect_stdout(out_json):
            self.assertEqual(cli.main(["--json", "key", "rotate"]), 0)
        self.assertEqual(json.loads(out_json.getvalue()), {"rotated": True})

    def test_access_login_replaces_the_key(self):
        tunnel = {"Cf-Ray": "x", "Cf-Access-Jwt-Assertion": "jwt"}
        with mock.patch.object(access_jwt, "allowed", return_value=True):
            self.assertEqual(self.req("/", headers=tunnel), (200, b"<p>page</p>"))  # no ?k= needed
            self.assertEqual(self.req("/api/answers", {"d1": {"choice": "a"}}, tunnel)[0], 200)
            self.assertEqual(self.req("/")[0], 401)  # from this machine the key is still required
            # a page on another origin cannot save an answer with the owner's Access cookie
            forged = b'{"d1": {"choice": "b"}}'
            self.assertEqual(self.req("/api/answers", headers={**tunnel, "Content-Type": "text/plain"}, raw=forged)[0], 415)
            self.assertEqual(self.req("/api/answers", headers={**tunnel, "Content-Type": "application/json",
                                                                "Sec-Fetch-Site": "same-site"}, raw=forged)[0], 403)
            self.assertEqual(json.loads(self.req("/api/answers", headers=tunnel)[1])["d1"]["choice"], "a")  # not "b"
        with mock.patch.object(access_jwt, "allowed", return_value=False):  # a bad or missing JWT
            self.assertEqual(self.req("/?k=" + self.key, headers=tunnel)[0], 403)

    def test_csp_allows_only_the_page_script(self):
        store.write_text("site/factory.html", "<script>run()</script><p>page</p>")
        with urllib.request.urlopen(self.base + "/?k=" + self.key, timeout=5) as r:
            policy = r.headers["Content-Security-Policy"]
        want = base64.b64encode(hashlib.sha256(b"run()").digest()).decode()
        self.assertIn(f"script-src 'sha256-{want}';", policy)
        self.assertIn("connect-src 'self'", policy)
        with self.assertRaises(urllib.error.HTTPError) as cm:  # the missing-key page is HTML too
            urllib.request.urlopen(self.base + "/", timeout=5)
        cm.exception.close()
        self.assertIn("script-src 'none';", cm.exception.headers["Content-Security-Policy"])
        # the real template has exactly one inline script, the one the hash covers
        with open(os.path.join(os.path.dirname(__file__), "..", "template", "factory.html"), "rb") as f:
            self.assertEqual(serve.csp(f.read()).count("'sha256-"), 1)

    def test_answers(self):
        k = "?k=" + self.key
        for body, code in (({"d1": {"choice": ["a"]}}, 400), ({"d1": {"choice": "z"}}, 400), ([1], 400),
                           ({"zz": {"choice": "a"}}, 404), ({"d1": {"choice": "a", "note": 5}}, 400)):
            self.assertEqual(self.req("/api/answers" + k, body)[0], code, body)
        self.assertEqual(self.req("/api/answers" + k, raw=b"[" * 50000)[0], 400)
        code, out = self.req("/api/answers" + k, {"d1": {"choice": "b", "note": "go"}})
        self.assertEqual((code, json.loads(out)["d1"]["choice"]), (200, "b"))
        self.assertEqual(json.loads(self.req("/api/answers" + k)[1])["d1"]["note"], "go")

    def test_corrupt_state_answers_500(self):
        with open(store.path("answers.json"), "w") as f:
            f.write("{")
        code, out = self.req("/api/answers?k=" + self.key)
        self.assertEqual(code, 500)
        self.assertIn("not valid JSON", json.loads(out)["error"])

    def test_corrupt_relay_json_still_saves_answers_200(self):
        k = "?k=" + self.key
        store.write("inbox.json", {"seq": 1, "resolved": [], "items": [
            {"id": "d1", "project": "ds", "kind": "urgent", "title": "T", "options": [["a", "A"], ["b", "B"]], "thread": "t-123"}]})
        store.write_text("relay.json", "{corrupt-relay-json")
        code, out = self.req("/api/answers" + k, {"d1": {"choice": "a", "note": "ok"}})
        self.assertEqual(code, 200)
        ans = store.read("answers.json", {})
        self.assertEqual(ans.get("d1", {}).get("choice"), "a")

    def test_inbox_open_content_and_gating(self):
        k = "?k=" + self.key
        # gating: local requires key
        self.assertEqual(self.req("/api/inbox/open")[0], 403)
        self.assertEqual(self.req("/api/inbox/open?k=")[0], 403)
        self.assertEqual(self.req("/api/inbox/open?k=wrong")[0], 403)
        # gating: tunnelled without JWT returns 403
        self.assertEqual(self.req("/api/inbox/open", headers={"Cf-Ray": "x"})[0], 403)
        self.assertEqual(self.req("/api/inbox/open" + k, headers={"Cf-Ray": "x"})[0], 403)
        # gating: tunnelled with valid JWT returns 200 without key
        tunnel = {"Cf-Ray": "x", "Cf-Access-Jwt-Assertion": "jwt"}
        with mock.patch.object(access_jwt, "allowed", return_value=True):
            code, out = self.req("/api/inbox/open", headers=tunnel)
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(out), {"open": ["d1"], "closed": {}})
        # local with key returns 200 and open list
        code, out = self.req("/api/inbox/open" + k)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(out), {"open": ["d1"], "closed": {}})

        # update items in inbox.json
        store.write("inbox.json", {"seq": 2, "resolved": [], "items": [
            {"id": "d1", "project": "ds", "kind": "urgent", "title": "T1", "options": [["a", "A"]]},
            {"id": "d2", "project": "ds", "kind": "action", "title": "T2", "options": [["done", "Done"]]},
        ]})
        code, out = self.req("/api/inbox/open" + k)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(out), {"open": ["d1", "d2"], "closed": {}})

        # resolve all items
        store.write("inbox.json", {"seq": 2, "resolved": [{"id": "d1"}, {"id": "d2"}], "items": []})
        code, out = self.req("/api/inbox/open" + k)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(out), {"open": [], "closed": {}})

    def test_answers_409_on_resolved_item_and_message_wording(self):
        k = "?k=" + self.key
        # Case 1: item resolved after being answered by the owner
        store.write("inbox.json", {
            "seq": 1,
            "items": [],
            "resolved": [{
                "id": "d1",
                "project": "ds",
                "kind": "urgent",
                "title": "T",
                "options": [["a", "A"], ["b", "B"]],
                "resolvedAt": "2026-10-29T14:35:00Z",
                "answer": {"choice": "a", "note": "fine", "at": "2026-10-29T14:30:00Z"},
            }],
        })
        store.write("answers.json", {"d1": {"choice": "a", "note": "fine", "at": "2026-10-29T14:30:00Z"}})

        code, out = self.req("/api/answers" + k, {"d1": {"choice": "b", "note": "re-answering"}})
        self.assertEqual(code, 409)
        b1 = json.loads(out)
        self.assertEqual(b1, {
            "error": "already handled",
            "id": "d1",
            "answeredAt": "2026-10-29T14:30:00Z",
            "resolvedAt": "2026-10-29T14:35:00Z",
        })

        # Case 2: item resolved without any answer from the owner
        store.write("inbox.json", {
            "seq": 2,
            "items": [],
            "resolved": [{
                "id": "d2",
                "project": "ds",
                "kind": "action",
                "title": "T2",
                "options": [["done", "Done"], ["blocked", "Blocked"]],
                "resolvedAt": "2026-10-29T15:20:00Z",
            }],
        })
        store.write("answers.json", {})

        code, out = self.req("/api/answers" + k, {"d2": {"choice": "done"}})
        self.assertEqual(code, 409)
        b2 = json.loads(out)
        self.assertEqual(b2, {
            "error": "already handled",
            "id": "d2",
            "answeredAt": None,
            "resolvedAt": "2026-10-29T15:20:00Z",
        })

    def test_inbox_open_closed_items_in_last_24h(self):
        k = "?k=" + self.key
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(hours=2)).isoformat(timespec="seconds").replace("+00:00", "Z")
        old = (now - timedelta(hours=26)).isoformat(timespec="seconds").replace("+00:00", "Z")
        store.write("answers.json", {"d_recent": {"choice": "a", "note": "ok", "at": recent}})
        store.write("inbox.json", {
            "seq": 3,
            "items": [{"id": "d_open", "options": [["a", "A"]]}],
            "resolved": [
                {"id": "d_recent", "title": "Secret Title", "resolvedAt": recent},
                {"id": "d_old", "title": "Old Title", "resolvedAt": old},
                {"title": "No ID"},  # must not 500
            ],
        })
        code, out = self.req("/api/inbox/open" + k)
        self.assertEqual(code, 200)
        body = json.loads(out)
        self.assertEqual(body["open"], ["d_open"])
        self.assertIn("d_recent", body["closed"])
        self.assertNotIn("d_old", body["closed"])
        self.assertEqual(body["closed"]["d_recent"], {
            "resolvedAt": recent,
            "answeredAt": recent,
        })

    def test_save_with_one_open_and_one_resolved_returns_409_atomically(self):
        k = "?k=" + self.key
        initial_answers = {"existing": {"choice": "x", "note": "prev", "at": "2026-10-29T10:00:00Z"}}
        store.write("answers.json", initial_answers)
        store.write("inbox.json", {
            "seq": 1,
            "items": [{
                "id": "open1",
                "project": "p1",
                "kind": "urgent",
                "title": "Open Item",
                "options": [["a", "A"], ["b", "B"]],
                "thread": {"threadId": "th-123"},
            }],
            "resolved": [{
                "id": "res1",
                "project": "p1",
                "kind": "action",
                "title": "Resolved Item",
                "options": [["a", "A"]],
                "resolvedAt": "2026-10-29T14:35:00Z",
            }],
        })
        with mock.patch("factory.relay.enqueue") as mock_enqueue, \
             mock.patch("factory.relay.trigger_delivery_async") as mock_trigger:
            code, out = self.req("/api/answers" + k, {
                "open1": {"choice": "a", "note": "my note"},
                "res1": {"choice": "a"},
            })
            self.assertEqual(code, 409)
            body = json.loads(out)
            self.assertEqual(body.get("error"), "already handled")
            self.assertEqual(body.get("id"), "res1")
            self.assertEqual(store.read("answers.json", {}), initial_answers)
            mock_enqueue.assert_not_called()
            mock_trigger.assert_not_called()

    def test_save_answers_skips_resolved_entry_without_id(self):
        k = "?k=" + self.key
        store.write("inbox.json", {
            "seq": 1,
            "items": [{"id": "open1", "options": [["a", "A"]], "kind": "action", "title": "T", "project": "p"}],
            "resolved": [{"resolvedAt": "2026-10-29T14:35:00Z"}, {"id": None}],
        })
        code, out = self.req("/api/answers" + k, {"open1": {"choice": "a"}})
        self.assertEqual(code, 200)




class RenderTest(unittest.TestCase):
    def test_script_breakout_is_escaped(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["FACTORY_HOME"] = d
            try:
                out = build.render({"builtAt": "2026-10-27T12:00:00Z", "title": "<!--<script></script>", "x": " "})
                with open(out, encoding="utf-8") as f:
                    html = f.read()
            finally:
                del os.environ["FACTORY_HOME"]
        blob = html.split("const DATA=", 1)[1].split(";\n", 1)[0]
        self.assertNotIn("<", blob)
        self.assertEqual(json.loads(blob)["title"], "<!--<script></script>")


class RenderConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        del os.environ["FACTORY_HOME"]

    def page(self):
        with open(store.path("site", "factory.html"), encoding="utf-8") as f:
            return json.loads(f.read().split("const DATA=", 1)[1].split(";\n", 1)[0])

    def test_older_build_never_replaces_a_newer_page(self):
        build.render({"builtAt": "2026-10-27T12:10:00Z", "n": 2})
        older = {"builtAt": "2026-10-27T12:00:00Z", "n": 1}
        build.render(older)
        self.assertEqual(self.page()["n"], 2)
        self.assertIn("skipped", older["warnings"][0])
        self.assertFalse(older["written"])

    def test_future_stamp_is_ignored(self):
        store.write_text("site/built-at", "2099-01-01T00:00:00Z\n")
        data = {"builtAt": "2026-10-27T12:00:00Z", "n": 3}
        build.render(data)
        self.assertTrue(data["written"])
        self.assertEqual(self.page()["n"], 3)
        self.assertIn("in the future", data["warnings"][0])

    def test_concurrent_builds_leave_one_whole_page(self):
        stamps = [f"2026-10-27T12:{m:02d}:00Z" for m in range(20)]
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda t: build.render({"builtAt": t, "pad": "x" * 200000}), stamps))
        self.assertEqual(self.page()["builtAt"], stamps[-1])  # parses: no mixed write; the newest wins
        self.assertEqual([f for f in os.listdir(store.path("site")) if f.endswith(".tmp")], [])


if __name__ == "__main__":
    unittest.main()
