"""Unit tests for factory/suggest_serve.py HTTP service and CLI commands."""
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from factory import cli, config, public_serve, secrets, store, suggest_serve, suggestions


class TestSuggestServe(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls._saved_env = dict(os.environ)
        os.environ["FACTORY_HOME"] = cls.tmp.name
        config.set_value("office.publicProducts", json.dumps(["acme", "other"]), as_json=True)
        store.write_text("secrets/suggest-vote-key-acme", "test-key-for-acme" + chr(10))
        class Srv(suggest_serve.ThreadingHTTPServer):
            request_queue_size = 128  # the default backlog of 5 drops the 40-thread race test's connections

        cls.srv = Srv(("127.0.0.1", 0), suggest_serve.SuggestHandler)
        cls.thread = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()
        os.environ.clear()
        os.environ.update(cls._saved_env)

    def setUp(self):
        # Reset DB before each test
        db_path = store.path("suggestions.db")
        for suffix in ("", "-wal", "-shm"):  # WAL mode keeps side files
            if os.path.exists(db_path + suffix):
                os.remove(db_path + suffix)
        # Ensure acme key exists
        store.write_text("secrets/suggest-vote-key-acme", "test-key-for-acme" + chr(10))
        config.set_value("office.publicProducts", json.dumps(["acme", "other"]), as_json=True)

    def post(self, path, obj, headers=None):
        body = json.dumps(obj).encode("utf-8")
        h = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        h.update(headers or {})
        status, resp, _ = self.req(path, method="POST", body=body, headers=h)
        return status, json.loads(resp)

    def ideas_json(self):
        status, body, _ = self.req("/suggest/ideas.json")
        self.assertEqual(status, 200)
        return json.loads(body)["ideas"]

    def token(self, uid="u1", product="acme", key="test-key-for-acme", exp=None):
        return suggest_serve.make_vote_token(product, uid, exp or int(time.time()) + 3600, key)

    def req(self, path, method="GET", body=None, headers=None):
        r = urllib.request.Request(self.base + path, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_public_ideas_leak_test(self):
        # Insert idea with sensitive fields
        idea_id = suggestions.add_idea(
            product="acme",
            text="A completely safe suggestion for public display",
            email="secret-author@example.com",
            ip_hash="secret-ip-hash-xyz",
        )
        # Screen it so it becomes public
        suggestions.screen_pending()

        # Add a vote with account hash
        suggestions.set_vote(idea_id, "secret-account-hash-abc", True)

        status, body, headers = self.req("/suggest/ideas.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Cache-Control"), "public, max-age=30")
        self.assertEqual(headers.get("Content-Type"), "application/json")

        raw_str = body.decode("utf-8")
        # Leak test: sensitive fields must NEVER appear anywhere in the serialized response
        self.assertNotIn("secret-author@example.com", raw_str)
        self.assertNotIn("secret-ip-hash-xyz", raw_str)
        self.assertNotIn("secret-account-hash-abc", raw_str)
        self.assertNotIn("screened", raw_str)
        self.assertNotIn("hidden", raw_str)
        self.assertNotIn("ip_hash", raw_str)
        self.assertNotIn("account_hash", raw_str)

        # Parse JSON and verify ONLY the 6 allowed keys exist
        data = json.loads(raw_str)
        self.assertIn("ideas", data)
        self.assertEqual(len(data["ideas"]), 1)
        item = data["ideas"][0]
        allowed_keys = {"id", "product", "text", "status", "votes", "created_at"}
        self.assertEqual(set(item.keys()), allowed_keys)
        self.assertEqual(item["id"], idea_id)
        self.assertEqual(item["product"], "acme")
        self.assertEqual(item["votes"], 1)
        self.assertEqual(item["status"], "under-review")

        # 13. pending, held and hidden ideas never reach ideas.json, and each item keeps the exact key set
        held = suggestions.add_idea("acme", "Held idea seed see www.example.com now", None, "ip-x")
        gone = suggestions.add_idea("acme", "Removed idea seed number three", None, "ip-x")
        suggestions.screen_pending()
        pending = suggestions.add_idea("acme", "Pending idea seed number four", None, "ip-x")
        suggestions.hide(gone)
        self.assertEqual(suggestions.get_idea(held)["screened"], 2)
        self.assertEqual(suggestions.get_idea(pending)["screened"], 0)
        items = self.ideas_json()
        self.assertEqual([i["id"] for i in items], [idea_id])
        for i in items:
            self.assertEqual(set(i.keys()), allowed_keys)

    def test_honeypot(self):
        payload = {
            "product": "acme",
            "text": "Spam text with enough characters",
            "website": "https://spam.example.com",
        }
        body = json.dumps(payload).encode("utf-8")
        status, resp_body, _ = self.req(
            "/suggest/idea",
            method="POST",
            body=body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        )
        self.assertEqual(status, 200)
        data = json.loads(resp_body)
        self.assertEqual(data, {"id": None})

        # Confirm nothing was stored
        self.assertEqual(len(suggestions.list_ideas()), 0)

    def test_validation_post_idea(self):
        # Missing Content-Type
        status, _, _ = self.req("/suggest/idea", method="POST", body=b"{}", headers={"Content-Length": "2"})
        self.assertEqual(status, 400)

        # Wrong Content-Type
        status, _, _ = self.req(
            "/suggest/idea",
            method="POST",
            body=b"{}",
            headers={"Content-Type": "text/plain", "Content-Length": "2"},
        )
        self.assertEqual(status, 400)

        # Content-Length > 2048 -> 413
        oversized_body = json.dumps({"product": "acme", "text": "a" * 2100}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/idea",
            method="POST",
            body=oversized_body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(oversized_body))},
        )
        self.assertEqual(status, 413)

        # Malformed JSON -> 400
        bad_json = b"{not json"
        status, _, _ = self.req(
            "/suggest/idea",
            method="POST",
            body=bad_json,
            headers={"Content-Type": "application/json", "Content-Length": str(len(bad_json))},
        )
        self.assertEqual(status, 400)

        # Non-dict JSON -> 400
        arr_json = b"[1, 2, 3]"
        status, _, _ = self.req(
            "/suggest/idea",
            method="POST",
            body=arr_json,
            headers={"Content-Type": "application/json", "Content-Length": str(len(arr_json))},
        )
        self.assertEqual(status, 400)

        # Product not in office.publicProducts -> 400
        p_bad = json.dumps({"product": "unknown-product", "text": "Valid text length here"}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/idea",
            method="POST",
            body=p_bad,
            headers={"Content-Type": "application/json", "Content-Length": str(len(p_bad))},
        )
        self.assertEqual(status, 400)

        # Text < 10 chars -> 400
        short_text = json.dumps({"product": "acme", "text": "too short"}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/idea",
            method="POST",
            body=short_text,
            headers={"Content-Type": "application/json", "Content-Length": str(len(short_text))},
        )
        self.assertEqual(status, 400)

        # Text > 500 chars -> 400
        long_text = json.dumps({"product": "acme", "text": "x" * 501}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/idea",
            method="POST",
            body=long_text,
            headers={"Content-Type": "application/json", "Content-Length": str(len(long_text))},
        )
        self.assertEqual(status, 400)

        # Invalid email -> 400
        bad_email = json.dumps({"product": "acme", "text": "Valid text length here", "email": "notanemail"}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/idea",
            method="POST",
            body=bad_email,
            headers={"Content-Type": "application/json", "Content-Length": str(len(bad_email))},
        )
        self.assertEqual(status, 400)

        # Valid submission -> 201
        valid_payload = {
            "product": "acme",
            "text": "Add keyboard navigation shortcuts",
            "email": "user@example.com",
        }
        valid_body = json.dumps(valid_payload).encode("utf-8")
        status, resp_body, _ = self.req(
            "/suggest/idea",
            method="POST",
            body=valid_body,
            headers={"Content-Type": "application/json", "Content-Length": str(len(valid_body))},
        )
        self.assertEqual(status, 201)
        data = json.loads(resp_body)
        self.assertEqual(data["product"], "acme")
        self.assertEqual(data["text"], "Add keyboard navigation shortcuts")
        self.assertEqual(data["status"], "under-review")
        self.assertIsInstance(data["id"], int)

    def test_idea_rate_limit_429(self):
        payload = json.dumps({"product": "acme", "text": "Valid suggestion text 12345"}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Content-Length": str(len(payload)),
            "CF-Connecting-IP": "203.0.113.1",
        }

        # 5 ideas allowed
        for i in range(5):
            status, _, _ = self.req("/suggest/idea", method="POST", body=payload, headers=headers)
            self.assertEqual(status, 201, f"Request {i+1} should succeed")

        # 6th idea rejected with 429
        status, resp_body, _ = self.req("/suggest/idea", method="POST", body=payload, headers=headers)
        self.assertEqual(status, 429)
        self.assertIn("rate limit", json.loads(resp_body).get("error", ""))

        # Different IP succeeds
        headers_other = dict(headers)
        headers_other["CF-Connecting-IP"] = "203.0.113.2"
        status, _, _ = self.req("/suggest/idea", method="POST", body=payload, headers=headers_other)
        self.assertEqual(status, 201)

    def test_vote_token_validation_and_voting(self):
        # Create an approved idea
        idea_id = suggestions.add_idea("acme", "A great idea to vote upon today", None, "ip1")
        suggestions.screen_pending()

        exp_future = int(time.time()) + 3600
        valid_token = suggest_serve.make_vote_token("acme", "user-100", exp_future, "test-key-for-acme")

        # Valid vote -> 200
        vote_payload = json.dumps({"idea_id": idea_id, "token": valid_token, "on": True}).encode("utf-8")
        status, resp_body, _ = self.req(
            "/suggest/vote",
            method="POST",
            body=vote_payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(vote_payload))},
        )
        self.assertEqual(status, 200)
        data = json.loads(resp_body)
        self.assertEqual(data["idea_id"], idea_id)
        self.assertEqual(data["votes"], 1)
        self.assertTrue(data["on"])

        # Toggle vote off -> 200, votes=0
        unvote_payload = json.dumps({"idea_id": idea_id, "token": valid_token, "on": False}).encode("utf-8")
        status, resp_body, _ = self.req(
            "/suggest/vote",
            method="POST",
            body=unvote_payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(unvote_payload))},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(resp_body)["votes"], 0)

        # Bad signature -> 401
        bad_sig_token = suggest_serve.make_vote_token("acme", "user-100", exp_future, "wrong-key")
        p = json.dumps({"idea_id": idea_id, "token": bad_sig_token, "on": True}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/vote",
            method="POST",
            body=p,
            headers={"Content-Type": "application/json", "Content-Length": str(len(p))},
        )
        self.assertEqual(status, 401)

        # Expired token -> 401
        exp_past = int(time.time()) - 100
        expired_token = suggest_serve.make_vote_token("acme", "user-100", exp_past, "test-key-for-acme")
        p = json.dumps({"idea_id": idea_id, "token": expired_token, "on": True}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/vote",
            method="POST",
            body=p,
            headers={"Content-Type": "application/json", "Content-Length": str(len(p))},
        )
        self.assertEqual(status, 401)

        # Wrong product -> 401
        store.write_text("secrets/suggest-vote-key-other", "test-key-for-other" + chr(10))
        wrong_prod_token = suggest_serve.make_vote_token("other", "user-100", exp_future, "test-key-for-other")
        p = json.dumps({"idea_id": idea_id, "token": wrong_prod_token, "on": True}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/vote",
            method="POST",
            body=p,
            headers={"Content-Type": "application/json", "Content-Length": str(len(p))},
        )
        self.assertEqual(status, 401)

        # Missing key -> 401
        missing_key_token = suggest_serve.make_vote_token("nokey", "user-100", exp_future, "any-key")
        p = json.dumps({"idea_id": idea_id, "token": missing_key_token, "on": True}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/vote",
            method="POST",
            body=p,
            headers={"Content-Type": "application/json", "Content-Length": str(len(p))},
        )
        self.assertEqual(status, 401)

        # Unknown idea -> 404
        p = json.dumps({"idea_id": 999999, "token": valid_token, "on": True}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/vote",
            method="POST",
            body=p,
            headers={"Content-Type": "application/json", "Content-Length": str(len(p))},
        )
        self.assertEqual(status, 404)

        # Unscreened idea (screened=0) -> 404
        unscreened_id = suggestions.add_idea("acme", "Another fresh suggestion not yet approved", None, "ip1")
        p = json.dumps({"idea_id": unscreened_id, "token": valid_token, "on": True}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/vote",
            method="POST",
            body=p,
            headers={"Content-Type": "application/json", "Content-Length": str(len(p))},
        )
        self.assertEqual(status, 404)

        # Hidden idea (hidden=1) -> 404
        suggestions.hide(idea_id)
        p = json.dumps({"idea_id": idea_id, "token": valid_token, "on": True}).encode("utf-8")
        status, _, _ = self.req(
            "/suggest/vote",
            method="POST",
            body=p,
            headers={"Content-Type": "application/json", "Content-Length": str(len(p))},
        )
        self.assertEqual(status, 404)

    def test_vote_rate_limit_429(self):
        exp_future = int(time.time()) + 3600
        token = suggest_serve.make_vote_token("acme", "power-voter", exp_future, "test-key-for-acme")

        # Create 61 ideas and screen them
        idea_ids = []
        for i in range(61):
            iid = suggestions.add_idea("acme", f"Idea number {i:03d} to be voted upon", None, "ip1")
            idea_ids.append(iid)
        suggestions.screen_pending()

        # Cast 60 votes directly in the store for this account
        import hashlib, hmac
        acct_hash = hmac.new(suggestions.suggest_salt(), b"acme:power-voter", hashlib.sha256).hexdigest()
        for i in range(60):
            suggestions.set_vote(idea_ids[i], acct_hash, True)
        self.assertEqual(suggestions.votes_count_last_hour(acct_hash), 60)

        # 61st vote gets 429
        p61 = json.dumps({"idea_id": idea_ids[60], "token": token, "on": True}).encode("utf-8")
        status, resp_body, _ = self.req(
            "/suggest/vote",
            method="POST",
            body=p61,
            headers={"Content-Type": "application/json", "Content-Length": str(len(p61))},
        )
        self.assertEqual(status, 429)
        self.assertIn("rate limit", json.loads(resp_body).get("error", ""))

    def test_404_and_405_routes(self):
        # 404 on nonexistent paths
        for path in ("/nonexistent", "/suggest/unknown", "/api/suggest"):
            status, _, _ = self.req(path)
            self.assertEqual(status, 404)

        # 405 on wrong methods
        status, _, headers = self.req("/suggest/ideas.json", method="POST", body=b"{}", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "GET, HEAD")

        status, _, headers = self.req("/suggest/idea", method="GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

        status, _, headers = self.req("/suggest/vote", method="GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

        # HEAD /suggest/ideas.json -> 200 with no body
        status, body, headers = self.req("/suggest/ideas.json", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Content-Type"), "application/json")

    def test_security_headers_present(self):
        status, _, headers = self.req("/suggest/ideas.json")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Security-Policy"), public_serve.csp())
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

        # On error response as well
        status, _, headers = self.req("/nonexistent")
        self.assertEqual(status, 404)
        self.assertEqual(headers.get("Content-Security-Policy"), public_serve.csp())
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

    def test_cli_parser_and_execution(self):
        p = cli.parser()

        # Test office serve-suggest args
        args = p.parse_args(["office", "serve-suggest"])
        self.assertEqual(args.cmd, "office")
        self.assertEqual(args.sub, "serve-suggest")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 3047)

        # Test suggestions list / status / hide / screen
        args_list = p.parse_args(["suggestions", "list", "--product", "acme", "--held"])
        self.assertEqual(args_list.cmd, "suggestions")
        self.assertEqual(args_list.sub, "list")
        self.assertEqual(args_list.product, "acme")
        self.assertTrue(args_list.held)

        args_status = p.parse_args(["suggestions", "status", "42", "planned"])
        self.assertEqual(args_status.id, 42)
        self.assertEqual(args_status.status, "planned")

        args_hide = p.parse_args(["suggestions", "hide", "42"])
        self.assertEqual(args_hide.id, 42)

        args_screen = p.parse_args(["suggestions", "screen"])
        self.assertEqual(args_screen.cmd, "suggestions")
        self.assertEqual(args_screen.sub, "screen")

        # Test CLI handler execution
        idea_id = suggestions.add_idea("acme", "Idea for CLI testing commands", "user@example.com", "ip1")
        args_screen = p.parse_args(["suggestions", "screen"])
        obj, text = cli.suggestions_screen(args_screen)
        self.assertEqual(obj["screened"], 1)
        self.assertEqual(obj["passed"], 1)
        self.assertIn("1 passed", text)

        args_list = p.parse_args(["suggestions", "list"])
        obj, text = cli.suggestions_list(args_list)
        self.assertEqual(len(obj), 1)
        self.assertIn("Idea for CLI testing commands", text)

        args_status = p.parse_args(["suggestions", "status", str(idea_id), "building"])
        obj, text = cli.suggestions_status(args_status)
        self.assertEqual(obj["status"], "building")

        args_hide = p.parse_args(["suggestions", "hide", str(idea_id)])
        obj, text = cli.suggestions_hide(args_hide)
        self.assertEqual(obj["hidden"], 1)

    # ---- review fixes ----

    def test_fix1_idea_rate_limit_race(self):
        """Fix 1: 40 parallel POSTs from one IP create exactly 5 ideas."""
        results = []
        def one(i):
            results.append(self.post("/suggest/idea", {"product": "acme", "text": f"Parallel idea number {i:03d} here"},
                                     {"CF-Connecting-IP": "198.51.100.7"})[0])
        threads = [threading.Thread(target=one, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(201), 5, results)
        self.assertEqual(results.count(429), 35, results)
        self.assertEqual(len(suggestions.list_ideas()), 5)

    def test_fix2_vote_limit_only_on_true(self):
        """Fix 2: 61st on:true gets 429, on:false is never limited."""
        iid = suggestions.add_idea("acme", "An idea to toggle votes on", None, "ip1")
        suggestions.screen_pending()
        tok = self.token("toggler")
        for _ in range(60):
            self.assertEqual(self.post("/suggest/vote", {"idea_id": iid, "token": tok, "on": True})[0], 200)
            self.assertEqual(self.post("/suggest/vote", {"idea_id": iid, "token": tok, "on": False})[0], 200)
        self.assertEqual(self.post("/suggest/vote", {"idea_id": iid, "token": tok, "on": True})[0], 429)
        status, body = self.post("/suggest/vote", {"idea_id": iid, "token": tok, "on": False})
        self.assertEqual(status, 200)
        self.assertEqual(body["votes"], 0)

    def test_fix4_screen_runs_on_post(self):
        """Fix 4: clean idea is public at once, link spam is held and reported public:false."""
        status, body = self.post("/suggest/idea", {"product": "acme", "text": "A perfectly clean suggestion"})
        self.assertEqual(status, 201)
        self.assertIs(body["public"], True)
        self.assertEqual([i["id"] for i in self.ideas_json()], [body["id"]])
        status, spam = self.post("/suggest/idea", {"product": "acme", "text": "Buy now at https://spam.example.com/x"})
        self.assertEqual(status, 201)
        self.assertIs(spam["public"], False)
        self.assertEqual([i["id"] for i in self.ideas_json()], [body["id"]])

    def test_look_on_public_idea_only(self):
        """201 carries the first look for a public idea; a held idea gets null and no model call."""
        look = {"fit": "Not on the public roadmap yet.", "effort": "About a day", "source": "jev"}
        with mock.patch.object(suggest_serve.suggest_look, "first_look", return_value=look) as m:
            status, body = self.post("/suggest/idea", {"product": "acme", "text": "A perfectly clean suggestion"})
            self.assertEqual((status, body["look"]), (201, look))
            m.assert_called_once_with("acme", "A perfectly clean suggestion")
        with mock.patch.object(suggest_serve.suggest_look, "first_look", return_value=look) as m:
            status, spam = self.post("/suggest/idea", {"product": "acme", "text": "Buy now at https://spam.example.com/x"})
            self.assertEqual((status, spam["public"], spam["look"]), (201, False, None))
            m.assert_not_called()

    def test_fix5_public_products_list_is_live(self):
        """Fix 5: removing a product hides its ideas and 404s votes."""
        iid = suggestions.add_idea("acme", "An idea on a product to remove", None, "ip1")
        suggestions.screen_pending()
        self.assertEqual(len(self.ideas_json()), 1)
        config.set_value("office.publicProducts", json.dumps(["other"]), as_json=True)
        self.assertEqual(self.ideas_json(), [])
        self.assertEqual(self.post("/suggest/vote", {"idea_id": iid, "token": self.token(), "on": True})[0], 404)
        # a plain string fails closed, like the build's public_view (item 9)
        config.set_value("office.publicProducts", "other acme", as_json=False)
        self.assertEqual(self.ideas_json(), [])

    def test_fix6_types_and_ranges(self):
        """Fix 6: bad idea_id, product type and Content-Length."""
        tok = self.token()
        for bad in (0, -1, 2**63, True, 1.5, "1", None):
            self.assertEqual(self.post("/suggest/vote", {"idea_id": bad, "token": tok, "on": True})[0], 400, bad)
        for bad in (["acme"], {"a": 1}, 5, None):
            self.assertEqual(self.post("/suggest/idea", {"product": bad, "text": "Valid text length here"})[0], 400, bad)
        body = b"{}"
        status, _, _ = self.req("/suggest/idea", method="POST", body=body,
                                headers={"Content-Type": "application/json", "Content-Length": "\xb2"})  # isdigit() but not ASCII
        self.assertEqual(status, 411)

    def test_fix7_token_strictness(self):
        """Fix 7: strict exp, NaN, and one identical 401 body for every failure."""
        iid = suggestions.add_idea("acme", "An idea for token testing", None, "ip1")
        suggestions.screen_pending()
        now = int(time.time())
        def payload_token(raw_json):
            import base64, hashlib, hmac
            b = suggest_serve.b64url_encode(raw_json.encode())
            sig = hmac.new(b"test-key-for-acme", b.encode(), hashlib.sha256).digest()
            return b + "." + suggest_serve.b64url_encode(sig)
        store.write_text("secrets/suggest-vote-key-other", "test-key-for-other" + chr(10))
        tokens = [
            "garbage",
            self.token(exp=now + 4000),                                   # too far ahead
            self.token(exp=now - 5),                                      # expired
            self.token(key="wrong-key"),
            self.token(product="nokey", key="x"),                         # no key for product
            self.token(product="other", key="test-key-for-other"),        # product mismatch with idea
            payload_token('{"product":"acme","uid":"u","exp":true}'),
            payload_token('{"product":"acme","uid":"u","exp":NaN}'),
            payload_token('{"product":"acme","uid":"u","exp":Infinity}'),
        ]
        bodies = set()
        for t in tokens:
            status, body = self.post("/suggest/vote", {"idea_id": iid, "token": t, "on": True})
            self.assertEqual(status, 401, t)
            bodies.add(json.dumps(body, sort_keys=True))
        self.assertEqual(bodies, {'{"error": "invalid token"}'})
        self.assertEqual(self.post("/suggest/vote", {"idea_id": iid, "token": self.token(exp=now + 3600), "on": True})[0], 200)
        # item 8: a finite decimal exp is fine
        self.assertEqual(self.post("/suggest/vote", {"idea_id": iid, "token": payload_token('{"product":"acme","uid":"u2","exp":%d.5}' % (now + 3600)), "on": True})[0], 200)

    def test_fix8_get_internal_error_is_json_500(self):
        """Fix 8: a failing store gives {"error": "internal error"} 500 on GET."""
        with mock.patch.object(suggestions, "public_ideas", side_effect=RuntimeError("boom")):
            status, body, _ = self.req("/suggest/ideas.json")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "internal error"})

    def test_fix9_control_characters(self):
        """Fix 9: bidi override, zero-width space and NUL are rejected; newline is fine."""
        for bad in ("Nice idea \u202e with override", "Nice idea \u200b with zero width", "Nice idea \x00 with nul"):
            status, body = self.post("/suggest/idea", {"product": "acme", "text": bad})
            self.assertEqual(status, 400, repr(bad))
            self.assertEqual(body["error"], "text has control characters")
        self.assertEqual(self.post("/suggest/idea", {"product": "acme", "text": "Two lines\nof a clean idea"})[0], 201)

    def test_item1_email_charset(self):
        for bad in ("a@b.com\x1b]0;x\x07", "a\u202e@b.com", "a@b.c", "a b@c.com"):
            status, body = self.post("/suggest/idea", {"product": "acme", "text": "A clean idea about emails", "email": bad})
            self.assertEqual((status, body["error"]), (400, "invalid email"), repr(bad))
        self.assertEqual(self.post("/suggest/idea", {"product": "acme", "text": "A clean idea about emails", "email": " a.b+c@ex-ample.co.uk "})[0], 201)

    def test_item4_text_characters(self):
        ok = ["\u0633\u0644\u0627\u0645\u200c\u0646\u0627\u0645\u0647 is a good idea", "Add \U0001F469\u200d\U0001F4BB emoji support", "a\tb with a tab inside"]
        for i, t in enumerate(ok):
            status, body = self.post("/suggest/idea", {"product": "acme", "text": t}, {"CF-Connecting-IP": f"10.0.0.{i}"})
            self.assertEqual(status, 201, repr(t))
        status, body = self.post("/suggest/idea", {"product": "acme", "text": "Line one of idea\r\nline two\rline three"})
        self.assertEqual(body["text"], "Line one of idea\nline two\nline three")
        self.assertEqual(suggestions.get_idea(body["id"])["text"], body["text"])
        for bad in ("Nice idea \u202e override", "Nice idea \u2066 isolate", "Nice idea \x00 nul", "Nice idea \u200b zws"):
            self.assertEqual(self.post("/suggest/idea", {"product": "acme", "text": bad})[0], 400, repr(bad))

    def test_item5_single_write_public_and_held(self):
        with mock.patch.object(suggestions, "screen_pending", side_effect=AssertionError("must not run")):
            status, body = self.post("/suggest/idea", {"product": "acme", "text": "Add dark mode to the dashboard"})
            self.assertEqual((status, body["public"]), (201, True))
            status, body = self.post("/suggest/idea", {"product": "acme", "text": "Visit example.com for free stuff"})
            self.assertEqual((status, body["public"]), (201, False))
            status, body = self.post("/suggest/idea", {"product": "acme", "text": "add dark mode to the dashboard"})
            self.assertEqual((status, body["public"]), (201, False))
        self.assertEqual(len(self.ideas_json()), 1)

    def test_item6_rate_key_ipv4_mapped(self):
        self.assertNotEqual(suggest_serve.rate_key("::ffff:1.2.3.4"), suggest_serve.rate_key("::ffff:5.6.7.8"))
        self.assertEqual(suggest_serve.rate_key("::ffff:1.2.3.4"), suggest_serve.rate_key("1.2.3.4"))

    def test_item8_float_exp_and_spec(self):
        iid = suggestions.add_idea("acme", "An idea for float exp testing", None, "ip1")
        suggestions.screen_pending()
        t = self.token(exp=time.time() + 3600.5)
        self.assertEqual(self.post("/suggest/vote", {"idea_id": iid, "token": t, "on": True})[0], 200)

    def test_fix11_ipv6_slash64_shares_limit(self):
        """Fix 11: two addresses in one /64 share the 5/hour limit; IPv4 stays per address."""
        for i in range(5):
            ip = f"2001:db8:1:2::{i + 1:x}"
            self.assertEqual(self.post("/suggest/idea", {"product": "acme", "text": f"IPv6 idea number {i:03d} text"},
                                       {"CF-Connecting-IP": ip})[0], 201)
        status, _ = self.post("/suggest/idea", {"product": "acme", "text": "IPv6 idea number 999 text"},
                              {"CF-Connecting-IP": "2001:db8:1:2:ffff::9"})
        self.assertEqual(status, 429)
        self.assertEqual(self.post("/suggest/idea", {"product": "acme", "text": "IPv6 idea elsewhere ok"},
                                   {"CF-Connecting-IP": "2001:db8:1:3::1"})[0], 201)

    def test_fix13_cli_json_and_missing_id(self):
        """Fix 13: cli.main --json list prints JSON; a missing id exits 1 with one stderr line."""
        import contextlib, io
        suggestions.add_idea("acme", "Idea listed through the CLI", None, "ip1")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--json", "suggestions", "list"]), 0)
        items = json.loads(out.getvalue())
        self.assertEqual(items[0]["text"], "Idea listed through the CLI")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["suggestions", "hide", "999999999999999999999"]), 1)
        self.assertEqual(err.getvalue().strip(), "factory: error: suggestion #999999999999999999999 not found")

    def test_fix3_cli_pass_unhide_markers(self):
        """Fix 3: pass/unhide commands and [pending]/[held] markers."""
        a = suggestions.add_idea("acme", "Pending idea for marker check", None, "ip1")
        b = suggestions.add_idea("acme", "Held idea see www.example.com", None, "ip1")
        suggestions.screen_pending()
        a2 = suggestions.add_idea("acme", "Fresh pending idea for markers", None, "ip1")
        _, text = cli.suggestions_list(cli.parser().parse_args(["suggestions", "list"]))
        self.assertIn("[pending]", text)
        self.assertNotIn("[held]", text)  # held ones are only in --held
        _, text = cli.suggestions_list(cli.parser().parse_args(["suggestions", "list", "--held"]))
        self.assertIn("[held]", text)
        p = cli.parser()
        cli.suggestions_pass(p.parse_args(["suggestions", "pass", str(b)]))
        cli.suggestions_pass(p.parse_args(["suggestions", "pass", str(a2)]))
        self.assertEqual(suggestions.get_idea(b)["screened"], 1)
        self.assertEqual(suggestions.get_idea(a2)["screened"], 1)
        suggestions.hide(a)
        cli.suggestions_unhide(p.parse_args(["suggestions", "unhide", str(a)]))
        self.assertEqual(suggestions.get_idea(a)["hidden"], 0)
        self.assertNotIn("hide a suggestion from public feed and lists", cli.parser().format_help())

    def test_fix10_db_mode(self):
        """Fix 10: suggestions.db is 0600."""
        suggestions.add_idea("acme", "Idea creating the database file", None, "ip1")
        self.assertEqual(os.stat(store.path("suggestions.db")).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
