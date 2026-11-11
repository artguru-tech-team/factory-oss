"""Tests for multi-provider token telemetry reading, decoding, cost, and history bucketing."""
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from factory import tokens


def encode_varint(val):
    buf = bytearray()
    while val >= 0x80:
        buf.append((val & 0x7F) | 0x80)
        val >>= 7
    buf.append(val & 0x7F)
    return bytes(buf)


def encode_varint_field(fn, val):
    return encode_varint((fn << 3) | 0) + encode_varint(val)


def encode_bytes_field(fn, b):
    return encode_varint((fn << 3) | 2) + encode_varint(len(b)) + b


def craft_antigravity_blob(prompt=1000, cand=200, cached=400, thought=50, sec=1793277000, model=b"gemini-3.8-flash-high"):
    usage = (encode_varint_field(2, prompt) +
             encode_varint_field(3, cand) +
             encode_varint_field(5, cached) +
             encode_varint_field(9, thought))
    f9_4 = encode_varint_field(1, sec)
    f9 = encode_bytes_field(4, f9_4)
    f1 = (encode_bytes_field(4, usage) +
          encode_bytes_field(9, f9) +
          encode_bytes_field(21, model))
    return encode_bytes_field(1, f1)


class AntigravityDecoderTest(unittest.TestCase):
    def test_decode_crafted_blob(self):
        blob = craft_antigravity_blob(prompt=1234, cand=56, cached=789, thought=12, sec=1793277123, model=b"gemini-3.8-flash-high")
        res = tokens.decode_antigravity_blob(blob)
        self.assertIsNotNone(res)
        self.assertEqual(res["model"], "gemini-3.8-flash-high")
        self.assertEqual(res["prompt"], 1234)
        self.assertEqual(res["cand"], 56)
        self.assertEqual(res["cached"], 789)
        self.assertEqual(res["thought"], 12)
        self.assertEqual(res["sec"], 1793277123)

    def test_decode_empty_or_truncated(self):
        self.assertIsNone(tokens.decode_antigravity_blob(b""))
        self.assertIsNone(tokens.decode_antigravity_blob(b"\x00\x01\x02"))


class ModelCostTest(unittest.TestCase):
    def test_known_claude_model(self):
        # Sonnet 5.5: input $2/M, output $10/M, cache_read $0.20/M, cache_write $2.50/M
        cost = tokens.model_cost("claude-sonnet-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(cost, 2.0 + 10.0 + 0.20 + 2.50)

    def test_all_official_claude_prices(self):
        # claude-opus-5-5: 4/20, read 0.20, write 5.0
        c = tokens.model_cost("claude-opus-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(c, 4.0 + 20.0 + 0.20 + 5.0)

        # claude-opus-5: 5/25, read 0.50, write 6.25
        c = tokens.model_cost("claude-opus-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(c, 5.0 + 25.0 + 0.50 + 6.25)

        # claude-sonnet-4-6: 3/15, read 0.30, write 3.75
        c = tokens.model_cost("claude-sonnet-4-6", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(c, 3.0 + 15.0 + 0.30 + 3.75)

        # claude-haiku-5-5: 0.10/0.50, read 0.01, write 0.125
        c = tokens.model_cost("claude-haiku-5-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(c, 0.10 + 0.50 + 0.01 + 0.125)

        # claude-haiku-4-5: 1/5, read 0.10, write 1.25
        c = tokens.model_cost("claude-haiku-4-5", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(c, 1.0 + 5.0 + 0.10 + 1.25)

        # claude-fable-5-1: 10/50, read 0.25, write 12.50
        c = tokens.model_cost("claude-fable-5-1", 1_000_000, 1_000_000, 1_000_000, 1_000_000)
        self.assertAlmostEqual(c, 10.0 + 50.0 + 0.25 + 12.50)

    def test_unpriced_models_return_none(self):
        # Gemini, big-pickle, kimi, gpt-4o, deepseek: no official first-party price source -> None ("—")
        for m in ("gemini-3.8-flash-high", "gemini-3-pro", "big-pickle", "kimi-k2", "gpt-4o", "deepseek-v4.1-flash-free"):
            self.assertIsNone(tokens.model_cost(m, 1_000_000, 500_000), f"Expected None for {m}")

    def test_no_substring_fallback_pricing(self):
        # Empty string, generic prefixes, or random substrings must return None, not match Opus
        self.assertIsNone(tokens.model_cost("", 1000, 1000))
        self.assertIsNone(tokens.model_cost("claude", 1000, 1000))
        self.assertIsNone(tokens.model_cost("opus", 1000, 1000))
        self.assertIsNone(tokens.model_cost("sonnet", 1000, 1000))

    def test_model_alias(self):
        # claude-haiku-4-5-20251001 should resolve via alias map to claude-haiku-4-5
        c = tokens.model_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
        self.assertAlmostEqual(c, 1.0 + 5.0)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(tokens.model_cost("mysterious-custom-llm-v1", 1000, 1000))

    def test_eur_rate_constant(self):
        self.assertEqual(tokens.EUR_USD_RATE, 0.92)


class OpenCodeStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "opencode.db")
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE session_v2 (id TEXT, cost REAL, model TEXT, time_created INTEGER)")
        con.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, time_created INTEGER, data TEXT)")
        con.execute("CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")

        # session_v2 rows: ses_1 (cost 1.25), ses_2 (free)
        con.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?)", ("ses_1", 1.25, "big-pickle", 1793277000 * 1000))
        con.execute("INSERT INTO session_v2 VALUES (?, ?, ?, ?)", ("ses_2", 0.0, "deepseek-v4.1-flash-free", 1793278000 * 1000))

        # session_message: ses_1 assistant message with 1000 in, 200 out, 50 reasoning, 300 cr
        d1 = json.dumps({
            "modelID": "big-pickle",
            "cost": 1.25,
            "tokens": {"input": 1000, "output": 200, "reasoning": 50, "cache": {"read": 300, "write": 0}}
        })
        con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?)", ("m1", "ses_1", "assistant", 1793277000 * 1000, d1))

        # legacy message table:
        # ses_1 legacy copy (should be de-duplicated because ses_1 is in v2!)
        con.execute("INSERT INTO message VALUES (?, ?, ?, ?)", ("m_leg1", "ses_1", 1793277000 * 1000, d1))
        # ses_legacy_only (should be loaded because it's not in v2)
        d_leg = json.dumps({
            "modelID": "claude-sonnet-5-5",
            "tokens": {"input": 500, "output": 100, "reasoning": 0, "cache": {"read": 0, "write": 0}}
        })
        con.execute("INSERT INTO message VALUES (?, ?, ?, ?)", ("m_leg2", "ses_legacy_only", 1793276000 * 1000, d_leg))

        con.commit()
        con.close()

    def test_load_opencode_events_v2_and_dedup_legacy(self):
        with mock.patch.object(tokens, "opencode_db_path", return_value=self.db_path):
            evs = tokens.load_opencode_events(1793270000)
            # Exactly 2 events: ses_1 from session_message, and ses_legacy_only from message (legacy ses_1 is de-duplicated)
            self.assertEqual(len(evs), 2)
            ev1 = next(e for e in evs if e["session_id"] == "ses_1")
            self.assertEqual(ev1["input"], 1000)
            self.assertEqual(ev1["output"], 250)
            self.assertEqual(ev1["cache_read"], 300)
            self.assertEqual(ev1["total"], 1250)
            self.assertEqual(ev1["cost"], 1.25)

            ev_leg = next(e for e in evs if e["session_id"] == "ses_legacy_only")
            self.assertEqual(ev_leg["input"], 500)
            self.assertEqual(ev_leg["total"], 600)

    def test_missing_opencode_db_is_not_an_error_no_warnings(self):
        with mock.patch.object(tokens, "opencode_db_path", return_value="/nonexistent/opencode.db"):
            warnings = []
            evs = tokens.load_opencode_events(1793270000, warnings)
            self.assertEqual(evs, [])
            self.assertEqual(len(warnings), 0)


class AntigravityStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        conv_dir = os.path.join(self.tmp.name, "inst1", "antigravity-acp", "conversations")
        os.makedirs(conv_dir)
        self.db_path = os.path.join(conv_dir, "conv-abc.db")
        con = sqlite3.connect(self.db_path)
        con.execute("CREATE TABLE gen_metadata (idx INTEGER, data BLOB)")
        blob = craft_antigravity_blob(prompt=5000, cand=300, cached=1200, sec=1793277500, model=b"gemini-3.8-flash-high")
        con.execute("INSERT INTO gen_metadata VALUES (?, ?)", (0, blob))
        con.commit()
        con.close()

    def test_load_antigravity_events_and_wal_freshness(self):
        db_mtime = os.path.getmtime(self.db_path)
        wal_mtime = db_mtime + 500
        wal_path = self.db_path + "-wal"
        with open(wal_path, "wb") as f:
            f.write(b"wal data")
        os.utime(wal_path, (wal_mtime, wal_mtime))

        cache_data = {"antigravity": {}}
        with mock.patch.object(tokens, "antigravity_dir", return_value=self.tmp.name):
            evs = tokens.load_antigravity_events(1793270000, cache_data=cache_data)
            self.assertEqual(len(evs), 1)
            ev = evs[0]
            self.assertEqual(ev["session_id"], "conv-abc")
            self.assertEqual(ev["provider"], "Antigravity")
            self.assertEqual(ev["input"], 5000)
            self.assertEqual(ev["output"], 300)
            self.assertEqual(ev["cache_read"], 1200)
            self.assertEqual(ev["total"], 5300)
            # gemini-3.8-flash-high is unpriced, returns None
            self.assertIsNone(ev["cost"])

            # Verify cached entry stored under db_path with max mtime
            self.assertIn(self.db_path, cache_data["antigravity"])
            key = cache_data["antigravity"][self.db_path]["key"]
            self.assertEqual(key[0], wal_mtime)  # WAL mtime wins


class TokenHistoryAndAgentsTest(unittest.TestCase):
    def test_aggregation_and_agent_attribution(self):
        now = datetime.fromtimestamp(1793280000, timezone.utc)
        now_sec = now.timestamp()

        c_events = [
            {"provider": "Claude", "session_id": "c_sess", "time": now_sec - 3600,
             "model": "claude-sonnet-5-5", "input": 10000, "output": 2000, "cache_read": 1000, "cache_write": 500,
             "total": 12500, "cost": 0.04},
        ]
        oc_events = [
            {"provider": "OpenCode", "session_id": "oc_sess", "time": now_sec - 7200,
             "model": "big-pickle", "input": 4000, "output": 1000, "cache_read": 200, "cache_write": 100,
             "total": 5100, "cost": 0.01},
        ]
        ag_events = [
            {"provider": "Antigravity", "session_id": "ag_sess", "time": now_sec - 1800,
             "model": "gemini-3.8-flash-high", "input": 8000, "output": 500, "cache_read": 500, "cache_write": 0,
             "total": 8500, "cost": None},
        ]

        rows = [
            {"hc": "1C01", "who": "Claude Agent", "provider": "Claude", "model": "claude-sonnet-5-5", "thread": "t1"},
            {"hc": "2O02", "who": "OpenCode Agent", "provider": "OpenCode", "model": "big-pickle", "thread": "t2"},
            {"hc": "2A03", "who": "Antigravity Agent", "provider": "Antigravity", "model": "gemini-3.8-flash-high", "thread": "t3"},
            {"hc": "3S05", "who": "Subagent", "provider": "Claude", "model": "claude-opus-5-5", "thread": "t4", "parent": "1C01"},
            {"hc": "4X04", "who": "Extra CLI Agent", "provider": "Claude", "via": "factory CLI"},
        ]
        sessions = {
            "1C01": ["c_sess"],
            "2O02": ["oc_sess"],
            "2A03": ["ag_sess"],
            "3S05": [],  # native subagent with role 3, no nativeThreadRef
        }

        with mock.patch.object(tokens, "load_claude_events", return_value=(c_events, now)), \
             mock.patch.object(tokens, "load_opencode_events", return_value=oc_events), \
             mock.patch.object(tokens, "load_antigravity_events", return_value=ag_events), \
             mock.patch.object(tokens, "load_tokens_cache", return_value={"antigravity": {}, "claude": {}}), \
             mock.patch.object(tokens, "save_tokens_cache"):
            th, at, meta = tokens.token_history_and_agents(rows, sessions, now)

            # Check 24h summary
            h24 = th["24h"]
            self.assertEqual(h24["tot"], 12500 + 5100 + 8500)
            self.assertEqual(h24["claude"], 12500)
            self.assertEqual(h24["opencode"], 5100)
            self.assertEqual(h24["antigravity"], 8500)
            self.assertEqual(len(h24["bars"]), 7)  # 7 slots covering rolling 24h
            self.assertEqual(sum(b["total"] for b in h24["bars"]), h24["tot"])

            # Check cache_write is tracked
            self.assertEqual(h24["cache_write"], 600)  # 500 + 100

            # Check 7d summary (7 daily bars)
            self.assertEqual(len(th["7d"]["bars"]), 7)

            # Check 30d summary (30 daily bars)
            self.assertEqual(len(th["30d"]["bars"]), 30)

            # Check agent rows updated with 24h tokens
            self.assertEqual(rows[0]["tokens"], 12500)
            self.assertEqual(rows[1]["tokens"], 5100)
            self.assertEqual(rows[2]["tokens"], 8500)
            self.assertIsNone(rows[3]["tokens"])  # subagent is unmetered
            self.assertIsNone(rows[4]["tokens"])  # extra CLI agent has unmetered

            # Check subagent counted in parent
            sub_at = next(a for a in at if a["hc"] == "3S05")
            self.assertTrue(sub_at["unmetered"])
            self.assertEqual(sub_at["parent"], "1C01")
            self.assertIn("counted in parent session 1C01", sub_at["why"])

            # Check extra CLI agent
            extra_at = next(a for a in at if a["hc"] == "4X04")
            self.assertTrue(extra_at["unmetered"])
            self.assertIn("factory CLI", extra_at["why"])

    def test_historical_threads_grouped_by_session_in_7d(self):
        now = datetime.fromtimestamp(1793280000, timezone.utc)
        now_sec = now.timestamp()

        # An event from 4 days ago
        hist_time = now_sec - 4 * 86400
        hist_events = [
            {"provider": "Claude", "session_id": "hist_sess_1", "time": hist_time,
             "model": "claude-sonnet-5-5", "input": 50000, "output": 10000, "cache_read": 5000, "cache_write": 2000,
             "total": 62000, "cost": 0.20},
        ]
        rows = []  # no active agents in 24h
        sessions = {}
        thread_map = {
            "hist_sess_1": {
                "thread": "tid_hist",
                "hc": "1H01",
                "who": "Old Worker",
                "provider": "Claude",
                "model": "claude-sonnet-5-5",
                "project": "proj-1",
                "parent": None,
            }
        }

        with mock.patch.object(tokens, "load_claude_events", return_value=(hist_events, now)), \
             mock.patch.object(tokens, "load_opencode_events", return_value=[]), \
             mock.patch.object(tokens, "load_antigravity_events", return_value=[]), \
             mock.patch.object(tokens, "load_tokens_cache", return_value={"antigravity": {}, "claude": {}}), \
             mock.patch.object(tokens, "save_tokens_cache"):
            th, at, meta = tokens.token_history_and_agents(rows, sessions, now, thread_map=thread_map)

            # In 7d, KPI has 62000 tokens
            self.assertEqual(th["7d"]["tot"], 62000)
            # In 7d agents table, 1H01 is present with 62000 tokens!
            agents_7d = th["7d"]["agents"]
            self.assertEqual(len(agents_7d), 1)
            self.assertEqual(agents_7d[0]["hc"], "1H01")
            self.assertEqual(agents_7d[0]["tot"], 62000)
            self.assertEqual(sum(a["tot"] for a in agents_7d), th["7d"]["tot"])

    def test_rolling_24h_window_includes_events_between_20h_and_24h(self):
        now = datetime(2026, 10, 29, 12, 5, tzinfo=timezone.utc)
        now_sec = now.timestamp()
        event_22h_ago = now_sec - 22 * 3600
        c_events = [
            {"provider": "Claude", "session_id": "c_sess", "time": event_22h_ago,
             "model": "claude-sonnet-5-5", "input": 1000, "output": 500, "cache_read": 0, "cache_write": 0,
             "total": 1500, "cost": 0.01},
        ]
        rows = [{"hc": "1C01", "who": "Claude Agent", "provider": "Claude", "model": "claude-sonnet-5-5", "thread": "t1"}]
        sessions = {"1C01": ["c_sess"]}
        with mock.patch.object(tokens, "load_claude_events", return_value=(c_events, now)), \
             mock.patch.object(tokens, "load_opencode_events", return_value=[]), \
             mock.patch.object(tokens, "load_antigravity_events", return_value=[]), \
             mock.patch.object(tokens, "load_tokens_cache", return_value={"version": getattr(tokens, "CACHE_VERSION", 1), "antigravity": {}, "claude": {}}), \
             mock.patch.object(tokens, "save_tokens_cache"):
            th, at, meta = tokens.token_history_and_agents(rows, sessions, now)
            # Must be included in 24h total and row tokens
            self.assertEqual(th["24h"]["tot"], 1500)
            self.assertEqual(rows[0]["tokens"], 1500)

    def test_24h_table_includes_unknown_sessions_and_merges_no_data_agents(self):
        now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
        now_sec = now.timestamp()
        events = [
            {"provider": "Antigravity", "session_id": "0443deec-1234", "time": now_sec - 3600,
             "model": "gemini-3.8-flash-high", "input": 8000, "output": 2000, "cache_read": 0, "cache_write": 0,
             "total": 10000, "cost": None},
        ]
        rows = [
            {"hc": "1A01", "who": "Active Agent", "provider": "Claude", "model": "claude-sonnet-5-5", "thread": "t1"},
            {"hc": "3S02", "who": "Subagent", "provider": "Claude", "model": "claude-opus-5-5", "thread": "t2", "parent": "1A01"},
        ]
        sessions = {"1A01": []}
        with mock.patch.object(tokens, "load_claude_events", return_value=([], now)), \
             mock.patch.object(tokens, "load_opencode_events", return_value=[]), \
             mock.patch.object(tokens, "load_antigravity_events", return_value=events), \
             mock.patch.object(tokens, "load_tokens_cache", return_value={"version": getattr(tokens, "CACHE_VERSION", 1), "antigravity": {}, "claude": {}}), \
             mock.patch.object(tokens, "save_tokens_cache"):
            th, at, meta = tokens.token_history_and_agents(rows, sessions, now)
            agents_24h = th["24h"]["agents"]
            # 24h table must include the unknown session
            unknown_entry = next((a for a in agents_24h if "0443DEEC" in a["hc"]), None)
            self.assertIsNotNone(unknown_entry)
            self.assertEqual(unknown_entry["tot"], 10000)
            # Sum of metered entries in 24h table equals 24h KPI total
            self.assertEqual(sum(a["tot"] for a in agents_24h if not a["unmetered"]), th["24h"]["tot"])
            # T3 active agents with no telemetry must be merged in
            sub_entry = next(a for a in agents_24h if a["hc"] == "3S02")
            self.assertTrue(sub_entry["unmetered"])
            self.assertIn("counted in parent", sub_entry["why"])
            no_data_entry = next(a for a in agents_24h if a["hc"] == "1A01")
            self.assertTrue(no_data_entry["unmetered"])
            self.assertIn("No telemetry", no_data_entry["why"])

    def test_cache_cost_computed_at_read_time_and_version_invalidation(self):
        now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
        cache_data = {
            "version": getattr(tokens, "CACHE_VERSION", 2),
            "claude": {
                "mtime": 123456.0,
                "scanned_at": now.isoformat(),
                "events": [{
                    "provider": "Claude", "session_id": "c1", "time": now.timestamp() - 100,
                    "model": "claude-opus-5-5", "input": 1_000_000, "output": 1_000_000,
                    "cache_read": 0, "cache_write": 0, "total": 2_000_000
                }],
            },
            "antigravity": {},
        }
        with mock.patch.object(tokens, "cache_path", return_value="/tmp/mock-cache.json"), \
             mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.path.getmtime", return_value=123456.0):
            orig_prices = dict(tokens.MODEL_PRICES["claude-opus-5-5"])
            try:
                tokens.MODEL_PRICES["claude-opus-5-5"]["input"] = 0.0
                tokens.MODEL_PRICES["claude-opus-5-5"]["output"] = 0.0
                evs, _ = tokens.load_claude_events(now.timestamp() - 1000, cache_data=cache_data)
                self.assertAlmostEqual(evs[0]["cost"], 0.0)
            finally:
                tokens.MODEL_PRICES["claude-opus-5-5"] = orig_prices

        # Cache format version invalidation
        old_cache = {"version": 999, "claude": {"mtime": 1.0, "events": []}, "antigravity": {}}
        with tempfile.TemporaryDirectory() as tmp:
            cp = os.path.join(tmp, "tokens-cache.json")
            with open(cp, "w") as f:
                json.dump(old_cache, f)
            with mock.patch.object(tokens.store, "path", return_value=cp):
                loaded = tokens.load_tokens_cache()
                self.assertEqual(loaded["claude"], {})

    def test_corrupt_or_wrong_shape_cache_recovers_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            cp = os.path.join(tmp, "tokens-cache.json")
            with mock.patch.object(tokens.store, "path", return_value=cp):
                with open(cp, "w") as f:
                    f.write("")
                loaded = tokens.load_tokens_cache()
                self.assertEqual(loaded["claude"], {})

                with open(cp, "w") as f:
                    f.write("{broken json: ")
                loaded = tokens.load_tokens_cache()
                self.assertEqual(loaded["claude"], {})

                with open(cp, "w") as f:
                    f.write("[]")
                loaded = tokens.load_tokens_cache()
                self.assertEqual(loaded["claude"], {})

    def test_counted_in_parent_applied_only_to_subagent(self):
        now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
        rows = [
            {"hc": "2D01", "who": "Claude Delegate", "provider": "Claude", "model": "claude-sonnet-5-5", "thread": "t1", "parent": "1C01"},
            {"hc": "3S02", "who": "Subagent", "provider": "Claude", "model": "claude-sonnet-5-5", "thread": "t2", "parent": "1C01"},
        ]
        sessions = {"2D01": [], "3S02": []}
        with mock.patch.object(tokens, "load_claude_events", return_value=([], now)), \
             mock.patch.object(tokens, "load_opencode_events", return_value=[]), \
             mock.patch.object(tokens, "load_antigravity_events", return_value=[]), \
             mock.patch.object(tokens, "load_tokens_cache", return_value={"version": getattr(tokens, "CACHE_VERSION", 2), "antigravity": {}, "claude": {}}), \
             mock.patch.object(tokens, "save_tokens_cache"):
            th, at, meta = tokens.token_history_and_agents(rows, sessions, now)
            agents_24h = th["24h"]["agents"]
            delegate = next(a for a in agents_24h if a["hc"] == "2D01")
            subagent = next(a for a in agents_24h if a["hc"] == "3S02")
            self.assertEqual(subagent["parent"], "1C01")
            self.assertIn("counted in parent", subagent["why"])
            self.assertIsNone(delegate["parent"])
            self.assertNotIn("counted in parent", delegate["why"])

    def test_opencode_explicit_zero_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "opencode.db")
            con = sqlite3.connect(db_path)
            con.execute("CREATE TABLE session_message (id TEXT, session_id TEXT, type TEXT, time_created INTEGER, data TEXT)")
            d = json.dumps({
                "modelID": "free-model-v1",
                "cost": 0,
                "tokens": {"input": 100, "output": 50, "reasoning": 0, "cache": {"read": 0, "write": 0}}
            })
            con.execute("INSERT INTO session_message VALUES (?, ?, ?, ?, ?)", ("m1", "s1", "assistant", 1000 * 1000, d))
            con.commit()
            con.close()
            with mock.patch.object(tokens, "opencode_db_path", return_value=db_path):
                evs = tokens.load_opencode_events(0)
                self.assertEqual(len(evs), 1)
                self.assertEqual(evs[0]["cost"], 0.0)

    def test_24h_bars_cover_exact_kpi_window_at_hh_05(self):
        now = datetime(2026, 10, 29, 12, 5, 0, tzinfo=timezone.utc)
        now_sec = now.timestamp()
        cutoff = now_sec - 24 * 3600

        # Events:
        # e_old: 10s before rolling cutoff -> must be excluded
        # e0: 10s after rolling cutoff -> Bar 0 (partial first bar: yesterday 12:05 to 16:00)
        # e1..e5: in each intermediate 4h block
        # e6: 10s before now -> Bar 6 (partial 7th bar: today 12:00 to 12:05)
        e_old = {"provider": "Claude", "session_id": "s_old", "time": cutoff - 10,
                 "model": "claude-sonnet-5-5", "input": 100, "output": 50, "cache_read": 0, "cache_write": 0,
                 "total": 150, "cost": 0.001}
        e0 = {"provider": "Claude", "session_id": "s0", "time": cutoff + 10,
              "model": "claude-sonnet-5-5", "input": 1000, "output": 200, "cache_read": 0, "cache_write": 0,
              "total": 1200, "cost": 0.01}
        e1 = {"provider": "OpenCode", "session_id": "s1", "time": now_sec - 20 * 3600,
              "model": "big-pickle", "input": 500, "output": 100, "cache_read": 0, "cache_write": 0,
              "total": 600, "cost": None}
        e2 = {"provider": "Antigravity", "session_id": "s2", "time": now_sec - 16 * 3600,
              "model": "gemini-3.8-flash-high", "input": 2000, "output": 300, "cache_read": 0, "cache_write": 0,
              "total": 2300, "cost": None}
        e3 = {"provider": "Claude", "session_id": "s3", "time": now_sec - 12 * 3600,
              "model": "claude-sonnet-5-5", "input": 800, "output": 200, "cache_read": 0, "cache_write": 0,
              "total": 1000, "cost": 0.01}
        e4 = {"provider": "OpenCode", "session_id": "s4", "time": now_sec - 8 * 3600,
              "model": "big-pickle", "input": 400, "output": 100, "cache_read": 0, "cache_write": 0,
              "total": 500, "cost": None}
        e5 = {"provider": "Antigravity", "session_id": "s5", "time": now_sec - 4 * 3600,
              "model": "gemini-3.8-flash-high", "input": 1500, "output": 200, "cache_read": 0, "cache_write": 0,
              "total": 1700, "cost": None}
        e6 = {"provider": "Claude", "session_id": "s6", "time": now_sec - 10,
              "model": "claude-sonnet-5-5", "input": 2000, "output": 500, "cache_read": 0, "cache_write": 0,
              "total": 2500, "cost": 0.02}

        all_events = [e_old, e0, e1, e2, e3, e4, e5, e6]
        c_events = [e for e in all_events if e["provider"] == "Claude"]
        oc_events = [e for e in all_events if e["provider"] == "OpenCode"]
        ag_events = [e for e in all_events if e["provider"] == "Antigravity"]

        rows = []
        sessions = {}
        with mock.patch.object(tokens, "load_claude_events", return_value=(c_events, now)), \
             mock.patch.object(tokens, "load_opencode_events", return_value=oc_events), \
             mock.patch.object(tokens, "load_antigravity_events", return_value=ag_events), \
             mock.patch.object(tokens, "load_tokens_cache", return_value={"version": getattr(tokens, "CACHE_VERSION", 2), "antigravity": {}, "claude": {}}), \
             mock.patch.object(tokens, "save_tokens_cache"):
            th, at, meta = tokens.token_history_and_agents(rows, sessions, now)
            h24 = th["24h"]
            expected_tot = e0["total"] + e1["total"] + e2["total"] + e3["total"] + e4["total"] + e5["total"] + e6["total"]
            self.assertEqual(h24["tot"], expected_tot)
            bars = h24["bars"]
            self.assertEqual(len(bars), 7)
            # Bars must sum to the KPI
            self.assertEqual(sum(b["total"] for b in bars), h24["tot"])
            # First bar has e0
            self.assertEqual(bars[0]["total"], e0["total"])
            self.assertEqual(bars[0]["label"], "12:05")
            # Last bar has e6
            self.assertEqual(bars[6]["total"], e6["total"])
            self.assertEqual(bars[6]["label"], "12:00")
            # Providers in bars sum to KPI providers
            for p in ("Claude", "OpenCode", "Antigravity"):
                self.assertEqual(sum(b[p.lower()] for b in bars), h24[p.lower()])

    def test_24h_bars_includes_events_newer_than_now_in_last_bar(self):
        # Events written by running agents during slow build: now is fixed before gh calls,
        # OpenCode/Antigravity read later. Events at -60s and +3s relative to now=12:05.
        # Both must be included in the KPI and in the bars (last bar has no upper bound).
        now = datetime(2026, 10, 29, 12, 5, 0, tzinfo=timezone.utc)
        now_sec = now.timestamp()

        e_prev = {"provider": "OpenCode", "session_id": "s1", "time": now_sec - 60,
                  "model": "big-pickle", "input": 80, "output": 20, "cache_read": 0, "cache_write": 0,
                  "total": 100, "cost": None}
        e_post = {"provider": "Antigravity", "session_id": "s2", "time": now_sec + 3,
                  "model": "gemini-3.8-flash-high", "input": 80, "output": 20, "cache_read": 0, "cache_write": 0,
                  "total": 100, "cost": None}

        with mock.patch.object(tokens, "load_claude_events", return_value=([], now)), \
             mock.patch.object(tokens, "load_opencode_events", return_value=[e_prev]), \
             mock.patch.object(tokens, "load_antigravity_events", return_value=[e_post]), \
             mock.patch.object(tokens, "load_tokens_cache", return_value={"version": getattr(tokens, "CACHE_VERSION", 2), "antigravity": {}, "claude": {}}), \
             mock.patch.object(tokens, "save_tokens_cache"):
            th, _, _ = tokens.token_history_and_agents([], {}, now)
            h24 = th["24h"]
            # KPI is 200
            self.assertEqual(h24["tot"], 200)
            bars = h24["bars"]
            self.assertEqual(len(bars), 7)
            # Bars sum must match KPI (200, not 100)
            self.assertEqual(sum(b["total"] for b in bars), 200)
            # Last bar has both events (>= 12:00 start with no upper bound)
            self.assertEqual(bars[-1]["total"], 200)
            self.assertEqual(bars[-1]["opencode"], 100)
            self.assertEqual(bars[-1]["antigravity"], 100)
            self.assertEqual(bars[-1]["label"], "12:00")
            # First bar has real cutoff label
            self.assertEqual(bars[0]["label"], "12:05")

    def test_24h_bars_cover_exact_kpi_window_at_hh_55(self):
        # Test at 12:55 UTC
        now_1255 = datetime(2026, 10, 29, 12, 55, 0, tzinfo=timezone.utc)
        now_sec = now_1255.timestamp()
        cutoff = now_sec - 24 * 3600

        e_old = {"provider": "Claude", "session_id": "s_old", "time": cutoff - 60,
                 "model": "claude-sonnet-5-5", "input": 500, "output": 100, "cache_read": 0, "cache_write": 0,
                 "total": 600, "cost": 0.005}
        e0 = {"provider": "Claude", "session_id": "s0", "time": cutoff + 60,
              "model": "claude-sonnet-5-5", "input": 1000, "output": 200, "cache_read": 0, "cache_write": 0,
              "total": 1200, "cost": 0.01}
        e6 = {"provider": "Claude", "session_id": "s6", "time": now_sec - 60,
              "model": "claude-sonnet-5-5", "input": 3000, "output": 500, "cache_read": 0, "cache_write": 0,
              "total": 3500, "cost": 0.03}

        with mock.patch.object(tokens, "load_claude_events", return_value=([e_old, e0, e6], now_1255)), \
             mock.patch.object(tokens, "load_opencode_events", return_value=[]), \
             mock.patch.object(tokens, "load_antigravity_events", return_value=[]), \
             mock.patch.object(tokens, "load_tokens_cache", return_value={"version": getattr(tokens, "CACHE_VERSION", 2), "antigravity": {}, "claude": {}}), \
             mock.patch.object(tokens, "save_tokens_cache"):
            th, _, _ = tokens.token_history_and_agents([], {}, now_1255)
            h24 = th["24h"]
            self.assertEqual(h24["tot"], e0["total"] + e6["total"])
            self.assertEqual(len(h24["bars"]), 7)
            self.assertEqual(sum(b["total"] for b in h24["bars"]), h24["tot"])
            self.assertEqual(h24["bars"][0]["total"], e0["total"])
            self.assertEqual(h24["bars"][6]["total"], e6["total"])

        # Test at 15:55 UTC (late in the 4-hour block)
        now_1555 = datetime(2026, 10, 29, 15, 55, 0, tzinfo=timezone.utc)
        now_sec = now_1555.timestamp()
        cutoff = now_sec - 24 * 3600

        e0 = {"provider": "Claude", "session_id": "s0", "time": cutoff + 60,
              "model": "claude-sonnet-5-5", "input": 2000, "output": 500, "cache_read": 0, "cache_write": 0,
              "total": 2500, "cost": 0.02}
        e6 = {"provider": "Claude", "session_id": "s6", "time": now_sec - 60,
              "model": "claude-sonnet-5-5", "input": 4000, "output": 1000, "cache_read": 0, "cache_write": 0,
              "total": 5000, "cost": 0.04}

        with mock.patch.object(tokens, "load_claude_events", return_value=([e0, e6], now_1555)), \
             mock.patch.object(tokens, "load_opencode_events", return_value=[]), \
             mock.patch.object(tokens, "load_antigravity_events", return_value=[]), \
             mock.patch.object(tokens, "load_tokens_cache", return_value={"version": getattr(tokens, "CACHE_VERSION", 2), "antigravity": {}, "claude": {}}), \
             mock.patch.object(tokens, "save_tokens_cache"):
            th, _, _ = tokens.token_history_and_agents([], {}, now_1555)
            h24 = th["24h"]
            self.assertEqual(h24["tot"], e0["total"] + e6["total"])
            self.assertEqual(len(h24["bars"]), 7)
            self.assertEqual(sum(b["total"] for b in h24["bars"]), h24["tot"])
            self.assertEqual(h24["bars"][0]["total"], e0["total"])
            self.assertEqual(h24["bars"][6]["total"], e6["total"])

    def test_24h_bar_labels_unique_right_after_a_block(self):
        # the cron build runs at hh:00:0x: yesterday's first bar must not read the same as today's last
        now = datetime(2026, 10, 29, 4, 0, 5, tzinfo=timezone.utc)
        labels = [b["label"] for b in tokens.build_bars_24h([], now)]
        self.assertEqual(len(set(labels)), len(labels), labels)
        self.assertEqual((labels[0], labels[-1]), ("04:00:05", "04:00"))

    def test_24h_bars_on_exact_block_boundary(self):
        now = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
        now_sec = now.timestamp()
        cutoff = now_sec - 24 * 3600
        e = {"provider": "Claude", "session_id": "s", "time": cutoff + 100,
             "model": "claude-sonnet-5-5", "input": 1000, "output": 200, "cache_read": 0, "cache_write": 0,
             "total": 1200, "cost": 0.01}
        with mock.patch.object(tokens, "load_claude_events", return_value=([e], now)), \
             mock.patch.object(tokens, "load_opencode_events", return_value=[]), \
             mock.patch.object(tokens, "load_antigravity_events", return_value=[]), \
             mock.patch.object(tokens, "load_tokens_cache", return_value={"version": getattr(tokens, "CACHE_VERSION", 2), "antigravity": {}, "claude": {}}), \
             mock.patch.object(tokens, "save_tokens_cache"):
            th, _, _ = tokens.token_history_and_agents([], {}, now)
            h24 = th["24h"]
            self.assertEqual(h24["tot"], 1200)
            self.assertEqual(len(h24["bars"]), 6)  # Exactly 6 full 4-hour slots
            self.assertEqual(sum(b["total"] for b in h24["bars"]), h24["tot"])


if __name__ == "__main__":
    unittest.main()
