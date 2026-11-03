import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from factory import board, build, cli, skills, sources, store, t3, tokens
from tests import datasource_env

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def run(*argv):
    assert cli.main(list(argv)) == 0, argv


class CollectTest(unittest.TestCase):
    """build.collect() on a small state with every external source mocked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        with mock.patch("sys.stdout"):
            run("product", "set", "bk", "--name", "Acme", "--repo", "o/r")
            run("project", "add", "b", "--product", "bk", "--kind", "build", "--name", "Build")
            run("project", "add", "empty", "--product", "bk", "--kind", "build", "--name", "No phases")
            run("project", "add", "lp", "--product", "bk", "--kind", "loop", "--name", "Hidden loop")
            run("project", "add", "run", "--product", "bk", "--kind", "run", "--name", "Run lane")
        store.write(store.project_rel("bk", "b"), {**store.find_project("b"), "prs": ["o/r#7"], "phases": [
            {"name": "A", "total": 4, "start": "2026-10-20", "due": "2026-10-31", "merged": 2, "inPR": 1, "blocked": 0}],
            "history": {"2026-10-22": [4, 1], "2026-10-25": [4, 2]}, "scopes": [["Before the flip", ["A"]]]})
        # a loop with a recorded run but no target: hidden, so its run must not reach the log
        store.write(store.project_rel("bk", "lp"), {**store.find_project("lp"), "baseline": 1.0, "target": None,
                                                    "exps": [["Baseline", 1.0, 1], ["x", 1.2, 1, "2026-10-26T10:00:00Z"]]})
        store.write("inbox.json", {"seq": 2, "resolved": [], "items": [
            {"id": "d1", "project": "b", "kind": "urgent", "title": "Q", "at": "2026-10-27T10:00:00Z",
             "options": [["a", "A"], ["b", "B"]], "reco": "a"},
            {"id": "d2", "project": "empty", "kind": "urgent", "title": "Lost", "at": "2026-10-27T10:00:00Z",
             "options": [["a", "A"], ["b", "B"]], "reco": "a"}]})
        agents = [{"hc": "2ABC", "product": "bk", "project": "empty", "provider": "Claude", "state": "running"}]
        files = {"f1": {"p": "claude", "r": [[int(NOW.timestamp() * 1000) - 1000, 0, 0, 500, 0, 0, 0]]}}
        # the token view reads the usage cache file itself: give it this fixture, never the machine's real one
        usage = os.path.join(self.tmp.name, "usage-scan-cache.json")
        with open(usage, "w") as f:
            json.dump({"version": 9, "sessions": ["s1"], "models": ["claude-sonnet-5-5"], "files": files}, f)
        nowhere = os.path.join(self.tmp.name, "nowhere")
        env = mock.patch.dict(os.environ, datasource_env(nowhere, FACTORY_USAGE_CACHE=usage))
        env.start()
        self.addCleanup(env.stop)
        patches = [mock.patch.object(t3, "connect", return_value=mock.MagicMock()),
                   mock.patch.object(t3, "agents", return_value=(agents, {"2ABC": ["s1"]})),
                   mock.patch.object(t3, "unknown_statuses", return_value={"queued": 2}),
                   mock.patch.object(tokens, "load", return_value=({"sessions": ["s1"], "models": ["claude-sonnet-5-5"], "files": files}, NOW)),
                   mock.patch.object(sources, "prs_for", return_value={"o/r#7": ["T", "pass", "Open"]}),
                   mock.patch.object(skills, "collect", return_value=([], [], 0.001)),
                   mock.patch.object(sources, "run_lane", return_value=([[1, 0]] * 14, [])),
                   mock.patch.object(board, "collect", return_value={"cards": [], "fetchedAt": None, "error": None})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_collect(self):
        d = build.collect(NOW)
        self.assertEqual(sorted(p["id"] for p in d["projects"]), ["b", "run"])
        self.assertTrue(any(w.startswith("empty: no phases") for w in d["warnings"]))
        self.assertTrue(any(w.startswith("lp: no baseline or target") for w in d["warnings"]))
        self.assertTrue(any("inbox d2" in w for w in d["warnings"]))
        self.assertTrue(any("'queued' is unknown (2 run(s))" in w for w in d["warnings"]))
        b = next(p for p in d["projects"] if p["id"] == "b")
        # 10-20 .. 10-27: nothing recorded before the first snapshot, then carried forward
        self.assertEqual(b["series"], [[4, 0], [4, 0], [4, 1], [4, 1], [4, 1], [4, 2], [4, 2], [4, 2]])
        self.assertEqual(b["scopes"], [["Whole project", [0]], ["Before the flip", [0]]])
        self.assertEqual(b["phases"], [["A", 2, 4, "2026-10-20", "2026-10-31", 1, 0]])
        self.assertEqual([lv["id"] for lv in d["levers"]], ["d1"])
        self.assertEqual(d["levers"][0]["target"], "no session")
        self.assertEqual(d["log"], [])
        self.assertEqual(d["agents"][0]["project"], None)   # its project is not on the page
        self.assertEqual(d["agents"][0]["tokens"], 500)
        self.assertEqual(d["prs"], {"o/r#7": ["T", "pass", "Open"]})

    def test_collect_mocks_thread_handles_without_reading_t3_db(self):
        with mock.patch.object(t3, "thread_handles", return_value={"thread-custom": "1ABC"}) as mock_th:
            store.write("inbox.json", {"seq": 1, "resolved": [], "items": [
                {"id": "d1", "project": "b", "kind": "urgent", "title": "Q", "at": "2026-10-27T10:00:00Z",
                 "options": [["a", "A"]], "thread": "thread-custom"}
            ]})
            d = build.collect(NOW)
            mock_th.assert_called_once()
            lv = next(l for l in d["levers"] if l["id"] == "d1")
            self.assertEqual(lv["target"], "1ABC")


class ThreadCollisionTest(unittest.TestCase):
    """Archived and live threads sharing a hash prefix must not misattribute lever targets."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)

    def test_archived_live_prefix_collision_lever_target(self):
        # Repro: archived t27 + live t78 sharing prefix 7cf
        # Agents tab assigns 17CF to t78 (t27 is archived).
        # Lever targets must match the Agents tab (17CF for t78, 'no session' for archived t27).
        with mock.patch("sys.stdout"):
            cli.main(["product", "set", "bk", "--name", "Acme", "--repo", "o/r", "--workspace", "/w/acme-monorepo"])
            cli.main(["project", "add", "b", "--product", "bk", "--kind", "build", "--name", "Build"])

        store.write(store.project_rel("bk", "b"), {**store.find_project("b"), "prs": ["o/r#7"], "phases": [
            {"name": "A", "total": 4, "start": "2026-10-20", "due": "2026-10-31", "merged": 2, "inPR": 1, "blocked": 0}]})

        now = datetime(2026, 10, 27, 12, 5, tzinfo=timezone.utc)

        db_path = os.path.join(self.tmp.name, "statev2_collision.sqlite")
        con = sqlite3.connect(db_path)
        for table, cols in t3.COLUMNS.items():
            con.execute(f"create table {table} ({', '.join(cols)})")
        con.executemany("insert into projection_projects values (?,?,?)",
                        [("p1", "/w/acme-monorepo", None)])

        def thread(tid, pid, title, created, archived=None):
            payload = {"lineage": {"rootThreadId": tid}}
            con.execute("insert into orchestration_v2_projection_threads values (?,?,?,?,?,?,?,?)",
                        (tid, pid, title, created, created, archived, None, json.dumps(payload)))

        def runrow(tid, status, req):
            con.execute("insert into orchestration_v2_projection_runs values (?,?,?,?,?,?)",
                        (tid, status, req, None, json.dumps({"modelSelection": {"model": "m"}}), "claudeAgent"))

        thread("t27", "p1", "Old thread", "2026-10-27T10:00:00Z", archived="2026-10-27T11:00:00Z")
        runrow("t27", "running", "2026-10-27T10:00:00Z")
        thread("t78", "p1", "Live thread", "2026-10-27T12:00:00Z")
        runrow("t78", "running", "2026-10-27T12:00:00Z")
        con.commit()
        con.close()

        store.write("inbox.json", {"seq": 1, "resolved": [], "items": [
            {"id": "d1", "project": "b", "kind": "urgent", "title": "Live item", "at": "2026-10-27T10:00:00Z",
             "options": [["a", "A"]], "thread": "t78"},
            {"id": "d2", "project": "b", "kind": "urgent", "title": "Archived item", "at": "2026-10-27T10:00:00Z",
             "options": [["a", "A"]], "thread": "t27"},
        ]})

        nowhere = os.path.join(self.tmp.name, "nowhere")
        with mock.patch.dict(os.environ, datasource_env(nowhere, FACTORY_T3_DB=db_path)), \
                mock.patch.object(sources, "prs_for", return_value={}), \
                mock.patch.object(sources, "run_lane", return_value=([[0, 0]] * 14, [])), \
                mock.patch.object(skills, "collect", return_value=([], [], 0.0)), \
                mock.patch.object(board, "collect", return_value={"cards": [], "fetchedAt": None, "error": None}):  # never gh or real transcripts
            d = build.collect(now)
            # Agents tab has t78 with 17CF
            self.assertEqual([(a["thread"], a["hc"]) for a in d["agents"]], [("t78", "17CF")])
            # Levers: d1 for live thread t78 matches Agents tab 17CF
            lv1 = next(l for l in d["levers"] if l["id"] == "d1")
            self.assertEqual(lv1["target"], "17CF")
            # d2 for archived thread t27 has no live session
            lv2 = next(l for l in d["levers"] if l["id"] == "d2")
            self.assertEqual(lv2["target"], "no session")


class SeriesTest(unittest.TestCase):
    def test_not_started_build_keeps_todays_merges(self):
        p = {"phases": [{"start": "2026-11-10"}], "history": {"2026-10-27": [5, 2]}}
        self.assertEqual(build.manual_series(p, NOW.date()), [[5, 2]])  # today, before the first phase


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        self.data = {
            "builtAt": "2026-10-27T12:00:00Z",
            "products": {}, "projects": [], "agents": [], "tokens": {},
            "levers": [], "answers": {}, "prs": {}, "log": [], "provColors": {},
            "warnings": []
        }

    def test_naive_built_at_stamp_does_not_crash(self):
        store.write_text(os.path.join("site", "built-at"), "2026-10-27T11:00:00\n")
        out = build.render(self.data)
        self.assertTrue(self.data["written"])

    def test_unparsable_built_at_stamp_warns(self):
        store.write_text(os.path.join("site", "built-at"), "invalid-stamp\n")
        build.render(self.data)
        self.assertTrue(any("unparsable" in w for w in self.data["warnings"]))

    def test_stamp_written_before_page(self):
        written = []
        with mock.patch("factory.store.write_text", side_effect=lambda rel, *args, **kwargs: written.append(rel)):
            build.render(self.data)
        self.assertEqual(written, [os.path.join("site", "built-at"), os.path.join("site", "factory.html")])


class SubagentBuildAndClosedMsgTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        with mock.patch("sys.stdout"):
            run("product", "set", "bk", "--name", "Acme", "--repo", "o/r")
            run("project", "add", "b", "--product", "bk", "--kind", "build", "--name", "Build")

    def test_subagent_can_receive_false_and_parent_hint_in_data(self):
        agents = [
            {"hc": "1A01", "who": "Thread", "thread": "thread-1", "product": "bk", "project": "b", "provider": "Claude", "state": "running"},
            {"hc": "2B02", "who": "Delegate", "thread": "thread:delegated-task:2", "product": "bk", "project": "b", "provider": "Claude", "state": "done"},
            {"hc": "3C03", "who": "Subagent", "thread": "thread:provider:claudeAgent:3", "product": "bk", "project": "b", "provider": "Claude", "state": "running", "parent": "1A01"},
            {"hc": "3D04", "who": "Subagent", "thread": "thread:provider:claudeAgent:4", "product": "bk", "project": "b", "provider": "Claude", "state": "running", "parent": None},
            {"hc": "4E05", "who": "Extra", "thread": None, "product": "bk", "project": "b", "provider": "Claude", "state": "running"},
        ]
        patches = [
            mock.patch.object(t3, "connect", return_value=mock.MagicMock()),
            mock.patch.object(t3, "agents", return_value=(agents, {})),
            mock.patch.object(t3, "thread_attribution_map", return_value=({}, {})),
            mock.patch.object(t3, "unknown_statuses", return_value={}),
            mock.patch.object(tokens, "load", return_value=({"sessions": [], "models": [], "files": {}}, NOW)),
            mock.patch.object(sources, "prs_for", return_value={}),
            mock.patch.object(skills, "collect", return_value=([], [], 0.001)),
            mock.patch.object(sources, "run_lane", return_value=([], [])),
            mock.patch.object(board, "collect", return_value={"cards": [], "fetchedAt": None, "error": None}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        d = build.collect(NOW)
        agent_by_hc = {a["hc"]: a for a in d["agents"]}

        # 1A01 (Thread) and 2B02 (Delegate) can receive messages
        self.assertTrue(agent_by_hc["1A01"]["canReceive"])
        self.assertTrue(agent_by_hc["2B02"]["canReceive"])

        # Role 3 subagents have canReceive=False
        self.assertFalse(agent_by_hc["3C03"]["canReceive"])
        self.assertFalse(agent_by_hc["3D04"]["canReceive"])

        # Non-T3 agent has canReceive=False
        self.assertFalse(agent_by_hc["4E05"]["canReceive"])

        # Parent hint data present in DATA
        self.assertEqual(agent_by_hc["3C03"]["parent"], "1A01")
        self.assertEqual(agent_by_hc["3C03"]["disabledHint"], "subagent: message its parent 1A01")

        self.assertIsNone(agent_by_hc["3D04"]["parent"])
        self.assertEqual(agent_by_hc["3D04"]["disabledHint"], "subagent: message its parent")

        self.assertEqual(agent_by_hc["4E05"]["disabledHint"], "not in T3: no way to deliver")

    def test_built_page_closed_msg_wording(self):
        # Test closedMsg wording via executing the built page in node (no string-matching of JS source)
        data = {
            "builtAt": "2026-10-27T12:00:00Z",
            "products": {"bk": {"name": "Acme"}}, "projects": [], "agents": [], "tokens": {},
            "levers": [], "answers": {}, "prs": {}, "log": [], "provColors": {},
            "warnings": []
        }
        path = build.render(data)
        node_script = f'''
const fs = require("fs");
const html = fs.readFileSync("{path}", "utf8");
const scriptMatch = html.match(/<script>([\\s\\S]*?)<\\/script>/);
const vm = require("vm");
const dummy = {{ addEventListener: () => {{}}, querySelector: () => dummy, querySelectorAll: () => [], dataset: {{}}, style: {{}}, classList: {{ add: () => {{}}, remove: () => {{}} }} }};
const ctx = vm.createContext({{
  URLSearchParams, Date, String, Boolean, isNaN, Math, Object, Array, RegExp, JSON,
  setInterval: () => {{}}, setTimeout: () => {{}}, clearInterval: () => {{}}, clearTimeout: () => {{}},
  fetch: () => new Promise(() => {{}}),
  document: {{ addEventListener: () => {{}}, querySelector: () => dummy, querySelectorAll: () => [], getElementById: () => dummy }},
  window: {{ addEventListener: () => {{}} }},
  addEventListener: () => {{}},
  scrollTo: () => {{}},
  confirm: () => true,
  location: {{ hash: '', search: '', reload: () => {{}} }},
  navigator: {{ platform: 'MacIntel' }},
  console: console,
}});
vm.runInContext(scriptMatch[1], ctx);
const res = {{
  withTimeKept: ctx.closedMsg({{ resolvedAt: "2026-10-30T10:00:00Z" }}, true),
  noTimeKept: ctx.closedMsg({{}}, true),
  withAnsweredKept: ctx.closedMsg({{ answeredAt: "2026-10-30T10:00:00Z" }}, true),
  withTimeNotKept: ctx.closedMsg({{ resolvedAt: "2026-10-30T10:00:00Z" }}, false),
  noTimeNotKept: ctx.closedMsg({{}}, false),
}};
process.stdout.write(JSON.stringify(res));
'''
        out = subprocess.check_output(["node", "-e", node_script], text=True)
        res = json.loads(out)
        self.assertEqual(res["withTimeKept"], "Already handled by the agent (closed at 10:00 UTC). Your draft is kept; saving will not change anything.")
        self.assertEqual(res["noTimeKept"], "Already handled by the agent. Your draft is kept; saving will not change anything.")
        self.assertEqual(res["withAnsweredKept"], "Already handled by the agent (your answer was applied at 10:00 UTC). Your draft is kept; saving will not change anything.")
        self.assertEqual(res["withTimeNotKept"], "Already handled by the agent (closed at 10:00 UTC).")
        self.assertEqual(res["noTimeNotKept"], "Already handled by the agent.")


if __name__ == "__main__":
    unittest.main()
