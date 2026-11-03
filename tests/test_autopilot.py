import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from factory import autopilot, build, cli, serve, sources, store, t3

NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)
WS = "/w/acme-monorepo"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--json", *argv])
    return code, (json.loads(out.getvalue()) if code == 0 and out.getvalue().strip() else None), err.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name

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

    def seed(self, pid, thread, goal="build the thing", every_hours=3, enabled=True, max_per_day=4,
             last_nudge_at=None, history=None, **cfg_kw):
        cfg = store.autopilot()
        for k, v in cfg_kw.items():
            cfg[k] = v
        cfg["projects"][pid] = {"thread": thread, "goal": goal, "every_hours": every_hours,
                                "enabled": enabled, "max_per_day": max_per_day,
                                "last_nudge_at": last_nudge_at, "history": history or []}
        store.save_autopilot(cfg)


class AutopilotRunTest(Base):
    def agents(self, **states):
        return {tid: {"state": s, "since": "2026-10-27T12:00:00Z"} for tid, s in states.items()}

    def test_nudges_a_due_idle_project_once(self):
        self.seed("ds", "idle1")
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async") as tr:
            summary, text = autopilot.run(now=NOW)
        self.assertEqual(summary["results"], {"ds": "nudged"})
        me.assert_called_once_with("idle1", autopilot.nudge_text("build the thing", "ds"), kind="autopilot", label=None)
        tr.assert_not_called()  # run enqueues only; the CLI delivers synchronously
        # not due again before every_hours
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async") as tr:
            summary, text = autopilot.run(now=NOW)
        self.assertEqual(summary["results"], {})
        me.assert_not_called()
        tr.assert_not_called()

    def test_skips_working_waiting_done_gone(self):
        self.seed("w", "t-working")
        self.seed("q", "t-waiting")
        self.seed("s", "t-stalled")
        self.seed("f", "t-failed")
        self.seed("g", "t-gone")
        self.seed("d", "thread:delegated-task:x")  # saved before delegates were refused
        agents = self.agents(**{"t-working": "running", "t-waiting": "waiting", "t-stalled": "stalled",
                                "t-failed": "failed", "thread:delegated-task:x": "done"})
        with mock.patch.object(autopilot, "agent_rows", return_value=agents), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            summary, _ = autopilot.run(now=NOW)
        self.assertEqual(summary["results"], {
            "d": autopilot.NOT_DRIVABLE_SKIP,
            "f": "skipped failed: check the session", "g": "skipped thread gone",
            "q": "skipped waiting on you", "s": "skipped working", "w": "skipped working"})
        me.assert_not_called()

    def test_skips_waiting_on_unresolved_inbox_item(self):
        self.seed("ds", "idle1")
        box = store.inbox()
        box["items"].append({"id": "a1", "project": "ds", "kind": "action", "title": "T",
                             "thread": "idle1", "options": [["done", "Done"], ["blocked", "Blocked"]]})
        store.write("inbox.json", box)
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            summary, _ = autopilot.run(now=NOW)
        self.assertEqual(summary["results"], {"ds": "skipped waiting on you: answer a1"})
        me.assert_not_called()
        # once answered it waits on the agent, not on you: the session is nudged
        store.write("answers.json", {"a1": {"choice": "blocked", "note": "", "at": "2026-10-27T13:00:00Z"}})
        self.assertEqual(autopilot.open_inbox_threads(), {})
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            summary, _ = autopilot.run(now=NOW)
        self.assertEqual(summary["results"], {"ds": "nudged"})
        me.assert_called_once()

    def test_per_project_cap(self):
        self.seed("ds", "idle1", max_per_day=1)
        cfg = store.autopilot()
        cfg["projects"]["ds"]["nudged_on"] = {NOW.date().isoformat(): 1}
        store.save_autopilot(cfg)
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            summary, _ = autopilot.run(now=NOW)
        self.assertEqual(summary["results"], {"ds": "skipped cap"})
        me.assert_not_called()

    def test_caps_do_not_depend_on_history_length(self):
        # a long history full of today's "nudged" rows must not cap: caps read nudged_on only
        self.seed("ds", "idle1", max_per_day=4,
                  history=[{"at": store.iso(NOW), "result": "nudged"} for _ in range(100)])
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            summary, _ = autopilot.run(now=NOW)
        self.assertEqual(summary["results"], {"ds": "nudged"})
        me.assert_called_once()

    def test_global_cap(self):
        self.seed("a", "idle-a")
        self.seed("b", "idle-b", daily_cap=1)
        with mock.patch.object(autopilot, "agent_rows",
                               return_value=self.agents(**{"idle-a": "done", "idle-b": "done"})), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            summary, _ = autopilot.run(now=NOW)
        self.assertEqual(summary["results"], {"a": "nudged", "b": "skipped cap"})
        me.assert_called_once()

    def test_paused_run_does_nothing(self):
        self.seed("ds", "idle1")
        cfg = store.autopilot()
        cfg["paused"] = True
        store.save_autopilot(cfg)
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            summary, text = autopilot.run(now=NOW)
        self.assertTrue(summary["paused"])
        self.assertEqual(summary["results"], {})
        self.assertIn("paused", text)
        me.assert_not_called()

    def test_word_list_makes_one_ask_per_day_and_no_nudge(self):
        self.seed("ds", "idle1", goal="Merge develop into main and deploy")
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            summary, _ = autopilot.run(now=NOW)
            summary2, _ = autopilot.run(now=NOW)
        self.assertEqual(summary["results"]["ds"], autopilot.NEEDS_YOU)
        # the open ask now targets the thread, so the next run sees it as waiting on you (still no nudge)
        self.assertEqual(summary2["results"]["ds"], "skipped waiting on you: answer a1")
        me.assert_not_called()
        asks = [it for it in store.inbox()["items"] if it["title"] == "Unblock autopilot: ds"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(asks[0]["kind"], "action")
        self.assertEqual(asks[0]["context"], "Merge develop into main and deploy")
        self.assertEqual(asks[0]["from"], "autopilot")
        self.assertEqual(asks[0]["thread"], "idle1")

    def test_word_list_whole_words_and_normalisation(self):
        for goal in ("merge to main", "PROD", "ｐｒｏｄ", "wallet", "payouts", "deploy"):
            self.assertTrue(autopilot.touches_sensitive(goal), goal)
        for goal in ("product", "reproduce", "productivity", "build the thing", ""):
            self.assertFalse(autopilot.touches_sensitive(goal), goal)

    def test_history_dedupes_repeated_skip(self):
        self.seed("ds", "t-working")
        agents = self.agents(**{"t-working": "running"})
        with mock.patch.object(autopilot, "agent_rows", return_value=agents), \
             mock.patch("factory.relay.enqueue_message"), \
             mock.patch("factory.relay.trigger_delivery_async"):
            autopilot.run(now=NOW)
            autopilot.run(now=NOW)
        self.assertEqual([h["result"] for h in store.autopilot()["projects"]["ds"]["history"]],
                         ["skipped working"])

    def test_history_keeps_every_nudge(self):
        self.seed("ds", "idle1", max_per_day=10)
        later = NOW + timedelta(hours=3)
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message"), \
             mock.patch("factory.relay.trigger_delivery_async"):
            autopilot.run(now=NOW)
            autopilot.run(now=later)
        self.assertEqual([h["result"] for h in store.autopilot()["projects"]["ds"]["history"]],
                         ["nudged", "nudged"])

    def test_skips_when_earlier_nudge_undelivered(self):
        self.seed("ds", "idle1")
        store.write("relay.json", [{"id": "m-1", "thread": "idle1", "kind": "autopilot",
                                    "state": "sent", "at": store.iso(NOW)}])
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            summary, _ = autopilot.run(now=NOW)
        self.assertEqual(summary["results"], {"ds": "skipped earlier nudge not delivered yet"})
        me.assert_not_called()

    def test_needs_you_ask_skips_open_item_from_any_day(self):
        self.seed("ds", "idle1", goal="merge to main")
        box = store.inbox()
        box["items"].append({"id": "a9", "project": "ds", "kind": "action",
                             "title": "Unblock autopilot: ds", "thread": "idle1",
                             "options": [["done", "Done"], ["blocked", "Blocked"]],
                             "at": "2026-10-20T00:00:00Z"})
        store.write("inbox.json", box)
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message"), \
             mock.patch("factory.relay.trigger_delivery_async"):
            autopilot.run(now=NOW)
        self.assertEqual(len([it for it in store.inbox()["items"]
                              if it["title"] == "Unblock autopilot: ds"]), 1)

    def test_needs_you_ask_skips_resolved_today(self):
        self.seed("ds", "idle1", goal="merge to main")
        box = store.inbox()
        box["resolved"].append({"id": "r1", "title": "Unblock autopilot: ds",
                                "resolvedAt": store.iso(NOW), "at": store.iso(NOW)})
        store.write("inbox.json", box)
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message"), \
             mock.patch("factory.relay.trigger_delivery_async"):
            summary, _ = autopilot.run(now=NOW)
        self.assertEqual(summary["results"]["ds"], autopilot.NEEDS_YOU)
        self.assertEqual([it for it in store.inbox()["items"]
                          if it["title"] == "Unblock autopilot: ds"], [])

    def test_nudge_text_uses_goal_source_and_no_owner_name(self):
        self.seed("ds", "idle1")
        cfg = store.autopilot()
        cfg["projects"]["ds"]["goal_source"] = "cli"
        store.save_autopilot(cfg)
        with mock.patch.object(autopilot, "agent_rows", return_value=self.agents(idle1="done")), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            autopilot.run(now=NOW)
        args, kwargs = me.call_args
        self.assertEqual(kwargs.get("kind"), "autopilot")
        self.assertIn("goal set from the CLI", args[1])
        self.assertNotIn("the owner", args[1])
        # default source is the dashboard
        self.assertIn("goal set from the dashboard", autopilot.nudge_text("g", "ds"))

    def test_t3_failure_skips_all_and_does_not_crash(self):
        self.seed("ds", "idle1")
        with mock.patch.object(autopilot, "agent_rows", side_effect=store.FactoryError("T3 database not found")), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            summary, _ = autopilot.run(now=NOW)
        self.assertEqual(summary["results"], {"ds": "T3 unreadable"})
        me.assert_not_called()

    def test_autopilot_shape_check_requires_fields(self):
        for field, bad in (("every_hours", "3"), ("max_per_day", "4"), ("enabled", 1),
                           ("history", "not-a-list"), ("history", ["not-a-dict"]),
                           ("nudged_on", "not-a-dict")):
            # the previous iteration left a corrupt file: start from a fresh valid config
            store.write("autopilot.json", {"paused": False, "pausedAt": None, "daily_cap": 12,
                                           "projects": {}})
            self.seed("ds", "t1")
            cfg = store.autopilot()
            cfg["projects"]["ds"][field] = bad
            store.save_autopilot(cfg)
            with self.assertRaises(store.FactoryError):
                store.autopilot()


class AutopilotCliTest(Base):
    def setUp(self):
        super().setUp()
        with mock.patch("sys.stdout"):
            cli.main(["product", "set", "bk", "--name", "Acme", "--workspace", WS])
            cli.main(["project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "DS"])

    def test_add_refuses_unknown_project_and_bad_interval(self):
        self.assertIn("no project 'nope'", self.fails("autopilot", "add", "nope", "--thread", "t", "--goal", "g"))
        self.assertIn("every_hours must be one of", self.fails("autopilot", "add", "ds", "--thread", "t", "--goal", "g", "--every", "5"))
        self.assertIn("--goal", self.fails("autopilot", "add", "ds", "--thread", "t", "--goal", "  "))

    def test_add_refuses_dead_or_subagent_thread(self):
        with mock.patch.object(autopilot, "agent_rows", return_value={}):
            self.assertIn("not a live T3 thread", self.fails("autopilot", "add", "ds", "--thread", "dead", "--goal", "g"))
            self.assertIn(autopilot.NOT_DRIVABLE, self.fails("autopilot", "add", "ds", "--thread", "thread:provider:x", "--goal", "g"))
            self.assertIn(autopilot.NOT_DRIVABLE, self.fails("autopilot", "add", "ds", "--thread", "thread:delegated-task:x", "--goal", "g"))
            # --force adds it anyway
            e = self.ok("autopilot", "add", "ds", "--thread", "dead", "--goal", "g", "--force", "--off")
            self.assertEqual((e["thread"], e["enabled"]), ("dead", False))
            # set --thread is checked the same way
            self.assertIn(autopilot.NOT_DRIVABLE, self.fails("autopilot", "set", "ds", "--thread", "thread:delegated-task:x"))

    def test_one_session_cannot_drive_two_projects(self):
        with mock.patch("sys.stdout"):
            cli.main(["project", "add", "ds2", "--product", "bk", "--kind", "build", "--name", "DS2"])
        with mock.patch.object(autopilot, "agent_rows", return_value={"t1": {}, "t2": {}}):
            self.ok("autopilot", "add", "ds", "--thread", "t1", "--goal", "g1")
            self.assertIn("already runs autopilot for 'ds'",
                          self.fails("autopilot", "add", "ds2", "--thread", "t1", "--goal", "g2"))
            self.ok("autopilot", "add", "ds2", "--thread", "t2", "--goal", "g2")
            self.assertIn("already runs autopilot for 'ds'", self.fails("autopilot", "set", "ds2", "--thread", "t1"))
            self.ok("autopilot", "set", "ds", "--thread", "t1")   # re-setting its own session is fine

    def test_add_set_done_remove_lifecycle(self):
        with mock.patch.object(autopilot, "agent_rows", return_value={"t1": {}}):
            e = self.ok("autopilot", "add", "ds", "--thread", "t1", "--goal", "g1")
            self.assertEqual((e["thread"], e["goal"], e["every_hours"], e["enabled"]), ("t1", "g1", 3, True))
            self.assertIn("already on autopilot", self.fails("autopilot", "add", "ds", "--thread", "t1", "--goal", "x"))
            e = self.ok("autopilot", "set", "ds", "--goal", "g2", "--every", "6", "--off")
            self.assertEqual((e["goal"], e["every_hours"], e["enabled"]), ("g2", 6, False))
            e = self.ok("autopilot", "set", "ds", "--on")
            self.assertTrue(e["enabled"])
        lst = self.ok("autopilot", "list")
        self.assertIn("ds", lst["projects"])
        self.ok("autopilot", "done", "ds")
        self.assertFalse(store.autopilot()["projects"]["ds"]["enabled"])
        self.assertEqual(store.autopilot()["projects"]["ds"]["history"][-1]["result"], "done")
        self.ok("autopilot", "remove", "ds")
        self.assertNotIn("ds", store.autopilot()["projects"])
        self.assertIn("not on autopilot", self.fails("autopilot", "set", "ds", "--goal", "x"))

    def test_pause_and_resume(self):
        self.ok("autopilot", "pause")
        self.assertTrue(store.autopilot()["paused"])
        self.assertIsNotNone(store.autopilot()["pausedAt"])
        self.ok("autopilot", "resume")
        self.assertFalse(store.autopilot()["paused"])

    def test_run_delivers_synchronously(self):
        with mock.patch.object(autopilot, "agent_rows", return_value={"t1": {"state": "done"}}):
            self.ok("autopilot", "add", "ds", "--thread", "t1", "--goal", "g1")
        with mock.patch.object(autopilot, "agent_rows", return_value={"t1": {"state": "done"}}), \
             mock.patch("factory.relay.enqueue_message"), \
             mock.patch("factory.relay.send_queued") as sq:
            self.ok("autopilot", "run")
        sq.assert_called_once()
        with mock.patch.object(autopilot, "agent_rows", return_value={"t1": {"state": "done"}}), \
             mock.patch("factory.relay.enqueue_message"), \
             mock.patch("factory.relay.send_queued") as sq:
            self.ok("autopilot", "run", "--dry-run")
        sq.assert_not_called()


class AutopilotServeTest(Base):
    def test_validation_400s(self):
        for patch in ([], None, "x", {"action": "bogus"}, {"action": "toggle", "project": 1},
                      {"action": "toggle", "project": "ds", "enabled": "yes"},
                      {"action": "save"}, {"action": "save", "project": "ds", "every_hours": 7},
                      {"action": "add", "project": "ds"}, {"action": "add", "project": "ds", "thread": "t",
                       "goal": "g", "every_hours": 9, "enabled": False},
                      {"action": "send"}):
            code, body = serve.save_autopilot(patch)
            self.assertEqual(code, 400, patch)
            self.assertIn("error", body)

    def test_add_validates_goal_and_thread(self):
        with mock.patch("sys.stdout"):
            cli.main(["product", "set", "bk", "--name", "Acme", "--workspace", WS])
            cli.main(["project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "DS"])
        code, body = serve.save_autopilot({"action": "add", "project": "nope", "thread": "t", "goal": "g", "enabled": True})
        self.assertEqual(code, 400)
        self.assertIn("no project", body["error"])
        with mock.patch.object(autopilot, "agent_rows", return_value={}):
            code, body = serve.save_autopilot({"action": "add", "project": "ds", "thread": "t", "goal": "g", "enabled": True})
            self.assertEqual(code, 400)
            self.assertIn("not a live T3 thread", body["error"])
        # a session already driven by another project is refused (two goals = two nudges a run)
        with mock.patch("sys.stdout"):
            cli.main(["project", "add", "ds2", "--product", "bk", "--kind", "build", "--name", "DS2"])
        with mock.patch.object(autopilot, "agent_rows", return_value={"t1": {"state": "done"}}):
            self.assertEqual(serve.save_autopilot({"action": "add", "project": "ds", "thread": "t1", "goal": "g"})[0], 200)
            code, body = serve.save_autopilot({"action": "add", "project": "ds2", "thread": "t1", "goal": "g"})
            self.assertEqual(code, 409)
            self.assertIn("already runs autopilot for 'ds'", body["error"])
        # a live delegate worker is still refused: autopilot drives the session that started it
        dlg = "thread:delegated-task:x"
        with mock.patch.object(autopilot, "agent_rows", return_value={dlg: {"state": "failed"}}):
            code, body = serve.save_autopilot({"action": "add", "project": "ds", "thread": dlg, "goal": "g", "enabled": True})
            self.assertEqual(code, 400)
            self.assertIn("delegate", body["error"])

    def test_only_top_level_sessions_are_drivable(self):
        self.assertTrue(autopilot.drivable("6f1c2d3e-top-level"))
        for t in ("thread:delegated-task:x", "thread:provider:claudeAgent:sub", "thread:project:p1:run1", None, ""):
            self.assertFalse(autopilot.drivable(t), t)

    def test_add_toggle_save_pause_resume(self):
        with mock.patch("sys.stdout"):
            cli.main(["product", "set", "bk", "--name", "Acme", "--workspace", WS])
            cli.main(["project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "DS"])
        with mock.patch.object(autopilot, "agent_rows", return_value={"t1": {}}):
            code, body = serve.save_autopilot({"action": "add", "project": "ds", "thread": "t1",
                                               "goal": "g1", "every_hours": 6, "enabled": False})
        self.assertEqual(code, 200)
        self.assertEqual((body["goal"], body["every_hours"], body["enabled"]), ("g1", 6, False))
        code, body = serve.save_autopilot({"action": "toggle", "project": "ds", "enabled": True})
        self.assertEqual((code, body["enabled"]), (200, True))
        code, body = serve.save_autopilot({"action": "save", "project": "ds", "goal": "g2", "every_hours": 12})
        self.assertEqual((code, body["goal"], body["every_hours"]), (200, "g2", 12))
        code, body = serve.save_autopilot({"action": "pause"})
        self.assertEqual((code, body["paused"]), (200, True))
        code, body = serve.save_autopilot({"action": "resume"})
        self.assertEqual((code, body["paused"]), (200, False))

    def test_send_on_waiting_thread_skips(self):
        self.seed("ds", "wait1")
        with mock.patch.object(autopilot, "agent_rows", return_value={"wait1": {"state": "waiting"}}), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            code, body = serve.save_autopilot({"action": "send", "project": "ds"})
        self.assertEqual(code, 200)
        self.assertEqual((body["result"], body["delivered"]), ("skipped waiting on you", False))
        me.assert_not_called()
        # an open inbox item on the session: the answer names it, so the page can ask "Send anyway?"
        box = store.inbox()
        box["items"].append({"id": "d45", "project": "ds", "kind": "urgent", "title": "T", "thread": "wait1",
                             "options": [["a", "A"]]})
        store.write("inbox.json", box)
        with mock.patch.object(autopilot, "agent_rows", return_value={"wait1": {"state": "done"}}), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            code, body = serve.save_autopilot({"action": "send", "project": "ds"})
        self.assertEqual((body["result"], body["delivered"], body["waitingOn"]),
                         ("skipped waiting on you: answer d45", False, ["d45"]))
        me.assert_not_called()

    def test_confirmed_send_on_waiting_thread_nudges(self):
        self.seed("ds", "wait1")
        with mock.patch.object(autopilot, "agent_rows", return_value={"wait1": {"state": "waiting"}}), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            code, body = serve.save_autopilot({"action": "send", "project": "ds", "confirmed": True})
        self.assertEqual((code, body["result"], body["delivered"]), (200, "nudged", True))
        me.assert_called_once()
        self.assertEqual(me.call_args.kwargs["label"], "forced")  # the relay lets it through to a waiting session

    def test_send_idle_nudges(self):
        self.seed("ds", "idle1")
        with mock.patch.object(autopilot, "agent_rows", return_value={"idle1": {"state": "done"}}), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async") as tr:
            code, body = serve.save_autopilot({"action": "send", "project": "ds"})
        self.assertEqual(code, 200)
        self.assertEqual((body["result"], body["delivered"]), ("nudged", True))
        me.assert_called_once_with("idle1", autopilot.nudge_text("build the thing", "ds"), kind="autopilot", label=None)
        tr.assert_called_once()

    def test_send_respects_pause_and_disabled(self):
        self.seed("ds", "idle1")
        cfg = store.autopilot()
        cfg["paused"] = True
        store.save_autopilot(cfg)
        with mock.patch.object(autopilot, "agent_rows", return_value={"idle1": {"state": "done"}}), \
             mock.patch("factory.relay.enqueue_message") as me:
            code, body = serve.save_autopilot({"action": "send", "project": "ds"})
        self.assertEqual((code, body["result"], body["delivered"]), (200, "paused", False))
        me.assert_not_called()
        cfg = store.autopilot()
        cfg["paused"] = False
        cfg["projects"]["ds"]["enabled"] = False
        store.save_autopilot(cfg)
        with mock.patch.object(autopilot, "agent_rows", return_value={"idle1": {"state": "done"}}), \
             mock.patch("factory.relay.enqueue_message") as me:
            code, body = serve.save_autopilot({"action": "send", "project": "ds"})
        self.assertEqual((code, body["result"], body["delivered"]),
                         (200, "skipped: autopilot off for this project", False))
        me.assert_not_called()

    def test_send_skips_undelivered_nudge(self):
        self.seed("ds", "idle1")
        store.write("relay.json", [{"id": "m-1", "thread": "idle1", "kind": "autopilot",
                                    "state": "queued", "at": store.iso(NOW)}])
        with mock.patch.object(autopilot, "agent_rows", return_value={"idle1": {"state": "done"}}), \
             mock.patch("factory.relay.enqueue_message") as me, \
             mock.patch("factory.relay.trigger_delivery_async"):
            code, body = serve.save_autopilot({"action": "send", "project": "ds"})
        self.assertEqual((code, body["result"], body["delivered"]),
                         (200, "skipped earlier nudge not delivered yet", False))
        me.assert_not_called()


class AutopilotBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)

    def test_corrupt_autopilot_json_warns_and_still_builds(self):
        with mock.patch("sys.stdout"):
            cli.main(["product", "set", "shop", "--name", "Shop"])
        store.write_text("autopilot.json", "{corrupt")
        with mock.patch.object(build, "agents_data", return_value=([], None, {}, [], [])), \
             mock.patch.object(build, "skills_data", return_value=[]), \
             mock.patch.object(sources, "prs_for", return_value={}), \
             mock.patch.object(t3, "thread_handles", return_value={}):
            data = build.collect(NOW)
        self.assertTrue(any("autopilot.json" in w for w in data["warnings"]))
        self.assertEqual(data["autopilot"]["projects"], [])
        self.assertIsNotNone(data["autopilot"]["error"])
        out = build.render(data)
        self.assertTrue(data["written"])
        self.assertTrue(os.path.exists(out))

    def test_clean_autopilot_reaches_the_page(self):
        with mock.patch("sys.stdout"):
            cli.main(["product", "set", "shop", "--name", "Shop"])
        cfg = store.autopilot()
        cfg["projects"]["ds"] = {"thread": "t1", "goal": "build it", "every_hours": 6, "enabled": True,
                                 "max_per_day": 4, "last_nudge_at": "2026-10-27T10:00:00Z", "history": []}
        store.save_autopilot(cfg)
        with mock.patch.object(build, "agents_data", return_value=([], None, {}, [], [])), \
             mock.patch.object(build, "skills_data", return_value=[]), \
             mock.patch.object(sources, "prs_for", return_value={}), \
             mock.patch.object(t3, "thread_handles", return_value={}), \
             mock.patch.object(autopilot, "agent_rows", return_value={}):
            data = build.collect(NOW)
        self.assertIsNone(data["autopilot"]["error"])
        self.assertEqual(data["autopilot"]["projects"][0]["project"], "ds")
        self.assertEqual(data["autopilot"]["projects"][0]["state"], "gone")
        self.assertEqual(data["autopilot"]["projects"][0]["next_due"], "2026-10-27T16:00:00Z")

    def _ap_data(self, agent_rows=None, t3_error=None):
        warnings = []
        if t3_error is not None:
            with mock.patch.object(autopilot, "agent_rows", side_effect=t3_error):
                return build.autopilot_data({}, [], NOW, warnings)
        with mock.patch.object(autopilot, "agent_rows", return_value=agent_rows or {}):
            return build.autopilot_data({}, [], NOW, warnings)

    def test_build_open_inbox_thread_shows_waiting(self):
        cfg = store.autopilot()
        cfg["projects"]["ds"] = {"thread": "t1", "goal": "build it", "every_hours": 6, "enabled": True,
                                 "max_per_day": 4, "last_nudge_at": None, "history": []}
        store.save_autopilot(cfg)
        box = store.inbox()
        box["items"].append({"id": "a1", "project": "ds", "kind": "action", "title": "T",
                             "thread": "t1", "options": [["done", "Done"], ["blocked", "Blocked"]]})
        store.write("inbox.json", box)
        data = self._ap_data(agent_rows={"t1": {"state": "done", "since": "2026-10-27T12:00:00Z", "tokens": 5}})
        self.assertEqual(data["projects"][0]["state"], "waiting")
        self.assertEqual(data["projects"][0]["stateLabel"], "Waiting on you: a1")
        self.assertEqual(data["projects"][0]["waitingOn"], ["a1"])
        self.assertEqual(autopilot.name_ids(["a", "b", "c", "d", "e"]), "a, b, c +2 more")

    def test_build_t3_error_sets_t3error_and_unknown(self):
        cfg = store.autopilot()
        cfg["projects"]["ds"] = {"thread": "t1", "goal": "build it", "every_hours": 6, "enabled": True,
                                 "max_per_day": 4, "last_nudge_at": None, "history": []}
        store.save_autopilot(cfg)
        data = self._ap_data(t3_error=store.FactoryError("T3 database not found"))
        self.assertEqual(data["t3Error"], "T3 database not found")
        self.assertEqual(data["projects"][0]["state"], "unknown")
        self.assertEqual(data["projects"][0]["stateLabel"], "Unknown")

    def test_build_state_labels_match_run(self):
        cfg = store.autopilot()
        cfg["projects"]["ds"] = {"thread": "done", "goal": "build it", "every_hours": 6, "enabled": True,
                                 "max_per_day": 4, "last_nudge_at": None, "history": []}
        store.save_autopilot(cfg)
        data = self._ap_data(agent_rows={"done": {"state": "done", "since": "2026-10-27T11:00:00Z", "tokens": None}})
        self.assertEqual(data["projects"][0]["stateLabel"], "Idle 3h")
        self.assertEqual(data["projects"][0]["nudge"], "warm")

    def test_build_footer_summary(self):
        cfg = store.autopilot()
        cfg["projects"]["ds"] = {"thread": "t1", "goal": "build it", "every_hours": 6, "enabled": True,
                                 "max_per_day": 4, "last_nudge_at": None,
                                 "history": [{"at": "2026-10-27T12:00:00Z", "result": "nudged"}]}
        store.save_autopilot(cfg)
        data = self._ap_data(agent_rows={"t1": {"state": "done", "since": "2026-10-27T12:00:00Z", "tokens": None}})
        self.assertEqual(data["last_nudged"], 1)
        self.assertEqual(data["last_skipped"], 0)
        self.assertIsNotNone(data["next_run"])


class AutopilotRound2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("FACTORY_HOME", None)

    def test_word_stems_catch_inflections_not_product(self):
        for g in ("get PR #12 merged", "finish the deployment", "deploying the API", "migrate the users table",
                  "releasing 1.2", "pay the creators", "merge to main", "PROD", "ｐｒｏｄ",
                  "rotate PROD_API_KEY", "fix the prodDeploy job", "update payment_webhook handler",
                  "wallet_service refactor", "productionize the API", "creators get paid out", "payroll export",
                  "run the prisma migration", "db migrate on staging", "write migrations"):
            self.assertTrue(autopilot.touches_sensitive(g), g)
        for g in ("Finish the product page", "Reproduce the flaky test", "productivity charts", "display name",
                  "Migrate Button and Tag to DS tokens", "Pay attention to edge cases", "ProductCard grid"):
            self.assertFalse(autopilot.touches_sensitive(g), g)

    def test_idle_over_a_day_thread_has_since_and_build_does_not_crash(self):
        from tests import test_t3
        db = os.path.join(self.tmp.name, "statev2.sqlite")
        test_t3.fixture(db)
        with mock.patch.dict(os.environ, {"FACTORY_T3_DB": db}):
            rows = autopilot.agent_rows(test_t3.PRODUCTS, test_t3.PROJECTS, test_t3.NOW)
            self.assertEqual(rows["old"]["state"], "done")
            self.assertTrue(rows["old"]["since"].startswith("2026-10-20"))
            store.save_autopilot({"paused": False, "pausedAt": None, "daily_cap": 12, "projects": {
                "ds": {"thread": "old", "goal": "build it", "every_hours": 3, "enabled": True,
                       "max_per_day": 4, "history": []}}})
            warnings = []
            data = build.autopilot_data(test_t3.PRODUCTS, test_t3.PROJECTS, test_t3.NOW, warnings)
        self.assertIn("Idle", json.dumps(data))

    def _cfg(self, paused=False, enabled=True):
        store.save_autopilot({"paused": paused, "pausedAt": None, "daily_cap": 12, "projects": {
            "ds": {"thread": "t1", "goal": "build it", "every_hours": 3, "enabled": enabled,
                   "max_per_day": 4, "history": []}}})

    def test_delivery_rechecks_pause_project_and_state(self):
        rows = {"t1": {"state": "done"}}
        with mock.patch.object(autopilot, "agent_rows", return_value=rows):
            self._cfg()
            self.assertTrue(autopilot.deliverable("t1"))
            self.assertFalse(autopilot.deliverable("other"))           # not an autopilot thread
            self._cfg(paused=True)
            self.assertFalse(autopilot.deliverable("t1"))
            self._cfg(enabled=False)                                   # turned off or marked done
            self.assertFalse(autopilot.deliverable("t1"))
            self._cfg()
            rows["t1"] = {"state": "waiting"}
            self.assertFalse(autopilot.deliverable("t1"))
            self.assertTrue(autopilot.deliverable("t1", forced=True))  # confirmed Send anyway
            rows["t1"] = {"state": "running"}
            self.assertFalse(autopilot.deliverable("t1", forced=True))
        with mock.patch.object(autopilot, "agent_rows", side_effect=store.FactoryError("no T3")):
            self.assertIsNone(autopilot.deliverable("t1"))             # keep it queued
        # a nudge queued to a delegate before delegates were refused is dropped, not sent
        dlg = "thread:delegated-task:x"
        store.save_autopilot({"paused": False, "pausedAt": None, "daily_cap": 12, "projects": {
            "ds": {"thread": dlg, "goal": "g", "every_hours": 3, "enabled": True, "max_per_day": 4, "history": []}}})
        with mock.patch.object(autopilot, "agent_rows", return_value={dlg: {"state": "done"}}):
            self.assertFalse(autopilot.deliverable(dlg))

    def test_repeated_skip_keeps_first_and_last_time(self):
        e = {"history": []}
        t0 = datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc)
        autopilot._record(e, "skipped working", t0)
        autopilot._record(e, "skipped working", t0 + timedelta(hours=10))
        self.assertEqual(len(e["history"]), 1)
        self.assertEqual((e["history"][0]["since"], e["history"][0]["at"]), ("2026-11-01T08:00:00Z", "2026-11-01T18:00:00Z"))

    def test_shape_rejects_zero_interval_bool_and_bad_daily_cap(self):
        base = {"thread": "t", "goal": "g", "every_hours": 3, "enabled": True, "max_per_day": 4, "history": []}
        for bad in ({**base, "every_hours": 0}, {**base, "every_hours": True}):
            store.write("autopilot.json", {"paused": False, "projects": {"p": bad}})
            with self.assertRaises(store.FactoryError):
                store.autopilot()
        store.write("autopilot.json", {"paused": False, "daily_cap": "12", "projects": {"p": base}})
        with self.assertRaises(store.FactoryError):
            store.autopilot()


if __name__ == "__main__":
    unittest.main()


def _bk_products():
    return {"bk": {"name": "B", "workspaces": ["/w/bk-repo"]}}


class AgentRowsTest(unittest.TestCase):
    """States come from the real T3 tables, including threads agents() leaves out."""

    def test_idle_long_and_other_product_threads_get_their_real_state(self):
        from tests import test_t3
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "statev2.sqlite")
            test_t3.fixture(db)
            with mock.patch.dict(os.environ, {"FACTORY_T3_DB": db}):
                rows = autopilot.agent_rows(test_t3.PRODUCTS, test_t3.PROJECTS, test_t3.NOW)
        state = {tid: r["state"] for tid, r in rows.items()}
        self.assertEqual(state["old"], "done")            # idle for a week: agents() drops it, still nudgeable
        self.assertEqual(state["elsewhere"], "running")   # outside every product: never defaulted to idle
        self.assertEqual(state["orch"], "done")           # finished its turn
        self.assertEqual(state["thread:delegated-task:a"], "running")
        self.assertNotIn("arch", state)                   # archived: thread gone

    def test_idle_sessions_are_offered_with_handle_title_and_project(self):
        """The Add drawer must offer a session idle for days: that is the one autopilot nudges."""
        from tests import test_t3
        from factory import build
        projects = [{"id": "ds", "product": "bk", "match": ["last week"]}]
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "statev2.sqlite")
            test_t3.fixture(db)
            with mock.patch.dict(os.environ, {"FACTORY_T3_DB": db}):
                rows = autopilot.agent_rows(test_t3.PRODUCTS, projects, test_t3.NOW)
        old = rows["old"]
        self.assertEqual((old["task"], old["product"], old["project"]), ("Done last week", "bk", "ds"))
        self.assertTrue(old["hc"].startswith("1"))
        offered = {r["thread"] for r in build.autopilot_sessions(rows)}
        self.assertIn("old", offered)
        self.assertTrue(all(autopilot.drivable(t) for t in offered))   # no delegates or subagents
        rows["old"]["state"] = "failed"
        self.assertNotIn("old", {r["thread"] for r in build.autopilot_sessions(rows)})
        rows["old"]["state"] = "unknown"
        self.assertNotIn("old", {r["thread"] for r in build.autopilot_sessions(rows)})
        # a session outside every product folder is still offered (own group in the drawer)
        self.assertIn("elsewhere", offered)
        self.assertIsNone(next(r for r in build.autopilot_sessions(rows) if r["thread"] == "elsewhere")["product"])

    def test_product_from_title_when_started_outside_every_product_folder(self):
        """Sessions named <product>/<type>/<feature> but started in the parent folder of all repos."""
        info = {"pid": "p-parent", "title": "bk/feat/support-bot", "payload": {}}
        roots = {"p-parent": "/w"}
        self.assertEqual(t3.thread_product(info, {}, roots, _bk_products()), "bk")
        self.assertIsNone(t3.thread_product({**info, "title": "chatwoot/support/x"}, {}, roots, _bk_products()))
        self.assertIsNone(t3.thread_product({**info, "title": "bk"}, {}, roots, _bk_products()))  # no "/": not a name
        # a delegate inherits its root session's product
        threads = {"root": {"title": "bk/fix/thing"}}
        child = {"pid": "p-parent", "title": "Fix one test", "payload": {"lineage": {"rootThreadId": "root"}}}
        self.assertEqual(t3.thread_product(child, threads, roots, _bk_products()), "bk")
        projects = [{"id": "bot", "product": "bk", "match": ["support-bot"]}]
        self.assertEqual(t3.thread_project(info, {}, roots, _bk_products(), projects), "bot")

    def test_fallback_unknown_status_is_unknown_not_done(self):
        from tests import test_t3
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "statev2.sqlite")
            test_t3.fixture(db)
            with mock.patch.dict(os.environ, {"FACTORY_T3_DB": db}):
                import sqlite3
                con = sqlite3.connect(db)
                con.execute("insert into orchestration_v2_projection_threads values (?,?,?,?,?,?,?,?)",
                            ("thread:elsewhere-unknown", "p2", "Mystery", "2026-10-27T13:00:00Z",
                             "2026-10-27T13:00:00Z", None, None, "{}"))
                con.execute("insert into orchestration_v2_projection_runs values (?,?,?,?,?,?)",
                            ("thread:elsewhere-unknown", "teleported", "2026-10-27T13:00:00Z", None,
                             '{"modelSelection":{"model":"x"}}', "claudeAgent"))
                con.commit()
                con.close()
                rows = autopilot.agent_rows(test_t3.PRODUCTS, test_t3.PROJECTS, test_t3.NOW)
        self.assertEqual(rows["thread:elsewhere-unknown"]["state"], "unknown")

    def test_agent_rows_closes_connection(self):
        con = mock.Mock()
        con.execute.return_value = []
        with mock.patch.object(t3, "connect", return_value=con), \
             mock.patch.object(t3, "agents", return_value=([], {})), \
             mock.patch.object(t3, "load_threads", return_value=({}, {})):
            autopilot.agent_rows({}, {}, NOW)
        con.close.assert_called_once()
