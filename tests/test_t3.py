import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone

from factory import t3, tokens
from factory.store import FactoryError

NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)
PRODUCTS = {"bk": {"name": "Acme", "workspaces": ["/w/acme-monorepo"]}}
PROJECTS = [{"id": "ds", "product": "bk", "match": ["acme-ds/"]}]


def fixture(path):
    """A small statev2.sqlite with the tables and columns factory reads."""
    con = sqlite3.connect(path)
    for table, cols in t3.COLUMNS.items():
        con.execute(f"create table {table} ({', '.join(cols)})")
    con.executemany("insert into projection_projects values (?,?,?)",
                    [("p1", "/w/acme-monorepo", None), ("p2", "/w/other", None)])

    def thread(tid, pid, title, updated, root=None, archived=None):
        payload = {"lineage": {"rootThreadId": root or tid}}
        con.execute("insert into orchestration_v2_projection_threads values (?,?,?,?,?,?,?,?)",
                    (tid, pid, title, updated, updated, archived, None, json.dumps(payload)))

    def runrow(tid, status, req, done=None, model="claude-opus-5-5", inst="claudeAgent"):
        con.execute("insert into orchestration_v2_projection_runs values (?,?,?,?,?,?)",
                    (tid, status, req, done, json.dumps({"modelSelection": {"model": model}}), inst))

    thread("orch", "p1", "acme-ds/migration-orchestrator", "2026-10-27T13:50:00Z")
    runrow("orch", "completed", "2026-10-27T13:00:00Z", "2026-10-27T13:50:00Z")
    thread("thread:delegated-task:a", "p1", "Build unit E1b", "2026-10-27T13:30:00Z", root="orch")
    runrow("thread:delegated-task:a", "running", "2026-10-27T13:00:00Z", None, "gemini-3.8-flash-high", "antigravity_antigravity_2")
    thread("thread:delegated-task:ghost", "p1", "Old build", "2026-10-27T09:00:00Z", root="orch")
    runrow("thread:delegated-task:ghost", "running", "2026-10-27T08:00:00Z")
    thread("thread:delegated-task:f", "p1", "Fix #1260", "2026-10-27T12:00:00Z", root="orch")
    runrow("thread:delegated-task:f", "completed", "2026-10-26T10:00:00Z", "2026-10-26T11:00:00Z")
    runrow("thread:delegated-task:f", "failed", "2026-10-27T11:00:00Z", "2026-10-27T12:00:00Z", "opencode/x", "opencode")
    thread("asks", "p1", "Unrelated thread", "2026-10-27T13:55:00Z")
    runrow("asks", "completed", "2026-10-27T13:00:00Z", "2026-10-27T13:10:00Z")
    con.execute("insert into orchestration_v2_projection_runtime_requests values (?,?)", ("asks", "pending"))
    con.execute("insert into orchestration_v2_projection_runtime_requests values (?,?)", ("orch", "resolved"))
    thread("old", "p1", "Done last week", "2026-10-20T10:00:00Z")
    runrow("old", "completed", "2026-10-20T09:00:00Z", "2026-10-20T10:00:00Z")
    thread("arch", "p1", "Archived", "2026-10-27T13:00:00Z", archived="2026-10-27T13:30:00Z")
    runrow("arch", "running", "2026-10-27T13:00:00Z")
    thread("elsewhere", "p2", "Other product", "2026-10-27T13:00:00Z")
    runrow("elsewhere", "running", "2026-10-27T13:00:00Z")
    thread("thread:provider:claudeAgent:native-thread:x", "p1", "Explore the kit", "2026-10-27T13:40:00Z", root="orch")
    con.execute("insert into orchestration_v2_projection_subagents values (?,?,?,?,?,?,?)",
                ("thread:provider:claudeAgent:native-thread:x", "running", "2026-10-27T13:35:00Z", None,
                 json.dumps({"model": "claude-haiku-5-5"}), "claudeAgent", "claudeAgent"))
    thread("thread:delegated-task:q", "p1", "Status from a newer T3", "2026-10-27T13:20:00Z", root="orch")
    runrow("thread:delegated-task:q", "teleported", "2026-10-27T13:20:00Z")
    thread("thread:delegated-task:w", "p1", "Queued", "2026-10-27T13:50:00Z", root="orch")
    runrow("thread:delegated-task:w", "queued", "2026-10-27T13:50:00Z")
    thread("thread:delegated-task:r", "p1", "Rolled back", "2026-10-27T13:10:00Z", root="orch")
    runrow("thread:delegated-task:r", "rolled_back", "2026-10-27T13:00:00Z", "2026-10-27T13:10:00Z")
    runrow("old", "teleported", "2026-10-01T09:00:00Z", "2026-10-01T10:00:00Z")  # outside the 24 h window
    thread("thread:delegated-task:i", "p1", "Interrupted", "2026-10-27T12:30:00Z", root="orch")
    runrow("thread:delegated-task:i", "interrupted", "2026-10-27T12:00:00Z", "2026-10-27T12:30:00Z")
    thread("thread:delegated-task:idl", "p1", "Idle task", "2026-10-27T13:45:00Z", root="orch")
    runrow("thread:delegated-task:idl", "idle", "2026-10-27T13:00:00Z", "2026-10-27T13:45:00Z")
    thread("thread:delegated-task:waitrun", "p1", "Run waiting on secret", "2026-10-27T13:40:00Z", root="orch")
    runrow("thread:delegated-task:waitrun", "waiting", "2026-10-27T13:00:00Z")
    # requested 2 days ago, still open, thread busy now: shown as unknown, so it must alarm too
    thread("thread:delegated-task:oldopen", "p1", "Old open unknown", "2026-10-27T13:30:00Z", root="orch")
    runrow("thread:delegated-task:oldopen", "hovering", "2026-10-25T10:00:00Z", None)
    thread("thread:delegated-task:deadwait", "p1", "Dead run left waiting", "2026-10-24T10:00:00Z", root="orch")
    runrow("thread:delegated-task:deadwait", "waiting", "2026-10-24T09:00:00Z")  # thread silent 3 days: not listed
    con.execute("insert into orchestration_v2_projection_provider_threads values (?,?,?)",
                ("orch", "claudeAgent", json.dumps({"nativeThreadRef": {"nativeId": "sess-1"}})))
    con.execute("insert into orchestration_v2_projection_provider_sessions values (?,?,?,?)",
                ("thread:delegated-task:a", "gemini-3.8-flash-high", "antigravity", "2026-10-27T13:00:00Z"))
    con.commit()
    con.close()


class T3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "statev2.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def test_agents(self):
        fixture(self.db)
        rows, sessions = t3.agents(PRODUCTS, PROJECTS, NOW, t3.connect(self.db))
        by = {r["thread"]: r for r in rows}
        sub = "thread:provider:claudeAgent:native-thread:x"
        self.assertEqual(set(by), {"orch", "thread:delegated-task:a", "thread:delegated-task:ghost",
                                   "thread:delegated-task:f", "asks", sub, "thread:delegated-task:i",
                                   "thread:delegated-task:q", "thread:delegated-task:w", "thread:delegated-task:r",
                                   "thread:delegated-task:idl", "thread:delegated-task:waitrun",
                                   "thread:delegated-task:oldopen"})
        self.assertEqual(by["thread:delegated-task:q"]["state"], "unknown")  # never shown as done
        self.assertEqual(by["thread:delegated-task:w"]["state"], "running")  # queued is an active T3 status
        self.assertEqual(by["thread:delegated-task:r"]["state"], "failed")   # rolled_back
        self.assertEqual(by["thread:delegated-task:idl"]["state"], "done")   # idle
        self.assertEqual(by["thread:delegated-task:waitrun"]["state"], "waiting")  # run status waiting
        self.assertEqual((by[sub]["state"], by[sub]["who"], by[sub]["model"], by[sub]["project"]),
                         ("running", "Subagent", "claude-haiku-5-5", "ds"))
        self.assertEqual(by["thread:delegated-task:i"]["state"], "failed")
        self.assertEqual(by["asks"]["state"], "waiting")
        self.assertEqual(by["thread:delegated-task:a"]["state"], "running")
        self.assertEqual(by["thread:delegated-task:ghost"]["state"], "stalled")
        self.assertEqual(by["thread:delegated-task:f"]["state"], "failed")
        self.assertEqual(by["orch"]["state"], "done")
        a = by["thread:delegated-task:a"]
        self.assertEqual((a["provider"], a["model"], a["who"], a["project"]),
                         ("Antigravity", "gemini-3.8-flash-high", "Delegate", "ds"))
        self.assertEqual(by["thread:delegated-task:f"]["model"], "opencode/x")
        self.assertIsNone(by["asks"]["project"])
        self.assertEqual(sessions[by["orch"]["hc"]], ["sess-1"])
        self.assertEqual([r["state"] for r in rows],
                         ["waiting", "waiting", "running", "running", "running", "stalled", "unknown", "unknown", "failed", "failed",
                          "failed", "done", "done"])
        # counted from the rows the page shows: the 2026-10-01 'teleported' run is not listed, so it does not alarm
        self.assertEqual(t3.unknown_statuses(rows), {"teleported": 1, "hovering": 1})
        self.assertEqual(len({r["hc"] for r in rows}), len(rows))
        self.assertTrue(by["orch"]["hc"].startswith("1") and a["hc"].startswith("2"))

    def test_handles_unique(self):
        ids = [f"thread:delegated-task:{i}" for i in range(5000)]
        hs = t3.handles({i: "2" for i in ids})
        self.assertEqual(len(set(hs.values())), 5000)
        self.assertTrue(all(h.startswith("2") and len(h) >= 4 for h in hs.values()))
        created = {i: f"2026-10-{n % 28 + 1:02d}" for n, i in enumerate(ids)}
        old = t3.handles({i: "2" for i in ids}, created)
        newer = {i: "2" for i in ids + [f"thread:new:{n}" for n in range(500)]}
        new = t3.handles(newer, {**created, **{i: "2026-12-01" for i in newer if i.startswith("thread:new")}})
        self.assertTrue(all(new[i] == old[i] for i in ids))  # newer threads never change older handles

    def test_attribution_handles_match_agents_tab(self):
        # issue #21: archived t27 (older) and live t78 share the sha1 prefix 7CF
        fixture(self.db)
        con = sqlite3.connect(self.db)
        for tid, created, archived in (("t27", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z"),
                                       ("t78", "2026-10-27T13:00:00Z", None)):
            con.execute("insert into orchestration_v2_projection_threads values (?,?,?,?,?,?,?,?)",
                        (tid, "p1", tid, created, created, archived, None, "{}"))
        con.commit()
        con.close()
        _, hcs = t3.thread_attribution_map(t3.connect(self.db), PRODUCTS, PROJECTS)
        live = t3.thread_handles(t3.connect(self.db))
        self.assertEqual(live["t78"], "17CF")
        self.assertTrue(all(hcs[t] == h for t, h in live.items()))
        self.assertEqual(hcs["t27"], "17CF0")

    def test_schema_change_fails_loudly(self):
        fixture(self.db)
        with sqlite3.connect(self.db) as con:
            con.execute("alter table orchestration_v2_projection_runs rename column status to state")
        with self.assertRaises(t3.T3SchemaError) as e:
            t3.connect(self.db)
        self.assertIn("orchestration_v2_projection_runs.status", str(e.exception))

    def test_turn_items_schema_and_thread_user_messages(self):
        fixture(self.db)
        with sqlite3.connect(self.db) as con:
            con.execute(
                "insert into orchestration_v2_projection_messages values (?,?,?,?)",
                ("thread-test-1", "user", "2026-10-30T01:00:00Z", json.dumps({"text": "Hello queued message", "createdBy": "agent", "creationSource": "mcp"}))
            )
            con.execute(
                "insert into orchestration_v2_projection_turn_items values (?,?,?,?)",
                ("thread-test-1", 1000001, "user_message", json.dumps({"text": "Hello user message"}))
            )
            con.execute(
                "insert into orchestration_v2_projection_turn_items values (?,?,?,?)",
                ("thread-test-1", 1000002, "agent_message", json.dumps({"text": "Agent reply"}))
            )
            # User chat message (createdBy == 'user') is ignored
            con.execute(
                "insert into orchestration_v2_projection_messages values (?,?,?,?)",
                ("thread-test-1", "user", "2026-10-30T01:01:00Z", json.dumps({"text": "Ignored human message", "createdBy": "user", "creationSource": "web"}))
            )
        con = t3.connect(self.db)
        msgs = t3.thread_user_messages(con, "thread-test-1")
        self.assertEqual(msgs, ["Hello queued message", "Hello user message"])
        con.close()

    def test_thread_user_messages_skips_prompt_dropped_by_hook(self):
        fixture(self.db)
        mcp = lambda t: json.dumps({"text": t, "createdBy": "agent", "creationSource": "mcp"})
        with sqlite3.connect(self.db) as con:
            con.executemany("insert into orchestration_v2_projection_messages values (?,?,?,?)", [
                ("t", "user", "2026-10-30T01:00:00Z", mcp("[factory relay d1] dropped")),
                ("t", "assistant", "2026-10-30T01:00:00Z", json.dumps({"text": "Prompt dropped by a hook: cache-tax: 461,169 tokens"})),
                ("t", "user", "2026-10-30T01:05:00Z", mcp("[factory relay d2] kept")),
                ("t", "assistant", "2026-10-30T01:05:30Z", json.dumps({"text": "Applied d2."})),
            ])
            con.executemany("insert into orchestration_v2_projection_turn_items values (?,?,?,?)", [
                ("t", 1000001, "user_message", mcp("[factory relay d1] dropped")),
                # the a51 shape seen live: the drop is only a system_notice turn item, no messages row
                ("t", 138000001, "user_message", mcp("[factory relay a51] dropped")),
                ("t", 138000002, "system_notice", json.dumps({"message": "Prompt dropped by a hook: cache-tax: 497,095 tokens"})),
                ("t", 139000001, "user_message", mcp("[factory relay a52] kept")),
                ("t", 139000002, "assistant_message", json.dumps({"text": "Applied a52."})),
            ])
        con = t3.connect(self.db)
        self.assertEqual(t3.thread_user_messages(con, "t"), ["[factory relay d2] kept", "[factory relay a52] kept"])
        con.close()

        # Schema change on messages fails loudly
        with sqlite3.connect(self.db) as con:
            con.execute("alter table orchestration_v2_projection_messages rename column payload_json to data")
        with self.assertRaises(t3.T3SchemaError) as e:
            t3.connect(self.db)
        self.assertIn("orchestration_v2_projection_messages.payload_json", str(e.exception))

        # Schema change on turn items fails loudly
        db2 = os.path.join(self.tmp.name, "statev2_2.sqlite")
        fixture(db2)
        with sqlite3.connect(db2) as con:
            con.execute("alter table orchestration_v2_projection_turn_items rename column payload_json to data")
        with self.assertRaises(t3.T3SchemaError) as e:
            t3.connect(db2)
        self.assertIn("orchestration_v2_projection_turn_items.payload_json", str(e.exception))

    def test_opens_read_only(self):
        fixture(self.db)
        con = t3.connect(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("delete from projection_projects")

    def test_missing_db(self):
        with self.assertRaises(FactoryError):
            t3.connect(os.path.join(self.tmp.name, "nope.sqlite"))

    def test_whoami(self):
        fixture(self.db)
        with sqlite3.connect(self.db) as raw:
            raw.execute("insert into orchestration_v2_projection_provider_threads values (?,?,?)",
                        ("thread:delegated-task:a", "antigravity", json.dumps({"nativeThreadRef": {"nativeId": "ag-1"}})))
            raw.execute("insert into orchestration_v2_projection_provider_threads values (?,?,?)",
                        ("thread:arch", "claudeAgent", json.dumps({"nativeThreadRef": {"nativeId": "s-arch"}})))
            raw.execute("insert into orchestration_v2_projection_threads values (?,?,?,?,?,?,?,?)",
                        ("thread:arch", "p1", "Archived", "2026-10-27T13:00:00Z", "2026-10-27T13:00:00Z",
                         "2026-10-27T13:30:00Z", None, "{}"))
            for tid, wt in (("thread:wt", "/w/wt"), ("thread:deep", "/w/wt/sub"), ("thread:twin", "/w/wt/sub")):
                raw.execute("insert into orchestration_v2_projection_threads values (?,?,?,?,?,?,?,?)",
                            (tid, "p1", tid, "2026-10-27T13:00:00Z", "2026-10-27T13:00:00Z", None, None,
                             json.dumps({"worktreePath": wt})))
        con = t3.connect(self.db)
        page = {r["thread"]: r["hc"] for r in t3.agents(PRODUCTS, PROJECTS, NOW, con)[0]}

        def who(cwd="/elsewhere", **kw):
            return t3.whoami(PRODUCTS, PROJECTS, con=con, cwd=cwd, **kw)

        def refused(**kw):
            with self.assertRaises(FactoryError) as cm:
                who(**kw)
            return str(cm.exception)

        with mock.patch.dict(os.environ, {}, clear=True):
            r = who(thread_id="orch")  # the handle and project the page shows for that thread
            self.assertEqual((r["handle"], r["title"], r["project"]),
                             (page["orch"], "acme-ds/migration-orchestrator", "ds"))
            self.assertEqual(who(cwd="/w/wt/x")["thread"], "thread:wt")  # the worktree that holds cwd
            self.assertIn("pass --thread", refused(cwd="/w/wt/sub/y"))   # two threads share the deepest one
            self.assertIn("pass --thread", refused(cwd="/w/wtx"))        # a sibling path is not inside /w/wt
            self.assertIn("pass --thread", refused())
            self.assertIn("no live T3 thread 'nope'", refused(thread_id="nope"))
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-1"}, clear=True):
            self.assertEqual(who(cwd="/w/wt")["thread"], "orch")  # the session id wins over the worktree
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "not-in-t3"}, clear=True):
            self.assertIn("pass --thread", refused(cwd="/w/wt"))  # never borrows the worktree's thread
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "s-arch"}, clear=True):
            self.assertIn("no live T3 thread 'thread:arch'", refused(cwd="/w/wt"))  # archived: refused, not the worktree
        with mock.patch.dict(os.environ, {"ANTIGRAVITY_CONVERSATION_ID": "ag-1"}, clear=True):
            r = who()
            self.assertEqual((r["thread"], r["handle"]), ("thread:delegated-task:a", page["thread:delegated-task:a"]))

class TokensTest(unittest.TestCase):
    def test_per_session(self):
        ms = lambda s: datetime.fromisoformat(s).timestamp() * 1000  # noqa: E731
        cache = {"version": 9, "sessions": ["sess-1", "sess-2"], "files": {
            "/a.jsonl": {"p": "claude", "r": [[ms("2026-10-27T10:00:00+00:00"), 0, 0, 2, 90000, 1000, 300, 0, "m1", None, 0, None],
                                             [ms("2026-10-25T10:00:00+00:00"), 0, 0, 2, 0, 5000, 500, 0, "m2", None, 0, None]]},
            "/a/subagents/x.jsonl": {"p": "claude", "r": [[ms("2026-10-27T11:00:00+00:00"), 0, 0, 10, 0, 0, 90, 0, "m3", None, 0, None]]},
            "/b.jsonl": {"p": "claude", "r": [[ms("2026-10-27T12:00:00+00:00"), 0, 1, 1, 0, 0, 9, 0, "m4", None, 0, None]]}}}
        by, total = tokens.per_session(cache, NOW)
        self.assertEqual(by, {"sess-1": 1302 + 100, "sess-2": 10})  # cache reads and the 2-day-old record left out
        self.assertEqual(total, 1412)

    def test_format_change_fails(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"version": 10}, f)
        try:
            with self.assertRaises(FactoryError):
                tokens.load(f.name)
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
