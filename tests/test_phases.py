"""Phases counted from their linked PRs, and stale-phase warnings (issue #39)."""
import contextlib
import io
import os
import tempfile
import unittest
from datetime import date, datetime, timezone

from factory import build, cli, sources, store

MERGED, OPEN, CLOSED = ["T", "pass", "Merged"], ["T", "run", "Review required"], ["T", "none", "Closed"]


def ph(name, total, merged=0, in_pr=0, due="2026-10-28", prs=None, blocked=0):
    out = {"name": name, "total": total, "start": "2026-10-27", "due": due, "merged": merged, "inPR": in_pr, "blocked": blocked}
    if prs is not None:
        out["prs"] = prs
    return out


class CountsTest(unittest.TestCase):
    def test_each_pr_carries_its_units_the_rest_stays_to_do(self):
        p = ph("A", 29, prs=[["o/r#1", 9], ["o/r#2", 10]])
        self.assertEqual(sources.phase_counts(p, {"o/r#1": MERGED, "o/r#2": OPEN}), (9, 10, 0))
        # one merged PR of a 29-unit phase is 9 merged, not 100% (review finding 2)
        self.assertEqual(sources.phase_counts(ph("A", 29, prs=[["o/r#1", 9]]), {"o/r#1": MERGED}), (9, 0, 0))
        self.assertEqual(sources.phase_counts(ph("B", 3, prs=[["o/r#1", 1], ["o/r#2", 1], ["o/r#3", 1]]),
                                              {"o/r#1": MERGED, "o/r#2": CLOSED, "o/r#3": OPEN}), (1, 1, 1))

    def test_no_prs_or_an_unreadable_pr_keeps_the_stored_counts(self):
        self.assertIsNone(sources.phase_counts(ph("A", 9), {}))
        self.assertIsNone(sources.phase_counts(ph("A", 9, prs=[["o/r#1", 4], ["o/r#2", 5]]), {"o/r#1": MERGED}))


class StaleTest(unittest.TestCase):
    def test_aura_obm_as_of_2026_10_31(self):
        """The case that sat at 17% for 4 days: units in PR, every PR merged, phases past due."""
        p = {"id": "gz-obm", "kind": "build", "prs": ["o/r#222", "o/r#224"],
             "phases": [ph("PR 1 backend", 9, in_pr=9), ph("PR 2 admin", 10, in_pr=10),
                        ph("Catalog follow-ups", 5, merged=5, due="2026-11-03")]}
        out = sources.stale_phases([p], {"o/r#222": MERGED, "o/r#224": MERGED}, date(2026, 10, 31))
        self.assertEqual(len(out), 4)
        self.assertIn("gz-obm: phase PR 1 backend counts 9 in PR but none of its PRs is open", out[0])
        self.assertIn("gz-obm: phase PR 1 backend is 3 days past due at 0 of 9 merged", out[1])
        self.assertIn("PR 2 admin counts 10 in PR", out[2])
        self.assertFalse([w for w in out if "Catalog" in w])

    def test_current_phases_and_ds_kit_are_quiet(self):
        p = {"id": "b", "kind": "build", "prs": ["o/r#1"], "phases": [ph("A", 4, in_pr=4, due="2026-10-30")]}
        self.assertEqual(sources.stale_phases([p], {"o/r#1": OPEN}, date(2026, 10, 31)), [])
        # two days late is tolerated, three is not
        late = {"id": "b", "kind": "build", "prs": [], "phases": [ph("A", 4, merged=3, due="2026-10-29")]}
        self.assertEqual(sources.stale_phases([late], {}, date(2026, 10, 31)), [])
        self.assertEqual(len(sources.stale_phases([late], {}, date(2026, 11, 1))), 1)
        kit = {**late, "source": "ds-kit"}
        self.assertEqual(sources.stale_phases([kit], {}, date(2026, 11, 5)), [])

    def test_linked_phase_is_judged_on_derived_counts(self):
        p = {"id": "b", "kind": "build", "prs": [], "phases": [ph("A", 4, in_pr=4, prs=[["o/r#1", 4]])]}
        self.assertEqual(sources.stale_phases([p], {"o/r#1": MERGED}, date(2026, 10, 29)), [])


class StaleEdgeTest(unittest.TestCase):
    def test_unreadable_pr_is_not_a_stale_phase(self):
        """gh down or a typo'd PR: the read failure is reported elsewhere, not as 'none open' (finding 4)."""
        p = {"id": "b", "kind": "build", "prs": ["o/r#1"], "phases": [ph("A", 4, in_pr=4, due="2026-10-30")]}
        self.assertEqual(sources.stale_phases([p], {}, date(2026, 10, 31)), [])

    def test_blocked_units_count_as_settled_for_the_due_date(self):
        """8 merged + 2 blocked of 10 is finished as far as the plan goes (finding 5)."""
        p = {"id": "b", "kind": "build", "prs": [], "phases": [ph("A", 10, merged=8, blocked=2, due="2026-10-20")]}
        self.assertEqual(sources.stale_phases([p], {}, date(2026, 10, 31)), [])


class StateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        self.ok("product", "set", "bk", "--name", "Acme", "--repo", "o/r")
        self.ok("project", "add", "b", "--product", "bk", "--kind", "build", "--name", "Build")

    def ok(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(list(argv)), 0, argv)

    def fails(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertNotEqual(cli.main(list(argv)), 0, argv)
        return err.getvalue()

    def test_phase_links_prs_and_units_refuses_counts(self):
        self.ok("plan", "phase", "b", "A", "--total", "10", "--start", "2026-10-27", "--due", "2026-10-28",
                "--pr", "222:6", "--pr", "x/y#5", "--pr", "222:7")
        self.assertEqual(store.find_project("b")["phases"][0]["prs"], [["o/r#222", 7], ["x/y#5", 1]])
        self.assertIn("come from its PRs", self.fails("plan", "units", "b", "A", "--merged", "3"))
        self.ok("plan", "units", "b", "A", "--blocked", "2")
        self.assertIn("is more than the phase total", self.fails("plan", "units", "b", "A", "--blocked", "3"))
        # the total is checked against the PRs' units + blocked, with the new --pr applied first (finding 3)
        self.assertIn("less than the units its PRs carry", self.fails(
            "plan", "phase", "b", "A", "--total", "9", "--start", "2026-10-27", "--due", "2026-10-28"))
        self.ok("plan", "phase", "b", "A", "--total", "8", "--start", "2026-10-27", "--due", "2026-10-28", "--pr", "222:6")
        # changing the dates keeps the links; --pr '' unlinks
        self.ok("plan", "phase", "b", "A", "--total", "8", "--start", "2026-10-27", "--due", "2026-10-30")
        self.assertEqual(store.find_project("b")["phases"][0]["prs"], [["o/r#222", 6]])
        self.ok("plan", "phase", "b", "A", "--total", "8", "--start", "2026-10-27", "--due", "2026-10-30", "--pr", "")
        self.ok("plan", "units", "b", "A", "--merged", "3")
        self.assertIn("bad count", self.fails("plan", "phase", "b", "B", "--total", "2", "--start", "2026-10-27",
                                              "--due", "2026-10-28", "--pr", "1:x"))

    def test_linking_refuses_to_drop_counted_units(self):
        """gz-obm 'PR 1 backend', 9 of 9 merged by hand, linked as --pr 222 with no :9 (round 2 finding)."""
        self.ok("plan", "phase", "b", "A", "--total", "9", "--start", "2026-10-27", "--due", "2026-10-28")
        self.ok("plan", "units", "b", "A", "--merged", "9")
        self.assertIn("would drop them", self.fails("plan", "phase", "b", "A", "--total", "9", "--start", "2026-10-27",
                                                    "--due", "2026-10-28", "--pr", "222"))
        self.assertNotIn("prs", store.find_project("b")["phases"][0])
        self.ok("plan", "phase", "b", "A", "--total", "9", "--start", "2026-10-27", "--due", "2026-10-28", "--pr", "222:9")

    def test_add_pr_appends_without_duplicates(self):
        self.ok("project", "set", "b", "--pr", "1")
        self.ok("project", "set", "b", "--add-pr", "2", "--add-pr", "o/r#1")
        self.assertEqual(store.find_project("b")["prs"], ["o/r#1", "o/r#2"])
        self.ok("project", "set", "b", "--pr", "3")
        self.assertEqual(store.find_project("b")["prs"], ["o/r#3"])

    def test_build_writes_derived_counts_back_with_a_snapshot(self):
        self.ok("plan", "phase", "b", "A", "--total", "12", "--start", "2026-10-27", "--due", "2026-10-28",
                "--pr", "1:5", "--pr", "2:4")
        self.ok("plan", "units", "b", "A", "--blocked", "3")
        warnings = []
        raw = build.sync_phases(store.projects(), {"o/r#1": MERGED, "o/r#2": OPEN}, warnings)
        saved = store.find_project("b")
        a = saved["phases"][0]
        # blocked survives the build (finding 1)
        self.assertEqual((a["merged"], a["inPR"], a["blocked"]), (5, 4, 3))
        self.assertEqual(saved["history"][datetime.now(timezone.utc).date().isoformat()], [12, 5])
        self.assertEqual(raw[0]["phases"][0]["merged"], 5)
        self.assertEqual(warnings, [])
        self.ok("plan", "units", "b", "A", "--blocked", "2")
        # a closed PR warns; an unchanged state writes nothing
        build.sync_phases(store.projects(), {"o/r#1": MERGED, "o/r#2": CLOSED}, warnings)
        self.assertIn("closed without merging", warnings[0])
        self.assertEqual(store.find_project("b")["phases"][0]["inPR"], 0)
        before = os.path.getmtime(store.path(store.project_rel("bk", "b")))
        build.sync_phases(store.projects(), {"o/r#1": MERGED, "o/r#2": CLOSED}, [])
        self.assertEqual(os.path.getmtime(store.path(store.project_rel("bk", "b"))), before)


if __name__ == "__main__":
    unittest.main()
