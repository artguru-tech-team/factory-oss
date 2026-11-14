"""Unit tests for factory/suggest_look.py; the HTTP call is always mocked."""
import http.server
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock

from factory import config, store, suggest_look

ITEMS = ["Item A", "Item B"]


def reply(effort=None, roadmap=None):
    a = {"effort": {"probabilities": effort or {"0": 0.26, "1": 0.54, "2": 0.19, "3": 0.01, "4": 0.0}}}
    if roadmap:
        a["roadmap"] = roadmap
    return {"answers": a}


def rm(choice, p):
    return {"choice": choice, "probabilities": {choice: p}}


class TestFirstLook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        os.environ["FACTORY_HOME"] = self.tmp.name
        store.write_text("secrets/typesafe-key", "k-test\n")
        os.chmod(store.path("secrets/typesafe-key"), 0o600)
        config.set_value("office.publicRoadmap.alpha", json.dumps(ITEMS), as_json=True)
        suggest_look._day, suggest_look._count = None, 0

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.clear()
        os.environ.update(self._env)

    def look(self, resp=None, side_effect=None):
        with mock.patch.object(suggest_look, "_post", return_value=resp, side_effect=side_effect) as m:
            return suggest_look.first_look("alpha", "an idea"), m

    def test_item_match(self):
        out, m = self.look(reply(roadmap=rm("Item A", 0.5)))
        self.assertEqual(out, {"fit": "Close to a planned item: Item A", "effort": "About a day", "source": "jev"})
        body = m.call_args[0][0]
        self.assertEqual(body["state"], {"product": "alpha", "idea": "an idea", "public_roadmap": ITEMS})
        self.assertEqual(list(body["questions"]["roadmap"]["criteria"]), ITEMS + ["none"])
        self.assertEqual(m.call_args[0][1], "k-test")

    def test_item_below_half(self):
        out, _ = self.look(reply(roadmap=rm("Item A", 0.49)))
        self.assertEqual(out["fit"], "Not on the public roadmap yet.")

    def test_choice_none(self):
        out, _ = self.look(reply(roadmap=rm("none", 0.9)))
        self.assertEqual(out["fit"], "Not on the public roadmap yet.")

    def test_no_roadmap(self):
        config.set_value("office.publicRoadmap.alpha", "[]", as_json=True)
        out, m = self.look(reply())
        self.assertEqual(out["fit"], "No public roadmap for this product yet.")
        self.assertNotIn("roadmap", m.call_args[0][0]["questions"])

    def test_items_capped_and_long_titles_skipped(self):
        titles = ["x" * 121] + [f"T{i}" for i in range(40)]
        config.set_value("office.publicRoadmap.alpha", json.dumps(titles), as_json=True)
        _, m = self.look(reply(roadmap=rm("none", 1)))
        sent = m.call_args[0][0]["state"]["public_roadmap"]
        self.assertEqual(sent, [f"T{i}" for i in range(30)])

    def test_effort_max_label(self):
        for idx, label in enumerate(suggest_look.EFFORTS):
            probs = {str(i): 0.1 for i in range(5)}
            probs[str(idx)] = 0.6
            out, _ = self.look(reply(effort=probs, roadmap=rm("none", 1)))
            self.assertEqual(out["effort"], label)

    def test_failures_return_none(self):
        cases = [
            urllib.error.URLError("timed out"),
            urllib.error.HTTPError("u", 500, "err", {}, io.BytesIO(b"")),
            json.JSONDecodeError("bad", "", 0),
            TimeoutError(),
        ]
        for exc in cases:
            with mock.patch("sys.stderr", new=io.StringIO()) as err:
                out, _ = self.look(side_effect=exc)
            self.assertIsNone(out)
            self.assertIn("first look failed", err.getvalue())
            self.assertNotIn("an idea", err.getvalue())
            self.assertNotIn("k-test", err.getvalue())
        out, _ = self.look({"nope": 1})
        self.assertIsNone(out)

    def test_http_layer_malformed_and_500(self):
        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass
        with mock.patch.object(suggest_look._OPENER, "open", return_value=R(b"not json")):
            self.assertIsNone(suggest_look.first_look("alpha", "an idea"))
        err = urllib.error.HTTPError("u", 500, "e", {}, io.BytesIO(b""))
        with mock.patch.object(suggest_look._OPENER, "open", side_effect=err) as u:
            self.assertIsNone(suggest_look.first_look("alpha", "an idea"))
        self.assertEqual(u.call_args[1]["timeout"], 3)

    def test_missing_secret(self):
        os.remove(store.path("secrets/typesafe-key"))
        out, m = self.look(reply())
        self.assertIsNone(out)
        m.assert_not_called()

    def test_cap(self):
        config.set_value("office.firstLook.dailyMax", "2", as_json=True)
        for _ in range(2):
            self.assertIsNotNone(self.look(reply(roadmap=rm("none", 1)))[0])
        out, m = self.look(reply())
        self.assertIsNone(out)
        m.assert_not_called()

    def test_disabled(self):
        # a JSON false, the plain string, and typos: only an explicit yes keeps it on
        for v, as_json in (("false", True), ("false", False), ("disabled", False), ("", False), ("0", True)):
            config.set_value("office.firstLook.enabled", v, as_json=as_json)
            out, m = self.look(reply())
            self.assertIsNone(out)
            m.assert_not_called()

    def test_request_has_no_email_or_ip(self):
        _, m = self.look(reply(roadmap=rm("none", 1)))
        blob = json.dumps(m.call_args[0][0]).lower()
        self.assertNotIn("email", blob)
        self.assertNotIn("ip", set(json.loads(blob)["state"]))
        self.assertEqual(set(json.loads(blob)["state"]), {"product", "idea", "public_roadmap"})

    def test_enabled_values(self):
        for v, as_json in (("true", True), ("on", False), (" Yes ", False), ("1", True)):
            config.set_value("office.firstLook.enabled", v, as_json=as_json)
            self.assertIsNotNone(self.look(reply(roadmap=rm("none", 1)))[0])

    def test_cap_string_and_bad_values(self):
        config.set_value("office.firstLook.dailyMax", "1")  # plain string, as `config set` stores it
        self.assertIsNotNone(self.look(reply(roadmap=rm("none", 1)))[0])
        self.assertIsNone(self.look(reply())[0])
        for bad, as_json in (("-1", True), ("-1", False), ("2.5", True), ("lots", False)):
            suggest_look._day, suggest_look._count = None, 0
            config.set_value("office.firstLook.dailyMax", bad, as_json=as_json)
            out, m = self.look(reply())
            self.assertIsNone(out, bad)
            m.assert_not_called()

    def test_none_title_dropped_and_duplicates_merged(self):
        config.set_value("office.publicRoadmap.alpha", json.dumps(["None", " Item A", "Item A", "none", ""]), as_json=True)
        out, m = self.look(reply(roadmap=rm("none", 0.9)))
        self.assertEqual(m.call_args[0][0]["state"]["public_roadmap"], ["Item A"])
        self.assertEqual(list(m.call_args[0][0]["questions"]["roadmap"]["criteria"]), ["Item A", "none"])
        self.assertEqual(out["fit"], "Not on the public roadmap yet.")

    def test_bad_probabilities_return_none(self):
        base = {str(i): 0.1 for i in range(5)}
        for effort in ({**base, "-1": 0.9}, {**base, "5": 0.9}, {**base, "0": float("nan")}, {**base, "0": 1.5}, {**base, "0": True},
                       {"0": 0.9}, {str(i): 0.0 for i in range(5)}, {**base, "01": 0.5}, {**base, "0": "0.6"}):
            with mock.patch("sys.stderr", new=io.StringIO()):
                self.assertIsNone(self.look(reply(effort=effort, roadmap=rm("none", 1)))[0], effort)
        empty = {"answers": {"effort": {"probabilities": {}}, "roadmap": rm("none", 1)}}
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.assertIsNone(self.look(empty)[0])
            self.assertIsNone(self.look(reply(roadmap=rm("Item A", float("nan"))))[0])

    def test_real_response_shape(self):
        # recorded from api.typesafe.ai (jev-1.14.0) for a CSV-export idea
        real = {"model": "jev-1.14.0", "answers": {
            "effort": {"type": "score", "score": 0.94, "confidence": 0.61,
                       "legend": {"0": "A few hours", "1": "About a day", "2": "A few days", "3": "One to two weeks", "4": "More than two weeks"},
                       "probabilities": {"0": 0.26, "1": 0.54, "2": 0.19, "3": 0.01, "4": 0.0}},
            "roadmap": {"type": "choice", "choice": "Item A", "confidence": 0.7,
                        "probabilities": {"Item A": 0.8, "Item B": 0.1, "none": 0.1}}},
            "usage": {"input_tokens": 337, "output_tokens": 18}}
        out, _ = self.look(real)
        self.assertEqual(out, {"fit": "Close to a planned item: Item A", "effort": "About a day", "source": "jev"})

    def test_wall_clock_deadline(self):
        gate = threading.Event()
        def stall(body, key):
            gate.wait(5)
            return reply()
        with mock.patch.object(suggest_look, "TIMEOUT", 0.2), mock.patch.object(suggest_look, "_request", side_effect=stall), \
                mock.patch("sys.stderr", new=io.StringIO()):
            t = time.monotonic()
            self.assertIsNone(suggest_look.first_look("alpha", "an idea"))
            self.assertLess(time.monotonic() - t, 1.5)
        gate.set()

    def test_redirect_not_followed(self):
        seen = []

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            do_POST = do_GET
            def log_message(self, *a): pass

        target = http.server.HTTPServer(("127.0.0.1", 0), Target)

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/")
                self.end_headers()
            def log_message(self, *a): pass

        src = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
        for srv in (target, src):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(suggest_look, "URL", f"http://127.0.0.1:{src.server_port}/"), \
                    mock.patch("sys.stderr", new=io.StringIO()) as err:
                self.assertIsNone(suggest_look.first_look("alpha", "an idea"))
            self.assertEqual(seen, [])
            self.assertIn("HTTPError", err.getvalue())
        finally:
            for srv in (target, src):
                srv.shutdown(); srv.server_close()

    def test_missing_secret_logs_reason(self):
        os.remove(store.path("secrets/typesafe-key"))
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIsNone(self.look(reply())[0])
        self.assertIn("secret typesafe-key not usable", err.getvalue())

    def _drip_server(self):
        class Drip(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(200)
                self.send_header("Content-Length", "60000")
                self.end_headers()
                try:
                    for _ in range(60):
                        self.wfile.write(b" "); self.wfile.flush(); time.sleep(0.05)
                except OSError:
                    pass
            def log_message(self, *a): pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Drip)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return f"http://127.0.0.1:{srv.server_port}/"

    def test_slow_drip_ends_at_deadline_and_frees_the_slot(self):
        url = self._drip_server()
        with mock.patch.object(suggest_look, "URL", url), mock.patch.object(suggest_look, "TIMEOUT", 0.5), \
                mock.patch("sys.stderr", new=io.StringIO()):
            t = time.monotonic()
            self.assertIsNone(suggest_look.first_look("alpha", "an idea"))
            self.assertLess(time.monotonic() - t, 1.5)
            time.sleep(0.4)  # the worker's own deadline ends the read and frees its slot
            for _ in range(suggest_look.MAX_IN_FLIGHT):
                self.assertTrue(suggest_look._slots.acquire(blocking=False))
            for _ in range(suggest_look.MAX_IN_FLIGHT):
                suggest_look._slots.release()

    def test_busy_fails_fast(self):
        for _ in range(suggest_look.MAX_IN_FLIGHT):
            suggest_look._slots.acquire()
        try:
            with mock.patch.object(suggest_look, "_request") as r, mock.patch("sys.stderr", new=io.StringIO()) as err:
                t = time.monotonic()
                self.assertIsNone(suggest_look.first_look("alpha", "an idea"))
                self.assertLess(time.monotonic() - t, 0.5)
            r.assert_not_called()
            self.assertIn("Busy", err.getvalue())
            self.assertEqual(suggest_look._count, 0)  # the day's call was given back
        finally:
            for _ in range(suggest_look.MAX_IN_FLIGHT):
                suggest_look._slots.release()


if __name__ == "__main__":
    unittest.main()
