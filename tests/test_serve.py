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

from factory import access_jwt, build, cli, comms, config, release, serve, store


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
        with mock.patch.object(access_jwt, "allowed", return_value=True), \
                mock.patch.object(access_jwt, "is_person", return_value=True):
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

    def test_service_token_may_only_push_machine_agents(self):
        """An Access service token (no email) through the tunnel reaches only the machine push route."""
        tunnel = {"Cf-Ray": "x", "Cf-Access-Jwt-Assertion": "jwt", "Content-Type": "application/json"}
        with mock.patch.object(access_jwt, "allowed", return_value=True), \
                mock.patch.object(access_jwt, "is_person", return_value=False):
            self.assertEqual(self.req("/", headers=tunnel)[0], 403)
            self.assertEqual(self.req("/api/office", headers=tunnel)[0], 403)
            self.assertEqual(self.req("/api/inbox/open", headers=tunnel)[0], 403)
            self.assertEqual(self.req("/api/answers", {"d1": {"choice": "a"}}, tunnel)[0], 403)
            self.assertEqual(self.req("/api/messages", {"thread": "t", "text": "x"}, tunnel)[0], 403)
            # the push route goes on to its own token check (unknown machine: 403 from the handler)
            code, out = self.req("/api/machines/laptop/agents", {"agents": []}, tunnel)
            self.assertEqual((code, json.loads(out)["error"]), (403, "unknown machine or bad token"))

    def test_is_person_needs_an_email_claim(self):
        config.set_value("access.aud", "aud-1")
        with mock.patch.object(access_jwt, "claims", return_value={"email": "owner@example.com", "aud": ["aud-1"]}):
            self.assertTrue(access_jwt.is_person({}))
        with mock.patch.object(access_jwt, "claims", return_value={"common_name": "abc.access"}):
            self.assertFalse(access_jwt.is_person({}))
        with mock.patch.object(access_jwt, "claims", return_value=None):
            self.assertFalse(access_jwt.is_person({}))

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
        with mock.patch.object(access_jwt, "allowed", return_value=True), \
                mock.patch.object(access_jwt, "is_person", return_value=True):
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

    def test_queue_endpoints(self):
        k = "?k=" + self.key
        # Unauthenticated GET
        self.assertEqual(self.req("/api/queue")[0], 403)
        # Unauthenticated POST
        self.assertEqual(self.req("/api/queue", {"repo_pr": "o/r#1", "action": "ok"})[0], 403)

        # Initialize release.json with a product
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["release", "product", "add", "c2e", "--repo", "o/r", "--flow", "pr", "--project", "c2e"])
        rel_data = release.release()
        rel_data["queue"] = {
            "at": "2026-11-10T08:00:00Z",
            "products": {
                "c2e": {"prs": [], "train": {}, "pipe": {"open": 0, "dev": [], "rel": [], "prod": None, "health": None}, "error": None}
            }
        }
        release.save_release(rel_data)

        # Authenticated GET /api/queue
        code, out = self.req("/api/queue" + k)
        self.assertEqual(code, 200)
        body = json.loads(out)
        self.assertEqual(body["at"], "2026-11-10T08:00:00Z")
        self.assertIn("c2e", body["products"])
        # products config names, flows and modes
        self.assertIn("c2e", body["product_configs"])
        self.assertEqual(body["product_configs"]["c2e"]["flow"], "pr")
        self.assertEqual(body["product_configs"]["c2e"]["mode"], "shadow")
        self.assertEqual(body["product_configs"]["c2e"]["repo"], "o/r")
        # no display fields set: name falls back to the title-cased id, short to the id
        self.assertEqual((body["product_configs"]["c2e"]["name"], body["product_configs"]["c2e"]["short"]), ("C2E", "c2e"))

        # POST /api/queue: invalid body
        code, out = self.req("/api/queue" + k, "not a dict")
        self.assertEqual(code, 400)
        code, out = self.req("/api/queue" + k, {"repo_pr": "o/r#1"})
        self.assertEqual(code, 400)

        # POST /api/queue: FactoryError on invalid repo_pr format -> 400
        code, out = self.req("/api/queue" + k, {"repo_pr": "bad", "action": "ok"})
        self.assertEqual(code, 400)
        self.assertIn("invalid repo#n format", json.loads(out)["error"])

        # POST /api/queue: FactoryError on unknown repo -> 400
        code, out = self.req("/api/queue" + k, {"repo_pr": "unknown/repo#1", "action": "ok"})
        self.assertEqual(code, 400)
        self.assertIn("does not belong to any configured product", json.loads(out)["error"])

        # POST /api/queue: FactoryError on unknown action -> 400
        code, out = self.req("/api/queue" + k, {"repo_pr": "o/r#1", "action": "fly_to_moon"})
        self.assertEqual(code, 400)
        self.assertIn("unknown action", json.loads(out)["error"])

        # POST /api/queue: valid action -> 200 and calls record_queue_action
        code, out = self.req("/api/queue" + k, {"repo_pr": "o/r#1", "action": "hold"})
        self.assertEqual(code, 200)
        res = json.loads(out)
        self.assertEqual(res["repo_pr"], "o/r#1")
        self.assertEqual(res["action"], "hold")
        self.assertIn("hold", res["flags"])

    def test_release_api(self):
        k = "?k=" + self.key
        self.assertEqual(self.req("/api/release")[0], 403)
        self.assertEqual(self.req("/api/release", {"action": "pause", "product": "*"})[0], 403)

        # no products: an empty, valid answer
        code, out = self.req("/api/release" + k)
        self.assertEqual((code, json.loads(out)), (200, {"paused": False, "products": {}, "journal": []}))

        with contextlib.redirect_stdout(io.StringIO()):
            for name in ("web", "app"):
                cli.main(["release", "product", "add", name, "--repo", "o/" + name, "--flow", "pr", "--project", name])
        now = datetime.now(timezone.utc)
        iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        today = now.astimezone(release.TUNIS).strftime("%Y-%m-%d")
        rel = release.release()
        rel["verdicts"][today] = {
            "web": {"verdict": "ship-all", "ship": [7], "held": [], "reasons": ["1 PR, all green"], "at": iso(now)},
            "app": {"verdict": "wait", "ship": [], "held": [], "reasons": ["could not read: GitHub API 502"], "at": iso(now)},
        }
        rel["holds"]["web"] = {"since": iso(now - timedelta(hours=6)), "issue": "bad deploy"}
        release.save_release(rel)

        body = json.loads(self.req("/api/release" + k)[1])
        web, app = body["products"]["web"], body["products"]["app"]
        self.assertEqual((web["verdict"]["verdict"], web["verdict"]["ship"], web["mode"], web["paused"]), ("ship-all", [7], "shadow", False))
        self.assertTrue(17.5 < web["hold"]["remaining_hours"] <= 18)
        self.assertIsNone(web["error"])
        self.assertEqual(app["error"], "could not read: GitHub API 502")
        self.assertIsNone(app["hold"])

        # input validation: 400
        for bad in ("not a dict", {"product": "web"}, {"action": "fly", "product": "web"}, {"action": "pause"},
                    {"action": "pause", "product": 3}, {"action": "pause", "product": "nope"}, {"action": "unhold", "product": "*"}):
            self.assertEqual(self.req("/api/release" + k, bad)[0], 400, bad)

        # pause / resume one product, then all; the answer is the fresh state
        code, out = self.req("/api/release" + k, {"action": "pause", "product": "web"})
        self.assertEqual((code, json.loads(out)["products"]["web"]["paused"]), (200, True))
        self.assertEqual(release.release()["paused"], {"web": True})
        code, out = self.req("/api/release" + k, {"action": "pause", "product": "*"})
        self.assertTrue(json.loads(out)["paused"])
        code, out = self.req("/api/release" + k, {"action": "resume", "product": "*"})
        self.assertFalse(json.loads(out)["paused"])
        self.assertTrue(json.loads(out)["products"]["web"]["paused"], "Resume all keeps a product's own pause")
        self.req("/api/release" + k, {"action": "resume", "product": "web"})
        self.assertEqual(release.release()["paused"], {})

        # unhold: refused under 24h with the CLI's reason, accepted after
        code, out = self.req("/api/release" + k, {"action": "unhold", "product": "web"})
        self.assertEqual(code, 400)
        self.assertIn("under 24h old", json.loads(out)["error"])
        rel = release.release()
        rel["holds"]["web"]["since"] = iso(now - timedelta(hours=25))
        release.save_release(rel)
        code, out = self.req("/api/release" + k, {"action": "unhold", "product": "web"})
        self.assertEqual((code, json.loads(out)["products"]["web"]["hold"]), (200, None))
        self.assertEqual(release.release()["holds"], {})

        # a hold with a non-string since, and an almost-expired hold (round up, never enable Unhold early)
        rel = release.release()
        rel["holds"]["web"] = {"since": iso(now - timedelta(hours=23, minutes=58)), "issue": "x"}
        release.save_release(rel)
        code, out = self.req("/api/release" + k)
        self.assertEqual(code, 200)
        self.assertGreater(json.loads(out)["products"]["web"]["hold"]["remaining_hours"], 0)
        rel["holds"]["web"]["since"] = 12345
        release.save_release(rel)
        self.assertEqual(self.req("/api/release" + k)[0], 200)
        self.assertIn("journal", json.loads(self.req("/api/release" + k)[1]))

        # in flight and last event come from release events only: queue noise never hides "shipped" or "rollback-failed"
        rel = release.release()
        rel["holds"] = {}
        ev = lambda e, h: {"at": iso(now - timedelta(hours=h)), "product": "web", "event": e}
        rel["journal"] = [ev("started", 9), ev("shipped", 8)] + [ev("queue-merged", 7 - i * 0.1) for i in range(6)]
        release.save_release(rel)
        web = json.loads(self.req("/api/release" + k)[1])["products"]["web"]
        self.assertIsNone(web["inFlightSince"])
        self.assertEqual([e["event"] for e in web["journal"]], ["started", "shipped"])
        rel["journal"] += [ev("started", 3), ev("rollback-failed", 2)] + [ev("queue-closed", 1)] * 3
        release.save_release(rel)
        out = json.loads(self.req("/api/release" + k)[1])
        self.assertEqual(out["products"]["web"]["lastEvent"], "rollback-failed")
        self.assertFalse(any(e["event"].startswith("queue-") for e in out["journal"]))
        rel["journal"] += [ev("started", 0.5), ev("held", 0.4)]
        release.save_release(rel)
        self.assertIsNotNone(json.loads(self.req("/api/release" + k)[1])["products"]["web"]["inFlightSince"], "held after started is still in flight")

    def test_comms_api(self):
        k = "?k=" + self.key
        with store.lock():
            r = release.release()
            r["products"]["app"] = {
                "repo": "acme/app", "flow": "pr", "base": "develop", "head": "main",
                "thread": "t-app", "red_paths": [], "project": "app", "mode": "live", "checks": [],
            }
            r["products"]["web"] = {
                "repo": "acme/web", "flow": "release-branch", "base": "develop", "head": "main",
                "thread": "t-web", "red_paths": [], "project": "web", "mode": "live", "checks": [],
            }
            r["products"]["app"].update(display_name="App", color="#17C964", short="ap", flow_label="develop → main",
                                        comms={"langs": ["en", "fr"], "audiences": ["creators"], "internal_scopes": []})
            r["products"]["web"].update(display_name="Web", color="#4670CF",
                                        comms={"langs": ["fr", "en"], "audiences": ["creators"], "internal_scopes": []})
            release.save_release(r)

            c_data = comms.comms()
            c_data["changelogs"]["app@2026-11-10T08:00:00Z"] = {
                "product": "app",
                "at": "2026-11-10T08:00:00Z",
                "label": "2026-11-10",
                "items": [{"n": 1, "title": "feat: test", "group": "new"}],
            }
            c_data["jobs"]["f-app-1"] = {
                "id": "f-app-1",
                "product": "app",
                "kind": "feature",
                "status": "ready",
                "updated": "2026-11-10T08:30:00Z",
            }
            comms.save_comms(c_data)

        # GET /api/comms: returns shape with products
        code, out = self.req("/api/comms" + k)
        self.assertEqual(code, 200)
        data = json.loads(out)
        self.assertEqual(list(data.keys()), ["products"])
        self.assertNotIn("app", data)
        products = data["products"]
        self.assertIn("app", products)
        self.assertIn("web", products)
        # store is not configured in release.json products yet, so excluded
        self.assertNotIn("store", products)

        app_prod = products["app"]
        self.assertEqual((app_prod["name"], app_prod["color"]), ("App", "#17C964"))
        self.assertEqual(list(products), ["app", "web"])
        self.assertEqual(len(app_prod["changelogs"]), 1)
        self.assertEqual(app_prod["changelogs"][0]["label"], "2026-11-10")
        self.assertEqual(len(app_prod["jobs"]), 1)
        self.assertEqual(app_prod["jobs"][0]["status"], "ready")
        self.assertIn("rules", app_prod)
        rules = app_prod["rules"]
        self.assertIn("internal_list", rules)
        self.assertIn("big_definition", rules)
        self.assertEqual(rules["friday_timing"], "Friday 10:00 Tunis")
        self.assertEqual(rules["language_order"], ["en", "fr"])

        # Per-product error resilience: error in due() for one product does not break others
        orig_due = comms.due
        def mock_due(prod, now=None, **kwargs):
            if prod == "web":
                raise store.FactoryError("simulated due failure for web")
            return orig_due(prod, now=now, **kwargs)

        with mock.patch.object(comms, "due", side_effect=mock_due):
            code, out = self.req("/api/comms" + k)
            self.assertEqual(code, 200)
            data2 = json.loads(out)
            self.assertIn("error", data2["products"]["web"])
            self.assertIn("simulated due failure", data2["products"]["web"]["error"])
            self.assertNotIn("error", data2["products"]["app"])

        # Read-only: POST is refused (404)
        code, _ = self.req("/api/comms" + k, {"anything": True})
        self.assertEqual(code, 404)




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
