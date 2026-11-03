import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

from factory import build, cli, serve, sources, store, t3

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def add_products():
    for key in ("shop", "b"):
        with mock.patch("sys.stdout"):
            cli.main(["product", "set", key, "--name", key.title()])


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--json", *argv])
    return code, (json.loads(out.getvalue()) if code == 0 and out.getvalue().strip() else None), err.getvalue()


class ProtoCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        add_products()

    def tearDown(self):
        self.tmp.cleanup()
        del os.environ["FACTORY_HOME"]

    def ok(self, *argv):
        code, obj, err = run(*argv)
        self.assertEqual(code, 0, err)
        return obj

    def fails(self, *argv):
        code, _, err = run(*argv)
        self.assertEqual(code, 1)
        return err

    def _html(self, name="p.html"):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w") as f:
            f.write("<!doctype html><p>proto</p>")
        return p

    def test_add_twice_v2_supersedes_v1(self):
        f = self._html()
        v1 = self.ok("proto", "add", "--product", "shop", "--feature", "Pay link", "--file", f)
        self.assertEqual((v1["v"], v1["status"]), (1, "waiting"))
        v2 = self.ok("proto", "add", "--product", "shop", "--feature", "Pay link", "--file", f)
        self.assertEqual(v2["v"], 2)
        protos = store.protos()
        self.assertEqual(len(protos), 1)
        p = protos[0]
        self.assertEqual(p["id"], "shop-pay-link")
        self.assertEqual([v["v"] for v in p["versions"]], [2, 1])
        self.assertEqual(p["versions"][0]["status"], "waiting")
        self.assertEqual(p["versions"][1]["status"], "superseded")
        self.assertTrue(os.path.exists(store.path("protos", "shop-pay-link", "v1.html")))
        self.assertTrue(os.path.exists(store.path("protos", "shop-pay-link", "v2.html")))

    def test_bad_file_refused(self):
        self.assertIn("no such file", self.fails("proto", "add", "--product", "b", "--feature", "f",
                                                  "--file", os.path.join(self.tmp.name, "nope.html")))
        txt = os.path.join(self.tmp.name, "not-html.txt")
        with open(txt, "w") as f:
            f.write("hi")
        self.assertIn("not a .html", self.fails("proto", "add", "--product", "b", "--feature", "f", "--file", txt))

    def test_unknown_product_and_id_collision_refused(self):
        f = self._html()
        self.assertIn("no product 'nope'", self.fails("proto", "add", "--product", "nope", "--feature", "f", "--file", f))
        self.assertIn("latin letter", self.fails("proto", "add", "--product", "b", "--feature", "支付", "--file", f))
        added = self.ok("proto", "add", "--product", "shop", "--feature", "Pay link", "--file", f)
        self.assertEqual(added["id"], "shop-pay-link")
        # same slug, different feature: refused, the first prototype keeps its waiting version
        self.assertIn("already shop", self.fails("proto", "add", "--product", "shop", "--feature", "pay-link", "--file", f))
        self.assertEqual([v["status"] for v in store.protos()[0]["versions"]], ["waiting"])

    def test_non_utf8_file_refused(self):
        p = os.path.join(self.tmp.name, "bin.html")
        with open(p, "wb") as f:
            f.write(b"\xff\xfe<p>")
        self.assertIn("not UTF-8", self.fails("proto", "add", "--product", "b", "--feature", "f", "--file", p))

    def test_built_marks_validated_version(self):
        f = self._html()
        self.ok("proto", "add", "--product", "b", "--feature", "f", "--file", f)
        self.assertIn("not validated", self.fails("proto", "built", "b-f", "--pr", "1"))
        code, body = serve.save_proto({"id": "b-f", "v": 1, "action": "validate"})
        self.assertEqual(code, 200)
        self.assertFalse(body["delivered"])  # no thread
        v = self.ok("proto", "built", "b-f", "--pr", "12")
        self.assertEqual((v["status"], v["pr"]), ("built", 12))


class ProtoServeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        add_products()
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
        h = {"Sec-Fetch-Site": "same-origin"}
        if data is not None:
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        r = urllib.request.Request(self.base + path, data=data, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def _add(self, thread=None, feature="Pay link"):
        p = os.path.join(self.tmp.name, "p.html")
        with open(p, "w") as f:
            f.write("<!doctype html><p>proto</p>")
        argv = ["proto", "add", "--product", "shop", "--feature", feature, "--file", p]
        if thread:
            argv += ["--thread", thread]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--json", *argv])
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue())

    def test_serve_csp_and_path_guard(self):
        self._add()
        # a real file outside protos/: a loosened path pattern would serve it
        store.write_text("other/v1.html", "<p>secret</p>")
        k = "?k=" + self.key
        code, body, headers = self.req("/protos/shop-pay-link/v1.html" + k)
        self.assertEqual(code, 200)
        self.assertIn("sandbox allow-scripts", headers["Content-Security-Policy"])
        for bad in ("/protos/../other/v1.html", "/protos/%2e%2e/other/v1.html", "/protos/shop-pay-link/../../other/v1.html",
                    "/protos/shop-pay-link/vx.html", "/protos/FOO/v1.html", "/protos/a.b/v1.html",
                    "/protos/shop-pay-link/v1.html/extra", "/protos/shop-pay-link/v2.html"):
            code, body, _ = self.req(bad + k)
            self.assertIn(code, (400, 404), bad)
            self.assertNotIn(b"secret", body, bad)

    def test_file_token_opens_only_its_file_never_the_api(self):
        self._add()
        self._add(feature="Other")
        t = store.proto_token("protos/shop-pay-link/v1.html")
        self.assertEqual(self.req("/protos/shop-pay-link/v1.html?t=" + t)[0], 200)
        self.assertEqual(self.req("/protos/shop-other/v1.html?t=" + t)[0], 401)
        self.assertEqual(self.req("/api/answers?t=" + t)[0], 403)
        self.assertEqual(self.req("/?t=" + t)[0], 401)

    def test_page_links_carry_the_file_token_not_the_key(self):
        self._add()
        with mock.patch.object(build, "agents_data", return_value=([], None, {}, [], [])), \
             mock.patch.object(build, "skills_data", return_value=[]), \
             mock.patch.object(sources, "prs_for", return_value={}), \
             mock.patch.object(t3, "thread_handles", return_value={}):
            data = build.collect(NOW)
        v = data["protos"][0]["versions"][0]
        self.assertEqual(v["token"], store.proto_token("protos/shop-pay-link/v1.html"))
        self.assertNotIn(self.key, json.dumps(data["protos"]))

    def test_sendback_requires_reason(self):
        self._add()
        k = "?k=" + self.key
        code, body, _ = self.req("/api/protos" + k, {"id": "shop-pay-link", "v": 1, "action": "sendback", "reason": "  "})
        self.assertEqual(code, 400)
        self.assertIn("reason", json.loads(body)["error"])

    def test_validate_non_waiting_is_409(self):
        self._add()
        k = "?k=" + self.key
        self.assertEqual(self.req("/api/protos" + k, {"id": "shop-pay-link", "v": 1, "action": "validate"})[0], 200)
        code, body, _ = self.req("/api/protos" + k, {"id": "shop-pay-link", "v": 1, "action": "validate"})
        self.assertEqual(code, 409)
        # the stale page gets the current version back to redraw itself
        self.assertEqual(json.loads(body)["version"]["status"], "validated")

    def test_sendback_saves_reason_and_relays_it(self):
        self._add(thread="thread-1")
        with mock.patch("factory.relay.enqueue_message") as me, mock.patch("factory.relay.trigger_delivery_async"), \
             mock.patch.object(serve, "live_agent_threads", return_value={"thread-1"}):
            code, body = serve.save_proto({"id": "shop-pay-link", "v": 1, "action": "sendback", "reason": " too dense "})
        self.assertEqual(code, 200)
        v = store.protos()[0]["versions"][0]
        self.assertEqual((v["status"], v["sent_back_reason"]), ("sent-back", "too dense"))
        me.assert_called_once_with("thread-1", "Prototype Pay link v1: sent back: too dense")
        self.assertEqual(serve.save_proto({"id": "nope", "v": 1, "action": "validate"})[0], 404)
        self.assertEqual(serve.save_proto(["x"])[0], 400)
        self.assertEqual(serve.save_proto({"id": "shop-pay-link", "v": 1, "action": "sendback", "reason": "x" * 2001})[0], 400)

    def test_decides_only_the_version_on_screen(self):
        self._add()
        self._add()  # an agent adds v2 while the page still shows v1
        code, body = serve.save_proto({"id": "shop-pay-link", "v": 1, "action": "validate"})
        self.assertEqual(code, 409)
        self.assertEqual(body["version"]["v"], 2)
        self.assertEqual([v["status"] for v in store.protos()[0]["versions"]], ["waiting", "superseded"])
        self.assertEqual(serve.save_proto({"id": "shop-pay-link", "action": "validate"})[0], 400)
        self.assertEqual(serve.save_proto({"id": "shop-pay-link", "v": True, "action": "validate"})[0], 400)

    def test_dead_or_subagent_thread_is_not_delivered(self):
        self._add(thread="ended-thread")
        with mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch.object(serve, "live_agent_threads", return_value={"other"}):
            code, body = serve.save_proto({"id": "shop-pay-link", "v": 1, "action": "validate"})
        self.assertEqual((code, body["delivered"], body["why"]), (200, False, "not in T3"))
        me.assert_not_called()
        self.assertEqual(store.protos()[0]["versions"][0]["status"], "validated")

    def test_unreadable_t3_keeps_decision_and_says_so(self):
        self._add(thread="thread-1")
        with mock.patch.object(serve, "live_agent_threads", side_effect=store.FactoryError("T3 database not found")):
            code, body = serve.save_proto({"id": "shop-pay-link", "v": 1, "action": "validate"})
        self.assertEqual((code, body["delivered"]), (200, False))
        self.assertIn("not queued", body["why"])
        self.assertEqual(store.protos()[0]["versions"][0]["status"], "validated")

    def test_relay_failure_keeps_decision_and_says_so(self):
        self._add(thread="thread-1")
        with mock.patch("factory.relay.enqueue_message", side_effect=store.FactoryError("outbox corrupt")), \
             mock.patch.object(serve, "live_agent_threads", return_value={"thread-1"}):
            code, body = serve.save_proto({"id": "shop-pay-link", "v": 1, "action": "validate"})
        self.assertEqual(code, 200)
        self.assertFalse(body["delivered"])
        self.assertIn("not queued", body["why"])
        self.assertEqual(store.protos()[0]["versions"][0]["status"], "validated")

    def test_validate_no_thread_delivered_false(self):
        self._add()
        k = "?k=" + self.key
        code, body, _ = self.req("/api/protos" + k, {"id": "shop-pay-link", "v": 1, "action": "validate"})
        self.assertEqual(code, 200)
        b = json.loads(body)
        self.assertEqual(b["delivered"], False)
        self.assertEqual(b["why"], "not in T3")
        v = store.protos()[0]["versions"][0]
        self.assertEqual(v["status"], "validated")
        self.assertEqual(len(v["decisions"]), 1)
        self.assertTrue(v["decisions"][0].startswith("Validated on "))

    def test_validate_with_thread_queues_relay(self):
        self._add(thread="thread-1")
        k = "?k=" + self.key
        with mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async") as tr, \
             mock.patch.object(serve, "live_agent_threads", return_value={"thread-1"}):
            code, body, _ = self.req("/api/protos" + k, {"id": "shop-pay-link", "v": 1, "action": "validate"})
        self.assertEqual(code, 200)
        b = json.loads(body)
        self.assertTrue(b["delivered"])
        me.assert_called_once_with("thread-1", "Prototype Pay link v1: validated")
        tr.assert_called_once()


class ProtoBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)

    def test_corrupt_protos_json_warns_and_still_builds(self):
        with mock.patch("sys.stdout"):
            cli.main(["product", "set", "shop", "--name", "Shop"])
        store.write_text("protos.json", "{corrupt")
        with mock.patch.object(build, "agents_data", return_value=([], None, {}, [], [])), \
             mock.patch.object(build, "skills_data", return_value=[]), \
             mock.patch.object(sources, "prs_for", return_value={}), \
             mock.patch.object(t3, "thread_handles", return_value={}):
            data = build.collect(NOW)
        self.assertTrue(any("protos.json" in w for w in data["warnings"]))
        self.assertEqual(data["protos"], [])
        self.assertIsNotNone(data["protoError"])
        out = build.render(data)
        self.assertTrue(data["written"])
        self.assertTrue(os.path.exists(out))
        with open(out, encoding="utf-8") as f:
            self.assertNotIn('"protoError": null', f.read())

    def test_wrong_shaped_protos_json_is_an_error_not_a_blank_page(self):
        with mock.patch("sys.stdout"):
            cli.main(["product", "set", "shop", "--name", "Shop"])
        for bad in ("{}", "[null]", '[{"id": "x", "versions": "no"}]', '[{"id": "x", "versions": []}]',
                    '[{"id": "x", "versions": [{"status": "waiting"}]}]'):
            store.write_text("protos.json", bad)
            with mock.patch.object(build, "agents_data", return_value=([], None, {}, [], [])), \
                 mock.patch.object(build, "skills_data", return_value=[]), \
                 mock.patch.object(sources, "prs_for", return_value={}), \
                 mock.patch.object(t3, "thread_handles", return_value={}):
                data = build.collect(NOW)
            self.assertEqual(data["protos"], [], bad)
            self.assertIn("not a list of prototypes", data["protoError"], bad)

    def test_clean_protos_reach_the_page(self):
        with mock.patch("sys.stdout"):
            cli.main(["product", "set", "shop", "--name", "Shop"])
        p = os.path.join(self.tmp.name, "p.html")
        with open(p, "w") as f:
            f.write("<!doctype html><p>x</p>")
        with mock.patch("sys.stdout"):
            cli.main(["proto", "add", "--product", "shop", "--feature", "Pay link", "--file", p])
        with mock.patch.object(build, "agents_data", return_value=([], None, {}, [], [])), \
             mock.patch.object(build, "skills_data", return_value=[]), \
             mock.patch.object(sources, "prs_for", return_value={}), \
             mock.patch.object(t3, "thread_handles", return_value={}):
            data = build.collect(NOW)
        self.assertEqual(data["protoError"], None)
        self.assertEqual([x["id"] for x in data["protos"]], ["shop-pay-link"])


if __name__ == "__main__":
    unittest.main()
