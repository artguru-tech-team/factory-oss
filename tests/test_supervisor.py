import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from factory import build, cli, store, supervisor
from factory.store import FactoryError

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
TODAY = "2026-11-10"


def vps_ctx(disk, now=NOW):
    return {"now": now, "release": {}, "disk_pct": disk, "mem_pct": None}


def cli_run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--json", *argv])
    return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()


class Base(unittest.TestCase):
    SIG = f"app:develop-fix-not-dispatched:{TODAY}"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        # the claim, send and bookkeeping live in release.send_queue_relay (tested in test_release)
        p = mock.patch.object(supervisor.release, "send_queue_relay", return_value=True)
        self.enqueue = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(supervisor.store, "add_ask_item", return_value={"id": "d1"})
        self.ask = p.start()
        self.addCleanup(p.stop)
        # one dispatch finding that keeps recurring: a release plan with an undelivered develop-fix
        self.ctx = self.release_ctx()

    def release_ctx(self, verdict=None, queue_log=None, agents=None):
        rel = {"products": {"app": {"mode": "live", "thread": "t-app", "project": "proj"}},
               "verdicts": {TODAY: {"app": verdict or {"verdict": "wait", "reasons": ["red PR"]}}},
               "queue_log": queue_log or [], "paused": {}}
        q = {"agents": agents if agents is not None else [{"job": "develop-fix"}], "merge": [], "pending_actions": [], "asks": []}
        return {"now": NOW, "release": rel, "queue_today": {"app": q}}

    def at(self, minutes):
        return NOW + timedelta(minutes=minutes)

    def run_at(self, minutes=0, live=True, ctx=None):
        ctx = dict(ctx or self.ctx)
        ctx["now"] = self.at(minutes)
        return supervisor.run(live=live, now=ctx["now"], ctx=ctx)


class ChecksTest(Base):
    def test_develop_fix_planned_but_relay_never_fired(self):
        (f,) = supervisor.check_release(self.ctx)
        self.assertEqual(f["signature"], self.SIG)
        self.assertEqual(f["action"]["kind"], "dispatch")
        self.assertEqual(f["action"]["thread"], "t-app")
        self.assertEqual(f["action"]["product"], "app")
        self.assertEqual(f["queue"]["agents"], [{"job": "develop-fix"}])

    def test_develop_fix_quiet_when_fact_matches_plan(self):
        sent = self.release_ctx(verdict={"reasons": [], "queue_relayed": TODAY})
        self.assertEqual(supervisor.check_release(sent), [])
        # once queue_log has today's develop-fix, queue.today stops listing the job
        self.assertEqual(supervisor.check_release(self.release_ctx(agents=[])), [])

    def test_develop_fix_ignored_when_relay_is_off(self):
        ctx = self.release_ctx()
        ctx["release"]["paused"] = {"app": "x"}
        self.assertEqual(supervisor.check_release(ctx), [])
        ctx = self.release_ctx()
        ctx["release"]["products"]["app"]["mode"] = "shadow"
        self.assertEqual(supervisor.check_release(ctx), [])

    def test_plan_read_failed_compare_truncated(self):
        ctx = self.release_ctx(verdict={"verdict": "wait", "reasons": ["could not read: compare truncated: 250 of 300 commits"]})
        f, fix = supervisor.check_release(ctx)
        self.assertEqual(f["signature"], f"app:plan-read-failed:{TODAY}")
        self.assertEqual(f["action"], {"kind": "escalate", "project": "proj"})
        self.assertIn("compare truncated", f["evidence"])
        # the failed read is why the queue relay never fired: the develop-fix still gets dispatched
        self.assertEqual(fix["signature"], self.SIG)
        self.assertEqual(fix["action"]["kind"], "dispatch")

    def test_plan_not_run_yet_is_not_a_finding(self):
        ctx = self.release_ctx()
        ctx["release"]["verdicts"] = {}
        self.assertEqual(supervisor.check_release(ctx), [])

    def project(self, due, merged, total=4):
        return {"id": "feat", "kind": "build", "createdAt": "2026-10-01T00:00:00Z",
                "phases": [{"name": "p1", "due": due, "merged": merged, "total": total}]}

    def health(self, p, reported, last="2026-11-10T09:00:00Z"):
        return supervisor.check_health({"now": NOW, "projects": [(p, [{"at": last, "health": reported}])]})

    def test_health_overdue_phase_escalates(self):
        (f,) = self.health(self.project("2026-11-01", 1), "onTrack")
        self.assertEqual((f["signature"], f["severity"], f["action"]["kind"]), ("feat:health-mismatch", "crit", "escalate"))
        self.assertIn("1 of 4 units", f["evidence"])

    def test_health_stale_update_is_recorded_only(self):
        (f,) = self.health(self.project("2026-12-01", 1), "onTrack", last="2026-11-05T09:00:00Z")
        self.assertEqual((f["severity"], f["action"]), ("warn", {"kind": "record"}))
        self.assertIn("computed atRisk", f["evidence"])

    def test_health_matching_or_done_or_unreported_is_quiet(self):
        self.assertEqual(self.health(self.project("2026-12-01", 1), "onTrack"), [])
        self.assertEqual(self.health(self.project("2026-11-01", 1), "offTrack"), [])
        self.assertEqual(self.health(self.project("2026-11-01", 4), "offTrack"), [])
        self.assertEqual(supervisor.check_health({"now": NOW, "projects": [(self.project("2026-11-01", 1), [])]}), [])

    def test_vps_thresholds(self):
        self.assertEqual(supervisor.check_vps(vps_ctx(79)), [])
        (w,) = supervisor.check_vps(vps_ctx(85))
        self.assertEqual((w["severity"], w["action"]), ("warn", {"kind": "infra"}))
        (c,) = supervisor.check_vps(vps_ctx(91))
        self.assertEqual((c["signature"], c["severity"]), ("vps:disk", "crit"))
        self.assertEqual(supervisor.check_vps({**vps_ctx(10), "mem_pct": 95})[0]["signature"], "vps:memory")
        self.assertEqual(supervisor.check_vps({**vps_ctx(10), "mem_pct": 95, "thresholds": {"mem_warn": 99}}), [])

    def test_job_heartbeats(self):
        ctx = {"now": NOW, "jobs": {"build.log": 30, "chat.log": 30, "new.log": 30},
               "mtimes": {"build.log": NOW - timedelta(minutes=10), "chat.log": NOW - timedelta(minutes=45), "new.log": None}}
        got = {f["signature"]: f for f in supervisor.check_jobs(ctx)}
        self.assertEqual(set(got), {"job:chat.log:stale", "job:new.log:stale"})
        self.assertEqual(got["job:chat.log:stale"]["action"]["kind"], "rerun")


class EngineTest(Base):
    def rec(self):
        return supervisor.state()["findings"][self.SIG]

    def test_budget_third_occurrence_escalates_once_fourth_is_quiet(self):
        for m in (0, 30):
            self.assertTrue(self.run_at(m)["findings"][0]["did"].startswith("dispatched"))
        self.assertEqual((self.enqueue.call_count, self.ask.call_count), (2, 0))
        third = self.run_at(60)["findings"][0]
        self.assertTrue(third["did"].startswith("inbox"), third)
        self.assertEqual(self.ask.call_count, 1)
        self.assertIn("gave up", self.ask.call_args[0][2])
        self.assertEqual(self.rec()["status"], "escalated")
        self.assertEqual(self.run_at(90)["findings"][0]["did"], "quiet")
        self.assertEqual((self.enqueue.call_count, self.ask.call_count), (2, 1))

    def test_budget_window_slides(self):
        for minutes in (0, 30, 25 * 60):  # the third is 24 h after the first two: it acts again, no escalation
            out = supervisor.run(live=True, now=self.at(minutes), ctx=vps_ctx(85, self.at(minutes)))
            self.assertEqual(out["findings"][0]["did"], "recorded")
        self.assertEqual(self.ask.call_count, 0)

    def test_resolved_when_absent_then_reopens(self):
        self.run_at(0)
        gone = self.release_ctx(verdict={"reasons": [], "queue_relayed": TODAY})
        out = self.run_at(30, ctx=gone)
        self.assertEqual(out["resolved"], [self.SIG])
        self.assertEqual(self.rec()["status"], "resolved")
        self.run_at(60)
        self.assertEqual(self.rec()["status"], "open")
        self.assertEqual(self.rec()["first_seen"], store.iso(NOW))

    def test_false_alarm_reopens_when_evidence_changes(self):
        ctx = vps_ctx(85)
        supervisor.run(live=True, now=NOW, ctx=ctx)
        supervisor.false_alarm("vps:disk")
        later = NOW + timedelta(minutes=30)
        out = supervisor.run(live=True, now=later, ctx={**ctx, "now": later})
        self.assertEqual(out["findings"][0]["did"], "false alarm")
        later = NOW + timedelta(minutes=60)
        out = supervisor.run(live=True, now=later, ctx={**vps_ctx(95), "now": later})
        self.assertEqual((out["findings"][0]["status"], out["findings"][0]["did"]), ("open", "recorded"))

    def test_mute_expires(self):
        self.run_at(0)
        supervisor.mute(self.SIG, 1, now=NOW)
        self.assertEqual(self.run_at(30)["findings"][0]["did"], "muted")
        self.assertEqual(self.enqueue.call_count, 1)
        out = self.run_at(61)
        self.assertTrue(out["findings"][0]["did"].startswith("dispatched"))
        self.assertEqual(self.enqueue.call_count, 2)

    def test_dry_run_sends_nothing_and_keeps_the_budget(self):
        for m in (0, 30, 60, 90):
            self.assertEqual(self.run_at(m, live=False)["findings"][0]["did"], "would dispatch")
        self.assertEqual((self.enqueue.call_count, self.ask.call_count), (0, 0))
        rec = self.rec()
        self.assertEqual((rec["actions_at"], rec["attempts"], rec["status"], rec["would"]), ([], 0, "open", "would dispatch"))
        self.assertEqual(supervisor.state()["last_run"], store.iso(self.at(90)))

    def test_dry_run_escalation_creates_no_inbox_item(self):
        ctx = self.release_ctx(verdict={"reasons": ["could not read: boom"]})
        out = self.run_at(0, live=False, ctx=ctx)
        self.assertEqual(out["findings"][0]["did"], "would escalate")
        self.assertEqual(self.ask.call_count, 0)

    def test_escalate_action_files_one_item_then_stays_quiet(self):
        ctx = self.release_ctx(verdict={"reasons": ["could not read: boom"]}, agents=[])
        for m in (0, 30, 60):
            self.run_at(m, ctx=ctx)
        self.assertEqual(self.ask.call_count, 1)
        args = self.ask.call_args[0]
        self.assertEqual((args[0], args[1], args[9]), ("proj", "urgent", None))

    def test_dispatch_skips_when_the_release_run_already_claimed_it(self):
        self.enqueue.return_value = False
        out = self.run_at(0)
        self.assertEqual(out["findings"][0]["did"], "already sent by the release run")
        rec = supervisor.state()["findings"][self.SIG]
        self.assertEqual(rec["actions_at"], [])  # a no-op spends no budget

    def test_escalations_are_capped_per_run(self):
        ctx = {"now": NOW, "projects": [({"id": f"p{i}", "phases": [{"name": "x", "due": "2026-01-01", "merged": 0, "total": 1}]},
                                         [{"at": "2026-11-09T00:00:00Z", "health": "onTrack"}]) for i in range(5)]}
        out = self.run_at(0, ctx=ctx)
        dids = [r["did"] for r in out["findings"]]
        self.assertEqual(self.ask.call_count, supervisor.MAX_ESCALATIONS)
        self.assertEqual(sum(d.startswith("deferred") for d in dids), 5 - supervisor.MAX_ESCALATIONS)
        stored = [r.get("did", "") for r in supervisor.state()["findings"].values()]
        self.assertEqual(sum(d.startswith("deferred") for d in stored), 5 - supervisor.MAX_ESCALATIONS)  # the dashboard shows it
        self.run_at(30, ctx=ctx)  # the deferred ones go out next run, the sent ones stay quiet
        self.assertEqual(self.ask.call_count, 5)

    def test_flapping_escalation_files_once_per_24h(self):
        bad = self.release_ctx(verdict={"reasons": ["could not read: boom"]}, agents=[])
        good = self.release_ctx(agents=[])
        self.run_at(0, ctx=bad)
        self.run_at(30, ctx=good)  # resolved
        self.run_at(60, ctx=bad)  # back within 24 h: quiet
        self.assertEqual(self.ask.call_count, 1)
        self.assertEqual(supervisor.state()["findings"][f"app:plan-read-failed:{TODAY}"]["status"], "open")
        # a health mismatch keeps its signature across days: after 24 h it may file again
        proj = [({"id": "p", "phases": [{"name": "x", "due": "2026-01-01", "merged": 0, "total": 1}]},
                 [{"at": "2026-11-09T00:00:00Z", "health": "onTrack"}])]
        for m in (0, 30, 60 * 25):
            self.run_at(m, ctx={"now": NOW, "projects": proj if m != 30 else []})
        self.assertEqual(self.ask.call_count, 3)

    def test_failed_escalations_do_not_use_up_the_cap(self):
        ctx = {"now": NOW, "projects": [({"id": f"p{i}", "phases": [{"name": "x", "due": "2026-01-01", "merged": 0, "total": 1}]},
                                         [{"at": "2026-11-09T00:00:00Z", "health": "onTrack"}]) for i in range(5)]}
        self.ask.side_effect = [FactoryError("no project")] * 2 + [{"id": "d1"}] * 10
        self.run_at(0, ctx=ctx)
        self.assertEqual(self.ask.call_count, 2 + supervisor.MAX_ESCALATIONS)

    def test_repeated_failures_make_status_degraded(self):
        self.enqueue.side_effect = RuntimeError("relay down")
        for m in (0, 30, 60):
            self.run_at(m)
        self.assertEqual(supervisor.heartbeat(self.at(61))[0], "degraded")
        self.enqueue.side_effect = None
        self.run_at(90)
        self.assertEqual(supervisor.heartbeat(self.at(91))[0], "alive")

    def test_failures_are_forgotten_when_a_finding_resolves(self):
        self.enqueue.side_effect = RuntimeError("relay down")
        for m in (0, 30, 60):
            self.run_at(m)
        self.run_at(90, ctx=self.release_ctx(agents=[]))  # resolved
        self.assertNotIn("fails", supervisor.state()["findings"][self.SIG])
        self.run_at(120)  # back, first run fails once more: not degraded yet
        self.assertEqual(supervisor.heartbeat(self.at(121))[0], "alive")

    def test_status_is_dead_on_a_corrupt_state_file(self):
        store.write(supervisor.REL, {"findings": "nope"})
        self.assertEqual(supervisor.heartbeat(NOW), ("dead", None))

    def test_failed_send_is_retried_and_not_counted(self):
        self.enqueue.side_effect = RuntimeError("relay down")
        self.assertTrue(self.run_at(0)["findings"][0]["did"].startswith("error"))
        self.assertEqual(self.rec()["actions_at"], [])
        self.enqueue.side_effect = None
        self.assertTrue(self.run_at(30)["findings"][0]["did"].startswith("dispatched"))

    def test_crashing_check_does_not_resolve_its_findings(self):
        self.run_at(0)
        with mock.patch.object(supervisor, "CHECKS", (("release", mock.Mock(side_effect=ValueError("bad"))),)):
            out = supervisor.run(live=False, now=NOW, ctx=self.ctx)
        self.assertEqual(out["resolved"], [])
        self.assertEqual(self.rec()["status"], "open")
        self.assertIn("supervisor:check-failed:release", supervisor.state()["findings"])

    def test_bad_state_file_is_refused(self):
        store.write(supervisor.REL, {"findings": []})
        with self.assertRaises(FactoryError):
            supervisor.state()


class HeartbeatAndCliTest(Base):
    def test_heartbeat_alive_late_dead(self):
        self.assertEqual(supervisor.heartbeat(NOW)[0], "dead")
        self.run_at(0, live=False)
        for minutes, want in ((29, "alive"), (30, "late"), (59, "late"), (60, "dead")):
            self.assertEqual(supervisor.heartbeat(NOW + timedelta(minutes=minutes))[0], want)

    def test_live_config_values(self):
        self.assertFalse(supervisor.is_live())
        for v in ("true", True):
            with mock.patch.object(supervisor.config, "get", return_value=v):
                self.assertTrue(supervisor.is_live())

    def test_cli_status_list_mute_false_alarm(self):
        code, obj, _ = cli_run("supervise", "status")
        self.assertEqual((code, obj["state"]), (3, "dead"))
        with mock.patch.object(supervisor, "load_ctx", return_value=self.ctx):
            code, rep, err = cli_run("supervise", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual((rep["live"], rep["findings"][0]["did"]), (False, "would dispatch"))
        self.assertEqual(cli_run("supervise", "status")[1]["state"], "alive")
        self.assertEqual(cli_run("supervise", "list")[1][0]["signature"], self.SIG)
        self.assertEqual(cli_run("supervise", "mute", self.SIG, "--hours", "2")[1]["status"], "muted")
        self.assertEqual(cli_run("supervise", "false-alarm", self.SIG)[0], 0)
        self.assertEqual(cli_run("supervise", "mute", "nope")[0], 1)
        self.assertEqual(cli_run("supervise", "--dry-run", "--live")[0], 1)
        self.assertEqual(self.enqueue.call_count, 0)

    def test_config_live_makes_a_bare_run_live_and_dry_run_overrides(self):
        store.write("config.json", {"supervisor": {"live": True}})
        with mock.patch.object(supervisor, "load_ctx", return_value=self.ctx), mock.patch.object(cli.relay, "send_queued"):
            self.assertTrue(cli_run("supervise")[1]["live"])
            self.assertFalse(cli_run("supervise", "--dry-run")[1]["live"])
        self.assertEqual(self.enqueue.call_count, 1)


class SnapshotTest(Base):
    def snap_ctx(self):
        proj = {"id": "web", "phases": []}
        ups = [{"at": "2026-11-09T10:00:00Z", "health": "onTrack"}]
        stale = {"id": "old", "phases": []}
        return {**self.ctx, "jobs": {"chatscan": 15, "digest": 60}, "projects": [(proj, ups), (stale, [{"at": "2026-11-01T10:00:00Z", "health": "onTrack"}]), ({"id": "none", "phases": []}, [])],
                "mtimes": {"chatscan": NOW - timedelta(minutes=5), "digest": None},
                "disk_pct": 41.2345, "mem_pct": None, "thresholds": {"disk_warn": 75}}

    def test_run_stores_what_it_saw(self):
        self.run_at(0, live=False, ctx=self.snap_ctx())
        sn = supervisor.state()["snapshot"]
        json.dumps(sn)
        self.assertEqual([(j["name"], j["stale"]) for j in sn["jobs"]], [("chatscan", False), ("digest", True)])
        self.assertEqual(sn["jobs"][1]["last"], None)
        self.assertEqual([(h["project"], h["reported"], h["computed"]) for h in sn["health"]],
                         [("web", "onTrack", "onTrack"), ("old", "onTrack", "atRisk")])
        self.assertEqual((sn["vps"]["disk_pct"], sn["vps"]["mem_pct"], sn["vps"]["thresholds"]["disk_warn"]), (41.2, None, 75))

    def test_a_snapshot_failure_does_not_break_the_run(self):
        self.run_at(0, live=False, ctx=self.snap_ctx())
        with mock.patch.object(supervisor, "snapshot", side_effect=KeyError("x")):
            self.run_at(1, live=False)
        st = supervisor.state()
        self.assertNotIn("snapshot", st)
        self.assertTrue(st["last_run"])

    def test_run_stores_its_mode(self):
        self.run_at(0, live=False)
        self.assertIs(supervisor.state()["live"], False)

    def test_did_is_cleared_when_a_finding_resolves_or_the_owner_closes_it(self):
        hot = {"now": NOW, "disk_pct": 95, "thresholds": {}}
        self.run_at(0, live=False, ctx=hot)
        self.assertEqual(supervisor.state()["findings"]["vps:disk"]["did"], "would infra")
        self.run_at(30, live=False, ctx={**hot, "now": self.at(30), "disk_pct": 10})
        rec = supervisor.state()["findings"]["vps:disk"]
        self.assertEqual((rec["status"], rec.get("did")), ("resolved", None))
        self.run_at(60, live=False, ctx={**hot, "now": self.at(60)})
        supervisor.mute("vps:disk", 24, now=self.at(61))
        self.assertNotIn("did", supervisor.state()["findings"]["vps:disk"])
        self.run_at(90, live=False, ctx={**hot, "now": self.at(90)})
        supervisor.false_alarm("vps:disk")
        self.assertNotIn("did", supervisor.state()["findings"]["vps:disk"])


class DashboardDataTest(Base):
    def rec(self, sig, status, sev, seen, **kw):
        return {"signature": sig, "status": status, "severity": sev, "title": sig, "evidence": "e", "check": "vps", "action": {"kind": "infra"},
                "attempts": 1, "actions_at": [], "first_seen": seen, "last_seen": seen, **kw}

    def seed(self, recs):
        store.write(supervisor.REL, {"last_run": store.iso(NOW), "findings": {r["signature"]: r for r in recs}, "snapshot": {"jobs": []}})

    def data(self):
        w = []
        return build.supervisor_data(NOW, w), w

    def test_sorted_open_first_crit_first_newest_first_and_resolved_expire(self):
        h = lambda n: store.iso(NOW - timedelta(hours=n))
        self.seed([self.rec("warn-new", "open", "warn", h(1)), self.rec("crit-old", "open", "crit", h(5)),
                   self.rec("crit-new", "escalated", "crit", h(2)), self.rec("done-1h", "resolved", "crit", h(1)),
                   self.rec("done-30h", "resolved", "warn", h(30)), self.rec("muted-3h", "muted", "warn", h(3), would="would infra")])
        d, _ = self.data()
        self.assertEqual([f["signature"] for f in d["findings"]], ["crit-new", "crit-old", "warn-new", "done-1h", "muted-3h"])
        self.assertEqual((d["status"], d["live"], d["snapshot"], d["error"]), ("alive", False, {"jobs": []}, None))
        self.assertEqual(d["findings"][-1]["did"], "would infra")

    def test_did_is_the_last_result_when_live(self):
        self.seed([self.rec("a", "open", "warn", store.iso(NOW), would="would infra", last_result="sent")])
        store.write("config.json", {"supervisor": {"live": True}})
        d, _ = self.data()
        self.assertEqual((d["live"], d["findings"][0]["did"]), (True, "sent"))

    def test_the_mode_shown_is_the_last_run_s_not_the_config(self):
        self.seed([self.rec("a", "open", "warn", store.iso(NOW), would="would infra")])
        st = store.read(supervisor.REL, {})
        store.write(supervisor.REL, {**st, "live": False})
        store.write("config.json", {"supervisor": {"live": True}})
        d, _ = self.data()
        self.assertEqual((d["live"], d["findings"][0]["did"]), (False, "would infra"))

    def test_budget_counts_only_dispatch_and_escalate_inside_24h(self):
        h = lambda n: store.iso(NOW - timedelta(hours=n))
        self.seed([self.rec("d", "open", "warn", h(0), action={"kind": "dispatch"}, actions_at=[h(30), h(3), h(2), h(1)]),  # 3 inside 24 h: a dispatch that also escalated
                   self.rec("e", "escalated", "crit", h(0), action={"kind": "escalate"}, actions_at=[h(1)], last_result="inbox a9"),
                   self.rec("i", "open", "warn", h(0), actions_at=[h(1)])])
        d, _ = self.data()
        got = {f["signature"]: (f["used"], f["budget"]) for f in d["findings"]}
        self.assertEqual(got, {"d": (2, 2), "e": (1, 1), "i": (None, None)})
        self.assertEqual([f["lastResult"] for f in d["findings"] if f["signature"] == "e"], ["inbox a9"])

    def test_an_unparsable_last_run_is_no_run(self):
        self.seed([])
        store.write(supervisor.REL, {**store.read(supervisor.REL, {}), "last_run": "garbage"})
        d, _ = self.data()
        self.assertEqual((d["status"], d["lastRun"]), ("dead", None))
        store.write(supervisor.REL, {**store.read(supervisor.REL, {}), "last_run": store.iso(NOW).replace("Z", "+00:00")})
        self.assertEqual(self.data()[0]["lastRun"], store.iso(NOW))  # normalised for the page's Date parser

    def test_a_snapshot_of_the_wrong_shape_is_dropped(self):
        for bad in ("x", {"jobs": "abc"}, {"health": [1]}, {"vps": {"disk_pct": "9"}}, {"vps": {"thresholds": {"disk_warn": "x"}}}):
            store.write(supervisor.REL, {"last_run": store.iso(NOW), "findings": {}, "snapshot": bad})
            self.assertIsNone(self.data()[0]["snapshot"], bad)

    def test_a_naive_now_does_not_crash(self):
        self.seed([self.rec("a", "resolved", "warn", store.iso(NOW))])
        build.supervisor_data(NOW.replace(tzinfo=None), [])

    def test_no_file_is_dead_without_error(self):
        d, w = self.data()
        self.assertEqual((d["status"], d["lastRun"], d["findings"], d["error"], w), ("dead", None, [], None, []))

    def test_corrupt_file_is_dead_with_an_error_and_never_raises(self):
        store.write(supervisor.REL, {"last_run": None, "findings": {"x": "junk"}})
        d, w = self.data()
        self.assertEqual((d["status"], d["findings"], d["snapshot"]), ("dead", [], None))
        self.assertIn("not a supervisor state file", d["error"])
        self.assertEqual(len(w), 1)


if __name__ == "__main__":
    unittest.main()
