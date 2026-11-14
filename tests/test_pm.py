import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from factory import autopilot, cli, pm, roadmap, serve, store

NOW = datetime(2026, 11, 12, 8, 0, tzinfo=timezone.utc)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--json", *argv])
    return code, (json.loads(out.getvalue()) if code == 0 and out.getvalue().strip() else None), err.getvalue()


def phase(name, merged, total, due="2026-12-01"):
    return {"name": name, "merged": merged, "inPR": 0, "blocked": 0, "total": total, "start": "2026-10-01", "due": due}


def iso(days_ago):
    return store.iso(NOW - timedelta(days=days_ago))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_HOME")
        store.write("products.json", {"acme": {"name": "Acme", "workspaces": [], "repos": ["acme/app"], "run_labels": []}})

    def project(self, pid, phases=None, kind="build", **kw):
        p = {"id": pid, "product": "acme", "kind": kind, "name": f"Project {pid}", "feat": "", "prio": None, "next": [],
             "prs": [], "match": [], "createdAt": iso(1), "phases": phases if phases is not None else [phase("One", 1, 4)],
             "scopes": [], "added": 0, "history": {}, **kw}
        store.save_project(p)
        return p

    def update(self, pid, days_ago):
        store.append_line(f"updates/{pid}.jsonl", {"at": iso(days_ago), "agent": "ab", "health": "onTrack", "text": "x"})

    def groups(self, now=NOW):
        r = pm.sweep(now)
        return {(f["group"], f["project"]) for f in r["findings"]}


class Sweep(Base):
    def test_empty_home_is_clean(self):
        r = pm.sweep(NOW)
        self.assertEqual((r["total"], r["findings"]), (0, []))

    def test_finished_needs_every_phase_merged_and_units(self):
        self.project("done", [phase("A", 4, 4), phase("B", 2, 2)])
        self.project("half", [phase("A", 4, 4), phase("B", 1, 2)])
        self.project("empty", [])
        f = pm.sweep(NOW)["findings"]
        self.assertEqual([(x["group"], x["project"]) for x in f if x["group"] == "finished"], [("finished", "done")])
        done = next(x for x in f if x["group"] == "finished")
        self.assertEqual([(a["action"], a.get("days")) for a in done["actions"]], [("close", None), ("snooze", 7)])
        self.assertEqual(done["evidence"], "6/6 units merged")

    def test_closed_project_is_not_finished_or_quiet(self):
        self.project("old", [phase("A", 4, 4)], closedAt=iso(1), createdAt=iso(60))
        self.project("old2", [phase("A", 1, 4, due="2026-10-01")], closedAt=iso(1), createdAt=iso(60))
        self.assertEqual(self.groups(), set())

    def test_quiet_after_quiet_days_and_updates_reset_it(self):
        self.project("fresh", createdAt=iso(7))  # exactly 7 days: not past the limit
        self.project("stale", createdAt=iso(8))
        self.project("revived", createdAt=iso(30))
        self.update("revived", 2)
        self.project("planned", createdAt=iso(30), history={"2026-11-10": [4, 1]})
        self.assertEqual(self.groups(), {("quiet", "stale")})
        stale = next(f for f in pm.sweep(NOW)["findings"] if f["project"] == "stale")
        self.assertEqual(stale["evidence"], "no update, plan change or result since 4 Nov (8 days)")

    def test_quiet_days_is_config(self):
        self.project("stale", createdAt=iso(8))
        store.write("config.json", {"pm": {"quiet_days": 10}})
        self.assertEqual(self.groups(), set())
        store.write("config.json", {"pm": {"quiet_days": "3"}})  # `factory config set` stores a string
        self.assertEqual(self.groups(), {("quiet", "stale")})
        store.write("config.json", {"pm": {"quiet_days": 0}})
        with self.assertRaises(store.FactoryError):
            pm.sweep(NOW)

    def test_a_project_with_no_dates_is_not_quiet(self):
        self.project("nodates", createdAt=None)
        self.assertEqual(self.groups(), set())

    def test_late_phase_due_before_today_and_unfinished(self):
        self.project("late", [phase("A", 4, 4, due="2026-10-01"), phase("B", 2, 4, due="2026-11-10"), phase("C", 0, 2, due="2026-11-11")])
        self.project("today", [phase("A", 1, 4, due="2026-11-12")])  # due today is not late
        self.assertEqual(self.groups(), {("late", "late")})
        f = pm.sweep(NOW)["findings"][0]
        self.assertEqual(f["evidence"], "phase B: 2/4 units, due 10 Nov (+1 more late)")

    def test_nudge_is_disabled_when_not_on_autopilot(self):
        self.project("late", [phase("A", 1, 4, due="2026-10-01")])
        nudge = pm.sweep(NOW)["findings"][0]["actions"][0]
        self.assertEqual(nudge["action"], "nudge")
        self.assertIn("autopilot", nudge["disabled"])
        store.write("autopilot.json", {"paused": False, "pausedAt": None, "daily_cap": 12, "projects": {"late": {
            "thread": "t1", "goal": "g", "every_hours": 4, "max_per_day": 2, "enabled": True}}})
        self.assertIsNone(pm.sweep(NOW)["findings"][0]["actions"][0]["disabled"])

    def protos(self, status="validated", project="shipped"):
        store.save_protos([{"id": "acme-panel", "product": "acme", "feature": "Panel", "project": project, "versions": [
            {"v": 2, "status": status, "date": "2026-11-05T10:00:00Z"}, {"v": 1, "status": "superseded", "date": "2026-11-01T10:00:00Z"}]}])

    def test_validated_proto_of_a_done_project_not_built(self):
        self.project("shipped", [phase("A", 2, 2)])
        self.protos()
        f = [x for x in pm.sweep(NOW)["findings"] if x["group"] == "proto"][0]
        self.assertEqual(f["project"], "shipped")
        self.assertEqual(f["evidence"], "validated 2026-11-05, project done, prototype not marked built: check the screen shipped")
        self.assertEqual([a["action"] for a in f["actions"]], ["open-proto", "proto-built"])
        self.assertEqual(f["actions"][1]["proto"], "acme-panel")
        self.assertEqual((f["actions"][0]["proto"], f["actions"][0]["v"]), ("acme-panel", 2))

    def test_proto_finding_id_is_per_prototype_and_closed_projects_are_skipped(self):
        self.project("shipped", [phase("A", 2, 2)])
        self.protos()
        self.assertEqual([x["id"] for x in pm.sweep(NOW)["findings"] if x["group"] == "proto"], ["proto:acme-panel"])
        pm.close("shipped")
        self.assertEqual([x for x in pm.sweep(NOW)["findings"] if x["group"] == "proto"], [])

    def test_autopilot_never_nudges_a_closed_project(self):
        self.project("done1", [phase("A", 2, 2)])
        cfg = {"paused": False, "daily_cap": 12, "projects": {"done1": {"thread": "t1", "goal": "g", "every_hours": 4,
                                                                            "max_per_day": 2, "enabled": True, "history": []}}}
        pm.close("done1")
        with mock.patch.object(autopilot, "drivable", return_value=True):
            result, action = autopilot.decide(cfg, "done1", NOW, {"t1": {"state": "done"}}, {}, True, False)
        self.assertEqual((result, action), ("skipped project closed", None))

    def test_proto_not_flagged_when_built_waiting_or_project_unfinished(self):
        self.project("shipped", [phase("A", 2, 2)])
        for status in ("built", "waiting"):
            self.protos(status)
            self.assertNotIn("proto", {g for g, _ in self.groups()})
        self.project("shipped", [phase("A", 1, 2)])
        self.protos()
        self.assertNotIn("proto", {g for g, _ in self.groups()})
        self.protos(project=None)
        self.assertNotIn("proto", {g for g, _ in self.groups()})

    def test_snooze_hides_every_row_until_it_ends(self):
        self.project("stale", createdAt=iso(20))
        until = pm.snooze("stale", 3, NOW)
        self.assertEqual(until, "2026-11-15T08:00:00Z")
        self.assertEqual(self.groups(NOW + timedelta(days=2, hours=23)), set())
        self.assertEqual(self.groups(NOW + timedelta(days=3, minutes=1)), {("quiet", "stale")})
        pm.snooze("stale", 0, NOW)  # 0 ends it
        self.assertEqual(self.groups(), {("quiet", "stale")})
        self.assertEqual(store.read("pm.json", None)["snoozed"], {})

    def test_record_drops_ended_snoozes_and_keeps_last_sweep(self):
        self.project("a", createdAt=iso(20))
        self.project("b", createdAt=iso(20))
        pm.snooze("a", 1, NOW - timedelta(days=5))
        pm.snooze("b", 5, NOW)
        pm.record(pm.sweep(NOW))
        st = store.read("pm.json", None)
        self.assertEqual(list(st["snoozed"]), ["b"])
        self.assertEqual(st["lastSweep"]["total"], 1)  # a: its snooze ended, so it is quiet again

    def test_corrupt_state_is_refused(self):
        store.write("pm.json", {"snoozed": [], "lastSweep": {}})
        with self.assertRaises(store.FactoryError):
            pm.sweep(NOW)


class Close(Base):
    def test_close_and_reopen_through_the_cli(self):
        self.project("a")
        code, p, err = run("project", "close", "a", "--note", "shipped in prod")
        self.assertEqual(code, 0, err)
        self.assertTrue(p["closedAt"])
        self.assertEqual(store.find_project("a")["closeNote"], "shipped in prod")
        self.assertEqual(run("project", "list")[1], [])  # hidden by default
        self.assertEqual([x["id"] for x in run("project", "list", "--all")[1]], ["a"])
        self.assertNotEqual(run("project", "close", "a")[0], 0)  # already closed
        code, p, _ = run("project", "reopen", "a")
        self.assertEqual(code, 0)
        self.assertNotIn("closedAt", store.find_project("a"))
        self.assertNotIn("closeNote", store.find_project("a"))
        self.assertNotEqual(run("project", "reopen", "a")[0], 0)  # not closed
        self.assertEqual([x["id"] for x in run("project", "list")[1]], ["a"])

    def test_close_unknown_project(self):
        code, _, err = run("project", "close", "nope")
        self.assertEqual(code, 1)
        self.assertIn("no project", err)

    def test_closed_project_still_in_the_build_with_closedAt(self):
        from factory import build
        self.project("a")
        pm.close("a", "done", NOW)
        row = build.project_data(store.find_project("a"), store.products()["acme"], NOW.date(), [])
        self.assertEqual((row["closedAt"], row["closeNote"]), ("2026-11-12T08:00:00Z", "done"))


class Memo(Base):
    def test_no_item_on_an_empty_sweep(self):
        self.project("fine", createdAt=iso(1))
        self.assertIsNone(pm.memo(NOW))
        self.assertEqual(store.inbox()["items"], [])
        self.assertEqual(store.read("pm.json", None)["lastSweep"]["total"], 0)

    def test_one_action_item_with_at_most_five_numbered_steps(self):
        for i in range(7):
            self.project(f"fin{i}", [phase("A", 2, 2)])
        for i in range(4):
            self.project(f"q{i}", createdAt=iso(20))
        self.project("late", [phase("A", 1, 4, due="2026-10-01")])
        item = pm.memo(NOW)
        box = store.inbox()["items"]
        self.assertEqual(len(box), 1)
        self.assertEqual((item["kind"], item["from"]), ("action", "pm"))
        self.assertLessEqual(len(item["title"]), 80)
        self.assertEqual(item["title"], "Close 7 finished projects")  # the largest sweep group is the only decision
        steps = item["context"].splitlines()
        self.assertEqual(len(steps), 2)
        self.assertTrue(all(s.startswith(f"{i}. ") for i, s in enumerate(steps, 1)))
        self.assertEqual(item["options"], [["done", "Done"], ["blocked", "Blocked"]])
        # a second run while the first is still open files nothing
        self.assertIsNone(pm.memo(NOW))
        self.assertEqual(len(store.inbox()["items"]), 1)
        # once the owner answers it, the next memo resolves it and files a fresh one
        store.write("answers.json", {item["id"]: {"choice": "done"}})
        again = pm.memo(NOW)
        self.assertIsNotNone(again)
        self.assertNotEqual(again["id"], item["id"])
        box = store.inbox()
        self.assertEqual([x["id"] for x in box["items"]], [again["id"]])
        self.assertIn(item["id"], [x["id"] for x in box["resolved"]])

    def test_memo_through_the_cli(self):
        code, obj, err = run("pm", "memo")
        self.assertEqual((code, obj), (0, None), err)
        self.project("fin", [phase("A", 2, 2)])
        code, obj, err = run("pm", "memo")
        self.assertEqual(code, 0, err)
        self.assertEqual(obj["title"], "Close 1 finished project")

    def test_item_anchors_on_a_project_the_page_can_draw(self):
        self.project("hidden", createdAt=iso(20), phases=[])  # a build with no phases is hidden from the page
        self.project("shown", [phase("A", 2, 2)])
        item = pm.memo(NOW)
        self.assertEqual(item["project"], "shown")


class Api(Base):
    def test_get_runs_the_sweep(self):
        self.project("fin", [phase("A", 2, 2)])
        status, body = serve.pm_data()
        self.assertEqual(status, 200)
        self.assertEqual([f["id"] for f in body["findings"]], ["finished:fin"])
        self.assertEqual(body["counts"]["finished"], 1)

    def test_post_actions(self):
        self.project("a")
        self.assertEqual(serve.save_pm({"action": "close", "project": "a"}), (200, {"ok": True}))
        self.assertTrue(store.find_project("a")["closedAt"])
        self.assertEqual(serve.save_pm({"action": "reopen", "project": "a"}), (200, {"ok": True}))
        self.assertEqual(serve.save_pm({"action": "snooze", "project": "a", "days": 3}), (200, {"ok": True}))
        self.assertIn("a", store.read("pm.json", None)["snoozed"])
        self.assertEqual(serve.save_pm({"action": "snooze", "project": "a", "days": 0}), (200, {"ok": True}))
        self.assertEqual(store.read("pm.json", None)["snoozed"], {})

    def test_post_proto_built(self):
        self.project("a", [phase("A", 2, 2)])
        store.save_protos([{"id": "acme-x", "project": "a", "versions": [{"v": 1, "status": "validated"}]},
                           {"id": "acme-y", "project": "a", "versions": [{"v": 1, "status": "validated"}]}])
        self.assertEqual(serve.save_pm({"action": "proto-built", "project": "a", "proto": "acme-x"}), (200, {"ok": True}))
        # only the clicked prototype is marked built; its sibling on the same project stays validated
        self.assertEqual([p["versions"][0]["status"] for p in store.protos()], ["built", "validated"])
        self.assertEqual(serve.save_pm({"action": "proto-built", "project": "a", "proto": "acme-x"})[0], 400)
        self.assertEqual(serve.save_pm({"action": "proto-built", "project": "a"})[0], 400)
        self.assertEqual(serve.save_pm({"action": "proto-built", "project": "a", "proto": "acme-y"}), (200, {"ok": True}))

    def test_post_nudge_uses_the_autopilot_sender(self):
        self.project("a")
        with mock.patch("factory.autopilot.nudge_project", return_value={"result": "nudged", "delivered": True}) as n:
            status, body = serve.save_pm({"action": "nudge", "project": "a"})
        self.assertEqual((status, body["delivered"]), (200, True))
        n.assert_called_once_with("a")
        with mock.patch("factory.autopilot.agent_rows", return_value=[]):
            status, body = serve.save_pm({"action": "nudge", "project": "a"})  # not on autopilot
        self.assertEqual(status, 400)
        self.assertIn("autopilot", body["error"])

    def test_bad_input_is_a_400_and_writes_nothing(self):
        self.project("a")
        for patch in ([], "x", {}, {"action": "close"}, {"project": "a"}, {"action": "delete", "project": "a"},
                      {"action": "close", "project": ""}, {"action": "close", "project": 7}, {"action": "close", "project": "nope"},
                      {"action": "reopen", "project": "a"},
                      {"action": "snooze", "project": "a", "days": -1}, {"action": "snooze", "project": "a", "days": 9999},
                      {"action": "snooze", "project": "a", "days": "3"}, {"action": "snooze", "project": "a", "days": True},
                      {"action": "snooze", "project": "nope", "days": 3}):
            status, body = serve.save_pm(patch)
            self.assertEqual(status, 400, patch)
            self.assertIn("error", body)
        self.assertNotIn("closedAt", store.find_project("a"))
        self.assertFalse(os.path.exists(store.path("pm.json")))


class Phase2(Base):
    """Signals, Now priority, the Today decisions and Merged, not in prod."""

    def setUp(self):
        super().setUp()
        pm._unreleased_cache.clear()
        # the page and the CLI read the clock: these tests stamp everything from the real time
        self.T = datetime.now(timezone.utc).replace(microsecond=0)
        self.now_rows("Faster checkout page", "Morocco bank transfers", "Lock withdrawals until identity is verified",
                      "Studio scenes for streamers")

    def ago(self, days=0, hours=0):
        return store.iso(self.T - timedelta(days=days, hours=hours))

    def now_rows(self, *titles, product="acme"):
        items = [{"id": f"R{k}", "product": product, "title": t, "lane": "now", "rank": k, "source": "factory"}
                 for k, t in enumerate(titles, 1)]
        items.append({"id": "R9", "product": product, "title": "Withdrawal status page for creators", "lane": "next", "rank": 1,
                      "source": "factory"})
        store.write("roadmap.json", {"items": items, "sources": {}})

    def digest(self, *history):
        from factory import digest
        store.write("digest.json", dict(digest.DEFAULT_DIGEST, history=list(history)))

    def run_entry(self, days_ago, result=None, error=None):
        return {"at": self.ago(days_ago), "conversations": 10, "cost_usd": 0.01, "result": result, "error": error, "posted": bool(result)}

    def order(self):
        return [i["id"] for i in sorted(store.read("roadmap.json", None)["items"], key=lambda i: i["rank"]) if i["lane"] == "now"]

    def test_support_signals_parse_tickets_and_keep_the_last_7_days(self):
        self.digest(self.run_entry(9, "Old ask (40 tickets)"),
                    self.run_entry(3, "Withdrawal not received or delayed (9 tickets)"),
                    self.run_entry(2, None),  # a run that posted nothing
                    self.run_entry(1, "Payments declined or stuck"))
        r = pm.signals(self.T)
        self.assertIsNone(r["supportError"])
        self.assertEqual([(s["src"], s["title"], s["tickets"]) for s in r["signals"]],
                         [("Support", "Payments declined or stuck", 0), ("Support", "Withdrawal not received or delayed", 9)])
        self.assertEqual(r["signals"][1]["evidence"], f"9 tickets · support digest {pm._day(self.T - timedelta(days=3))}")
        self.assertEqual(r["signals"][0]["evidence"], f"support digest {pm._day(self.T - timedelta(days=1))}")
        # the Next row "Withdrawal status page" shares one word only: no map
        self.assertEqual([m["id"] for m in r["signals"][1]["maps"]], [])

    def test_support_error_banner_keeps_the_older_signals(self):
        self.digest(self.run_entry(2, "Payments declined or stuck (8 tickets)"), self.run_entry(0, error="chatwoot timed out"))
        r = pm.signals(self.T)
        self.assertEqual(r["supportError"], {"error": "chatwoot timed out", "from": self.ago(2)})
        self.assertEqual([s["tickets"] for s in r["signals"]], [8])
        store.write("digest.json", {"broken": True})  # a corrupt digest file is a banner too, not a dead page
        r = pm.signals(self.T)
        self.assertIn("not a digest config", r["supportError"]["error"])
        self.assertEqual(r["signals"], [])

    def test_owner_and_inbox_signals(self):
        old = store.add_ask_item("p", "decision", "Pick a payout partner", "s", "c", [["a", "A"]], "a", "", "agent", None, at=self.ago(2))
        store.add_ask_item("p", "decision", "Fresh question", "s", "c", [["a", "A"]], "a", "", "agent", None, at=self.ago(hours=3))
        store.add_ask_item("p", "action", "PM memo", "s", "c", [["done", "Done"]], None, "", "pm", None, at=self.ago(3))
        store.write("answers.json", {old["id"]: {"choice": "a", "note": "creators keep asking where their withdrawal status is", "at": self.ago(1)},
                                     "d99": {"choice": "a", "note": "", "at": self.ago(1)}})
        store.write("relay.json", [{"id": "m-1", "kind": "message", "raw": "please speed up checkout page loading", "at": self.ago(1), "thread": "t"},
                                   {"id": "m-2", "kind": "autopilot", "raw": "nudge", "at": self.ago(1), "thread": "t"},
                                   {"id": "m-3", "kind": "message", "raw": "too old", "at": self.ago(9), "thread": "t"}])
        r = pm.signals(self.T)["signals"]
        got = {(s["src"], s["title"]) for s in r}
        self.assertEqual(got, {("You", "creators keep asking where their withdrawal status is"),
                               ("You", "please speed up checkout page loading"), ("Inbox", "Pick a payout partner")})
        by = {s["title"]: s for s in r}
        self.assertEqual(by["Pick a payout partner"]["evidence"], "open 2 days, waiting on you")
        self.assertEqual([m["id"] for m in by["creators keep asking where their withdrawal status is"]["maps"]], ["R9"])
        self.assertEqual([m["id"] for m in by["please speed up checkout page loading"]["maps"]], ["R1"])

    def test_two_significant_words_map_a_signal(self):
        rows = [{"id": "A", "title": "Lock KYC on withdrawals", "product": "acme"}, {"id": "B", "title": "The new pricing engine", "product": "acme"}]
        self.assertEqual([m["id"] for m in pm.maps_to("withdrawals stuck without KYC lock", rows)], ["A"])
        self.assertEqual(pm.maps_to("withdrawals are slow", rows), [])  # one shared word is not enough
        self.assertEqual(pm.maps_to("KYC on the new", rows), [])  # words of 3 letters or fewer do not count
        self.assertEqual(pm.maps_to("these withdrawals, those", [{"id": "C", "title": "these withdrawals those", "product": "acme"}]), [])  # stop words
        self.assertEqual([m["id"] for m in pm.maps_to("Pricing ENGINE broken", rows)], ["B"])

    def test_priority_score_and_moves(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        p = pm.priority(pm.signals(self.T)["signals"])
        self.assertEqual(len(p), 1)
        rows = p[0]["rows"]
        # R3: 9 tickets x 3 + P3 (5) = 32; R1 7, R2 6, R4 4
        self.assertEqual([(r["id"], r["score"], r["was"], r["rank"], r["move"]) for r in rows],
                         [("R3", 32, 3, 1, "+2"), ("R1", 7, 1, 2, "-1"), ("R2", 6, 2, 3, "-1"), ("R4", 4, 4, 4, "=")])
        self.assertEqual(rows[0]["why"], "9 tickets this week, your P3")
        self.assertEqual(p[0]["moves"], 3)

    def test_money_risk_adds_3_and_names_it(self):
        self.now_rows("Faster checkout page", "Morocco bank transfers", "Payout fraud checks")
        rows = pm.priority([])[0]["rows"]
        self.assertEqual([(r["id"], r["score"], r["move"]) for r in rows], [("R3", 8, "+2"), ("R1", 7, "-1"), ("R2", 6, "-1")])
        self.assertEqual(rows[0]["why"], "no tickets this week, money risk, your P3")

    def test_approve_reject_and_undo_through_the_api(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        status, body = serve.save_pm({"action": "approve", "product": "acme", "row": "R3", "shown": self.ORDER, "rank": 1})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.order(), ["R3", "R1", "R2", "R4"])
        self.assertEqual((body["before"], body["after"]), (self.ORDER, ["R3", "R1", "R2", "R4"]))
        status, body = serve.save_pm({"action": "restore", "product": "acme", "ids": body["before"], "expect": body["after"]})
        self.assertEqual((status, body["order"]), (200, self.ORDER))
        self.assertEqual(self.order(), ["R1", "R2", "R3", "R4"])
        # reject keeps R3 where it is while its score is unchanged
        status, body = serve.save_pm({"action": "reject", "product": "acme", "row": "R3"})
        self.assertEqual(status, 200, body)
        rows = pm.priority(pm.signals(self.T)["signals"])[0]["rows"]
        self.assertEqual([(r["id"], r["move"], r["rejected"]) for r in rows],
                         [("R1", "=", False), ("R2", "=", False), ("R3", "=", True), ("R4", "=", False)])
        self.assertEqual(serve.save_pm({"action": "approve-all", "product": "acme", "shown": self.ORDER, "proposed": self.ORDER})[0],
                         400)  # nothing left to move
        self.assertEqual(self.order(), ["R1", "R2", "R3", "R4"])
        self.assertEqual(serve.save_pm({"action": "unreject", "product": "acme", "row": "R3"})[0], 200)
        status, body = serve.save_pm({"action": "approve-all", "product": "acme", "shown": self.ORDER, "proposed": ["R3", "R1", "R2", "R4"]})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.order(), ["R3", "R1", "R2", "R4"])
        serve.save_pm({"action": "restore", "product": "acme", "ids": body["before"], "expect": body["after"]})
        self.assertEqual(self.order(), ["R1", "R2", "R3", "R4"])

    ORDER = ["R1", "R2", "R3", "R4"]

    def test_stale_approve_is_refused(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        for patch in ({"action": "approve", "product": "acme", "row": "R3", "shown": ["R2", "R1", "R3", "R4"], "rank": 1},  # lane moved
                      {"action": "approve", "product": "acme", "row": "R3", "shown": self.ORDER, "rank": 2},  # proposal moved
                      {"action": "approve-all", "product": "acme", "shown": ["R4", "R1", "R2", "R3"], "proposed": ["R3", "R1", "R2", "R4"]},
                      {"action": "approve-all", "product": "acme", "shown": self.ORDER, "proposed": ["R3", "R2", "R1", "R4"]}):
            status, body = serve.save_pm(patch)
            self.assertEqual((status, body.get("error")), (400, "acme's Now lane changed: reload"), patch)
        self.assertEqual(self.order(), self.ORDER)

    def test_undo_refused_once_the_lane_moved_again(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        body = pm.approve("acme", "R3", shown=self.ORDER, rank=1)
        roadmap.move("R4", "now", before="R3")  # someone else moved the lane after the approve
        status, err = serve.save_pm({"action": "restore", "product": "acme", "ids": body["before"], "expect": body["after"]})
        self.assertEqual((status, err.get("error")), (400, "acme's Now lane changed: reload"))
        self.assertEqual(self.order(), ["R4", "R3", "R1", "R2"])

    def test_rejection_holds_while_tickets_and_risk_are_unchanged(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        pm.reject("acme", "R3")
        roadmap.move("R3", "now")  # R3 now sits at P4: its total score changes, its tickets do not
        rows = {r["id"]: r for r in pm.priority(pm.signals(self.T)["signals"])[0]["rows"]}
        self.assertTrue(rows["R3"]["rejected"])
        self.assertEqual(rows["R3"]["move"], "=")
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"),
                    self.run_entry(0, "Withdrawals locked until identity verified again (2 tickets)"))
        rows = {r["id"]: r for r in pm.priority(pm.signals(self.T)["signals"])[0]["rows"]}
        self.assertFalse(rows["R3"]["rejected"])  # new tickets: the proposal moves it again

    def test_record_prunes_ended_ignores_and_rejections_off_now(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        sid = pm.signals(self.T)["signals"][0]["id"]
        pm.hide_signal(sid, "ok", self.T - timedelta(days=8))  # ended a day ago
        pm.reject("acme", "R3")
        st = store.read("pm.json", None)
        st["rejected"]["acme:GONE"] = {"at": self.ago(1), "base": 0}
        store.write("pm.json", st)
        pm.record(pm.sweep(self.T))
        st = store.read("pm.json", None)
        self.assertEqual((st["signals"], list(st["rejected"])), ({}, ["acme:R3"]))
        roadmap.move("R3", "next")
        pm.record(pm.sweep(self.T))
        self.assertEqual(store.read("pm.json", None)["rejected"], {})

    def test_a_bad_file_costs_its_section_not_the_page(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        store.write("answers.json", ["not", "a", "map"])
        store.write("inbox.json", {"seq": 1, "items": "broken"})
        status, body = serve.pm_data()
        self.assertEqual(status, 200, body)
        self.assertEqual([s["src"] for s in body["signals"]], ["Support"])
        self.assertTrue(any(e.startswith("answers.json") for e in body["errors"]["sig"]), body["errors"])
        self.assertTrue(any(e.startswith("inbox.json") for e in body["errors"]["sig"]), body["errors"])
        self.assertEqual(body["stats"]["inbox"], 0)
        data = store.read("roadmap.json", None)
        del data["items"][0]["title"]
        store.write("roadmap.json", data)
        status, body = serve.pm_data()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["priority"], [])
        self.assertTrue(body["errors"]["prio"] and body["errors"]["sig"], body["errors"])
        self.assertEqual(body["signals"][0]["maps"], [])

    def test_quiet_day_with_a_stale_inbox_item_and_a_message_is_still_quiet(self):
        store.add_ask_item("p", "decision", "Pick a mascot for the newsletter", "s", "c", [["a", "A"]], "a", "", "agent", None,
                           at=self.ago(3))
        store.write("relay.json", [{"id": "m-1", "kind": "message", "raw": "rename the weekly newsletter", "at": self.ago(1), "thread": "t"}])
        o = pm.overview(self.T)
        self.assertEqual({s["src"] for s in o["signals"]}, {"Inbox", "You"})
        self.assertEqual(o["decisions"], [])

    def test_bad_priority_and_signal_input_is_a_400(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        for patch in ({"action": "approve", "product": "nope", "row": "R3", "shown": self.ORDER, "rank": 1},
                      {"action": "approve", "product": "acme", "shown": self.ORDER, "rank": 1},
                      {"action": "approve", "product": "acme", "row": "R3", "rank": 1},
                      {"action": "approve", "product": "acme", "row": "R3", "shown": self.ORDER},
                      {"action": "approve-all", "product": "acme", "shown": self.ORDER},
                      {"action": "approve", "product": "acme", "row": "R77", "shown": self.ORDER, "rank": 1},
                      {"action": "approve", "product": "acme", "row": "R4", "shown": self.ORDER, "rank": 4},
                      {"action": "reject", "product": "acme", "row": "R4"}, {"action": "unreject", "product": "acme", "row": "R3"},
                      {"action": "restore", "product": "acme", "ids": ["R1"], "expect": ["R1"]},
                      {"action": "restore", "product": "acme", "ids": self.ORDER},
                      {"action": "restore", "product": "acme", "ids": "R1", "expect": self.ORDER},
                      {"action": "ignore", "signal": "support:nope"}, {"action": "ignore"}, {"action": "unhide", "signal": "x"},
                      {"action": "explode", "product": "acme"}):
            status, body = serve.save_pm(patch)
            self.assertEqual(status, 400, patch)
            self.assertIn("error", body)
        self.assertEqual(self.order(), ["R1", "R2", "R3", "R4"])

    def test_ignore_hides_and_leaves_the_score_ok_only_hides(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        sid = pm.signals(self.T)["signals"][0]["id"]
        self.assertEqual(serve.save_pm({"action": "ok", "signal": sid})[0], 200)
        sig = pm.signals(self.T)["signals"]
        self.assertEqual(sig[0]["hidden"], "ok")
        self.assertEqual(pm.priority(sig)[0]["rows"][0]["id"], "R3")  # noted, still counted
        self.assertEqual(serve.save_pm({"action": "unhide", "signal": sid})[0], 200)
        self.assertNotIn("hidden", pm.signals(self.T)["signals"][0])
        self.assertEqual(serve.save_pm({"action": "ignore", "signal": sid})[0], 200)
        sig = pm.signals(self.T)["signals"]
        self.assertEqual(sig[0]["hidden"], "ignore")
        self.assertEqual(pm.priority(sig)[0]["moves"], 0)  # ignored tickets no longer move R3
        pm.hide_signal(sid, "ignore", self.T)
        self.assertEqual(store.read("pm.json", None)["signals"][sid], {"by": "ignore", "until": store.iso(self.T + timedelta(days=14))})

    def test_unreleased_read_failure_on_one_product(self):
        from factory import release
        prod = lambda repo: {"repo": repo, "flow": "pr", "base": "develop", "head": "main", "thread": None, "red_paths": [],
                             "project": "p", "mode": "live", "checks": []}
        store.write("release.json", dict(release.DEFAULT_RELEASE, products={"acme": prod("acme/app"), "beta": prod("acme/beta")},
                                         shipped_prs={"acme": [12]}))

        def fake(cfg):
            if cfg["repo"] == "acme/beta":
                raise store.FactoryError("gh: HTTP 502")
            return [{"number": 11, "title": "Fee engine"}, {"number": 12, "title": "Shipped already"}], "sha", []
        t0 = time.time()
        with mock.patch("factory.release.unreleased_prs", side_effect=fake) as m, mock.patch("factory.pm.time.time", return_value=t0):
            u = pm.unreleased()
            pm.unreleased()  # cached: acme for 10 min, beta's failure for 60 s
            self.assertEqual(m.call_count, 2)
        self.assertEqual(u[0], {"product": "acme", "name": "Acme", "repo": "acme/app", "prs": [{"number": 11, "title": "Fee engine", "mergedAt": None}]})
        self.assertEqual((u[1]["product"], u[1]["prs"], u[1]["error"]), ("beta", [], "gh: HTTP 502"))
        with mock.patch("factory.release.unreleased_prs", side_effect=fake) as m, mock.patch("factory.pm.time.time", return_value=t0 + 61):
            pm.unreleased()
            self.assertEqual([c.args[0]["repo"] for c in m.call_args_list], ["acme/beta"])  # only the failure is read again
        data = store.read("release.json", None)
        data["products"]["acme"]["repo"] = "acme/app2"  # a new repo is a new cache entry
        store.write("release.json", data)
        with mock.patch("factory.release.unreleased_prs", side_effect=fake) as m, mock.patch("factory.pm.time.time", return_value=t0 + 62):
            pm.unreleased()
            self.assertEqual([c.args[0]["repo"] for c in m.call_args_list], ["acme/app2"])

    def test_memo_lists_the_same_decisions_as_today(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"),
                    self.run_entry(2, "Dark mode for the dashboard (4 tickets)"))
        for i in range(2):
            self.project(f"fin{i}", [phase("A", 2, 2)])
        dec = pm.overview(self.T)["decisions"]
        self.assertEqual([d["kind"] for d in dec], ["reorder", "sweep", "signal"])
        self.assertEqual(dec[0]["title"], "Reorder Now for Acme: move Lock withdrawals until... to #1")
        self.assertEqual(dec[0]["text"], "9 tickets this week, your P3. 3 rows move.")
        self.assertEqual(dec[1]["title"], "Close 2 finished projects")
        self.assertEqual(dec[2]["title"], "No roadmap row covers: Dark mode for the dashboard")
        item = pm.memo(self.T)
        steps = item["context"].splitlines()
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[1:], [f"{i}. {d['title']}: {d['text']}" for i, d in enumerate(dec, 2)])
        self.assertEqual(item["title"], "3 decisions: Reorder Now for Acme: move Lock withdrawals until... to #1")
        run_log = store.read("pm.json", None)["runs"]
        self.assertEqual(run_log[-1]["filed"], item["id"])

    def test_quiet_day(self):
        self.now_rows("Faster checkout page", "Morocco bank transfers")
        self.project("fine", createdAt=self.ago(1))
        o = pm.overview(self.T)
        self.assertEqual(o["decisions"], [])
        self.assertIsNone(pm.memo(self.T))
        self.assertEqual(store.inbox()["items"], [])
        nxt = datetime.fromisoformat(o["nextRun"].replace("Z", "+00:00")).astimezone()
        self.assertEqual((nxt.hour, nxt.minute), (8, 0))
        self.assertLess(nxt.weekday(), 5)
        self.assertTrue(self.T < nxt <= self.T + timedelta(days=3, hours=1))  # at most a weekend away
        self.assertEqual(o["stats"], {"followed": 1, "stuck": 0, "launched": 0, "signals": 0, "inbox": 0})

    def memo_quiet(self):
        """pm.memo() with its stderr: (item, stderr)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            item = pm.memo(self.T)
        return item, err.getvalue()

    def test_a_failed_source_is_never_a_quiet_day(self):
        self.project("fine", createdAt=self.ago(1))
        data = store.read("roadmap.json", None)
        del data["items"][0]["title"]
        store.write("roadmap.json", data)
        item, err = self.memo_quiet()
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], "Fix the product manager's sources: roadmap")
        self.assertIn("Could not read: roadmap. The memo may be incomplete.", item["context"])
        self.assertEqual(err, "pm memo: could not read roadmap; the memo may be incomplete\n")
        self.assertEqual(len(store.read("pm.json", None)["runs"]), 1)  # the sweep itself ran: recorded
        # an unreadable inbox cannot take an item: the cron log says what failed
        store.write("inbox.json", {"seq": 1, "items": "broken", "resolved": []})
        item, err = self.memo_quiet()
        self.assertIsNone(item)
        self.assertEqual(err, "pm memo: could not read inbox, roadmap; the memo may be incomplete; inbox.json cannot take "
                              "the memo item, nothing filed\n")

    def test_a_failed_sweep_is_not_recorded_as_a_run(self):
        self.project("fine", createdAt=self.ago(1))
        with mock.patch("factory.pm.sweep", side_effect=store.FactoryError("config pm.quiet_days is 0")):
            item, err = self.memo_quiet()
        self.assertIn("Could not read: sweep.", item["context"])
        self.assertIn("could not read sweep", err)
        self.assertFalse(os.path.exists(store.path("pm.json")))  # no "swept 0 projects" run stored

    def test_undo_order_is_the_order_applied_not_a_reread(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        real = roadmap.move

        def move_then_someone_else(id, lane, before=None):
            item = real(id, lane, before=before)
            real("R4", "now", before="R1")  # another writer, right after our move
            return item
        with mock.patch("factory.pm.roadmap.move", side_effect=move_then_someone_else):
            body = pm.approve("acme", "R3", shown=self.ORDER, rank=1)
        self.assertEqual(body["after"], ["R3", "R1", "R2", "R4"])
        self.assertEqual(self.order(), ["R3", "R4", "R1", "R2"])
        with self.assertRaises(store.FactoryError):  # Undo sees the other writer's change and refuses
            pm.restore("acme", body["before"], body["after"])
        for i in self.ORDER:
            roadmap.move(i, "now")
        with mock.patch("factory.pm.roadmap.move", side_effect=move_then_someone_else):
            body = pm.approve("acme", shown=self.ORDER, proposed=["R3", "R1", "R2", "R4"])
        self.assertEqual(body["after"], ["R3", "R1", "R2", "R4"])  # approve-all: the proposed order, not the lane now

    def test_restore_returns_the_order_it_applied(self):
        roadmap.move("R3", "now", before="R1")
        real = roadmap.move
        calls = []

        def move_and_count(id, lane, before=None):
            calls.append(id)
            item = real(id, lane, before=before)
            if len(calls) == 4:
                real("R4", "now", before="R1")  # another writer once the restore is done
            return item
        with mock.patch("factory.pm.roadmap.move", side_effect=move_and_count):
            r = pm.restore("acme", self.ORDER, ["R3", "R1", "R2", "R4"])
        self.assertEqual(r["order"], self.ORDER)

    def test_a_malformed_products_file_does_not_break_the_page(self):
        from factory import release
        prod = {"repo": "acme/app", "flow": "pr", "base": "develop", "head": "main", "thread": None, "red_paths": [],
                "project": "p", "mode": "live", "checks": []}
        store.write("release.json", dict(release.DEFAULT_RELEASE, products={"acme": prod}))
        store.write("products.json", {"acme": "not a product"})
        with mock.patch("factory.release.unreleased_prs", return_value=([], "sha", [])):
            status, body = serve.pm_data()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["unreleased"], [])
        self.assertTrue(any(e.startswith("unreleased") for e in body["errors"]["sweep"]), body["errors"])

    def test_record_survives_a_bad_roadmap_item(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        pm.reject("acme", "R3")
        data = store.read("roadmap.json", None)
        data["items"].append({"lane": "now", "title": "no id or product"})
        store.write("roadmap.json", data)
        pm.record(pm.sweep(self.T))  # does not raise
        self.assertEqual(list(store.read("pm.json", None)["rejected"]), ["acme:R3"])  # kept until the roadmap is fixed
        self.project("fine", createdAt=self.ago(1))
        item, _ = self.memo_quiet()
        self.assertIn("roadmap", item["title"])

    def test_cli_parity(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"))
        code, sig, err = run("pm", "signals")
        self.assertEqual(code, 0, err)
        self.assertEqual(sig["signals"][0]["tickets"], 9)
        code, prio, err = run("pm", "priority", "--product", "acme")
        self.assertEqual(code, 0, err)
        self.assertEqual(prio[0]["rows"][0]["id"], "R3")
        self.assertNotEqual(run("pm", "approve", "acme")[0], 0)  # neither a row nor --all
        code, r, err = run("pm", "reject", "acme", "R1")
        self.assertEqual(code, 0, err)
        code, r, err = run("pm", "approve", "acme", "R3")
        self.assertEqual((code, r["before"]), (0, ["R1", "R2", "R3", "R4"]), err)
        self.assertEqual(self.order(), ["R1", "R3", "R2", "R4"])  # R1 is rejected: it keeps its place
        code, r, err = run("pm", "approve", "acme", "--all")
        self.assertNotEqual(code, 0)  # nothing left to move
        self.assertNotEqual(run("pm", "restore", "acme", "R1", "R2", "R3", "R4", "--expect", "R1", "R2", "R3", "R4")[0], 0)  # stale
        code, r, err = run("pm", "restore", "acme", "R1", "R2", "R3", "R4", "--expect", "R1", "R3", "R2", "R4")
        self.assertEqual((code, r["order"]), (0, ["R1", "R2", "R3", "R4"]), err)
        code, r, err = run("pm", "reject", "acme", "R1", "--undo")
        self.assertEqual((code, r["rejected"]), (0, False), err)
        self.assertEqual(store.read("pm.json", None)["rejected"], {})
        sid = sig["signals"][0]["id"]
        for action, hidden in (("ignore", "ignore"), ("unhide", None), ("ok", "ok")):
            code, r, err = run("pm", "signal", action, sid)
            self.assertEqual((code, r["hidden"]), (0, hidden), err)
        self.assertNotEqual(run("pm", "signal", "unhide", "support:nope")[0], 0)
        self.assertNotEqual(run("pm", "priority", "--product", "nosuch")[0], 0)


class Phase3(Base):
    """After launch: launch cards, their chips, the day checks, verdicts and the Today decision."""
    ERRORS = {"name": "sentry", "kind": "errors", "cmd": ["errs-now"], "baseline_cmd": ["errs-base"]}
    ago, now_rows, digest, run_entry = Phase2.ago, Phase2.now_rows, Phase2.digest, Phase2.run_entry

    def setUp(self):
        super().setUp()
        pm._launch_cache.clear()
        self.T = datetime.now(timezone.utc).replace(microsecond=0)
        self.now_rows("Faster checkout page", "Morocco bank transfers", "Lock withdrawals until identity is verified",
                      "Studio scenes for streamers")
        self.result = {"name": "sentry", "kind": "errors", "ok": True, "value": 2, "baseline": 3, "detail": ""}
        self.ran = []

        def check(c, judge):
            self.ran.append(c["name"])
            if isinstance(self.result, Exception):
                raise self.result
            return dict(self.result)
        self.facts = {41: {"title": "Bulk export as CSV"}, 42: {"title": "Faster checkout page loading"}}
        p1 = mock.patch("factory.release._run_signal_check", side_effect=check)
        p2 = mock.patch("factory.comms.pr_facts", side_effect=lambda repo, ns: {n: self.facts[n] for n in ns if n in self.facts})
        p1.start(), p2.start()
        self.addCleanup(p1.stop), self.addCleanup(p2.stop)
        self.release()

    def release(self, journal=(), checks=None, comms_cfg=True):
        from factory import release
        prod = {"repo": "acme/app", "flow": "pr", "base": "develop", "head": "main", "thread": None, "red_paths": [],
                "project": "p", "mode": "live", "checks": [self.ERRORS] if checks is None else checks}
        if comms_cfg:
            prod["comms"] = {"langs": ["en"], "audiences": ["users"]}
        store.write("release.json", dict(release.DEFAULT_RELEASE, products={"acme": prod}, journal=list(journal)))

    def ship(self, days, prs, sha=None, event="shipped"):
        return {"at": self.ago(days), "product": "acme", "event": event, "prs": prs, "sha": sha, "note": ""}

    def card(self, lid="acme:41", now=None):
        return next(c for c in pm.launches(pm.signals(self.T)["signals"], now or self.T) if c["id"] == lid)

    def chip(self, name, **kw):
        return next((x["state"], x["detail"]) for x in self.card(**kw)["chips"] if x["name"] == name)

    def jobs(self, *jobs, deferred=None):
        store.write("comms.json", {"changelogs": {}, "jobs": {j["id"]: j for j in jobs}, "since": {}, "deferred": deferred or {},
                                   "relayed": {}})

    def test_launch_discovery_window_and_rollbacks(self):
        self.release([self.ship(2, [41], "aaa"), self.ship(10, [42], "bbb"), self.ship(9, [43], "ccc"),
                      self.ship(8, [43], "ccc", event="rolled-back"), self.ship(36, [44], "ddd"), self.ship(1, [], "eee"),
                      {**self.ship(3, [45]), "event": "started"}])
        cards = pm.launches([], self.T)
        self.assertEqual([c["id"] for c in cards], ["acme:eee", "acme:aaa", "acme:bbb"])  # newest first; 43 rolled back, 44 too old
        c = cards[1]
        self.assertEqual((c["day"], c["prs"], c["next"]), (2, [{"number": 41, "title": "Bulk export as CSV"}],
                                                           f"Day-7 check {pm._day(self.T + timedelta(days=5))}"))
        self.assertEqual(c["due"], ["d1"])

    def test_titles_failure_shows_numbers_only(self):
        self.release([self.ship(2, [41])])
        with mock.patch("factory.comms.pr_facts", side_effect=store.FactoryError("gh failure reading acme/app")):
            c = pm.launches([], self.T)[0]
        self.assertEqual((c["prs"], c["titlesError"]), ([{"number": 41, "title": ""}], "gh failure reading acme/app"))
        self.assertEqual(c["chips"][2]["state"], "w")

    def test_comms_chip(self):
        self.release([self.ship(3, [41])])
        self.assertEqual(self.chip("Comms sent"), ("w", "no comms job yet"))
        job = {"id": "f-acme-41", "product": "acme", "kind": "feature", "prs": [{"n": 41}], "status": "needs-approval"}
        self.jobs(job)
        state, detail = self.chip("Comms sent")
        self.assertEqual(state, "n")
        self.assertIn("f-acme-41 needs-approval", detail)
        self.jobs(dict(job, status="deferred"), deferred={"acme": {"41": (self.T + timedelta(days=3)).date().isoformat()}})
        self.assertEqual(self.chip("Comms sent"), ("w", "comms job not due yet"))  # scheduled: not late
        self.jobs(dict(job, status="sent"))
        self.assertEqual(self.chip("Comms sent"), ("y", "f-acme-41 sent"))
        self.release([self.ship(3, [41])], comms_cfg=False)
        self.assertEqual(self.chip("Comms sent"), ("w", "no comms configured"))

    def test_errors_chip(self):
        self.release([self.ship(3, [41])])
        self.assertEqual(self.chip("Errors since launch"), ("y", "sentry: 2 (baseline 3)"))
        self.chip("Errors since launch")
        self.assertEqual(self.ran, ["sentry"])  # cached
        pm._launch_cache.clear()
        self.result = dict(self.result, ok=False, value=40, detail="40 > 11 (baseline 3)")
        self.assertEqual(self.chip("Errors since launch"), ("n", "sentry: 40 > 11 (baseline 3)"))
        pm._launch_cache.clear()
        self.result = dict(self.result, ok=False, value=None, detail="cmd error: command exited 1: token expired")
        self.assertEqual(self.chip("Errors since launch")[0], "w")  # a broken probe is not an error spike
        pm._launch_cache.clear()
        self.result = OSError("no such command")
        self.assertEqual(self.chip("Errors since launch"), ("w", "could not run the error check: no such command"))
        self.release([self.ship(3, [41])], checks=[{"name": "home", "kind": "http", "url": "https://example.com"}])
        self.assertEqual(self.chip("Errors since launch"), ("w", "no error check configured"))

    def test_tickets_chip(self):
        self.release([self.ship(3, [41])])
        self.assertEqual(self.chip("Support tickets"), ("y", "0 mention it"))
        self.digest(self.run_entry(5, "Bulk export broken as CSV (7 tickets)"),  # before the launch: not counted
                    self.run_entry(2, "Bulk export as CSV times out (2 tickets)"),
                    self.run_entry(1, "CSV bulk export missing columns (1 ticket)"), self.run_entry(1, "Dark mode (5 tickets)"))
        self.assertEqual(self.chip("Support tickets"), ("w", "3 tickets mention it, newest: CSV bulk export missing columns"))

    def test_a_broken_probe_never_hides_a_real_spike(self):
        checks = [dict(self.ERRORS, name="probe"), dict(self.ERRORS, name="sentry")]
        self.release([self.ship(3, [41])], checks=checks)
        res = {"probe": {"name": "probe", "kind": "errors", "ok": False, "value": None, "baseline": None,
                         "detail": "cmd error: command timed out after 60s"},
               "sentry": {"name": "sentry", "kind": "errors", "ok": False, "value": 90, "baseline": 3,
                          "detail": "90 > 11 (baseline 3)"}}
        with mock.patch("factory.release._run_signal_check", side_effect=lambda c, j: dict(res[c["name"]])):
            self.assertEqual(self.chip("Errors since launch"), ("n", "sentry: 90 > 11 (baseline 3)"))

    def test_a_digest_is_late_only_after_its_friday_cut(self):
        self.release([self.ship(4, [41])])
        job = {"id": "d-acme-x", "product": "acme", "kind": "digest", "prs": [{"n": 41}], "status": "needs-approval",
               "until": store.iso(self.T)}
        self.jobs(job)
        self.assertEqual(self.chip("Comms sent"), ("w", "comms job not due yet"))  # cut today: not late
        self.jobs(dict(job, until=self.ago(2)))
        self.assertEqual(self.chip("Comms sent")[0], "n")  # cut 2 days ago, still not sent

    def test_tickets_chip_skips_ignored_signals_and_counts_uncounted_results_as_one(self):
        at = self.T - timedelta(days=3)
        sig = {"src": "Support", "at": self.ago(1), "title": "Faster checkout page loading is broken", "tickets": 0}
        titles = {42: "Faster checkout page loading"}
        self.assertEqual(pm._tickets_chip(titles, at, [sig])["detail"],
                         "1 ticket mentions it, newest: Faster checkout page loading is broken")
        self.assertEqual(pm._tickets_chip(titles, at, [dict(sig, hidden="ignore")])["state"], "y")

    def test_day_checks_are_recorded_once_and_pruned(self):
        self.release([self.ship(0, [41])])
        self.assertEqual(pm.record_checks(pm.launches([], self.T), self.T), [])  # day 0: nothing due
        later = self.T + timedelta(days=1)
        self.assertEqual(pm.record_checks(pm.launches([], later), later), ["acme:41"])
        self.assertEqual(pm.record_checks(pm.launches([], later), later), [])  # once only
        self.result = dict(self.result, ok=False, value=40, detail="up")
        pm._launch_cache.clear()
        d8 = self.T + timedelta(days=8)
        self.assertEqual(pm.record_checks(pm.launches([], d8), d8), ["acme:41"])
        checks = self.card(now=d8)["checks"]
        self.assertEqual(sorted(checks), ["d1", "d7"])
        self.assertEqual(checks["d1"]["chips"][1]["state"], "y")
        self.assertEqual(checks["d7"]["chips"][1]["state"], "n")
        d41 = self.T + timedelta(days=41)
        pm.record_checks([], d41)
        self.assertEqual(store.read("pm.json", None)["launches"], {})

    def test_failing_launch_is_the_first_decision_and_in_the_memo(self):
        self.digest(self.run_entry(1, "Withdrawals locked until identity verified (9 tickets)"),
                    self.run_entry(2, "Dark mode for the dashboard (4 tickets)"))
        for i in range(2):
            self.project(f"fin{i}", [phase("A", 2, 2)])
        self.release([self.ship(2, [41])])
        self.result = dict(self.result, ok=False, value=40, detail="40 > 11 (baseline 3)")
        o = pm.overview(self.T)
        self.assertEqual([d["kind"] for d in o["decisions"]], ["launch", "reorder", "sweep"])
        self.assertEqual(o["decisions"][0]["title"], f"Acme release {pm._day(self.T - timedelta(days=2))}: errors up since launch")
        self.assertEqual(o["decisions"][0]["text"], "sentry: 40 > 11 (baseline 3)")
        self.assertEqual(o["stats"]["launched"], 1)
        item = pm.memo(self.T)
        self.assertEqual(item["context"].splitlines()[1], f"2. {o['decisions'][0]['title']}: sentry: 40 > 11 (baseline 3)")
        self.assertEqual(store.read("pm.json", None)["runs"][-1]["checked"]["launches"], 1)  # the day-1 check ran
        pm.verdict("acme:41", "iterate", self.T)
        o = pm.overview(self.T)
        self.assertNotIn("launch", [d["kind"] for d in o["decisions"]])  # decided
        self.assertEqual(o["stats"]["launched"], 0)

    def test_verdict_and_undo_through_the_api(self):
        self.release([self.ship(2, [41])])
        status, body = serve.save_pm({"action": "verdict", "launch": "acme:41", "verdict": "rollback"})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["verdict"]["by"], body["command"]), ("rollback", pm.ROLLBACK_HINT))
        c = self.card()
        self.assertEqual((c["verdict"]["by"], c["rollback"]), ("rollback", pm.ROLLBACK_HINT))
        self.assertEqual(store.read("release.json", None)["journal"][-1]["event"], "shipped")  # nothing rolled back
        self.assertEqual(serve.save_pm({"action": "unverdict", "launch": "acme:41"})[0], 200)
        self.assertIsNone(self.card()["verdict"])
        for patch in ({"action": "verdict", "launch": "acme:99", "verdict": "keep"}, {"action": "verdict", "launch": "acme:41"},
                      {"action": "verdict", "launch": "acme:41", "verdict": "ship-it"}, {"action": "verdict", "verdict": "keep"},
                      {"action": "unverdict", "launch": "acme:41"}, {"action": "unverdict", "launch": 41}):
            status, body = serve.save_pm(patch)
            self.assertEqual(status, 400, patch)
            self.assertIn("error", body)
        self.assertIsNone(self.card()["verdict"])

    def test_after_tab_errors_never_break_the_page(self):
        store.write("release.json", {"products": "broken"})
        status, body = serve.pm_data()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["launches"], [])
        self.assertTrue(body["errors"]["after"], body["errors"])

    def test_cli_launches_and_verdict(self):
        self.release([self.ship(2, [41])])
        code, r, err = run("pm", "launches", "--product", "acme")
        self.assertEqual((code, [c["id"] for c in r]), (0, ["acme:41"]), err)
        self.assertNotEqual(run("pm", "launches", "--product", "nosuch")[0], 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["pm", "launches"]), 0)
        self.assertIn("ok Errors since launch: sentry: 2 (baseline 3)", out.getvalue())
        code, r, err = run("pm", "verdict", "acme:41", "keep")
        self.assertEqual((code, r["verdict"]["by"]), (0, "keep"), err)
        self.assertNotEqual(run("pm", "verdict", "acme:41")[0], 0)  # neither a verdict nor --undo
        self.assertNotEqual(run("pm", "verdict", "acme:41", "maybe")[0], 0)
        code, r, err = run("pm", "verdict", "acme:41", "--undo")
        self.assertEqual((code, r["verdict"]), (0, None), err)
        self.assertNotEqual(run("pm", "verdict", "acme:41", "--undo")[0], 0)


class Phase4(Base):
    """Lifecycle: the stage each feature reached, its days there, stuck, Unstick and the stuck tile."""
    ERRORS = Phase3.ERRORS
    ago, now_rows, digest, run_entry = Phase2.ago, Phase2.now_rows, Phase2.digest, Phase2.run_entry
    release, ship, jobs = Phase3.release, Phase3.ship, Phase3.jobs

    def setUp(self):
        super().setUp()
        pm._unreleased_cache.clear()
        self.T = datetime.now(timezone.utc).replace(microsecond=0)
        self.now_rows("Faster checkout page", "Morocco bank transfers", "Lock withdrawals until identity is verified",
                      "Studio scenes for streamers")
        self.release(checks=[])
        p = mock.patch("factory.release.unreleased_prs", return_value=([{"number": 51, "title": "Merged, not shipped"}], "sha", []))
        p.start(), self.addCleanup(p.stop)

    def touch(self, pid, days, text="x"):
        store.append_line(f"updates/{pid}.jsonl", {"at": self.ago(days), "agent": "ab", "health": "onTrack", "text": text})

    def proto(self, pid, feature, status, project=None, days=1):
        protos = store.read("protos.json", [])
        protos.append({"id": pid, "product": "acme", "feature": feature, "project": project,
                       "versions": [{"v": 1, "status": status, "date": self.ago(days)}]})
        store.write("protos.json", protos)

    def pm_state(self, **kw):
        store.write("pm.json", {"snoozed": {}, "lastSweep": {}, **kw})

    def rows(self, now=None, errs=None):
        now = now or self.T
        return {r["id"]: r for r in pm.lifecycle(pm.signals(now)["signals"], pm.unreleased(), now, errs)}

    def seed(self):
        self.digest(self.run_entry(1, "Morocco bank transfers declined (3 tickets)"))
        self.proto("acme-faster-checkout", "Faster checkout page", "waiting")  # no project: matched to R1 by 2 title words
        self.proto("acme-lock", "Lock withdrawals for identity", "validated", days=2)
        self.project("plan", createdAt=self.ago(2))
        for pid, pr in (("rev", 50), ("dev", 51), ("prod", 41), ("ann", 42), ("d7", 43)):
            self.project(pid, [], prs=[f"acme/app#{pr}"], createdAt=self.ago(2))
        self.project("studio", [], feat="R4", createdAt=self.ago(2))
        self.project("elsewhere", [], prs=["other/repo#5"], createdAt=self.ago(2))
        self.project("idle", [], createdAt=self.ago(2))
        data = store.read("release.json", None)
        store.write("release.json", dict(data, shipped_prs={"acme": [41, 42, 43]},
                                         journal=[self.ship(2, [41]), self.ship(4, [42]), self.ship(8, [43], "s43")]))
        self.jobs({"id": "f-acme-42", "product": "acme", "kind": "feature", "prs": [{"n": 42}], "status": "sent", "sent_at": self.ago(1)})
        self.pm_state(launches={"acme:s43": {"at": self.ago(8), "checks": {"d1": {"at": self.ago(7), "chips": []},
                                                                           "d7": {"at": self.ago(1), "chips": []}}}})

    def test_each_stage_is_derived_from_its_source(self):
        self.seed()
        r = self.rows()
        self.assertEqual({k: v["stageName"] for k, v in r.items()}, {
            "acme:R1": "Prototype", "acme:R2": "Roadmap", "acme:R3": "Validated", "plan": "Build", "rev": "Review", "dev": "Develop",
            "prod": "Prod", "ann": "Announced", "d7": "Day 7", "studio": "Roadmap", "elsewhere": "Build", "idle": None})
        self.assertEqual((r["acme:R2"]["reached"][:2], r["acme:R1"]["reached"][:2]), ([True, True], [False, True]))  # the ticket maps to R2
        self.assertEqual((r["studio"]["tag"], r["studio"]["title"]), ("R4", "Project studio"))  # R4 rides on its project
        self.assertEqual((r["prod"]["days"], r["ann"]["days"], r["d7"]["days"], r["acme:R3"]["days"]), (2, 1, 1, 2))
        self.assertIsNone(r["dev"]["days"])  # no merge time is kept: the first pm run that sees it there dates it
        self.assertEqual(r["idle"]["note"], "No update yet")

    def test_days_in_stage_and_the_stuck_limit(self):
        self.project("rev", [], prs=["acme/app#50"], createdAt=self.ago(20))
        self.touch("rev", 3, "PR up")
        r = self.rows()["rev"]
        self.assertEqual((r["days"], r["stuck"], r["note"]), (3, False, "PR up"))  # 3 days: at the limit, not past it
        r = self.rows(self.T + timedelta(days=1))["rev"]
        self.assertEqual((r["days"], r["stuck"], r["note"]), (4, True, "Review for 4 days (limit 3): PR not merged"))
        # a Now row has no date of its own: a pm run notes when it first saw it at a stage; a new stage restarts it
        o = pm.overview(self.T)
        self.assertIsNone(next(x for x in o["lifecycle"] if x["id"] == "acme:R1")["days"])
        pm.record(o)
        self.assertEqual(store.read("pm.json", None)["stages"]["acme:R1"], {"stage": "Roadmap", "at": o["at"]})
        pm.record(pm.overview(self.T + timedelta(days=1)))
        self.assertEqual(store.read("pm.json", None)["stages"]["acme:R1"]["at"], o["at"])
        self.assertEqual((self.rows(self.T + timedelta(days=14))["acme:R1"]["stuck"], self.rows(self.T + timedelta(days=15))["acme:R1"]["stuck"]),
                         (False, True))
        self.proto("acme-faster-checkout", "Faster checkout page", "waiting")
        pm.record(pm.overview(self.T + timedelta(days=15)))
        self.assertEqual(store.read("pm.json", None)["stages"]["acme:R1"]["stage"], "Prototype")

    def test_a_stage_with_no_limit_is_never_stuck(self):
        self.seed()
        r = self.rows(self.T + timedelta(days=400))  # announced over a year ago: no limit, no crash
        self.assertEqual((r["ann"]["stageName"], r["ann"]["stuck"]), ("Announced", False))

    def test_one_failing_product_read_does_not_stop_the_other_clocks(self):
        data = store.read("release.json", None)
        store.write("release.json", dict(data, products=dict(data["products"], beta=dict(data["products"]["acme"], repo="beta/app"))))

        def fake(cfg):
            if cfg["repo"] == "beta/app":
                raise store.FactoryError("compare truncated")
            return ([], "sha", [])
        with mock.patch("factory.release.unreleased_prs", side_effect=fake):
            pm.record(pm.overview(self.T))
            self.assertEqual(store.read("pm.json", None)["stages"]["acme:R1"]["stage"], "Roadmap")
            pm._unreleased_cache.clear()
            r1 = next(x for x in pm.overview(self.T + timedelta(days=20))["lifecycle"] if x["id"] == "acme:R1")
        self.assertEqual((r1["days"], r1["stuck"]), (20, True))

    def test_shipped_with_no_journal_entry_is_finished_not_stuck(self):
        self.project("old", [], prs=["acme/app#43"], createdAt=self.ago(400))
        data = store.read("release.json", None)
        store.write("release.json", dict(data, shipped_prs={"acme": [43]}, journal=[]))  # the entry rotated out
        pm.record(pm.overview(self.T))
        r = self.rows(self.T + timedelta(days=4))["old"]
        self.assertEqual((r["stageName"], r["stuck"]), ("Prod", False))

    def test_another_features_error_does_not_block_unstick(self):
        self.stuck_review()
        real = pm._reached

        def broken(title, product, p, *a):
            if p and p["id"] == "idle":
                raise ValueError("bad project")
            return real(title, product, p, *a)
        self.project("idle", [], createdAt=self.ago(2))
        with mock.patch("factory.pm._reached", side_effect=broken):
            self.assertEqual(pm.unstick("rev", now=self.T)["feature"], "rev")
            with self.assertRaises(store.FactoryError):
                pm.unstick("idle", now=self.T)

    def test_stuck_rows_first_then_by_stage(self):
        self.seed()
        self.project("plan", createdAt=self.ago(11))  # Build, no update for 11 days: stuck
        order = list(self.rows())  # same stage: the longest there first
        self.assertEqual(order, ["plan", "idle", "studio", "acme:R2", "acme:R1", "acme:R3", "elsewhere", "rev", "dev", "prod", "ann", "d7"])

    def test_a_failing_source_costs_its_part_not_the_tab(self):
        self.seed()
        store.write("protos.json", {"not": "a list"})
        self.project("bad", [], prs=["acme/app#notanumber"])
        status, body = serve.pm_data()
        self.assertEqual(status, 200, body)
        rows = {r["id"]: r["stageName"] for r in body["lifecycle"]}
        self.assertEqual((rows["acme:R1"], rows["acme:R3"], rows["prod"]), ("Roadmap", "Roadmap", "Prod"))  # prototypes left out
        self.assertNotIn("bad", rows)
        self.assertTrue(any(e.startswith("protos.json") for e in body["errors"]["life"]), body["errors"])
        self.assertTrue(any(e.startswith("lifecycle bad:") for e in body["errors"]["life"]), body["errors"])
        pm.record(pm.overview(self.T))
        self.assertEqual(store.read("pm.json", None)["stages"], {})  # a failed read starts no clock

    def stuck_review(self, phases=None):
        self.project("rev", [phase("One", 1, 4)] if phases is None else phases, prs=["acme/app#50"], createdAt=self.ago(20))
        self.touch("rev", 5, "PR up")

    def test_unstick_and_undo_through_the_api(self):
        self.stuck_review()
        self.project("fine", prs=["acme/app#51"])
        for patch in ({"action": "unstick", "feature": "nosuch"}, {"action": "unstick"}, {"action": "unstick", "feature": 7},
                      {"action": "unstick", "feature": "fine"}, {"action": "unstick", "feature": "rev", "undo": "yes"},
                      {"action": "unstick", "feature": "rev", "undo": True}):
            status, body = serve.save_pm(patch)
            self.assertEqual(status, 400, (patch, body))
            self.assertIn("error", body)
        self.assertEqual(store.inbox()["items"], [])
        status, body = serve.save_pm({"action": "unstick", "feature": "rev"})
        self.assertEqual(status, 200, body)
        items = store.inbox()["items"]
        self.assertEqual([(i["project"], i["from"], i["kind"]) for i in items], [("rev", pm.UNSTICK_SENDER, "action")])
        self.assertEqual(items[0]["title"], "Unstick Project rev: Review 5 days")
        self.assertEqual(items[0]["context"], "1. Open Factory > Product manager > Lifecycle (1 min)\n"
                                              "2. Open the PR and merge or close it (5 min)")
        self.assertEqual(items[0]["why"], "Review for 5 days (limit 3): PR not merged")
        self.assertEqual(self.rows()["rev"]["nudged"], body["nudged"]["at"])
        self.assertEqual(store.read("pm.json", None)["nudged"]["rev"]["item"], items[0]["id"])
        self.assertEqual(serve.save_pm({"action": "unstick", "feature": "rev"})[0], 400)  # once per 3 days
        self.assertEqual(len(store.inbox()["items"]), 1)
        self.assertIsNone(self.rows(self.T + timedelta(days=3, minutes=1))["rev"]["nudged"])  # the button is back after 3 days
        self.assertFalse(pm._open_memo())  # an open unstick item never holds back the memo
        self.assertEqual(serve.save_pm({"action": "unstick", "feature": "rev", "undo": True})[0], 200)
        self.assertIsNone(self.rows()["rev"]["nudged"])
        self.assertEqual(len(store.inbox()["items"]), 1)  # the inbox item stays

    def test_build_step_names_the_project(self):
        self.project("slow", createdAt=self.ago(12))
        iid = pm.unstick("slow")["item"]
        item = next(i for i in store.inbox()["items"] if i["id"] == iid)
        self.assertEqual(item["context"].splitlines()[1], "2. Post an update or a plan for slow (2 min)")

    def test_two_unsticks_at_once_file_one_item(self):
        import threading
        self.stuck_review()
        orig, errors = store.add_ask_item, []

        def slow(*a, **k):
            time.sleep(0.3)  # the second click arrives while the first one files its item
            return orig(*a, **k)

        def click():
            try:
                pm.unstick("rev", now=self.T)
            except store.FactoryError as e:
                errors.append(e)
        with mock.patch("factory.store.add_ask_item", slow):
            ts = [threading.Thread(target=click) for _ in range(2)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        self.assertEqual(len([i for i in store.inbox()["items"] if i["from"] == pm.UNSTICK_SENDER]), 1)
        self.assertEqual(len(errors), 1)

    def test_unstick_never_files_on_a_project_the_page_cannot_draw(self):
        self.stuck_review(phases=[])  # PRs but no phases: the build skips it
        self.project("drawn", createdAt=self.ago(1))
        iid = pm.unstick("rev")["item"]
        item = next(i for i in store.inbox()["items"] if i["id"] == iid)
        self.assertEqual(item["project"], "drawn")
        pm.close("drawn")
        pm.unstick("rev", undo=True)
        with self.assertRaises(store.FactoryError):
            pm.unstick("rev")

    def test_record_drops_nudges_after_3_days(self):
        self.stuck_review()
        pm.unstick("rev", now=self.T)
        pm.record(pm.sweep(self.T + timedelta(days=2)))
        self.assertIn("rev", store.read("pm.json", None)["nudged"])
        pm.record(pm.sweep(self.T + timedelta(days=3)))
        self.assertEqual(store.read("pm.json", None)["nudged"], {})

    def test_a_row_only_feature_files_on_an_open_project(self):
        self.project("fine", createdAt=self.ago(1))
        self.pm_state(stages={"acme:R2": {"stage": "Roadmap", "at": self.ago(20)}})
        r = pm.unstick("acme:R2")
        item = next(i for i in store.inbox()["items"] if i["id"] == r["item"])
        self.assertEqual(item["project"], "fine")
        self.assertEqual(item["why"], "Roadmap for 20 days (limit 14): no prototype yet")

    def test_row_ids_are_per_product(self):
        store.write("products.json", {k: {"name": k.title(), "workspaces": [], "repos": [f"{k}/app"], "run_labels": []}
                                      for k in ("acme", "beta")})
        data = store.read("roadmap.json", None)
        data["items"] += [{"id": f"R{k}", "product": "beta", "title": t, "lane": "now", "rank": k, "source": "factory"}
                          for k, t in ((1, "Onboarding wizard steps"), (2, "Invoices export to spreadsheets"))]
        store.write("roadmap.json", data)
        self.project("ck", [], feat="R1", createdAt=self.ago(2))  # acme's R1, not beta's
        self.digest(self.run_entry(1, "Invoices export to spreadsheets fails (2 tickets)"))
        r = self.rows()
        self.assertEqual(sorted(r), ["acme:R2", "acme:R3", "acme:R4", "beta:R1", "beta:R2", "ck"])
        self.assertEqual((r["beta:R1"]["tag"], r["beta:R1"]["product"]), ("R1", "beta"))
        self.assertEqual((r["beta:R2"]["reached"][0], r["acme:R2"]["reached"][0]), (True, False))  # the ticket maps to beta's R2

    def test_develop_is_dated_by_the_newest_merge(self):
        self.project("dev", [], prs=["acme/app#51", "acme/app#52"], createdAt=self.ago(20))
        merged = [{"number": 51, "title": "a", "mergedAt": self.ago(8)}, {"number": 52, "title": "b", "mergedAt": self.ago(6)}]
        with mock.patch("factory.release.unreleased_prs", return_value=(merged, "sha", [])):
            r = self.rows()["dev"]
        self.assertEqual((r["stageName"], r["days"], r["stuck"]), ("Develop", 6, True))
        self.assertEqual(r["note"], "Develop for 6 days (limit 5): not in prod yet")

    def test_a_failed_merged_read_is_a_lifecycle_error(self):
        self.project("dev", [phase("One", 1, 4)], prs=["acme/app#51"], createdAt=self.ago(20))
        pm.record(pm.overview(self.T - timedelta(days=6)))
        self.assertEqual(store.read("pm.json", None)["stages"]["dev"]["stage"], "Develop")
        pm._unreleased_cache.clear()
        with mock.patch("factory.release.unreleased_prs", side_effect=store.FactoryError("gh: HTTP 502")):
            o = pm.overview(self.T)
            self.assertIn("unreleased acme: gh: HTTP 502", o["errors"]["life"])
            pm.record(o)
            self.assertEqual(store.read("pm.json", None)["stages"]["dev"]["stage"], "Develop")  # not overwritten by Build
            code, r, err = run("pm", "lifecycle")
            self.assertEqual(code, 0, err)
            self.assertIn("could not read unreleased acme: gh: HTTP 502", err)
            with self.assertRaises(store.FactoryError):
                pm.unstick("dev")
        with mock.patch("factory.pm.store.protos", side_effect=store.FactoryError("protos.json is not a list")):
            with self.assertRaisesRegex(store.FactoryError, "protos.json"):
                pm.unstick("dev")

    def test_a_launch_past_the_launch_window_is_never_stuck(self):
        self.project("old", [], prs=["acme/app#43"], createdAt=self.ago(60))
        data = store.read("release.json", None)
        store.write("release.json", dict(data, shipped_prs={"acme": [43]}, journal=[self.ship(25, [43], "s43")]))
        self.pm_state(launches={"acme:s43": {"at": self.ago(25), "checks": {"d7": {"at": self.ago(18), "chips": []}}}})
        self.assertEqual(self.rows()["old"]["stageName"], "Day 7")
        r = self.rows(self.T + timedelta(days=11))["old"]  # 36 days after the ship: the launch left the window
        self.assertEqual((r["stageName"], r["stuck"]), ("Prod", False))

    def test_answered_unstick_items_resolve_on_a_quiet_day(self):
        self.stuck_review()
        iid = pm.unstick("rev", now=self.T)["item"]
        store.write("answers.json", {iid: {"choice": "done", "at": self.ago(0)}})
        with mock.patch("factory.release.unreleased_prs", return_value=([], "sha", [])):
            pm._unreleased_cache.clear()
            self.assertIsNone(pm.memo(self.T))  # a quiet day
        self.assertEqual(store.inbox()["items"], [])
        self.assertEqual([i["id"] for i in store.inbox()["resolved"]], [iid])
        self.assertEqual(pm.overview(self.T)["stats"]["inbox"], 0)

    def test_stuck_tile_counts_stuck_lifecycle_rows(self):
        self.stuck_review()
        self.project("fine", [], createdAt=self.ago(1))
        self.pm_state(stages={"acme:R2": {"stage": "Roadmap", "at": self.ago(20)}})
        o = pm.overview(self.T)
        self.assertEqual(o["stats"]["stuck"], 2)
        self.assertEqual([r["id"] for r in o["lifecycle"] if r["stuck"]], ["acme:R2", "rev"])
        self.assertEqual(o["lifeStages"], pm.STAGES)

    def test_cli_lifecycle_and_unstick(self):
        self.stuck_review()
        code, r, err = run("pm", "lifecycle")
        self.assertEqual((code, r[0]["id"], r[0]["stuck"], err), (0, "rev", True, ""))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["pm", "lifecycle"]), 0)
        self.assertIn("rev\tReview\t5d\tSTUCK\tProject rev\tReview for 5 days (limit 3): PR not merged", out.getvalue())
        self.assertNotEqual(run("pm", "unstick", "nosuch")[0], 0)
        code, r, err = run("pm", "unstick", "rev")
        self.assertEqual((code, r["nudged"]["stage"]), (0, "Review"), err)
        self.assertNotEqual(run("pm", "unstick", "rev")[0], 0)
        code, r, err = run("pm", "unstick", "rev", "--undo")
        self.assertEqual((code, r["nudged"]), (0, None), err)
        self.assertNotEqual(run("pm", "unstick", "rev", "--undo")[0], 0)


class Kit(Base):
    """After launch, phase 4b: the launch kit line and Draft the missing (stores the changelog; the comms run drafts the rest)."""
    ago = Phase2.ago
    ship = Phase3.ship
    DUE = "due: the release comms run drafts it"

    def setUp(self):
        super().setUp()
        pm._launch_cache.clear()
        self.T = datetime.now(timezone.utc).replace(microsecond=0)
        self.facts = {41: {"title": "feat: Bulk export as CSV", "labels": ["big"]}, 42: {"title": "feat: Saved filters", "labels": ["big"]}}
        p = mock.patch("factory.comms.pr_facts", side_effect=lambda repo, ns: {n: self.facts[n] for n in ns if n in self.facts})
        p.start(), self.addCleanup(p.stop)
        self.release([self.ship(2, [41])])

    def release(self, journal=None, comms_cfg=True, paused=None, **cfg):
        """A live product with a thread (the comms relay runs for it) unless cfg says otherwise."""
        Phase3.release(self, journal if journal is not None else [self.ship(2, [41])], checks=[], comms_cfg=comms_cfg)
        rel = store.read("release.json", None)
        rel["products"]["acme"].update({"thread": "t-acme", **cfg})
        rel["paused"] = paused or {}
        store.write("release.json", rel)
        self.at = rel["journal"][0]["at"]

    def comms(self, *jobs, changelog=True, group="new", big=True, since=30, prs=(41,)):
        items = [{"n": n, "title": f"PR {n}", "group": group, "big": big} for n in prs]
        cl = {f"acme@{self.at}": {"product": "acme", "at": self.at, "items": items}} if changelog else {}
        store.write("comms.json", {"changelogs": cl, "jobs": {j["id"]: j for j in jobs},
                                   "since": {} if since is None else {"acme": self.ago(since)}, "deferred": {}, "relayed": {}})

    def kit(self, lid="acme:41"):
        return next(c for c in pm.launches([], self.T) if c["id"] == lid)["kit"]

    def items(self):
        return {x["name"]: (x["state"], x["detail"]) for x in self.kit()["items"]}

    def job(self, n=41, **kw):
        return {"id": f"f-acme-{n}", "product": "acme", "kind": "feature", "prs": [{"n": n}], **kw}

    def test_item_states(self):
        self.comms(changelog=False)
        k = self.kit()
        self.assertEqual(k["missing"], ["Changelog"])  # the only item the click makes
        wait = ("w", "waits for the changelog")
        self.assertEqual((self.items()["Email"], self.items()["Brag video"], self.items()["Caption"]), (wait,) * 3)
        self.assertIsNone(k["queued"])
        self.comms()  # a big PR with no job: due, the comms run drafts it; never counted
        self.assertEqual((self.kit()["missing"], self.items()["Email"], self.items()["Caption"]), ([], ("w", self.DUE), ("w", self.DUE)))
        video = {"url": "https://example.com/v.mp4", "frames": ["x"]}
        self.comms(self.job(status="sent", draft={"caption": "c", "video": video}))
        self.assertEqual(self.items(), {"Changelog": ("y", ""), "Email": ("y", "sent"), "Brag video": ("y", ""), "Caption": ("y", "")})
        self.comms(self.job(status="skipped"))
        self.assertEqual(self.items()["Email"], ("y", "skipped"))
        for st in ("ready", "needs-approval"):
            self.comms(self.job(status=st, draft={"subject": "s"}))
            self.assertEqual((self.items()["Email"], self.items()["Caption"]), (("w", st), ("w", "drafted without it")))
        self.comms(self.job(status="needs-approval", draft={"subject": "s"}, video_failed_at=self.ago(1)))
        self.assertEqual(self.items()["Brag video"], ("w", "static card"))
        self.comms(self.job(video_failed_at=self.ago(1)))  # stored by a video failure, no draft yet: still due
        self.assertEqual(self.items()["Email"], ("w", self.DUE))
        self.comms(self.job(99, status="sent"))  # another launch's job
        self.assertEqual(self.items()["Email"], ("w", self.DUE))

    def test_email_is_per_pr(self):
        self.release([self.ship(2, [41, 42])])
        self.comms(self.job(41, status="sent"), prs=(41, 42))
        self.assertEqual(self.items()["Email"], ("w", self.DUE))  # 41 went out, 42 is still due
        self.assertEqual(self.items()["Brag video"], ("w", self.DUE))
        self.comms(self.job(41, status="sent"), self.job(42, status="skipped"), prs=(41, 42))
        self.assertEqual(self.items()["Email"], ("y", "sent"))

    def test_comms_relay_off(self):
        self.comms()
        for kw, why in (({"mode": "shadow"}, "shadow mode"), ({"thread": None}, "no thread"),
                        ({"paused": {"acme": True}}, "paused"), ({"paused": {"*": True}}, "paused")):
            self.release(**kw)
            self.assertEqual(self.items()["Email"], ("w", f"comms relay off: {why}"), kw)

    def test_redraft_pending(self):
        for st in ("needs-edit", "failed"):
            self.comms(self.job(status=st, draft={"subject": "s"}))
            self.assertEqual((self.items()["Email"], self.items()["Brag video"], self.items()["Caption"]),
                             (("w", st), ("w", "redraft pending"), ("w", "redraft pending")))

    def test_sent_without_it_is_not_missing(self):
        self.comms(self.job(status="sent", draft={"subject": "s"}))
        self.assertEqual((self.items()["Brag video"], self.items()["Caption"]), (("w", "sent without it"),) * 2)
        self.assertEqual(self.kit()["missing"], [])  # no button
        self.assertEqual(serve.save_pm({"action": "kit", "launch": "acme:41"})[0], 400)

    def test_small_prs_wait_for_the_digest(self):
        self.comms(big=False)
        email = self.items()["Email"]
        self.assertEqual(email[0], "w")
        self.assertRegex(email[1], r"^in the \d+ \w{3} digest$")
        self.assertEqual((self.items()["Caption"], self.kit()["missing"]), (email, []))
        self.release(mode="shadow")  # no relay: no agent drafts the digest either
        self.assertEqual(self.items()["Email"], ("w", "comms relay off: shadow mode"))
        self.release([self.ship(0, [41])], mode="shadow")
        self.comms(since=None)  # comms never ran: with the relay off, no run will start it
        self.assertEqual(self.items()["Email"], ("w", "comms relay off: shadow mode"))

    def test_internal_prs_need_no_email(self):
        self.release([self.ship(2, [41, 42])])
        self.comms(self.job(41, status="sent"), prs=(41, 42))
        data = store.read("comms.json", None)
        data["changelogs"][f"acme@{self.at}"]["items"][1]["group"] = "internal"  # 42 is internal
        store.write("comms.json", data)
        self.assertEqual(self.items()["Email"], ("y", "sent"))

    def test_shipped_before_comms_started(self):
        self.comms(since=1)  # comms started a day after the ship
        self.assertEqual((self.items()["Email"], self.kit()["missing"]), (("w", "shipped before comms started"), []))
        self.release([self.ship(10, [41])])
        self.comms(since=None)  # unset: the Monday of this week, as comms.due(write=True) would set it
        self.assertEqual(self.items()["Email"], ("w", "shipped before comms started"))

    def test_no_kit_cases(self):
        self.comms(group="internal")
        self.assertEqual((self.kit()["note"], self.kit()["missing"]), ("internal release, no kit", []))
        self.assertRaises(store.FactoryError, pm.kit, "acme:41", now=self.T)
        self.release(comms_cfg=False)
        self.assertEqual(self.kit()["note"], "no comms configured")
        self.release()
        with mock.patch("factory.comms.comms", side_effect=ValueError("comms.json is corrupt")):
            c = next(c for c in pm.launches([], self.T) if c["id"] == "acme:41")
            self.assertEqual(c["kit"]["error"], "could not read: comms.json is corrupt")
            self.assertEqual(len(c["chips"]), 3)  # the card stays
            status, body = serve.pm_data()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["launches"][0]["kit"]["error"], "could not read: comms.json is corrupt")

    def test_draft_the_missing_and_undo_through_the_api(self):
        self.comms(changelog=False)
        status, body = serve.save_pm({"action": "kit", "launch": "acme:41"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["queued"]["items"], ["Changelog"])
        self.assertNotIn("jobs", body)
        self.assertEqual(store.read("comms.json", None)["changelogs"][f"acme@{self.at}"]["items"][0]["group"], "new")
        self.assertEqual(store.read("comms.json", None)["jobs"], {})  # it drafts nothing
        k = self.kit()
        self.assertEqual((k["queued"], k["items"][0]["state"], k["missing"]), (body["queued"], "y", []))
        self.assertEqual(serve.save_pm({"action": "kit", "launch": "acme:41"})[0], 400)  # already queued
        self.assertEqual(serve.save_pm({"action": "kit", "launch": "acme:41", "undo": True})[0], 200)
        self.assertIsNone(self.kit()["queued"])
        self.assertIn(f"acme@{self.at}", store.read("comms.json", None)["changelogs"])  # the changelog stays
        for patch in ({"action": "kit", "launch": "acme:99"}, {"action": "kit"}, {"action": "kit", "launch": "acme:41", "undo": "yes"},
                      {"action": "kit", "launch": "acme:41", "undo": True}, {"action": "kit", "launch": "acme:41"}):
            status, body = serve.save_pm(patch)
            self.assertEqual(status, 400, patch)  # the last: the changelog is already stored
            self.assertIn("error", body)

    def test_a_failing_step_records_nothing(self):
        self.comms(changelog=False)
        with mock.patch("factory.comms.changelog", side_effect=store.FactoryError("gh failure reading acme/app")):
            status, body = serve.save_pm({"action": "kit", "launch": "acme:41"})
        self.assertEqual(status, 500)
        self.assertIn("gh failure reading acme/app", body["error"])
        self.assertIsNone(self.kit()["queued"])
        self.assertNotIn("acme:41", store.read("pm.json", {}).get("launches", {}))

    def test_an_unreadable_kit_is_a_500(self):
        self.comms(changelog=False)
        with mock.patch("factory.comms.comms", side_effect=ValueError("comms.json is corrupt")):
            status, body = serve.save_pm({"action": "kit", "launch": "acme:41"})
        self.assertEqual(status, 500)
        self.assertIn("comms.json is corrupt", body["error"])
        self.assertNotIn("acme:41", store.read("pm.json", {}).get("launches", {}))

    def test_cli(self):
        self.comms(changelog=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["pm", "launches"]), 0)
        self.assertIn("kit: FAIL Changelog warn Email (waits for the changelog) warn Brag video (waits for the changelog)", out.getvalue())
        code, r, err = run("pm", "kit", "acme:41")
        self.assertEqual((code, r["queued"]["items"]), (0, ["Changelog"]), err)
        self.assertNotEqual(run("pm", "kit", "acme:41")[0], 0)
        code, r, err = run("pm", "kit", "acme:41", "--undo")
        self.assertEqual((code, r["queued"]), (0, None), err)
        self.assertNotEqual(run("pm", "kit", "acme:41", "--undo")[0], 0)
        self.assertNotEqual(run("pm", "kit", "acme:99")[0], 0)


class Roadmaps(Base):
    ROWS = [{"lane": "next", "title": "Team invites", "goal": "Invite by email", "evidence": "9 tickets"},
            {"lane": "now", "title": "Faster checkout", "goal": "Checkout under 2 s", "evidence": "#12 #14"},
            {"lane": "later", "title": "Dark mode", "goal": "A dark theme", "evidence": "4 tickets"}]
    LANES = [{"lane": "next", "title": "N1", "goal": "g", "evidence": "e"}, {"lane": "now", "title": "W1", "goal": "g", "evidence": "e"},
             {"lane": "next", "title": "N2", "goal": "g", "evidence": "e"}, {"lane": "now", "title": "W2", "goal": "g", "evidence": "e"},
             {"lane": "next", "title": "N3", "goal": "g", "evidence": "e"}]

    def setUp(self):
        super().setUp()
        store.write("products.json", {"acme": {"name": "Acme"}, "tools": {"name": "Tools"}})
        notion = mock.patch("factory.roadmap._notion_http", side_effect=AssertionError("tests never call Notion"))
        notion.start()
        self.addCleanup(notion.stop)

    def propose(self, rows=None, product="acme", **kw):
        return pm.propose_roadmap(product, rows if rows is not None else [dict(r) for r in self.ROWS], now=NOW, **kw)

    def at(self, product="acme"):
        return store.read("pm.json", {})["roadmaps"][product]["at"]

    def row(self, rid, action, undo=False, **kw):
        return pm.roadmap_row(rid, action, self.at(rid.rsplit("-r", 1)[0]), undo, **kw)

    def api(self, **patch):
        product = patch.get("product") or patch.get("row", "acme-r").rsplit("-r", 1)[0]
        return serve.save_pm({"at": self.at(product) if product in ("acme", "tools") else "x", **patch})

    def rows(self, product="acme"):
        return {r["id"]: r for r in store.read("pm.json", {})["roadmaps"][product]["rows"]}

    def card(self, product):
        return next(c for c in pm.roadmaps() if c["product"] == product)

    def lane(self, lane, product="acme"):
        return [i["title"] for i in roadmap.items(product) if i["lane"] == lane]

    def fill_now(self, n=7):
        data = roadmap.load()
        data["items"] += [{"id": f"N{k}", "product": "acme", "title": f"Now {k}", "lane": "now", "rank": k, "source": "factory"}
                          for k in range(1, n + 1)]
        store.save_roadmap(data)

    def test_proposal_validation(self):
        r = self.ROWS[0]
        bad = [("nosuch", self.ROWS, "unknown product"), ("acme", self.ROWS[:2], "3 to 12 rows, got 2 rows"),
               ("acme", self.ROWS * 5, "3 to 12 rows, got 15"), ("acme", {"rows": 1}, "got no list"),
               ("acme", self.ROWS[:2] + ["x"], "row 3 must be an object"),
               ("acme", self.ROWS[:2] + [dict(r, goal=" ")], "row 3: goal must be a non-empty string"),
               ("acme", self.ROWS[:2] + [dict(r, evidence=4)], "row 3: evidence must be a non-empty string"),
               ("acme", self.ROWS[:2] + [{k: v for k, v in r.items() if k != "lane"}], "row 3: lane must be"),
               ("acme", self.ROWS[:2] + [dict(r, title="x" * 81)], "title is 81 characters, at most 80"),
               ("acme", self.ROWS[:2] + [dict(r, title="two\nlines")], "title must be one line"),
               ("acme", self.ROWS[:2] + [dict(r, lane="icebox")], "lane 'icebox' is not one of now, next, later"),
               ("acme", self.ROWS[:2] + [dict(self.ROWS[1], lane="later", title="P2 — " + self.ROWS[1]["title"].upper())],
                "titles must differ")]
        for product, rows, msg in bad:
            with self.assertRaises(store.FactoryError, msg=msg) as e:
                pm.propose_roadmap(product, rows)
            self.assertIn(msg, str(e.exception))
        self.assertNotIn("acme", store.read("pm.json", {}).get("roadmaps", {}))
        self.propose(self.ROWS[:2] + [dict(r, title="x" * 80)])  # 80 is fine
        r = self.propose(sources=" 12 issues ")
        self.assertEqual((r["sources"], [x["id"] for x in r["rows"]], r["rows"][1]["status"]),
                         ("12 issues", ["acme-r1", "acme-r2", "acme-r3"], "proposed"))
        self.assertEqual(self.rows()["acme-r3"]["title"], "Dark mode")  # the newer proposal replaced the older one

    def test_a_product_with_a_roadmap_is_refused(self):
        self.fill_now(1)
        with self.assertRaisesRegex(store.FactoryError, "already has a roadmap"):
            self.propose()

    def test_more_now_rows_than_the_room_in_now_is_refused(self):
        store.write("config.json", {"roadmap": {"cap": 2}})
        with self.assertRaisesRegex(store.FactoryError, "3 Now rows, but acme's Now lane has room for 2"):
            self.propose([dict(r, lane="now") for r in self.ROWS])
        self.propose([dict(r, lane="now") for r in self.ROWS[:2]] + [self.ROWS[2]])

    def test_a_source_in_error_is_refused(self):
        store.save_roadmap({"items": [], "sources": {"acme": {"syncedAt": iso(1), "error": "Notion HTTP 401"}}})
        with self.assertRaisesRegex(store.FactoryError, r"acme's roadmap source is in error \(Notion HTTP 401\)"):
            self.propose()

    def test_a_notion_source_never_synced_is_refused(self):
        store.write("config.json", {"roadmap": {"sources": {"acme": {"notion_database": "db", "lanes": {"now": "Now"}}}}})
        with self.assertRaisesRegex(store.FactoryError, "acme's Notion roadmap has never synced: run factory roadmap sync acme"):
            self.propose()
        store.save_roadmap({"items": [], "sources": {"acme": {"syncedAt": iso(1), "error": None}}})
        self.propose()

    def test_a_proposal_with_an_approved_row_is_not_replaced(self):
        self.propose()
        self.row("acme-r1", "approve")
        store.save_roadmap(dict(roadmap.load(), items=[]))  # the item left the roadmap since
        with self.assertRaisesRegex(store.FactoryError, "has approved rows: it cannot be replaced"):
            self.propose()
        self.assertEqual(self.rows()["acme-r1"]["status"], "approved")

    def test_approve_adds_the_item_with_its_note_and_marks_the_row(self):
        self.propose()
        status, body = self.api(action="roadmap-row", row="acme-r2", op="approve")
        self.assertEqual(status, 200, body)
        item = next(i for i in roadmap.items("acme") if i["title"] == "Faster checkout")
        self.assertEqual((item["lane"], item["note"]), ("now", "Goal: Checkout under 2 s · Evidence: #12 #14"))
        row = self.rows()["acme-r2"]
        self.assertEqual((row["status"], row["item"], body["row"]["item"]), ("approved", item["id"], item["id"]))
        self.assertTrue(row["at"])
        for undo in (False, True):  # once only, and no undo
            self.assertEqual(self.api(action="roadmap-row", row="acme-r2", op="approve", undo=undo)[0], 400)
        self.assertEqual(len(roadmap.items("acme")), 1)

    def test_approve_all_keeps_the_proposal_order_in_each_lane(self):
        self.propose([dict(r) for r in self.LANES])
        status, body = self.api(action="roadmap-all", product="acme")
        self.assertEqual((status, body["approved"]), (200, 5), body)
        self.assertEqual((self.lane("next"), self.lane("now")), (["N1", "N2", "N3"], ["W1", "W2"]))
        self.assertEqual([i["rank"] for i in roadmap.items("acme") if i["lane"] == "next"], [1, 2, 3])

    def test_single_approves_keep_the_proposal_order_in_any_click_order(self):
        self.propose([dict(r) for r in self.LANES])
        self.row("acme-r1", "approve")
        self.row("acme-r3", "approve")  # top to bottom
        self.assertEqual(self.lane("next"), ["N1", "N2"])
        self.row("acme-r4", "approve")
        self.row("acme-r2", "approve")  # bottom to top
        self.assertEqual(self.lane("now"), ["W1", "W2"])
        self.row("acme-r5", "approve")
        self.assertEqual(([i["rank"] for i in roadmap.items("acme") if i["lane"] == "next"], self.lane("next")),
                         ([1, 2, 3], ["N1", "N2", "N3"]))

    def test_a_stale_proposal_is_a_409(self):
        self.propose()
        old = self.at()
        pm.propose_roadmap("acme", [dict(r, title=r["title"] + " v2") for r in self.ROWS], now=NOW + timedelta(minutes=5))
        for patch in ({"action": "roadmap-row", "row": "acme-r1", "op": "approve", "at": old},
                      {"action": "roadmap-row", "row": "acme-r1", "op": "drop", "at": old},
                      {"action": "roadmap-all", "product": "acme", "at": old}):
            status, body = serve.save_pm(patch)
            self.assertEqual((status, body["error"]), (409, "acme's proposal changed, reload"), patch)
        self.assertEqual(serve.save_pm({"action": "roadmap-all", "product": "acme"})[0], 400)  # at is required
        self.assertEqual((roadmap.items("acme"), {r["status"] for r in self.rows().values()}), ([], {"proposed"}))
        new = self.at()
        pm.propose_roadmap("acme", [dict(r, title=r["title"] + " v3") for r in self.ROWS], now=NOW + timedelta(minutes=5, microseconds=500))
        self.assertEqual(serve.save_pm({"action": "roadmap-row", "row": "acme-r1", "op": "drop", "at": new})[0], 409)  # same second

    def test_approve_after_a_failed_mark_does_not_add_twice(self):
        self.propose()
        with mock.patch("factory.pm._rm_set", side_effect=OSError("disk full")):
            status, body = self.api(action="roadmap-row", row="acme-r2", op="approve")
        self.assertEqual((status, body["row"]), (500, "acme-r2"))
        self.assertIn("acme-r2: added as acme-faster-checkout, but the row was not marked (disk full)", body["error"])
        self.assertEqual(self.rows()["acme-r2"]["status"], "proposed")
        status, body = self.api(action="roadmap-row", row="acme-r2", op="approve")
        self.assertEqual((status, body["row"]["item"]), (200, "acme-faster-checkout"), body)
        self.assertIn("already in the roadmap as acme-faster-checkout", body["row"]["warning"])
        self.assertEqual(self.lane("now"), ["Faster checkout"])

    def test_a_same_title_in_another_lane_is_not_reused(self):
        self.propose()
        data = roadmap.load()  # added by hand after the proposal, in another lane
        data["items"].append({"id": "acme-w", "product": "acme", "title": "Faster checkout", "lane": "later", "rank": 1, "source": "factory"})
        store.save_roadmap(data)
        status, body = self.api(action="roadmap-row", row="acme-r2", op="approve")
        self.assertEqual(status, 200, body)
        self.assertNotEqual(body["row"]["item"], "acme-w")
        self.assertEqual(self.lane("now"), ["Faster checkout"])

    def test_a_failed_note_marks_the_row_with_a_warning(self):
        self.propose()
        with mock.patch("factory.pm.roadmap.update", side_effect=roadmap.RoadmapError("read-only")):
            status, body = self.api(action="roadmap-row", row="acme-r1", op="approve")
        self.assertEqual((status, body["row"]["status"]), (200, "approved"), body)
        self.assertIn("the goal and evidence note was not saved: read-only", body["row"]["warning"])

    def test_approve_on_a_full_now_lane_marks_nothing(self):
        self.propose()
        self.fill_now()
        status, body = self.api(action="roadmap-row", row="acme-r2", op="approve")
        self.assertEqual((status, body["row"]), (500, "acme-r2"))
        self.assertIn("now is full", body["error"])
        self.assertEqual(self.rows()["acme-r2"]["status"], "proposed")
        self.assertNotIn("item", self.rows()["acme-r2"])
        self.assertEqual(len(roadmap.items("acme")), 7)

    def test_a_notion_failure_is_a_500_and_marks_nothing(self):
        self.propose()
        with mock.patch("factory.pm.roadmap.add", side_effect=roadmap.RoadmapError("Notion HTTP 502", code="notion_error")):
            status, body = self.api(action="roadmap-row", row="acme-r1", op="approve")
        self.assertEqual((status, body["error"]), (500, "acme-r1: Notion HTTP 502"))
        self.assertEqual(self.rows()["acme-r1"]["status"], "proposed")

    def test_approve_all_stops_at_the_first_error(self):
        self.propose()
        self.fill_now()
        status, body = self.api(action="roadmap-all", product="acme")
        self.assertEqual((status, body["row"]), (500, "acme-r2"))
        self.assertIn("1 of 3 added, then acme-r2: now is full", body["error"])
        self.assertEqual([r["status"] for r in self.rows().values()], ["approved", "proposed", "proposed"])
        store.save_roadmap(dict(roadmap.load(), items=[i for i in roadmap.load()["items"] if not i["id"].startswith("N")]))
        status, body = self.api(action="roadmap-all", product="acme")
        self.assertEqual((status, body["approved"]), (200, 2), body)
        self.assertEqual(sorted(i["title"] for i in roadmap.items("acme")), ["Dark mode", "Faster checkout", "Team invites"])
        self.assertEqual(self.api(action="roadmap-all", product="acme")[0], 400)  # nothing left to approve

    def test_drop_and_undo(self):
        self.propose()
        self.assertEqual(self.row("acme-r3", "drop", now=NOW)["row"]["status"], "dropped")
        self.assertEqual(self.rows()["acme-r3"]["at"], store.iso(NOW))
        for args in (("acme-r3", "drop"), ("acme-r3", "approve"), ("acme-r1", "drop", True)):
            with self.assertRaises(store.FactoryError):
                self.row(*args)
        r = self.row("acme-r3", "drop", undo=True)["row"]
        self.assertEqual((r["status"], "at" in r), ("proposed", False))
        self.assertEqual(roadmap.items("acme"), [])

    def test_bad_requests_are_400s(self):
        self.propose()
        at = self.at()
        for patch in ({"action": "roadmap-row", "row": "acme-r9", "op": "approve"}, {"action": "roadmap-row", "row": "nosuch", "op": "drop"},
                      {"action": "roadmap-row", "op": "drop"}, {"action": "roadmap-row", "row": "acme-r1", "op": "move"},
                      {"action": "roadmap-row", "row": "acme-r1", "op": "drop", "undo": "yes"},
                      {"action": "roadmap-row", "row": "acme-r1", "op": "drop", "at": None},
                      {"action": "roadmap-all", "product": "nosuch"}, {"action": "roadmap-all"}):
            status, body = serve.save_pm({"at": at, **patch})
            self.assertEqual(status, 400, patch)
            self.assertIn("error", body)
        store.write("products.json", {"tools": {"name": "Tools"}})  # acme was removed since
        self.assertEqual(serve.save_pm({"action": "roadmap-row", "row": "acme-r1", "op": "approve", "at": at}), (400, {"error": "unknown product 'acme'"}))
        self.assertEqual({r["status"] for r in self.rows().values()}, {"proposed"})

    def test_card_states(self):
        self.assertEqual([(c["product"], c["status"], c["sources"]) for c in pm.roadmaps()],
                         [("acme", "none", "none yet"), ("tools", "none", "none yet")])
        self.assertEqual(self.card("tools")["command"], "factory pm propose-roadmap tools --file rows.json")
        self.propose(sources="12 issues")
        c = self.card("acme")
        self.assertEqual((c["status"], c["sources"], c["name"], len(c["rows"]), c["at"][:19]), ("draft", "12 issues", "Acme", 3, store.iso(NOW)[:19]))
        self.row("acme-r1", "approve")
        self.assertEqual(self.card("acme")["status"], "draft")  # rows still to decide
        self.row("acme-r2", "drop")
        self.row("acme-r3", "approve")
        self.assertEqual((self.card("acme")["status"], self.card("acme")["items"]), ("live", 2))
        self.propose(product="tools", sources="")
        self.assertEqual(self.card("tools")["sources"], "not given")
        for rid in ("tools-r1", "tools-r2", "tools-r3"):
            self.row(rid, "drop")
        self.assertEqual(self.card("tools")["status"], "none")  # all dropped: still no roadmap
        o = pm.overview(NOW)
        self.assertEqual(([c["status"] for c in o["roadmaps"]], o["errors"]["rm"]), (["live", "none"], []))

    def test_a_hand_edited_proposal_is_a_tab_error(self):
        store.write("pm.json", {"snoozed": {}, "lastSweep": {}, "roadmaps": {"acme": {"at": iso(1), "rows": ["x"]}}})
        o = pm.overview(NOW)
        self.assertEqual((o["roadmaps"], o["errors"]["rm"]), ([], ["roadmap: pm.json roadmaps.acme.rows is not a list of rows: fix or restore it"]))
        self.assertEqual(serve.save_pm({"action": "roadmap-row", "row": "acme-r1", "op": "drop", "at": iso(1)})[0], 400)

    def test_an_unreadable_proposal_costs_the_tab(self):
        with mock.patch("factory.pm.roadmaps", side_effect=ValueError("bad roadmaps")):
            o = pm.overview(NOW)
        self.assertEqual((o["roadmaps"], o["errors"]["rm"]), ([], ["roadmap: bad roadmaps"]))

    def test_cli(self):
        path = os.path.join(self.tmp.name, "rows.json")
        with open(path, "w") as f:
            json.dump(self.ROWS, f)
        self.assertNotEqual(run("pm", "propose-roadmap", "acme", "--file", path + ".x")[0], 0)
        code, r, err = run("pm", "propose-roadmap", "acme", "--file", path, "--sources", "12 issues")
        self.assertEqual((code, len(r["rows"]), r["sources"]), (0, 3, "12 issues"), err)
        self.assertNotEqual(run("pm", "propose-roadmap", "nosuch", "--file", path)[0], 0)
        code, r, err = run("pm", "roadmaps")
        self.assertEqual((code, [c["status"] for c in r]), (0, ["draft", "none"]), err)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["pm", "roadmaps"]), 0)
        at = self.at()
        self.assertIn(f"acme\tdraft ready\tSources: 12 issues\tproposed {at}\n  acme-r1\tproposed\tnext\tTeam invites", out.getvalue())
        self.assertIn("tools\tno roadmap\tSources: none yet\n  agent: factory pm propose-roadmap tools --file rows.json", out.getvalue())
        self.assertNotEqual(run("pm", "roadmap-row", "acme-r1", "approve", "--at", "2000-01-01T00:00:00Z")[0], 0)  # stale
        code, r, err = run("pm", "roadmap-row", "acme-r1", "approve", "--at", at)
        self.assertEqual((code, r["row"]["status"]), (0, "approved"), err)
        self.assertNotEqual(run("pm", "roadmap-row", "acme-r1", "approve", "--undo")[0], 0)
        code, r, err = run("pm", "roadmap-row", "acme-r2", "drop")
        self.assertEqual((code, r["row"]["status"]), (0, "dropped"), err)
        code, r, err = run("pm", "roadmap-row", "acme-r2", "drop", "--undo")
        self.assertEqual((code, r["row"]["status"]), (0, "proposed"), err)
        self.assertNotEqual(run("pm", "roadmap-row", "acme-r9", "drop")[0], 0)
        self.assertNotEqual(run("pm", "approve-roadmap", "acme", "--at", "2000-01-01T00:00:00Z")[0], 0)  # stale
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["pm", "approve-roadmap", "acme"]), 0)
        self.assertEqual(out.getvalue().strip(), "ok: acme 2 rows added: acme-r2 as acme-faster-checkout, acme-r3 as acme-dark-mode")
        self.assertNotEqual(run("pm", "approve-roadmap", "acme")[0], 0)  # nothing left
        self.assertNotEqual(run("pm", "approve-roadmap", "nosuch")[0], 0)


if __name__ == "__main__":
    unittest.main()
