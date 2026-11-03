import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from factory import cli, serve, sources, store


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--json", *argv])
    return code, (json.loads(out.getvalue()) if code == 0 and out.getvalue().strip() else None), err.getvalue()


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.whoami_patcher = mock.patch("factory.t3.whoami", side_effect=lambda prods, projs, tid=None, **kw: {"thread": tid or "cli-test-thread", "handle": "1A3F", "title": "Test Thread", "project": "bk"})
        self.whoami_patcher.start()
        self.addCleanup(self.whoami_patcher.stop)
        self.assertEqual(run("product", "set", "bk", "--name", "Acme", "--repo", "o/r")[0], 0)

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
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        return err

    def test_whoami_cli(self):
        self.whoami_patcher.stop()
        db_path = os.path.join(self.tmp.name, "statev2.sqlite")
        from tests.test_t3 import fixture
        fixture(db_path)
        self.ok("product", "set", "bk", "--workspace", "/w/acme-monorepo")
        self.ok("project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "DS")
        self.ok("project", "set", "ds", "--match", "acme-ds/")
        with mock.patch.dict(os.environ, {"FACTORY_T3_DB": db_path}):
            # JSON mode (via run)
            obj = self.ok("whoami", "--thread", "orch")
            self.assertEqual(obj["thread"], "orch")
            self.assertEqual(obj["title"], "acme-ds/migration-orchestrator")
            self.assertEqual(obj["project"], "ds")
            self.assertTrue(obj["handle"].startswith("1"))

            # Text mode
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["whoami", "--thread", "orch"])
            self.assertEqual(code, 0)
            self.assertTrue(out.getvalue().startswith("1"))
            self.assertIn("\torch\tacme-ds/migration-orchestrator\tds", out.getvalue())

    def test_build_plan(self):
        self.ok("project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "DS", "--feat", "FEAT-1")
        self.ok("plan", "phase", "ds", "A", "--total", "4", "--start", "2026-10-01", "--due", "2026-10-10")
        self.ok("plan", "phase", "ds", "B", "--total", "6", "--start", "2026-10-11", "--due", "2026-10-20")
        ph = self.ok("plan", "units", "ds", "A", "--merged", "3", "--in-pr", "1")
        self.assertEqual((ph["merged"], ph["inPR"], ph["blocked"]), (3, 1, 0))
        self.ok("plan", "scope", "ds", "Before the flip", "A")
        self.ok("plan", "added", "ds", "2")
        p = store.find_project("ds")
        self.assertEqual(p["scopes"], [["Before the flip", ["A"]]])
        self.assertEqual(p["added"], 2)
        self.assertEqual(p["history"][datetime.now(timezone.utc).date().isoformat()], [10, 3])
        self.assertIn("more than the phase total", self.fails("plan", "units", "ds", "A", "--merged", "5"))
        self.assertIn("no phase", self.fails("plan", "scope", "ds", "X", "Z"))
        self.assertIn("bad date", self.fails("plan", "phase", "ds", "C", "--total", "1", "--start", "nope", "--due", "x"))
        self.assertIn("less than the units", self.fails("plan", "phase", "ds", "A", "--total", "2", "--start", "2026-10-01", "--due", "2026-10-10"))
        self.assertIn("bad count", self.fails("plan", "units", "ds", "A", "--merged", "\u00b2"))
        self.assertIn("at least one phase", self.fails("plan", "scope", "ds", "S", ","))
        self.ok("plan", "phase", "ds", "Z", "--total", "0", "--start", "2026-10-21", "--due", "2026-10-25")
        self.assertIn("holds 0 units", self.fails("plan", "scope", "ds", "Later", "Z"))
        self.ok("plan", "phase", "ds", "P0", "--total", "1", "--start", "2026-09-20", "--due", "2026-09-30")
        self.assertEqual([x["name"] for x in store.find_project("ds")["phases"]], ["P0", "A", "B", "Z"])

    def test_plan_rename(self):
        self.ok("project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "DS")
        self.ok("plan", "phase", "ds", "A", "--total", "4", "--start", "2026-10-01", "--due", "2026-10-10")
        self.ok("plan", "phase", "ds", "B", "--total", "6", "--start", "2026-10-11", "--due", "2026-10-20")
        self.ok("plan", "scope", "ds", "Scope1", "A")
        self.ok("plan", "scope", "ds", "Scope2", "A, B")

        # failure cases
        self.assertIn("no project 'nope'", self.fails("plan", "rename", "nope", "A", "Alpha"))
        self.ok("project", "add", "lp", "--product", "bk", "--kind", "loop", "--name", "Loop")
        self.assertIn("not build", self.fails("plan", "rename", "lp", "A", "Alpha"))
        self.assertIn("no phase 'X'", self.fails("plan", "rename", "ds", "X", "Alpha"))
        self.assertIn("empty", self.fails("plan", "rename", "ds", "A", "   "))
        self.assertIn("same as old", self.fails("plan", "rename", "ds", "A", " A "))
        self.assertIn("no phase 'X'", self.fails("plan", "rename", "ds", "X", "X"))
        self.assertIn("comma", self.fails("plan", "rename", "ds", "A", "A,C"))
        self.assertIn("comma", self.fails("plan", "phase", "ds", "A,C", "--total", "1", "--start", "2026-10-01", "--due", "2026-10-02"))
        self.assertIn("already exists", self.fails("plan", "rename", "ds", "A", "B"))

        # success with --json
        ph = self.ok("plan", "rename", "ds", "A", " Alpha ")  # stripped, as `plan scope` strips
        self.assertEqual(ph["name"], "Alpha")
        self.assertEqual(ph["total"], 4)
        p = store.find_project("ds")
        self.assertEqual([x["name"] for x in p["phases"]], ["Alpha", "B"])
        self.assertEqual(p["scopes"], [["Scope1", ["Alpha"]], ["Scope2", ["Alpha", "B"]]])

        # success without --json (text output)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["plan", "rename", "ds", "B", "Beta"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "ok: ds renamed phase B to Beta")
        self.assertEqual([x["name"] for x in store.find_project("ds")["phases"]], ["Alpha", "Beta"])

    def test_plan_remove(self):
        self.ok("project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "DS")
        self.ok("plan", "phase", "ds", "A", "--total", "4", "--start", "2026-10-01", "--due", "2026-10-10")
        self.ok("plan", "phase", "ds", "B", "--total", "6", "--start", "2026-10-11", "--due", "2026-10-20")
        self.ok("plan", "phase", "ds", "C", "--total", "2", "--start", "2026-10-21", "--due", "2026-10-25")
        self.ok("plan", "phase", "ds", "Z", "--total", "0", "--start", "2026-10-26", "--due", "2026-10-30")
        self.ok("plan", "units", "ds", "A", "--merged", "3")
        self.ok("plan", "scope", "ds", "ScopeA", "A")
        self.ok("plan", "scope", "ds", "ScopeAB", "A, B")
        self.ok("plan", "scope", "ds", "ScopeBZ", "B, Z")
        self.ok("plan", "phase", "ds", "W", "--total", "1", "--start", "2026-10-26", "--due", "2026-10-30")
        self.ok("plan", "scope", "ds", "ScopeW", "W")
        self.ok("plan", "phase", "ds", "W", "--total", "0", "--start", "2026-10-26", "--due", "2026-10-30")
        today = datetime.now(timezone.utc).date().isoformat()

        self.assertIn("no project 'nope'", self.fails("plan", "remove", "nope", "A"))
        self.ok("project", "add", "lp", "--product", "bk", "--kind", "loop", "--name", "Loop")
        self.assertIn("not build", self.fails("plan", "remove", "lp", "A"))
        self.assertIn("no phase 'X'", self.fails("plan", "remove", "ds", "X"))
        before = store.find_project("ds")
        self.assertIn("3 merged units", self.fails("plan", "remove", "ds", "A"))
        self.assertIn("named in scopes: 'ScopeAB', 'ScopeBZ' (use --force)", self.fails("plan", "remove", "ds", "B"))
        self.assertEqual(store.find_project("ds"), before)  # a refused remove changes nothing

        self.assertEqual(self.ok("plan", "remove", "ds", "C")["name"], "C")
        p = store.find_project("ds")
        self.assertEqual([x["name"] for x in p["phases"]], ["A", "B", "Z", "W"])
        self.assertEqual(p["history"][today], [10, 3])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["plan", "remove", "ds", "B", "--force"]), 0)
        self.assertEqual(out.getvalue().strip(), "ok: ds removed phase B")
        p = store.find_project("ds")
        self.assertEqual([x["name"] for x in p["phases"]], ["A", "Z", "W"])
        # ScopeBZ is left with Z alone, 0 units, so it goes; ScopeW (0 units, never named B) is not touched
        self.assertEqual(p["scopes"], [["ScopeA", ["A"]], ["ScopeAB", ["A"]], ["ScopeW", ["W"]]])
        self.assertEqual(p["history"][today], [4, 3])

        # issue #2's case: a duplicate phase that holds merged units goes with --force, and its units with it
        self.ok("plan", "remove", "ds", "A", "--force")
        p = store.find_project("ds")
        self.assertEqual(p["scopes"], [["ScopeW", ["W"]]])
        self.assertEqual(p["history"][today], [0, 0])

    def test_project_rules(self):
        self.ok("project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "DS")
        self.assertIn("already exists", self.fails("project", "add", "ds", "--product", "bk", "--kind", "loop", "--name", "x"))
        self.assertIn("no product", self.fails("project", "add", "x", "--product", "nope", "--kind", "run", "--name", "x"))
        self.assertIn("bad project id", self.fails("project", "add", "Bad/Id", "--product", "bk", "--kind", "run", "--name", "x"))
        self.assertIn("not loop", self.fails("loop", "start", "ds", "x"))
        p = self.ok("project", "set", "ds", "--next", "one", "--next", "two", "--pr", "12", "--pr", "a/b#3", "--match", "acme-ds/")
        self.assertEqual((p["next"], p["prs"], p["match"]), (["one", "two"], ["o/r#12", "a/b#3"], ["acme-ds/"]))
        self.assertEqual(self.ok("project", "set", "ds", "--pr", "o/r#0012")["prs"], ["o/r#12"])
        for bad in ("abc", "a#b#c", "#5", "../x#1"):
            self.assertIn("bad PR reference", self.fails("project", "set", "ds", "--pr", bad))
        self.assertEqual([x["id"] for x in self.ok("project", "list")], ["ds"])

    def test_loop(self):
        self.ok("project", "add", "rev", "--product", "bk", "--kind", "loop", "--name", "Revenue")
        self.assertIn("baseline first", self.fails("loop", "record", "rev", "x", "--value", "1", "--kept"))
        for bad in ("nan", "inf", "1e999", "x"):
            self.assertIn("bad number", self.fails("loop", "meta", "rev", "--baseline", bad))
        self.assertIn("must be above", self.fails("loop", "meta", "rev", "--baseline", "3", "--target", "3"))
        self.assertIn("must be above", self.fails("loop", "meta", "rev", "--baseline", "200", "--target", "100"))
        self.ok("loop", "meta", "rev", "--metric", "Revenue per 1k", "--unit", "€", "--baseline", "2.71", "--target", "3.5")
        self.ok("loop", "start", "rev", "Bigger CTA")
        self.ok("loop", "record", "rev", "Bigger CTA", "--value", "2.69", "--discarded")
        p = store.find_project("rev")
        self.assertEqual(p["exps"][0], ["Baseline", 2.71, 1])
        self.assertEqual(p["exps"][1][:3], ["Bigger CTA", 2.69, 0])
        self.assertIsNone(p["running"])

    def test_update(self):
        self.ok("project", "add", "rev", "--product", "bk", "--kind", "loop", "--name", "Revenue")
        row = self.ok("update", "rev", "--health", "at-risk", "--agent", "3K15", "Slipped two days")
        self.assertEqual(row["health"], "atRisk")
        self.assertEqual(store.read_lines("updates/rev.jsonl")[0]["text"], "Slipped two days")
        self.assertEqual(store.find_project("rev")["lastActive"], row["at"])

    def test_inbox_round_trip(self):
        self.ok("project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "DS")
        self.assertIn("not on the page yet", self.fails("ask", "--project", "ds", "--kind", "action", "--title", "x"))
        self.ok("plan", "phase", "ds", "A", "--total", "0", "--start", "2026-10-01", "--due", "2026-10-10")
        self.assertIn("0 units", self.fails("ask", "--project", "ds", "--kind", "action", "--title", "x"))
        self.ok("plan", "phase", "ds", "A", "--total", "1", "--start", "2026-10-01", "--due", "2026-10-10")
        item = self.ok("ask", "--project", "ds", "--kind", "urgent", "--title", "Which price?", "--option", "a=Higher",
                       "--option", "b=Store", "--reco", "b", "--why", "Money", "--from", "2O11")
        self.assertEqual(item["id"], "d1")
        action = self.ok("ask", "--project", "ds", "--kind", "action", "--title", "Rotate key")
        self.assertEqual(action["options"], [["done", "Done"], ["blocked", "Blocked"]])
        self.assertIn("not an option", self.fails("ask", "--project", "ds", "--kind", "urgent", "--title", "x",
                                                  "--option", "a=1", "--option", "b=2", "--reco", "c"))
        self.assertEqual(self.ok("answers"), [])
        self.assertEqual(serve.save_answers({"d1": {"choice": "z"}})[0], 400)
        self.assertEqual(serve.save_answers({"nope": {"choice": "a"}})[0], 404)
        self.assertEqual(serve.save_answers({"d1": {"choice": "a", "note": "x" * 5000}})[0], 400)
        code, body = serve.save_answers({"d1": {"choice": "a", "note": "go"}})
        self.assertEqual((code, body["d1"]["choice"]), (200, "a"))
        got = self.ok("answers")
        self.assertEqual([(x["id"], x["choiceLabel"], x["note"]) for x in got], [("d1", "Higher", "go")])
        self.assertEqual(self.ok("answers", "--since", got[0]["at"]), [])
        self.assertEqual(len(self.ok("answers", "--since", "2000-01-01T00:00:00+02:00")), 1)
        self.assertIn("bad time", self.fails("answers", "--since", "yesterday"))
        self.assertIn("--wait", self.fails("answers", "--wait", "nan"))
        self.ok("resolve", "d1")
        self.assertEqual(self.ok("answers"), [])
        box = store.read("inbox.json", {})
        self.assertEqual(box["resolved"][0]["answer"]["choice"], "a")
        self.assertIn("no open inbox item", self.fails("resolve", "d1"))

    def test_answers_filters(self):
        self.ok("project", "add", "bk-b", "--product", "bk", "--kind", "build", "--name", "Build")
        self.ok("plan", "phase", "bk-b", "A", "--total", "1", "--start", "2026-10-01", "--due", "2026-10-10")
        item1 = self.ok("ask", "--project", "bk-b", "--kind", "urgent", "--title", "First question?",
                        "--option", "a=Option A", "--option", "b=Option B", "--from", "ag1")
        self.assertEqual(item1["id"], "d1")
        item2 = self.ok("ask", "--project", "bk-b", "--kind", "urgent", "--title", "Second question?",
                        "--option", "a=Yes", "--option", "b=No", "--from", "ag2")
        self.assertEqual(item2["id"], "d2")

        # Answer d1 only; d2 remains unanswered
        code, _ = serve.save_answers({"d1": {"choice": "a", "note": "ok"}})
        self.assertEqual(code, 200)

        # (a) --id d2 with d1 answered and d2 unanswered: wait with a tiny timeout returns nothing
        self.assertEqual(self.ok("answers", "--id", "d2", "--wait", "0.05"), [])
        self.assertEqual(self.ok("answers", "--id", "d2"), [])

        # (b) d2 answered: returns d2 only
        code, _ = serve.save_answers({"d2": {"choice": "b", "note": "sure"}})
        self.assertEqual(code, 200)
        got_d2 = self.ok("answers", "--id", "d2")
        self.assertEqual([x["id"] for x in got_d2], ["d2"])
        self.assertEqual(got_d2[0]["choice"], "b")

        # Repeatable --id filter: both d1 and d2
        got_both = self.ok("answers", "--id", "d1", "--id", "d2")
        self.assertEqual([x["id"] for x in got_both], ["d1", "d2"])

        # (c) --from x filters by agent
        self.assertEqual([x["id"] for x in self.ok("answers", "--from", "ag1")], ["d1"])
        self.assertEqual([x["id"] for x in self.ok("answers", "--from", "ag2")], ["d2"])
        # Also test with handle "x" directly in inbox
        store.add_ask_item("bk-b", "urgent", "Third question?", "Short", "ctx", [["a", "A"], ["b", "B"]],
                           "a", "why", "x", "thread-1")
        code, _ = serve.save_answers({"d3": {"choice": "a", "note": "from-x"}})
        self.assertEqual(code, 200)
        self.assertEqual([x["id"] for x in self.ok("answers", "--from", "x")], ["d3"])

        # (d) no filter keeps the old behaviour
        all_answers = self.ok("answers")
        self.assertEqual([x["id"] for x in all_answers], ["d1", "d2", "d3"])

        # Both --id and --from filters together
        self.assertEqual([x["id"] for x in self.ok("answers", "--id", "d1", "--from", "ag1")], ["d1"])
        self.assertEqual(self.ok("answers", "--id", "d1", "--from", "ag2"), [])

        # unknown or resolved ids fail at once instead of waiting out the timeout
        self.assertIn("no open inbox item", self.fails("answers", "--id", "d99", "--wait", "60"))
        self.ok("resolve", "d1")
        self.assertIn("already resolved", self.fails("answers", "--id", "d1", "--wait", "60"))

    def test_resolve_with_corrupt_relay_json_warns_and_succeeds(self):
        self.ok("project", "add", "bk-b", "--product", "bk", "--kind", "build", "--name", "Build")
        self.ok("plan", "phase", "bk-b", "A", "--total", "1", "--start", "2026-10-01", "--due", "2026-10-10")
        self.ok("ask", "--project", "bk-b", "--kind", "urgent", "--title", "Corrupt relay test",
                "--option", "a=Yes", "--option", "b=No")
        # Corrupt relay.json
        store.write_text("relay.json", "{corrupt-json")

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["resolve", "d1"])

        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("ok: resolved d1", out.getvalue())
        self.assertIn("warning: cannot update relay outbox:", err.getvalue())

        box = store.read("inbox.json", {})
        self.assertEqual(len(box["items"]), 0)
        self.assertEqual(box["resolved"][0]["id"], "d1")

    def test_agent_set(self):
        self.ok("project", "add", "bk-run", "--product", "bk", "--kind", "run", "--name", "Run")
        a = self.ok("agent", "set", "4H01", "--who", "Hermes", "--provider", "LiteLLM", "--project", "bk-run", "--state", "running")
        self.assertEqual((a["product"], a["state"]), ("bk", "running"))
        self.assertEqual(self.ok("agent", "set", "4H01", "--state", "done")["who"], "Hermes")
        self.assertIn("needs --project", self.fails("agent", "set", "5X01", "--who", "x"))
        self.assertIn("bad agent handle", self.fails("agent", "set", "no way", "--product", "bk"))

    def test_key_file(self):
        k = store.key()
        self.assertEqual(os.stat(store.path(".key")).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(store.home()).st_mode & 0o777, 0o700)
        self.assertEqual(store.key(), k)
        with open(store.path(".key"), "w") as f:
            f.write("  \n")
        self.assertTrue(len(store.key()) > 20)  # an empty key file is replaced, never accepted

    def test_file_mode_0600(self):
        self.assertEqual(os.stat(store.path("products.json")).st_mode & 0o777, 0o600)

    def test_build_skipped(self):
        fake = ("/tmp/factory.html", {"written": False, "builtAt": "2026-10-27T12:00:00Z",
                                      "projects": [], "agents": [], "warnings": []})
        with mock.patch("factory.build.build", return_value=fake):
            code, obj, _ = run("build")
            self.assertFalse(obj["written"])
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli.main(["build"])
            self.assertTrue(out.getvalue().startswith("skipped:"))

    def test_corrupt_state_is_one_line(self):
        with open(store.path("inbox.json"), "w") as f:
            f.write("{broken")
        self.assertIn("not valid JSON", self.fails("answers"))

    def test_ds_notes_import(self):
        self.ok("project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "DS")
        notes, done = os.path.join(self.tmp.name, "n.json"), os.path.join(self.tmp.name, "d.json")
        with open(notes, "w") as f:
            json.dump({"decisions": [{"id": "d26", "title": "T", "context": "C", "options": [["a", "A"], ["b", "B"]],
                                      "reco": "a", "why": "Appliqué par défaut."},
                                     {"id": "d27", "title": "T2", "options": [["a", "A"], ["b", "B"]], "reco": "b", "why": ""}],
                       "actions": [{"id": "a09", "title": "Act"}, {"id": "a10", "title": "Act2"}]}, f)
        with open(done, "w") as f:
            json.dump({"a10": {"choice": "done"}}, f)
        got = self.ok("import", "ds-notes", "--notes", notes, "--answered", done)
        self.assertEqual(got["added"], ["ds-d26", "ds-d27", "ds-a09"])
        kinds = {x["id"]: x["kind"] for x in store.read("inbox.json", {})["items"]}
        self.assertEqual(kinds, {"ds-d26": "applied", "ds-d27": "urgent", "ds-a09": "action"})
        self.assertEqual(self.ok("import", "ds-notes", "--notes", notes, "--answered", done)["added"], [])
        # without flags, the paths come from kit.notes / kit.answered
        notes2 = os.path.join(self.tmp.name, "n2.json")
        with open(notes2, "w") as f:
            json.dump({"decisions": [], "actions": [{"id": "a11", "title": "New"}, {"id": "a12", "title": "Done"}]}, f)
        with open(done, "w") as f:
            json.dump({"a12": {"choice": "done"}}, f)
        self.ok("config", "set", "kit.notes", notes2)
        self.ok("config", "set", "kit.answered", done)
        self.assertEqual(self.ok("import", "ds-notes")["added"], ["ds-a11"])


class RunLaneTest(unittest.TestCase):
    def test_intake_vs_closed(self):
        issues = [
            {"number": 1, "title": "old open", "createdAt": "2026-09-01T00:00:00Z", "closedAt": None, "state": "OPEN"},
            {"number": 2, "title": "old, closed in window", "createdAt": "2026-09-02T00:00:00Z", "closedAt": "2026-10-20T00:00:00Z", "state": "CLOSED"},
            {"number": 3, "title": "old, closed before", "createdAt": "2026-09-03T00:00:00Z", "closedAt": "2026-10-01T00:00:00Z", "state": "CLOSED"},
            {"number": 4, "title": "new open", "createdAt": "2026-10-25T00:00:00Z", "closedAt": None, "state": "OPEN"},
        ]
        with mock.patch.object(sources, "gh_json", return_value=issues):
            daily, items = sources.run_lane({"repos": ["o/r"], "run_labels": ["bug"]}, date(2026, 10, 27))
        self.assertEqual(len(daily), 14)
        self.assertEqual(daily[0], [2, 0])      # 2026-10-14: issues 1 and 2 open
        self.assertEqual(daily[6], [2, 1])      # 2026-10-20: issue 2 closed
        self.assertEqual(daily[-1], [3, 1])     # issue 4 came in
        self.assertEqual([i[0] for i in items], ["#1", "#4"])

    def test_unit_status(self):
        u = lambda k: {"kitStatus": k}  # noqa: E731
        self.assertEqual(sources.unit_status(u("merged (pr #1081, `e8570d68`)"), []), "merged")
        self.assertEqual(sources.unit_status(u("done (answered 2026-10-11)"), []), "merged")
        self.assertEqual(sources.unit_status(u("in review"), []), "in-pr")
        self.assertEqual(sources.unit_status(u("todo"), [{"state": "OPEN"}]), "in-pr")

    def test_ci_state(self):
        self.assertEqual(sources.ci_of([]), "none")
        self.assertEqual(sources.ci_of([{"status": "COMPLETED", "conclusion": "SUCCESS"}]), "pass")
        self.assertEqual(sources.ci_of([{"status": "IN_PROGRESS", "conclusion": ""}]), "run")
        self.assertEqual(sources.ci_of([{"status": "COMPLETED", "conclusion": "FAILURE"}, {"state": "PENDING"}]), "fail")

    def test_week_dates(self):
        self.assertEqual(sources.week_dates("W1"), (date(2026, 10, 12), date(2026, 10, 23)))  # FAST-TRACK Phase 0
        self.assertEqual(sources.week_dates("W5"), (date(2026, 10, 26), date(2026, 10, 30)))
        self.assertEqual(sources.week_dates("end W18")[0], date(2027, 1, 25))
        self.assertEqual(sources.week_dates("W6 (moved by M1)")[0], date(2026, 11, 2))
        self.assertIsNone(sources.week_dates("conditional"))


class DevCheckoutGuardTest(unittest.TestCase):
    def test_guard_fires_in_dev_checkout_without_factory_home(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("FACTORY_HOME", None)
            with mock.patch("factory.store.LIVE_PATH", "/nonexistent/live/path"):
                with self.assertRaises(store.FactoryError) as cm:
                    store.home()
                want = (
                    "this is a development checkout: set FACTORY_HOME "
                    "(e.g. export FACTORY_HOME=$(mktemp -d)); "
                    "set FACTORY_HOME=~/.factory explicitly to use the live state"
                )
                self.assertEqual(str(cm.exception), want)

                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = cli.main(["product", "list"])
                self.assertEqual(code, 1)
                self.assertEqual(err.getvalue().strip(), f"factory: error: {want}")

    def test_guard_passes_with_explicit_factory_home(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"FACTORY_HOME": d}):
                with mock.patch("factory.store.LIVE_PATH", "/nonexistent/live/path"):
                    self.assertEqual(store.home(), d)

        with mock.patch.dict(os.environ, {"FACTORY_HOME": "~/.factory"}):
            with mock.patch("factory.store.LIVE_PATH", "/nonexistent/live/path"):
                self.assertEqual(store.home(), os.path.expanduser("~/.factory"))

    def test_guard_passes_when_under_live_path(self):
        pkg_dir = os.path.dirname(os.path.dirname(os.path.realpath(store.__file__)))
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("FACTORY_HOME", None)
            with mock.patch("factory.store.LIVE_PATH", pkg_dir):
                self.assertEqual(store.home(), os.path.expanduser("~/.factory"))

    def test_guard_refuses_worktree_inside_live_path(self):
        pkg_dir = os.path.dirname(os.path.dirname(os.path.realpath(store.__file__)))
        parent_dir = os.path.dirname(pkg_dir)
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop("FACTORY_HOME", None)
            with mock.patch("factory.store.LIVE_PATH", parent_dir):
                with self.assertRaises(store.FactoryError):
                    store.home()


if __name__ == "__main__":
    unittest.main()
