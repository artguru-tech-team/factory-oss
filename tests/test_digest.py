import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from unittest import mock

from factory import cli, digest, secrets, store
from factory.store import FactoryError

NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--json", *argv])
    return code, (json.loads(out.getvalue()) if code == 0 and out.getvalue().strip() else None), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)

    def set_secret(self, name, value):
        with mock.patch("sys.stdin", io.StringIO(value + "\n")):
            secrets.set_secret(name)

    def set_config(self, **overrides):
        """Write a valid digest.json with optional overrides."""
        cfg = {
            "enabled": True,
            "project": "support-digest",
            "chatwoot": {"base_url": "https://chat.example.com", "account_id": 1, "inboxes": [1, 3]},
            "llm": {"base_url": "https://gw.example.com/v1", "model": "gpt-4o-mini", "usd_per_mtok": 1.0},
            "question": "Top 3 customer asks this week",
            "days": 7,
            "max_conversations": 300,
            "max_chars_per_conversation": 1200,
            "daily_cap_usd": 1.0,
            "history": [],
        }
        cfg.update(overrides)
        store.write("digest.json", cfg)
        return cfg

    def set_project(self, pid="support-digest"):
        store.write("products.json", {"acme": {"name": "Acme", "repos": ["o/r"]}})
        os.makedirs(store.path("products/acme/projects"), exist_ok=True)
        store.write(f"products/acme/projects/{pid}.json", {
            "id": pid, "product": "acme", "kind": "run",
            "name": "Support digest", "feat": "", "prio": 10, "next": [],
            "prs": [], "match": [], "createdAt": store.now_iso(),
        })


# ---------- scrubbing tests ----------


class ScrubTest(unittest.TestCase):
    def test_email(self):
        self.assertEqual(digest.scrub("contact john@example.com for help"),
                         "contact [email] for help")

    def test_phone_international(self):
        self.assertEqual(digest.scrub("call +216 55 123 456"),
                         "call [phone]")

    def test_phone_with_dashes(self):
        self.assertEqual(digest.scrub("call 55-123-456-78"),
                         "call [phone]")

    def test_phone_8_digits(self):
        self.assertEqual(digest.scrub("call 12345678 now"),
                         "call [phone] now")

    def test_phone_with_dots(self):
        self.assertEqual(digest.scrub("call 55.111.222 now"),
                         "call [phone] now")

    def test_long_digits_order_id(self):
        self.assertEqual(digest.scrub("order 123456 is missing"),
                         "order [number] is missing")

    def test_url_with_query(self):
        self.assertEqual(digest.scrub("see https://shop.example.com/page?id=42&ref=x for details"),
                         "see [url] for details")

    def test_amount_tnd(self):
        self.assertEqual(digest.scrub("paid 150.5 TND"), "paid [amount]")

    def test_amount_euro_symbol(self):
        self.assertEqual(digest.scrub("costs 99€"), "costs [amount]")

    def test_amount_dollar(self):
        self.assertEqual(digest.scrub("paid 20$"), "paid [amount]")

    def test_amount_mad(self):
        self.assertEqual(digest.scrub("it was 3000 MAD"), "it was [amount]")

    def test_amount_dt(self):
        self.assertEqual(digest.scrub("only 50DT left"), "only [amount] left")

    def test_amount_currency_before_number(self):
        self.assertEqual(digest.scrub("costs €50 now"), "costs [amount] now")
        self.assertEqual(digest.scrub("paid $20 yesterday"), "paid [amount] yesterday")
        self.assertEqual(digest.scrub("balance TND 50 total"), "balance [amount] total")

    def test_amount_nospace_dt(self):
        self.assertEqual(digest.scrub("price is 50dt"), "price is [amount]")

    def test_amount_thousands_with_spaces(self):
        self.assertEqual(digest.scrub("total was 1 500 TND paid"), "total was [amount] paid")

    def test_combined(self):
        text = "Hi, I'm john@acme.com, order 1234567, paid 50 EUR, call +216 55 111 222"
        result = digest.scrub(text)
        self.assertNotIn("john@acme.com", result)
        self.assertNotIn("1234567", result)
        self.assertNotIn("+216", result)
        self.assertIn("[email]", result)
        self.assertIn("[amount]", result)
        self.assertIn("[phone]", result)

    def test_empty_and_none(self):
        self.assertEqual(digest.scrub(""), "")
        self.assertEqual(digest.scrub(None), "")


# ---------- config tests ----------


class ConfigTest(Base):
    def test_shape_check_refuses_corrupt_config(self):
        store.write("digest.json", {"enabled": "yes"})
        with self.assertRaises(FactoryError):
            digest.digest()
        store.write("digest.json", {"chatwoot": {"inboxes": "all"}})
        with self.assertRaises(FactoryError):
            digest.digest()

    def test_default_config_valid(self):
        cfg = digest.digest()
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["days"], 7)
        self.assertEqual(cfg["project"], "")
        self.assertIsNone(cfg["llm"]["usd_per_mtok"])

    def test_save_and_reload(self):
        cfg = self.set_config(days=14)
        reloaded = digest.digest()
        self.assertEqual(reloaded["days"], 14)


# ---------- Chatwoot reading tests ----------


def _fake_chatwoot_conversations(pages, inbox_filter=None):
    """Build fake Chatwoot conversation pages for testing."""
    def handler(method, url, headers=None, body=None, source="source", **kw):
        if "conversations/" in url and "/messages" in url:
            conv_id = int(url.split("/conversations/")[1].split("/messages")[0])
            msgs = [
                {"message_type": 0, "private": False, "content": f"Customer message for conv {conv_id}"},
                {"message_type": 1, "private": False, "content": "Agent reply"},
                {"message_type": 0, "private": True, "content": "Private note from customer"},
            ]
            return 200, {}, {"payload": msgs}
        elif "/conversations" in url and "page=" in url:
            page_num = 1
            for param in url.split("?")[1].split("&"):
                if param.startswith("page="):
                    page_num = int(param.split("=")[1])
            if page_num <= len(pages):
                return 200, {}, {"data": {"payload": pages[page_num - 1]}}
            return 200, {}, {"data": {"payload": []}}
        return 200, {}, {}
    return handler


class ChatwootReadTest(Base):
    def test_paging_stops_at_days(self):
        """Conversations older than `days` are not read."""
        self.set_config(days=7)
        self.set_secret("chatwoot-reader", "tok123")
        old_ts = (NOW - timedelta(days=10)).isoformat()
        recent_ts = (NOW - timedelta(days=1)).isoformat()
        pages = [[
            {"id": 1, "last_activity_at": recent_ts},
            {"id": 2, "last_activity_at": old_ts},
        ]]
        with mock.patch.object(digest, "_json", side_effect=_fake_chatwoot_conversations(pages)):
            convs = digest.read_conversations(digest.digest(), "tok123", NOW)
        ids = [c["id"] for c in convs]
        self.assertIn(1, ids)
        self.assertNotIn(2, ids)

    def test_only_incoming_non_private_messages_kept(self):
        """Only message_type==0 and non-private messages are kept."""
        self.set_config(days=7)
        self.set_secret("chatwoot-reader", "tok123")
        recent_ts = (NOW - timedelta(days=1)).isoformat()
        pages = [[{"id": 10, "last_activity_at": recent_ts}]]

        def handler(method, url, headers=None, body=None, source="source", **kw):
            if "/messages" in url:
                msgs = [
                    {"message_type": 0, "private": False, "content": "Customer says hello"},
                    {"message_type": 1, "private": False, "content": "Agent reply"},
                    {"message_type": 0, "private": True, "content": "Private customer note"},
                    {"message_type": 2, "private": False, "content": "Activity message"},
                ]
                return 200, {}, {"payload": msgs}
            return 200, {}, {"data": {"payload": pages[0]}}

        with mock.patch.object(digest, "_json", side_effect=handler):
            convs = digest.read_conversations(digest.digest(), "tok123", NOW)
        self.assertEqual(len(convs), 1)
        self.assertIn("Customer says hello", convs[0]["text"])
        self.assertNotIn("Agent reply", convs[0]["text"])
        self.assertNotIn("Private customer note", convs[0]["text"])
        self.assertNotIn("Activity message", convs[0]["text"])

    def test_messages_fetch_error_propagates(self):
        """HTTP error on messages endpoint must propagate and write history error in run."""
        self.set_config()
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        self.set_project()

        recent_ts = (NOW - timedelta(days=1)).isoformat()
        pages = [[{"id": 1, "last_activity_at": recent_ts}]]

        def handler(method, url, headers=None, body=None, source="source", **kw):
            if "/messages" in url:
                raise FactoryError("GET messages -> HTTP 401")
            return 200, {}, {"data": {"payload": pages[0]}}

        with mock.patch.object(digest, "_json", side_effect=handler):
            with self.assertRaises(FactoryError) as cm:
                digest.run(now=NOW)
        self.assertIn("HTTP 401", str(cm.exception))
        cfg = digest.digest()
        self.assertEqual(len(cfg["history"]), 1)
        self.assertIn("HTTP 401", cfg["history"][0]["error"])
        self.assertIsNone(cfg["history"][0]["result"])


# ---------- prompt tests ----------


class PromptTest(unittest.TestCase):
    def test_prompt_contains_no_unscrubbed_email_or_phone(self):
        conversations = [
            {"id": 1, "text": "Contact me at user@example.com or +216 55 111 222"},
        ]
        prompt = digest.build_prompt("Top asks", conversations)
        self.assertNotIn("user@example.com", prompt)
        self.assertNotIn("+216", prompt)
        self.assertIn("[email]", prompt)
        self.assertIn("[phone]", prompt)


# ---------- model call tests ----------


class NoLogTest(Base):
    def test_no_log_sent_in_body(self):
        """The request body must contain 'no-log': true."""
        self.set_config()
        self.set_secret("llm-key", "sk_test_key")
        bodies = []

        def capture(method, url, headers=None, body=None, source="source", **kw):
            bodies.append(body)
            return 200, {}, {
                "choices": [{"message": {"content": '{"asks": []}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

        with mock.patch.object(digest, "_json", side_effect=capture):
            digest.call_model(digest.digest(), "test prompt", "sk_test_key")
        self.assertTrue(bodies[0].get("no-log"))
        self.assertEqual(bodies[0]["metadata"]["job"], "factory-support-digest")


# ---------- model reply validation tests ----------


class ValidateReplyTest(Base):
    def test_valid_reply(self):
        result = {
            "asks": [
                {"title": "Missing orders", "count": 5, "conversation_ids": [1, 2, 3], "summary": "Customers report missing orders"},
                {"title": "Late delivery", "count": 3, "conversation_ids": [4, 5], "summary": "Deliveries arriving late"},
            ]
        }
        conv_ids = list(range(1, 11))
        asks = digest.validate_reply(result, conv_ids)
        self.assertEqual(len(asks), 2)
        self.assertEqual(asks[0]["title"], "Missing orders")

    def test_malformed_reply_rejected(self):
        with self.assertRaises(FactoryError) as cm:
            digest.validate_reply("not a dict", [1, 2])
        self.assertIn("model reply rejected", str(cm.exception))

    def test_missing_asks_rejected(self):
        with self.assertRaises(FactoryError) as cm:
            digest.validate_reply({"data": []}, [1, 2])
        self.assertIn("model reply rejected", str(cm.exception))

    def test_too_many_asks_rejected(self):
        asks = [{"title": f"Ask {i}", "count": 1, "conversation_ids": [1], "summary": "s"} for i in range(4)]
        with self.assertRaises(FactoryError):
            digest.validate_reply({"asks": asks}, [1, 2, 3])

    def test_overlong_title_rejected(self):
        result = {"asks": [{"title": "x" * 81, "count": 1, "conversation_ids": [1], "summary": "s"}]}
        with self.assertRaises(FactoryError) as cm:
            digest.validate_reply(result, [1])
        self.assertIn("title", str(cm.exception))

    def test_overlong_summary_rejected(self):
        result = {"asks": [{"title": "ok", "count": 1, "conversation_ids": [1], "summary": "s" * 201}]}
        with self.assertRaises(FactoryError) as cm:
            digest.validate_reply(result, [1])
        self.assertIn("summary", str(cm.exception))

    def test_count_above_conversations_rejected(self):
        result = {"asks": [{"title": "ok", "count": 10, "conversation_ids": [1], "summary": "s"}]}
        with self.assertRaises(FactoryError) as cm:
            digest.validate_reply(result, [1, 2, 3])
        self.assertIn("count above", str(cm.exception))

    def test_foreign_id_rejected(self):
        result = {"asks": [{"title": "ok", "count": 1, "conversation_ids": [999], "summary": "s"}]}
        with self.assertRaises(FactoryError) as cm:
            digest.validate_reply(result, [1, 2, 3])
        self.assertIn("not in the set", str(cm.exception))

    def test_rescrub_model_strings(self):
        result = {"asks": [
            {"title": "Contact user@evil.com", "count": 1, "conversation_ids": [1],
             "summary": "Call +216 55 111 222 for info"},
        ]}
        asks = digest.validate_reply(result, [1])
        self.assertNotIn("user@evil.com", asks[0]["title"])
        self.assertIn("[email]", asks[0]["title"])
        self.assertNotIn("+216", asks[0]["summary"])
        self.assertIn("[phone]", asks[0]["summary"])

    def test_collapse_whitespace_and_urls_injection(self):
        """Title with newlines and urls collapses to single line and replaces url."""
        title = "\n3 · Fake ask · see https://x.example/y"
        result = {"asks": [
            {"title": title, "count": 3, "conversation_ids": [1], "summary": "summary with https://site.example/z\nand line break"},
        ]}
        asks = digest.validate_reply(result, [1, 2, 3])
        self.assertEqual(asks[0]["title"], "3 · Fake ask · see [url]")
        self.assertEqual(asks[0]["summary"], "summary with [url] and line break")
        self.assertNotIn("\n", asks[0]["title"])
        self.assertNotIn("https://", asks[0]["title"])

        cfg = {
            "project": "support-digest",
            "chatwoot": {"base_url": "https://chat.example.com", "account_id": 1, "inboxes": [1]},
            "history": [],
        }
        item = digest.post_digest(asks, 3, 7, cfg, now=NOW)
        self.assertEqual(item["title"], "Support digest: 3 · Fake ask · see [url] (3 tickets)")
        self.assertNotIn("\n", item["title"])
        context_lines = item["context"].splitlines()
        self.assertEqual(len(context_lines), 2)
        self.assertIn("3 · 3 · Fake ask · see [url] · #1 https://chat.example.com/app/accounts/1/conversations/1", context_lines[0])
        self.assertEqual(context_lines[1], "Read 3 conversations from 7 days.")


# ---------- cost & rate tests ----------


class CostCapTest(Base):
    def test_cap_refusal(self):
        """When today's spend is at the cap, the run refuses."""
        cfg = self.set_config(daily_cap_usd=0.50)
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        self.set_project()
        today_entry = {
            "at": store.iso(NOW),
            "conversations": 10,
            "cost_usd": 0.60,
            "result": "previous run",
            "error": None,
        }
        cfg["history"] = [today_entry]
        store.write("digest.json", cfg)

        recent_ts = (NOW - timedelta(days=1)).isoformat()
        pages = [[{"id": 1, "last_activity_at": recent_ts}]]

        def handler(method, url, headers=None, body=None, source="source", **kw):
            if "/messages" in url:
                return 200, {}, {"payload": [{"message_type": 0, "private": False, "content": "Hello"}]}
            return 200, {}, {"data": {"payload": pages[0]}}

        with mock.patch.object(digest, "_json", side_effect=handler):
            summary, text = digest.run(now=NOW)
        self.assertEqual(summary["result"], "cap reached")
        self.assertIn("cap", text)

    def test_non_json_content_records_real_cost(self):
        """A 200 response with non-JSON content records the real cost in history."""
        self.set_config()
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        self.set_project()

        recent_ts = (NOW - timedelta(days=1)).isoformat()
        pages = [[{"id": 1, "last_activity_at": recent_ts}]]

        def handler(method, url, headers=None, body=None, source="source", **kw):
            if "/chat/completions" in url:
                return 200, {"x-litellm-response-cost": "0.0075"}, {
                    "choices": [{"message": {"content": "not valid json"}}],
                }
            if "/messages" in url:
                return 200, {}, {"payload": [{"message_type": 0, "private": False, "content": "Hello"}]}
            return 200, {}, {"data": {"payload": pages[0]}}

        with mock.patch.object(digest, "_json", side_effect=handler):
            summary, text = digest.run(now=NOW)
        self.assertIn("error", summary)
        self.assertAlmostEqual(summary["cost_usd"], 0.0075)
        cfg = digest.digest()
        self.assertEqual(len(cfg["history"]), 1)
        self.assertAlmostEqual(cfg["history"][0]["cost_usd"], 0.0075)
        self.assertIn("not valid JSON", cfg["history"][0]["error"])

    def test_reply_cut_off_at_max_tokens_says_so(self):
        """finish_reason=length (reasoning used the budget) is reported as a cut-off, not bad JSON."""
        self.set_config()
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        self.set_project()
        recent_ts = (NOW - timedelta(days=1)).isoformat()
        sent = {}

        def handler(method, url, headers=None, body=None, source="source", **kw):
            if "/chat/completions" in url:
                sent.update(body, timeout=kw.get("timeout"))
                return 200, {"x-litellm-response-cost": "0.004"}, {"choices": [{"finish_reason": "length", "message": {"content": '{"asks": ['}}]}
            if "/messages" in url:
                return 200, {}, {"payload": [{"message_type": 0, "private": False, "content": "Hello"}]}
            return 200, {}, {"data": {"payload": [{"id": 1, "last_activity_at": recent_ts}]}}

        with mock.patch.object(digest, "_json", side_effect=handler):
            summary, _ = digest.run(now=NOW)
        self.assertIn("cut off at max_tokens", summary["error"])
        self.assertGreaterEqual(sent["max_tokens"], 8000)
        self.assertEqual(sent["timeout"], 300)
        self.assertAlmostEqual(digest.digest()["history"][-1]["cost_usd"], 0.004)

    def test_unset_usd_per_mtok_refuses_run(self):
        """When llm.usd_per_mtok is unset, run refuses before calling."""
        cfg = self.set_config()
        cfg["llm"]["usd_per_mtok"] = None
        store.write("digest.json", cfg)
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        self.set_project()

        with self.assertRaises(FactoryError) as cm:
            digest.run(now=NOW)
        self.assertIn("set llm.usd_per_mtok", str(cm.exception))


# ---------- disabled run test ----------


class DisabledTest(Base):
    def test_disabled_run(self):
        self.set_config(enabled=False)
        summary, text = digest.run(now=NOW)
        self.assertFalse(summary["enabled"])
        self.assertEqual(text, "digest disabled")


# ---------- dry-run tests ----------


class DryRunTest(Base):
    def test_dry_run_no_model_call_and_posts_nothing(self):
        self.set_config()
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        self.set_project()

        recent_ts = (NOW - timedelta(days=1)).isoformat()
        pages = [[{"id": 1, "last_activity_at": recent_ts}]]

        call_count = {"model": 0, "post": 0}

        def handler(method, url, headers=None, body=None, source="source", **kw):
            if "/chat/completions" in url:
                call_count["model"] += 1
                return 200, {}, {"choices": [{"message": {"content": '{"asks":[]}'}}]}
            if "/messages" in url:
                return 200, {}, {"payload": [{"message_type": 0, "private": False, "content": "Hello"}]}
            return 200, {}, {"data": {"payload": pages[0]}}

        with mock.patch.object(digest, "_json", side_effect=handler):
            summary, text = digest.run(dry=True, now=NOW)

        self.assertTrue(summary["dry"])
        self.assertEqual(call_count["model"], 0)
        self.assertEqual(summary["conversations"], 1)
        self.assertIn("dry-run", text)
        cfg = digest.digest()
        self.assertEqual(len(cfg["history"]), 0)
        self.assertEqual(store.inbox()["items"], [])


# ---------- zero conversations test ----------


class ZeroConversationsTest(Base):
    def test_zero_conversations_posts_nothing(self):
        self.set_config()
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        self.set_project()

        def handler(method, url, headers=None, body=None, source="source", **kw):
            return 200, {}, {"data": {"payload": []}}

        with mock.patch.object(digest, "_json", side_effect=handler):
            summary, text = digest.run(now=NOW)

        self.assertEqual(summary["conversations"], 0)
        self.assertEqual(summary["result"], "no conversations")
        cfg = digest.digest()
        self.assertEqual(len(cfg["history"]), 1)
        self.assertEqual(cfg["history"][0]["result"], "no conversations")
        self.assertEqual(store.inbox()["items"], [])


# ---------- dedupe tests ----------


class DedupeTest(Base):
    def test_day_2_digest_not_dropped(self):
        """Day-2 digest with same top ask posts successfully."""
        self.set_config()
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        self.set_project()

        model_response = {
            "asks": [{"title": "Same ask", "count": 1, "conversation_ids": [1], "summary": "Summary"}],
        }

        def handler(method, url, headers=None, body=None, source="source", **kw):
            if "/chat/completions" in url:
                return 200, {"x-litellm-response-cost": "0.001"}, {
                    "choices": [{"message": {"content": json.dumps(model_response)}}],
                }
            if "/messages" in url:
                return 200, {}, {"payload": [{"message_type": 0, "private": False, "content": "Need help"}]}
            if "/conversations" in url:
                return 200, {}, {"data": {"payload": [{"id": 1, "last_activity_at": store.iso(NOW)}]}}
            return 200, {}, {}

        # Day 1 run
        with mock.patch.object(digest, "_json", side_effect=handler):
            summary1, text1 = digest.run(now=NOW)
        self.assertIn("ok", text1)
        self.assertEqual(len(store.inbox()["items"]), 1)

        # Day 2 run (24 hours later)
        now_day2 = NOW + timedelta(days=1)
        with mock.patch.object(digest, "_json", side_effect=handler):
            summary2, text2 = digest.run(now=now_day2)
        self.assertIn("ok", text2)
        self.assertEqual(len(store.inbox()["items"]), 2)

    def test_duplicate_run_same_day_records_not_posted(self):
        """Second run on same day outputs 'not posted (duplicate today)' and records it in history."""
        self.set_config()
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        self.set_project()

        model_response = {
            "asks": [{"title": "Ask", "count": 1, "conversation_ids": [1], "summary": "Summary"}],
        }

        def handler(method, url, headers=None, body=None, source="source", **kw):
            if "/chat/completions" in url:
                return 200, {"x-litellm-response-cost": "0.001"}, {
                    "choices": [{"message": {"content": json.dumps(model_response)}}],
                }
            if "/messages" in url:
                return 200, {}, {"payload": [{"message_type": 0, "private": False, "content": "Need help"}]}
            if "/conversations" in url:
                return 200, {}, {"data": {"payload": [{"id": 1, "last_activity_at": store.iso(NOW)}]}}
            return 200, {}, {}

        with mock.patch.object(digest, "_json", side_effect=handler):
            summary1, text1 = digest.run(now=NOW)
        self.assertIn("ok", text1)

        # Second run on same day
        with mock.patch.object(digest, "_json", side_effect=handler):
            summary2, text2 = digest.run(now=NOW + timedelta(hours=1))
        self.assertEqual(text2, "not posted (duplicate today)")
        self.assertEqual(summary2["result"], "not posted (duplicate today)")

        cfg = digest.digest()
        self.assertEqual(len(cfg["history"]), 2)
        self.assertEqual(cfg["history"][1]["result"], "not posted (duplicate today)")


# ---------- target project tests ----------


class TargetProjectTest(Base):
    def test_project_unset_fails_run_and_doctor(self):
        self.set_config(project="")
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")

        with self.assertRaises(FactoryError) as cm:
            digest.run(now=NOW)
        self.assertIn("digest project is not configured", str(cm.exception))

        with self.assertRaises(FactoryError) as cm:
            digest.doctor_digest()
        self.assertIn("digest project is not configured", str(cm.exception))

    def test_nonexistent_project_fails_run_and_doctor(self):
        self.set_config(project="bogus-project")
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")

        with self.assertRaises(FactoryError) as cm:
            digest.run(now=NOW)
        self.assertIn("bogus-project", str(cm.exception))

        with self.assertRaises(FactoryError) as cm:
            digest.doctor_digest()
        self.assertIn("bogus-project", str(cm.exception))


# ---------- secrets never in output ----------


class SecretsNeverExposedTest(Base):
    def test_secrets_never_appear_in_output_or_exceptions(self):
        self.set_config()
        token = "super_secret_chatwoot_token_42"
        key = "sk_secret_llm_key_99"
        self.set_secret("chatwoot-reader", token)
        self.set_secret("llm-key", key)
        self.set_project()

        def handler(method, url, headers=None, body=None, source="source", **kw):
            # fake echoes request headers into its error
            raise urllib.error.HTTPError(url, 401, f"Unauthorized: {headers}", headers, None)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("factory.sweep._opener.open", side_effect=handler), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(Exception) as cm:
                digest.run(now=NOW)

        self.assertNotIn(token, str(cm.exception))
        self.assertNotIn(key, str(cm.exception))
        combined = out.getvalue() + err.getvalue()
        self.assertNotIn(token, combined)
        self.assertNotIn(key, combined)


# ---------- history tests ----------


class HistoryTest(Base):
    def test_history_kept_to_30(self):
        cfg = self.set_config()
        for i in range(35):
            entry = {"at": store.iso(NOW), "conversations": i, "cost_usd": 0.0, "result": "ok", "error": None}
            digest._record_history(cfg, NOW, entry)
        reloaded = digest.digest()
        self.assertEqual(len(reloaded["history"]), 30)


# ---------- doctor tests ----------


class DoctorTest(Base):
    def test_doctor_not_configured(self):
        checks = digest.doctor_checks()
        self.assertEqual(checks, [("digest", True, "not configured")])

    def test_doctor_disabled(self):
        self.set_config(enabled=False)
        checks = digest.doctor_checks()
        self.assertEqual(len(checks), 1)
        name, ok, detail = checks[0]
        self.assertTrue(ok)
        self.assertEqual(detail, "disabled; secrets: chatwoot-reader missing, llm-key missing")

    def test_doctor_disabled_with_secrets(self):
        self.set_config(enabled=False)
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        checks = digest.doctor_checks()
        self.assertEqual(checks, [("digest", True, "disabled; secrets: chatwoot-reader ok, llm-key ok")])

    def test_doctor_missing_secrets_when_enabled(self):
        self.set_config(enabled=True)
        self.set_project()
        checks = digest.doctor_checks()
        self.assertEqual(len(checks), 1)
        name, ok, detail = checks[0]
        self.assertFalse(ok)
        self.assertEqual(detail, "enabled; secrets: chatwoot-reader missing, llm-key missing; chatwoot auth missing token")

    def test_doctor_ok_with_auth_check(self):
        self.set_config(enabled=True)
        self.set_project()
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        with mock.patch.object(digest, "_json", return_value=(200, {}, {"id": 1})):
            checks = digest.doctor_checks()
        self.assertEqual(checks, [("digest", True, "enabled; secrets: chatwoot-reader ok, llm-key ok; chatwoot ok")])

    def test_doctor_auth_fail(self):
        self.set_config(enabled=True)
        self.set_project()
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        with mock.patch.object(digest, "_json", return_value=(401, {}, None)):
            checks = digest.doctor_checks()
        name, ok, detail = checks[0]
        self.assertFalse(ok)
        self.assertIn("HTTP 401", detail)


# ---------- CLI tests ----------


class CLITest(Base):
    def test_digest_config_set(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["digest", "config", "set", "enabled", "true"])
        self.assertEqual(code, 0, err.getvalue())
        cfg = digest.digest()
        self.assertTrue(cfg["enabled"])

    def test_digest_config_set_base_url(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["digest", "config", "set", "chatwoot.base_url", "https://chat.example.com"])
        self.assertEqual(code, 0, err.getvalue())
        cfg = digest.digest()
        self.assertEqual(cfg["chatwoot"]["base_url"], "https://chat.example.com")

    def test_digest_config_set_bad_url(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["digest", "config", "set", "chatwoot.base_url", "not-a-url"])
        self.assertEqual(code, 1)

    def test_digest_config_set_project(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["digest", "config", "set", "project", "my-project"])
        self.assertEqual(code, 0, err.getvalue())
        cfg = digest.digest()
        self.assertEqual(cfg["project"], "my-project")

    def test_digest_config_set_usd_per_mtok(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["digest", "config", "set", "llm.usd_per_mtok", "2.5"])
        self.assertEqual(code, 0, err.getvalue())
        cfg = digest.digest()
        self.assertEqual(cfg["llm"]["usd_per_mtok"], 2.5)

    def test_digest_inboxes(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["digest", "inboxes", "1", "3", "5"])
        self.assertEqual(code, 0, err.getvalue())
        cfg = digest.digest()
        self.assertEqual(cfg["chatwoot"]["inboxes"], [1, 3, 5])

    def test_digest_run_disabled(self):
        self.set_config(enabled=False)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["digest", "run"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("disabled", out.getvalue())

    def test_digest_config_set_unknown_key(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["digest", "config", "set", "bogus", "x"])
        self.assertEqual(code, 1)
        self.assertIn("unknown", err.getvalue())


# ---------- full run integration test ----------


class FullRunTest(Base):
    def test_full_run_with_fake_transports(self):
        """End-to-end run with fake Chatwoot and LLM, verifying inbox posting."""
        self.set_config()
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        self.set_project()

        recent_ts = (NOW - timedelta(days=1)).isoformat()
        model_response = {
            "asks": [
                {"title": "Cannot track my order", "count": 3,
                 "conversation_ids": [1, 2, 3], "summary": "Customers want order tracking"},
                {"title": "Refund not received", "count": 2,
                 "conversation_ids": [4, 5], "summary": "Refunds are late"},
            ]
        }

        def handler(method, url, headers=None, body=None, source="source", **kw):
            if "/chat/completions" in url:
                return 200, {"x-litellm-response-cost": "0.003"}, {
                    "choices": [{"message": {"content": json.dumps(model_response)}}],
                    "usage": {"prompt_tokens": 500, "completion_tokens": 100},
                }
            if "/messages" in url:
                return 200, {}, {"payload": [
                    {"message_type": 0, "private": False, "content": "Where is my order 123456?"},
                ]}
            if "/conversations" in url and "page=" in url:
                convs = [{"id": i, "last_activity_at": recent_ts} for i in range(1, 6)]
                return 200, {}, {"data": {"payload": convs}}
            return 200, {}, {}

        with mock.patch.object(digest, "_json", side_effect=handler):
            summary, text = digest.run(now=NOW)

        self.assertEqual(summary["conversations"], 5)
        self.assertAlmostEqual(summary["cost_usd"], 0.003)
        self.assertIn("Cannot track my order", summary["result"])
        self.assertIsNotNone(summary["item"])

        # verify inbox item was created with exact title and from fields
        box = store.read("inbox.json", {"items": []})
        items = box.get("items", [])
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["title"], "Support digest: Cannot track my order (3 tickets)")
        self.assertEqual(item["from"], "support-digest")
        self.assertEqual(item["kind"], "applied")
        self.assertEqual(item["reco"], "seen")
        # verify plain text context format: #<id> <url>
        expected_line = "3 · Cannot track my order · #1 https://chat.example.com/app/accounts/1/conversations/1 #2 https://chat.example.com/app/accounts/1/conversations/2 #3 https://chat.example.com/app/accounts/1/conversations/3"
        self.assertIn(expected_line, item["context"])
        self.assertIn("Read 5 conversations from 7 days.", item["context"])

        # verify history was recorded
        cfg = digest.digest()
        self.assertEqual(len(cfg["history"]), 1)
        self.assertAlmostEqual(cfg["history"][0]["cost_usd"], 0.003)


if __name__ == "__main__":
    unittest.main()


# ---------- review round 2 ----------


class Round2Test(Base):
    def setUp(self):
        super().setUp()
        self.set_config()
        self.set_secret("chatwoot-reader", "tok")
        self.set_secret("llm-key", "sk")
        self.set_project()
        self.model_calls = 0

    def handler(self, reply=None, headers=None, usage=None, choices=None):
        def h(method, url, headers=None, body=None, source="source", _hdrs=headers, **kw):
            if "/chat/completions" in url:
                self.model_calls += 1
                data = {"choices": choices if choices is not None else
                        [{"message": {"content": json.dumps(reply)}}]}
                if usage:
                    data["usage"] = usage
                return 200, _hdrs or {}, data
            if "/messages" in url:
                return 200, {}, {"payload": [{"message_type": 0, "private": False, "content": "Need help"}]}
            if "/conversations" in url:
                return 200, {}, {"data": {"payload": [{"id": 1, "last_activity_at": store.iso(NOW)}]}}
            return 200, {}, {}
        return h

    def test_same_day_rerun_makes_no_model_call(self):
        reply = {"asks": [{"title": "Ask", "count": 1, "conversation_ids": [1], "summary": "S"}]}
        with mock.patch.object(digest, "_json", side_effect=self.handler(reply, {"x-litellm-response-cost": "0.001"})):
            digest.run(now=NOW)
            summary, text = digest.run(now=NOW + timedelta(hours=1))
        self.assertEqual(self.model_calls, 1)
        self.assertEqual(text, "not posted (duplicate today)")
        self.assertEqual(digest.digest()["history"][-1]["cost_usd"], 0.0)

    def test_empty_asks_records_no_asks(self):
        with mock.patch.object(digest, "_json", side_effect=self.handler({"asks": []}, {"x-litellm-response-cost": "0.001"})):
            summary, text = digest.run(now=NOW)
        self.assertEqual(summary["result"], "no asks")
        self.assertEqual(digest.digest()["history"][-1]["result"], "no asks")
        self.assertEqual(store.inbox()["items"], [])

    def test_bad_cost_header_falls_back_to_usage(self):
        reply = {"asks": [{"title": "Ask", "count": 1, "conversation_ids": [1], "summary": "S"}]}
        usage = {"prompt_tokens": 500_000, "completion_tokens": 500_000}
        for bad in ("nan", "inf", "-5"):
            self.setUp()
            with mock.patch.object(digest, "_json", side_effect=self.handler(reply, {"x-litellm-response-cost": bad}, usage)):
                summary, _ = digest.run(now=NOW)
            self.assertAlmostEqual(summary["cost_usd"], 1.0, msg=bad)

    def test_non_dict_choice_keeps_cost(self):
        with mock.patch.object(digest, "_json", side_effect=self.handler(
                headers={"x-litellm-response-cost": "0.002"}, choices=["oops"])):
            summary, _ = digest.run(now=NOW)
        self.assertIn("error", summary)
        self.assertAlmostEqual(digest.digest()["history"][-1]["cost_usd"], 0.002)

    def test_dry_run_chatwoot_error_leaves_no_history(self):
        def fail(method, url, headers=None, body=None, source="source", **kw):
            raise FactoryError("chatwoot: HTTP 401")
        with mock.patch.object(digest, "_json", side_effect=fail):
            with self.assertRaises(FactoryError):
                digest.run(dry=True, now=NOW)
        self.assertEqual(digest.digest()["history"], [])
