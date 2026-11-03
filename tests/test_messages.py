import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from threading import Thread

from factory import relay, serve, store


def wait_for_delivery(timeout=2.0):
    for t in list(threading.enumerate()):
        if t.name == "factory-relay" and t.is_alive():
            t.join(timeout=timeout)


class TestWebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silent

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.received_posts.append({
            "path": self.path,
            "headers": dict(self.headers),
            "json": json.loads(body.decode("utf-8")),
        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')


class MessagesApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.webhook_server = HTTPServer(("127.0.0.1", 0), TestWebhookHandler)
        cls.webhook_server.received_posts = []
        cls.webhook_thread = Thread(target=cls.webhook_server.serve_forever, daemon=True)
        cls.webhook_thread.start()
        port = cls.webhook_server.server_address[1]
        cls.webhook_url = f"http://127.0.0.1:{port}/t3-webhook/secret-token-messages-xyz"

    @classmethod
    def tearDownClass(cls):
        cls.webhook_server.shutdown()
        cls.webhook_server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.webhook_server.received_posts.clear()

        self.t3_connect_patcher = mock.patch("factory.t3.connect")
        self.t3_connect_patcher.start()
        self.addCleanup(self.t3_connect_patcher.stop)

        self.t3_agents_patcher = mock.patch("factory.t3.agents", return_value=([
            {"hc": "1A01", "thread": "live-thread-1", "canReceive": True},
            {"hc": "2B14", "thread": "live-thread-2", "canReceive": True},
            {"hc": "3C99", "thread": "thread:provider:claudeAgent:subagent-1", "canReceive": False, "parent": "1A01"},
            {"hc": "4X99", "thread": None, "canReceive": False},
        ], {}))
        self.t3_agents_patcher.start()
        self.addCleanup(self.t3_agents_patcher.stop)

        self.key = store.key()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        wait_for_delivery(timeout=2.0)
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()
        del os.environ["FACTORY_HOME"]

    def req(self, path, body=None, headers=None, raw=None, method=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Sec-Fetch-Site": "same-origin"}
        if data is not None:
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_post_messages_validation(self):
        k = "?k=" + self.key

        # Unknown thread -> 400
        code, out = self.req("/api/messages" + k, {"thread": "unknown-thread-999", "text": "hello"})
        self.assertEqual(code, 400)
        self.assertIn("unknown or non-T3 thread", json.loads(out)["error"])

        # Agent with no thread (canReceive: false) -> 400
        code, out = self.req("/api/messages" + k, {"thread": "4X99", "text": "hello"})
        self.assertEqual(code, 400)

        # Empty thread -> 400
        code, out = self.req("/api/messages" + k, {"thread": "", "text": "hello"})
        self.assertEqual(code, 400)

        # Role-3 subagent thread -> 400 "subagents cannot receive messages; message the parent"
        code, out = self.req("/api/messages" + k, {"thread": "thread:provider:claudeAgent:subagent-1", "text": "hello"})
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(out)["error"], "subagents cannot receive messages; message the parent")

        # Missing thread -> 400
        code, out = self.req("/api/messages" + k, {"text": "hello"})
        self.assertEqual(code, 400)

        # Too long text (> 2000 chars) -> 400
        code, out = self.req("/api/messages" + k, {"thread": "live-thread-1", "text": "x" * 2001})
        self.assertEqual(code, 400)
        self.assertIn("text must be 1..2000 characters", json.loads(out)["error"])

        # Empty text -> 400
        code, out = self.req("/api/messages" + k, {"thread": "live-thread-1", "text": ""})
        self.assertEqual(code, 400)

        # Whitespace-only text -> 400
        code, out = self.req("/api/messages" + k, {"thread": "live-thread-1", "text": "   \n\t  "})
        self.assertEqual(code, 400)

        # Non-JSON body -> 400
        code, out = self.req("/api/messages" + k, raw=b"not valid json")
        self.assertEqual(code, 400)

        # Cross-site POST -> 403
        code, out = self.req("/api/messages" + k, {"thread": "live-thread-1", "text": "hello"},
                             headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(code, 403)
        self.assertIn("cross-site request refused", json.loads(out)["error"])

        # Non-application/json Content-Type -> 415
        code, out = self.req("/api/messages" + k, raw=b'{"thread":"live-thread-1","text":"hi"}',
                             headers={"Content-Type": "text/plain"})
        self.assertEqual(code, 415)
        self.assertIn("send application/json", json.loads(out)["error"])

        # Valid message -> 200, outbox entry returned
        code, out = self.req("/api/messages" + k, {"thread": "live-thread-1", "text": "hello agent"})
        self.assertEqual(code, 200)
        entry = json.loads(out)
        self.assertEqual(entry["thread"], "live-thread-1")
        self.assertEqual(entry["raw"], "hello agent")
        self.assertEqual(entry["text"], 'Message from the owner via the factory dashboard: "hello agent"')
        self.assertEqual(entry["kind"], "message")
        self.assertEqual(entry["state"], "queued")
        self.assertTrue(entry["id"].startswith("m-"))

    def test_enqueue_and_delivery_webhook(self):
        relay.set_webhook(self.webhook_url)
        k = "?k=" + self.key

        code, out = self.req("/api/messages" + k, {"thread": "live-thread-1", "text": "Priority: review PR 10"})
        self.assertEqual(code, 200)
        entry = json.loads(out)
        self.assertEqual(entry["raw"], "Priority: review PR 10")
        expected_text = 'Message from the owner via the factory dashboard: "Priority: review PR 10"'
        self.assertEqual(entry["text"], expected_text)

        # Wait for delivery thread
        wait_for_delivery(timeout=2.0)

        # Verify webhook received
        self.assertEqual(len(self.webhook_server.received_posts), 1)
        post = self.webhook_server.received_posts[0]["json"]
        self.assertEqual(post["id"], entry["id"])
        self.assertEqual(post["thread"], "live-thread-1")
        self.assertEqual(post["text"], expected_text)

        # Check outbox on disk
        outbox = relay.load_outbox()
        matched = [x for x in outbox if x["id"] == entry["id"]]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["state"], "sent")

    def test_history_endpoint(self):
        k = "?k=" + self.key

        # Missing thread parameter -> 400
        code, out = self.req("/api/messages" + k)
        self.assertEqual(code, 400)
        self.assertIn("thread parameter required", json.loads(out)["error"])

        # Add message to live-thread-1
        m1 = relay.enqueue_message("live-thread-1", "First message to thread 1")

        # Add sent answer to live-thread-1
        relay.enqueue(
            {"id": "d10", "title": "Deploy plan", "thread": "live-thread-1", "options": [["a", "Deploy"], ["b", "Wait"]]},
            "a", "ready for prod", "2026-10-29T10:05:00Z"
        )
        outbox = relay.load_outbox()
        # Mark answer as sent
        for x in outbox:
            if x["id"].startswith("d10"):
                x["state"] = "sent"
                x["sentAt"] = "2026-10-29T10:05:30Z"
        store.write("relay.json", outbox)

        # Add queued (not yet delivered) answer to live-thread-1 - should NOT appear in history
        relay.enqueue(
            {"id": "d11", "title": "Pending check", "thread": "live-thread-1", "options": [["a", "Yes"]]},
            "a", "note", "2026-10-29T10:10:00Z"
        )

        # Add message to live-thread-2 - should NOT appear in live-thread-1's history
        relay.enqueue_message("live-thread-2", "Message to thread 2")

        # Query history for live-thread-1
        code, out = self.req("/api/messages" + k + "&thread=live-thread-1")
        self.assertEqual(code, 200)
        hist = json.loads(out)
        self.assertEqual(len(hist), 2)  # d10 answer (10:05 UTC) then m1 (now)
        self.assertEqual(hist[0]["kind"], "answer")
        self.assertEqual(hist[0]["itemId"], "d10")
        self.assertEqual(hist[0]["state"], "sent")
        self.assertEqual(hist[1]["id"], m1["id"])
        self.assertEqual(hist[1]["kind"], "message")

        # Query history for live-thread-2
        code, out = self.req("/api/messages" + k + "&thread=live-thread-2")
        self.assertEqual(code, 200)
        hist2 = json.loads(out)
        self.assertEqual(len(hist2), 1)
        self.assertEqual(hist2[0]["raw"], "Message to thread 2")
        self.assertEqual(hist2[0]["text"], 'Message from the owner via the factory dashboard: "Message to thread 2"')

    def test_retry_creates_no_new_outbox_entry(self):
        k = "?k=" + self.key

        # Send an initial message
        code, post_out = self.req("/api/messages" + k, {"thread": "live-thread-1", "text": "retry me"})
        self.assertEqual(code, 200)
        entry = json.loads(post_out)
        msg_id = entry["id"]

        # Simulate failure in outbox
        outbox = relay.load_outbox()
        for x in outbox:
            if x["id"] == msg_id:
                x["lastError"] = "Connection refused"
        store.write("relay.json", outbox)

        initial_count = len(relay.load_outbox())

        # Post retry
        with mock.patch("factory.relay.trigger_delivery_async") as mock_trigger:
            code, retry_out = self.req("/api/messages" + k, {"retry": msg_id})
            self.assertEqual(code, 200)
            mock_trigger.assert_called_once()

        # Verify no new outbox entry was created
        current_outbox = relay.load_outbox()
        self.assertEqual(len(current_outbox), initial_count)
        self.assertEqual(current_outbox[0]["id"], msg_id)

        # Retry on non-existent message returns 400
        code, bad_out = self.req("/api/messages" + k, {"retry": "m-nonexistent"})
        self.assertEqual(code, 400)

        # Retry on already sent message returns 400
        for x in current_outbox:
            if x["id"] == msg_id:
                x["state"] = "sent"
        store.write("relay.json", current_outbox)
        code, sent_out = self.req("/api/messages" + k, {"retry": msg_id})
        self.assertEqual(code, 400)

    def test_webhook_url_never_leaked_in_responses(self):
        secret_url = f"http://127.0.0.1:8888/very-confidential-path-token-9999"
        relay.set_webhook(secret_url)
        k = "?k=" + self.key

        # Send a message
        code, post_out = self.req("/api/messages" + k, {"thread": "live-thread-1", "text": "secret test"})
        self.assertEqual(code, 200)
        self.assertNotIn("very-confidential", post_out.decode("utf-8"))
        self.assertNotIn("token-9999", post_out.decode("utf-8"))

        # Query history
        code, get_out = self.req("/api/messages" + k + "&thread=live-thread-1")
        self.assertEqual(code, 200)
        self.assertNotIn("very-confidential", get_out.decode("utf-8"))
        self.assertNotIn("token-9999", get_out.decode("utf-8"))

    def test_finding1_inflight_guard_and_repeat_prevention(self):
        """Review 1: Ctrl+Enter key repeat ignored and in-flight guard prevents multiple submissions."""
        template_path = os.path.join(os.path.dirname(__file__), "..", "template", "factory.html")
        with open(template_path, "r", encoding="utf-8") as f:
            tmpl = f.read()
        self.assertIn("if(e.repeat)return;", tmpl)
        self.assertIn("if(mf.dataset.busy)return;", tmpl)
        self.assertIn('mf.dataset.busy="1";', tmpl)
        self.assertIn("delete f.dataset.busy", tmpl)

    def test_finding2_unsent_draft_pauses_autoreload_and_preserves_textarea(self):
        """Review 2: Unsent message draft pauses auto-reload; paintTray updates only .msg-thread."""
        template_path = os.path.join(os.path.dirname(__file__), "..", "template", "factory.html")
        with open(template_path, "r", encoding="utf-8") as f:
            tmpl = f.read()
        self.assertIn("hasMsgDraft", tmpl)
        self.assertIn("const draft=hasMsgDraft()", tmpl)
        self.assertIn("Auto-refresh paused: unsent draft", tmpl)
        # paintTray must target .msg-thread and NOT overwrite the whole panel or textarea
        self.assertIn("document.querySelector(`#agent-tray-${a.hc} .msg-thread`)", tmpl)
        self.assertIn("th.innerHTML=renderThread(a)", tmpl)

    def test_finding3_french_message_quoted_and_sender_format(self):
        """Review 3: Agent receives formatted sender block; raw text kept for history."""
        relay.set_webhook(self.webhook_url)
        k = "?k=" + self.key

        french_msg = 'Bonjour "l\'agent",\ncomment vas-tu ?'
        code, out = self.req("/api/messages" + k, {"thread": "live-thread-1", "text": french_msg})
        self.assertEqual(code, 200)
        entry = json.loads(out)
        self.assertEqual(entry["raw"], french_msg)
        expected_text = f'Message from the owner via the factory dashboard: {json.dumps(french_msg, ensure_ascii=False)}'
        self.assertEqual(entry["text"], expected_text)

        wait_for_delivery(timeout=2.0)
        self.assertEqual(len(self.webhook_server.received_posts), 1)
        post = self.webhook_server.received_posts[0]["json"]
        self.assertEqual(post["text"], expected_text)

        # In history endpoint, raw text is available
        code, hist_out = self.req("/api/messages" + k + "&thread=live-thread-1")
        self.assertEqual(code, 200)
        hist = json.loads(hist_out)
        self.assertEqual(hist[0]["raw"], french_msg)

    def test_finding4_messages_expire_after_24h_and_cap_tries(self):
        """Review 4: Messages older than 24h are marked stale; tries growth is capped."""
        old_time = store.iso(datetime.now(timezone.utc) - timedelta(hours=25))
        entry = {
            "id": "m-old-123",
            "thread": "live-thread-1",
            "text": "old message",
            "raw": "old message",
            "kind": "message",
            "state": "queued",
            "tries": 9,
            "at": old_time,
        }
        store.write("relay.json", [entry])

        pruned = relay.prune_outbox(relay.load_outbox())
        self.assertEqual(len(pruned), 1)
        self.assertEqual(pruned[0]["state"], "stale")
        self.assertIn("expired", pruned[0].get("lastError", ""))

        # Verify tries growth cap in delivery failure
        fail_entry = {
            "id": "m-fail-123",
            "thread": "live-thread-1",
            "text": "failing message",
            "raw": "failing message",
            "kind": "message",
            "state": "queued",
            "tries": 10,
            "at": store.now_iso(),
        }
        store.write("relay.json", [fail_entry])
        relay.set_webhook("http://127.0.0.1:1/nonexistent-endpoint")
        relay.send_queued()
        after = relay.load_outbox()
        self.assertEqual(after[0]["tries"], 11)  # the real count, never frozen

    def test_unreadable_t3_keeps_message_queued(self):
        """T3 unreadable at delivery time is not "thread closed": the message stays queued."""
        store.write("relay.json", [{"id": "m-t3down", "thread": "live-thread-1", "text": "t", "raw": "t",
                                    "kind": "message", "state": "queued", "tries": 0, "at": store.now_iso()}])
        relay.set_webhook("http://127.0.0.1:1/nonexistent-endpoint")
        with mock.patch("factory.t3.connect", side_effect=Exception("database is locked")):
            self.assertIsNone(relay.is_thread_live("live-thread-1"))
            relay.send_queued()
        after = relay.load_outbox()
        self.assertEqual((after[0]["state"], after[0]["lastError"]), ("queued", "T3 database unavailable"))

    def test_finding5_thread_liveness_recheck_before_delivery(self):
        """Review 5: Re-check thread liveness right before posting; marks stale if no longer live."""
        relay.set_webhook(self.webhook_url)
        k = "?k=" + self.key

        # Enqueue while thread is live without triggering delivery yet
        with mock.patch("factory.relay.trigger_delivery_async"):
            code, out = self.req("/api/messages" + k, {"thread": "live-thread-1", "text": "will become dead"})
            self.assertEqual(code, 200)

        # Now simulate thread dying before delivery executes
        with mock.patch("factory.relay.is_thread_live", return_value=False):
            res = relay.send_queued()
            self.assertEqual(res["sent"], 0)

        outbox = relay.load_outbox()
        self.assertEqual(outbox[0]["state"], "stale")
        self.assertEqual(outbox[0].get("lastError"), "thread no longer live")

    def test_finding6_history_view_treats_missing_kind_as_answer(self):
        """Review 6: Answers from live #14 code without kind are treated as answers."""
        # Answer with no kind field
        entry = {
            "id": "d1-2026-10-29T10:00:00Z-abcd1234",
            "itemId": "d1",
            "thread": "live-thread-1",
            "text": "The owner answered d1: option a",
            "state": "sent",
            "at": "2026-10-29T10:00:00Z",
        }
        self.assertNotIn("kind", entry)
        store.write("relay.json", [entry])

        k = "?k=" + self.key
        code, out = self.req("/api/messages" + k + "&thread=live-thread-1")
        self.assertEqual(code, 200)
        hist = json.loads(out)
        self.assertEqual(len(hist), 1)

        # Verify template treats kind !== "message" as answer
        template_path = os.path.join(os.path.dirname(__file__), "..", "template", "factory.html")
        with open(template_path, "r", encoding="utf-8") as f:
            tmpl = f.read()
        self.assertIn('const isAns=m.kind!=="message";', tmpl)

    def test_finding7_show_server_error_text_on_failure(self):
        """Review 7: Show server error text (r.json().error) instead of HTTP 400."""
        template_path = os.path.join(os.path.dirname(__file__), "..", "template", "factory.html")
        with open(template_path, "r", encoding="utf-8") as f:
            tmpl = f.read()
        self.assertIn('r.json().catch(()=>({})).then(b=>Promise.reject(new Error(b.error||("HTTP "+r.status))))', tmpl)

    def test_finding8_serve_closes_t3_connection(self):
        """Review 8: serve.live_agent_threads closes T3 connection with closing(t3.connect())."""
        with mock.patch("factory.serve.t3.connect") as mock_conn:
            with mock.patch("factory.serve.t3.agents", return_value=([], {})):
                serve.live_agent_threads()
                mock_conn.return_value.close.assert_called_once()

    def test_finding9_hide_shortcut_hint_at_720px(self):
        """Review 9: Hide the whole 'Ctrl ↵ to send' hint at <=720px."""
        template_path = os.path.join(os.path.dirname(__file__), "..", "template", "factory.html")
        with open(template_path, "r", encoding="utf-8") as f:
            tmpl = f.read()
        self.assertIn(".msg-hint-send{display:none}", tmpl)
        self.assertIn('<span class="msg-hint-send"><span class="kbd">', tmpl)

    def test_delivery_badges_in_template(self):
        """History shows Sent (T3 accepted) -> Delivered (seen in the session) -> Failed with Retry."""
        template_path = os.path.join(os.path.dirname(__file__), "..", "template", "factory.html")
        with open(template_path, "r", encoding="utf-8") as f:
            tmpl = f.read()
        self.assertIn(".msg-badge.delivered", tmpl)
        self.assertIn(".msg-badge.sent", tmpl)
        self.assertIn('isDelivered?`<span class="msg-badge delivered" title="Seen in the session">', tmpl)
        self.assertIn('isSent?`<span class="msg-badge sent" title="T3 accepted">', tmpl)
        self.assertIn('isFailed?`<span class="msg-badge failed">', tmpl)
        self.assertIn('data-retry-msg="${esc(m.id)}"', tmpl)


if __name__ == "__main__":
    unittest.main()
