import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from factory import cli, queue, release, sources, store
from factory.store import FactoryError


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--json", *argv])
    return code, (json.loads(out.getvalue()) if code == 0 and out.getvalue().strip() else None), err.getvalue()


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)

    def ok(self, *argv):
        code, obj, err = run(*argv)
        self.assertEqual(code, 0, err)
        return obj

    def test_open_prs_graphql_parsing(self):
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        fake_graphql_resp = {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            # 1. Draft PR - should be skipped
                            {
                                "number": 1,
                                "title": "Draft PR",
                                "baseRefName": "develop",
                                "isDraft": True,
                            },
                            # 2. Regular PR
                            {
                                "number": 2,
                                "title": "fix: bug fix",
                                "baseRefName": "develop",
                                "headRefName": "fix-branch",
                                "isDraft": False,
                                "mergeable": "MERGEABLE",
                                "createdAt": "2026-11-01T10:00:00Z",
                                "updatedAt": "2026-11-02T10:00:00Z",
                                "author": {"login": "alice"},
                                "labels": {"nodes": [{"name": "bug"}]},
                                "commits": {
                                    "nodes": [
                                        {
                                            "commit": {
                                                "statusCheckRollup": {
                                                    "contexts": {
                                                        "nodes": [
                                                            {
                                                                "__typename": "CheckRun",
                                                                "name": "tests",
                                                                "conclusion": "SUCCESS",
                                                            },
                                                            {
                                                                "__typename": "StatusContext",
                                                                "context": "factory/review",
                                                                "state": "SUCCESS",
                                                            },
                                                        ]
                                                    }
                                                }
                                            }
                                        }
                                    ]
                                },
                            },
                            # 3. Bot PR with failure
                            {
                                "number": 3,
                                "title": "bump foo from v1.0.0 to v1.0.1",
                                "baseRefName": "develop",
                                "headRefName": "dependabot/foo",
                                "isDraft": False,
                                "mergeable": "MERGEABLE",
                                "createdAt": "2026-11-01T10:00:00Z",
                                "updatedAt": "2026-11-02T10:00:00Z",
                                "author": {"login": "dependabot[bot]"},
                                "labels": {"nodes": [{"name": "dependencies"}]},
                                "commits": {
                                    "nodes": [
                                        {
                                            "commit": {
                                                "statusCheckRollup": {
                                                    "contexts": {
                                                        "nodes": [
                                                            {
                                                                "__typename": "CheckRun",
                                                                "name": "ci",
                                                                "conclusion": "FAILURE",
                                                            }
                                                        ]
                                                    }
                                                }
                                            }
                                        }
                                    ]
                                },
                            },
                        ],
                    }
                }
            }
        }

        with mock.patch.object(sources, "gh_json", return_value=fake_graphql_resp) as mock_gh:
            prs = queue.open_prs(product)
            self.assertEqual(len(prs), 2)
            self.assertEqual(prs[0]["number"], 2)
            self.assertFalse(prs[0]["is_bot"])
            self.assertEqual(prs[0]["failing_contexts"], 0)
            self.assertEqual(prs[0]["review_state"], "SUCCESS")

            self.assertEqual(prs[1]["number"], 3)
            self.assertTrue(prs[1]["is_bot"])
            self.assertEqual(prs[1]["failing_contexts"], 1)

    def test_open_prs_gh_failure_raises_factory_error(self):
        product = {"repo": "acme/app"}
        with mock.patch.object(sources, "gh_json", side_effect=Exception("network error")):
            with self.assertRaises(FactoryError):
                queue.open_prs(product)

    # Table row tests for classify()
    def test_classify_row1_wrong_base(self):
        pr = {
            "number": 1,
            "title": "Fix something",
            "baseRefName": "main",  # app wanted_base is develop
            "is_bot": False,
            "mergeable": "MERGEABLE",
            "failing_contexts": 0,
            "updatedAt": "2026-11-01T10:00:00Z",
            "review_state": "SUCCESS",
        }
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        c = queue.classify(pr, product, False, {}, now)
        self.assertEqual(c["group"], "you")
        self.assertEqual(c["status"], "base")
        self.assertIn("base is main, not develop", c["why"])
        self.assertEqual(c["does"], "retarget or close")

    def test_classify_row2_bot_red_or_conflicting(self):
        pr = {
            "number": 2,
            "title": "bump foo from 1.0.0 to 1.0.1",
            "baseRefName": "develop",
            "is_bot": True,
            "mergeable": "MERGEABLE",
            "failing_contexts": 1,
            "updatedAt": "2026-11-01T10:00:00Z",
            "review_state": "SUCCESS",
        }
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        c = queue.classify(pr, product, False, {}, now)
        self.assertEqual(c["group"], "fix")
        self.assertEqual(c["status"], "red")
        self.assertEqual(c["does"], "wait for Dependabot rebase")

        # When develop is red
        c_dev_red = queue.classify(pr, product, True, {}, now)
        self.assertEqual(c_dev_red["group"], "fix")
        self.assertEqual(c_dev_red["does"], "wait for develop fix")

    def test_classify_row3_bot_major_bump(self):
        pr = {
            "number": 3,
            "title": "bump foo from v1.2.3 to v2.0.0",
            "baseRefName": "develop",
            "is_bot": True,
            "mergeable": "MERGEABLE",
            "failing_contexts": 0,
            "updatedAt": "2026-11-01T10:00:00Z",
            "review_state": "SUCCESS",
        }
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        c = queue.classify(pr, product, False, {}, now)
        self.assertEqual(c["group"], "you")
        self.assertEqual(c["status"], "major")
        self.assertEqual(c["does"], "list for the owner")

    def test_classify_row4_bot_ready(self):
        pr = {
            "number": 4,
            "title": "bump foo from v1.2.0 to v1.2.1",
            "baseRefName": "develop",
            "is_bot": True,
            "mergeable": "MERGEABLE",
            "failing_contexts": 0,
            "updatedAt": "2026-11-01T10:00:00Z",
            "review_state": "SUCCESS",
        }
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        c = queue.classify(pr, product, False, {}, now)
        self.assertEqual(c["group"], "bots")
        self.assertEqual(c["status"], "ready")
        self.assertEqual(c["does"], "Wednesday batch")

    def test_classify_row5_stale_broken(self):
        pr = {
            "number": 5,
            "title": "some work",
            "baseRefName": "develop",
            "is_bot": False,
            "mergeable": "CONFLICTING",
            "failing_contexts": 0,
            "updatedAt": "2026-08-01T10:00:00Z",
            "review_state": "SUCCESS",
        }
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)  # > 90 days
        c = queue.classify(pr, product, False, {}, now)
        self.assertEqual(c["group"], "you")
        self.assertEqual(c["status"], "stale")
        self.assertEqual(c["does"], "revive or close")

    def test_classify_row6_conflicts(self):
        pr = {
            "number": 6,
            "title": "some work",
            "baseRefName": "develop",
            "is_bot": False,
            "mergeable": "CONFLICTING",
            "failing_contexts": 0,
            "updatedAt": "2026-11-01T10:00:00Z",  # fresh
            "review_state": "SUCCESS",
        }
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        c = queue.classify(pr, product, False, {}, now)
        self.assertEqual(c["group"], "fix")
        self.assertEqual(c["status"], "conf")
        self.assertEqual(c["does"], "rebase agent")

    def test_classify_row7_red_ci(self):
        pr = {
            "number": 7,
            "title": "some work",
            "baseRefName": "develop",
            "is_bot": False,
            "mergeable": "MERGEABLE",
            "failing_contexts": 2,
            "updatedAt": "2026-11-01T10:00:00Z",
            "review_state": "SUCCESS",
        }
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        c = queue.classify(pr, product, False, {}, now)
        self.assertEqual(c["group"], "fix")
        self.assertEqual(c["status"], "red")
        self.assertEqual(c["does"], "fix agent")

        # When develop is red
        c_dev_red = queue.classify(pr, product, True, {}, now)
        self.assertEqual(c_dev_red["group"], "fix")
        self.assertEqual(c_dev_red["does"], "wait for develop fix")

    def test_classify_row8_stale_docs(self):
        pr = {
            "number": 8,
            "title": "docs: update architecture guide",
            "baseRefName": "develop",
            "is_bot": False,
            "mergeable": "MERGEABLE",
            "failing_contexts": 0,
            "updatedAt": "2026-08-01T10:00:00Z",
            "review_state": "SUCCESS",
        }
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)  # > 90 days
        c = queue.classify(pr, product, False, {}, now)
        self.assertEqual(c["group"], "you")
        self.assertEqual(c["status"], "stale")
        self.assertEqual(c["does"], "merge as is or close")

    def test_classify_row9_needs_review(self):
        pr = {
            "number": 9,
            "title": "clean up imports",
            "baseRefName": "develop",
            "is_bot": False,
            "mergeable": "MERGEABLE",
            "failing_contexts": 0,
            "updatedAt": "2026-11-01T10:00:00Z",
            "review_state": None,
        }
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        c = queue.classify(pr, product, False, {}, now)
        self.assertEqual(c["group"], "review")
        self.assertEqual(c["status"], "review")
        self.assertEqual(c["does"], "review loop agent")

    def test_classify_row10_feature_ready(self):
        pr = {
            "number": 10,
            "title": "feat: new checkout modal",
            "baseRefName": "develop",
            "is_bot": False,
            "mergeable": "MERGEABLE",
            "failing_contexts": 0,
            "updatedAt": "2026-11-01T10:00:00Z",
            "review_state": "SUCCESS",
        }
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        c = queue.classify(pr, product, False, {}, now)
        self.assertEqual(c["group"], "ready")
        self.assertEqual(c["status"], "feat")
        self.assertEqual(c["does"], "merges on train day once OK")

        # With OK override
        overrides = {"acme/app#10": {"ok": "2026-11-09T07:00:00Z"}}
        c_ok = queue.classify(pr, product, False, overrides, now)
        self.assertEqual(c_ok["group"], "ready")
        self.assertEqual(c_ok["status"], "ready")
        self.assertEqual(c_ok["does"], "merges on train day")

    def test_classify_row11_ready(self):
        pr = {
            "number": 11,
            "title": "fix: resolve memory leak",
            "baseRefName": "develop",
            "is_bot": False,
            "mergeable": "MERGEABLE",
            "failing_contexts": 0,
            "updatedAt": "2026-11-01T10:00:00Z",
            "review_state": "SUCCESS",
        }
        product = {"repo": "acme/app", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        c = queue.classify(pr, product, False, {}, now)
        self.assertEqual(c["group"], "ready")
        self.assertEqual(c["status"], "ready")
        self.assertEqual(c["does"], "merges on train day")

    def test_dev_red_rule(self):
        # < 5 bot PRs -> not dev_red
        prs_under_5 = [
            {"is_bot": True, "baseRefName": "develop", "failing_contexts": 1, "mergeable": "MERGEABLE"}
            for _ in range(4)
        ]
        self.assertFalse(queue.dev_red(prs_under_5))

        # 5 bot PRs, 4 failing (80% exactly) -> not > 80%, so False
        prs_80 = [
            {"is_bot": True, "baseRefName": "develop", "failing_contexts": 1, "mergeable": "MERGEABLE"}
            for _ in range(4)
        ] + [
            {"is_bot": True, "baseRefName": "develop", "failing_contexts": 0, "mergeable": "MERGEABLE"}
        ]
        self.assertFalse(queue.dev_red(prs_80))

        # 5 bot PRs, all 5 failing (100% > 80%) -> True
        prs_100 = [
            {"is_bot": True, "baseRefName": "develop", "failing_contexts": 1, "mergeable": "MERGEABLE"}
            for _ in range(5)
        ]
        self.assertTrue(queue.dev_red(prs_100))

        # Conflicting bot PR counts as failing
        prs_conf = [
            {"is_bot": True, "baseRefName": "develop", "failing_contexts": 0, "mergeable": "CONFLICTING"}
            for _ in range(5)
        ]
        self.assertTrue(queue.dev_red(prs_conf))

    def test_train_scheduling_and_batches(self):
        # Mon-Thu layout:
        # Ready fixes in batches of 3 oldest first;
        # Then each feature alone on its own day;
        # Review PRs fill later days (<3, no feature, cond: True);
        # Wednesday bots up to 5 on Wednesday
        today = datetime(2026, 11, 9, tzinfo=timezone.utc).date()  # Monday

        prs = [
            # 3 ready fixes
            {"number": 1, "title": "fix 1", "createdAt": "2026-11-01T01:00:00Z", "classification": {"group": "ready", "status": "ready", "does": "merges on train day"}},
            {"number": 2, "title": "fix 2", "createdAt": "2026-11-01T02:00:00Z", "classification": {"group": "ready", "status": "ready", "does": "merges on train day"}},
            {"number": 3, "title": "fix 3", "createdAt": "2026-11-01T03:00:00Z", "classification": {"group": "ready", "status": "ready", "does": "merges on train day"}},
            # 1 feature (needs OK)
            {"number": 10, "title": "feat 10", "createdAt": "2026-11-01T04:00:00Z", "classification": {"group": "ready", "status": "feat", "does": "merges on train day once OK"}},
            # 2 bot ready
            {"number": 20, "title": "bot 20", "createdAt": "2026-11-01T05:00:00Z", "classification": {"group": "bots", "status": "ready", "does": "Wednesday batch"}},
            {"number": 21, "title": "bot 21", "createdAt": "2026-11-01T06:00:00Z", "classification": {"group": "bots", "status": "ready", "does": "Wednesday batch"}},
            # 2 review PRs
            {"number": 30, "title": "rev 30", "createdAt": "2026-11-01T07:00:00Z", "classification": {"group": "review", "status": "review", "does": "review loop agent"}},
            {"number": 31, "title": "rev 31", "createdAt": "2026-11-01T08:00:00Z", "classification": {"group": "review", "status": "review", "does": "review loop agent"}},
        ]

        t = queue.train(prs, {}, today, False, repo="acme/app")
        days = t["days"]

        # Monday (days[0]): 3 fixes
        self.assertEqual([p["number"] for p in days[0]], [1, 2, 3])

        # Tuesday (days[1]): feature alone
        self.assertEqual([p["number"] for p in days[1]], [10])
        self.assertTrue(days[1][0].get("needs_ok"))

        # Wednesday (days[2]): bots batch (20, 21) + review PRs if room
        wed_nums = [p["number"] for p in days[2]]
        self.assertIn(20, wed_nums)
        self.assertIn(21, wed_nums)
        self.assertIn(30, wed_nums)  # room for 1 review PR (up to 3)

        # Thursday (days[3]): review PR 31
        thu_nums = [p["number"] for p in days[3]]
        self.assertEqual(thu_nums, [31])
        self.assertTrue(days[3][0].get("cond"))

    def test_train_dev_red_blocks_train(self):
        prs = [
            {"number": 1, "title": "fix 1", "createdAt": "2026-11-01T01:00:00Z", "classification": {"group": "ready", "status": "ready", "does": "merges on train day"}}
        ]
        t = queue.train(prs, {}, datetime.now(timezone.utc).date(), True, repo="acme/app")
        self.assertTrue(t.get("blocked"))

    def test_overrides_front_hold_ok(self):
        self.ok(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )

        # queue-action hold
        self.ok("release", "queue-action", "acme/app#10", "hold")
        state = release.release()
        self.assertIn("acme/app#10", state["queue_actions"])
        self.assertIn("hold", state["queue_actions"]["acme/app#10"])

        # classified as held
        pr = {
            "number": 10,
            "title": "feat: new feature",
            "baseRefName": "develop",
            "is_bot": False,
            "mergeable": "MERGEABLE",
            "failing_contexts": 0,
            "updatedAt": "2026-11-01T10:00:00Z",
            "review_state": "SUCCESS",
        }
        product = state["products"]["app"]
        c = queue.classify(pr, product, False, state["queue_actions"], datetime.now(timezone.utc))
        self.assertEqual(c["group"], "held")
        self.assertEqual(c["status"], "hold")

        # unhold
        self.ok("release", "queue-action", "acme/app#10", "unhold")
        state2 = release.release()
        self.assertNotIn("acme/app#10", state2["queue_actions"])

        # front override
        self.ok("release", "queue-action", "acme/app#2", "front")
        state3 = release.release()
        self.assertIn("front", state3["queue_actions"]["acme/app#2"])

        # Train ordering with front:
        p1 = {"number": 1, "title": "fix 1", "createdAt": "2026-11-01T01:00:00Z", "classification": {"group": "ready", "status": "ready", "does": "merges on train day"}}
        p2 = {"number": 2, "title": "fix 2", "createdAt": "2026-11-01T02:00:00Z", "classification": {"group": "ready", "status": "ready", "does": "merges on train day"}}
        t = queue.train([p1, p2], state3["queue_actions"], datetime(2026, 11, 9, tzinfo=timezone.utc).date(), False, repo="acme/app")
        # p2 should be first because of front override
        self.assertEqual([x["number"] for x in t["days"][0]], [2, 1])

    def test_agent_assignment(self):
        prs = [
            {"number": 1, "updatedAt": "2026-11-01T10:00:00Z", "classification": {"does": "fix agent"}},
            {"number": 2, "updatedAt": "2026-11-03T10:00:00Z", "classification": {"does": "rebase agent"}},
            {"number": 3, "updatedAt": "2026-11-02T10:00:00Z", "classification": {"does": "review loop agent"}},
            {"number": 4, "updatedAt": "2026-11-04T10:00:00Z", "classification": {"does": "fix agent"}},
        ]
        queue.assign_agents(prs)
        # Newest are 4 (Nov 4), 2 (Nov 3), 3 (Nov 2) -> agent_today: True
        # 1 (Nov 1) -> agent_today: False, agent_queued: True
        by_num = {p["number"]: p for p in prs}
        self.assertTrue(by_num[4]["agent_today"])
        self.assertTrue(by_num[2]["agent_today"])
        self.assertTrue(by_num[3]["agent_today"])
        self.assertFalse(by_num[1]["agent_today"])
        self.assertTrue(by_num[1]["agent_queued"])

    def test_queue_gh_error_on_one_product_does_not_block_others(self):
        self.ok(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        self.ok(
            "release", "product", "add", "store",
            "--repo", "acme/store",
            "--flow", "pr",
            "--project", "aura",
        )

        def fake_gh(*args):
            # Fail for app, succeed for store
            if any("app" in str(arg) for arg in args):
                raise Exception("API rate limit exceeded")
            return {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        }
                    }
                }
            }

        with mock.patch.object(sources, "gh_json", side_effect=fake_gh):
            snapshot, text = queue.queue()
            self.assertIn("error: gh failure reading acme/app", text)
            self.assertIsNotNone(snapshot["products"]["app"]["error"])
            self.assertIsNone(snapshot["products"]["store"]["error"])

    def _gate_plan(self, checks, mode_live=False):
        self.ok("release", "product", "add", "app", "--repo", "acme/app",
                "--flow", "release-branch", "--project", "c2e")
        ship = {"verdict": "ship-all", "ship": [5], "held": [], "reasons": ["1 PR"], "score": "green"}
        with mock.patch.object(release, "check", return_value={"ok": False, "checks": checks}) as chk, \
             mock.patch.object(release, "unreleased_prs", return_value=([], "c0", [])), \
             mock.patch.object(release, "ci_green", return_value=True), \
             mock.patch.object(release, "decide", return_value=ship):
            v = release.plan(only="app", dry=True)["app"]
        return v, chk

    def test_production_gate_holds_a_shipping_verdict(self):
        v, chk = self._gate_plan([{"name": "health-ping", "ok": False, "detail": "status 500"}])
        self.assertEqual(v["verdict"], "wait")
        self.assertEqual(v["ship"], [])
        self.assertEqual(v["gate"], ["health-ping"])
        self.assertIn("production already failing health-ping", v["reasons"][0])
        self.assertEqual(chk.call_count, 2)  # re-sampled before holding

    def test_production_gate_counts_broken_probes(self):
        # the watch rolls back on a broken probe, so the gate must hold on it too
        v, _ = self._gate_plan([{"name": "errors-sentry", "ok": False, "detail": "cmd error: 401"}])
        self.assertEqual(v["verdict"], "wait")
        self.assertIn("errors-sentry cmd error: 401", v["reasons"][0])

    def test_production_gate_error_keeps_prs_and_holds(self):
        self.ok("release", "product", "add", "app", "--repo", "acme/app",
                "--flow", "release-branch", "--project", "c2e")
        ship = {"verdict": "ship-all", "ship": [5], "held": [], "reasons": ["1 PR"], "score": "green"}
        with mock.patch.object(release, "check", side_effect=FactoryError("boom")), \
             mock.patch.object(release, "unreleased_prs", return_value=([], "c0", [])), \
             mock.patch.object(release, "ci_green", return_value=True), \
             mock.patch.object(release, "decide", return_value=ship):
            v = release.plan(only="app", dry=True)["app"]
        self.assertEqual(v["verdict"], "wait")
        self.assertIn("could not run: boom", v["reasons"][0])

    def test_production_gate_skips_checks_when_nothing_ships(self):
        self.ok("release", "product", "add", "app", "--repo", "acme/app",
                "--flow", "release-branch", "--project", "c2e")
        with mock.patch.object(release, "check") as chk, \
             mock.patch.object(release, "unreleased_prs", return_value=([], "c0", [])), \
             mock.patch.object(release, "ci_green", return_value=True):
            v = release.plan(only="app", dry=True)["app"]
        self.assertEqual(v["reasons"][0], "nothing to release")
        chk.assert_not_called()

    def test_production_gate_passes_after_a_blip(self):
        self.ok("release", "product", "add", "app", "--repo", "acme/app",
                "--flow", "release-branch", "--project", "c2e")
        bad = {"ok": False, "checks": [{"name": "home", "ok": False, "detail": "timed out"}]}
        good = {"ok": True, "checks": [{"name": "home", "ok": True, "detail": ""}]}
        with mock.patch.object(release, "check", side_effect=[bad, good]):
            self.assertEqual(release.production_failing("app"), [])


class QueueFixesTest(unittest.TestCase):
    def _pr(self, **kw):
        base = {"number": 1, "title": "fix x", "baseRefName": "develop", "is_bot": False, "mergeable": "MERGEABLE",
                "failing_contexts": 0, "has_pending": False, "updatedAt": "2026-11-08T10:00:00Z", "review_state": "SUCCESS"}
        base.update(kw)
        return base

    def test_pending_ci_is_not_ready(self):
        product = {"repo": "o/r", "flow": "pr", "base": "develop"}
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        self.assertEqual(queue.classify(self._pr(has_pending=True), product, False, {}, now)["group"], "review")
        self.assertEqual(queue.classify(self._pr(is_bot=True, has_pending=True, title="bump a from 1.0.0 to 1.0.1"), product, False, {}, now)["does"], "waits for CI")

    def test_train_starts_today_and_weekend_plans_next_week(self):
        prs = [{"number": n, "title": f"fix {n}", "createdAt": f"2026-11-01T0{n}:00:00Z",
                "classification": {"group": "ready", "status": "ready", "does": "merges on train day"}} for n in range(1, 5)]
        wed = datetime(2026, 11, 11, tzinfo=timezone.utc).date()
        t = queue.train(prs, {}, wed, False, repo="o/r")
        self.assertEqual([len(d) for d in t["days"]], [0, 0, 3, 1])
        sat = datetime(2026, 11, 14, tzinfo=timezone.utc).date()
        self.assertEqual([len(d) for d in queue.train(prs, {}, sat, False, repo="o/r")["days"]], [3, 1, 0, 0])

    def test_bot_batch_stays_on_wednesday_with_a_feature(self):
        prs = [{"number": 10, "title": "feat a", "createdAt": "2026-11-01T01:00:00Z", "classification": {"group": "ready", "status": "feat"}},
               {"number": 11, "title": "feat b", "createdAt": "2026-11-01T02:00:00Z", "classification": {"group": "ready", "status": "feat"}},
               {"number": 12, "title": "feat c", "createdAt": "2026-11-01T03:00:00Z", "classification": {"group": "ready", "status": "feat"}},
               {"number": 20, "title": "bump", "createdAt": "2026-11-01T04:00:00Z", "classification": {"group": "bots", "status": "ready"}}]
        mon = datetime(2026, 11, 9, tzinfo=timezone.utc).date()
        wed = [p["number"] for p in queue.train(prs, {}, mon, False, repo="o/r")["days"][2]]
        self.assertIn(20, wed)

    def test_agent_override_goes_first(self):
        prs = [{"number": n, "updatedAt": f"2026-11-0{n}T00:00:00Z", "classification": {"does": "fix agent"}} for n in range(1, 6)]
        queue.assign_agents(prs, {"o/r#1": {"agent": "2026-11-10T07:00:00Z"}, "o/r#2": {"agent": "2026-11-09T07:00:00Z"}}, "o/r", "2026-11-10")
        today = sorted(p["number"] for p in prs if p["agent_today"])
        self.assertEqual(today, [1, 4, 5])

    def test_flags_combine(self):
        product = {"repo": "o/r", "flow": "pr", "base": "develop"}
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        feat = self._pr(title="feat: x")
        both = {"o/r#1": {"ok": "t", "front": "t"}}
        self.assertEqual(queue.classify(feat, product, False, both, now)["status"], "ready")
        held_sent = {"o/r#1": {"hold": "t", "agent": "t"}}
        self.assertEqual(queue.classify(self._pr(failing_contexts=2), product, False, held_sent, now)["group"], "held")

    def test_release_prs_are_not_wrong_base(self):
        product = {"repo": "o/r", "flow": "release-branch", "base": "develop"}
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        for head in ("develop", "release/2026-11-10", "revert-12", "acme/chore/release-2026-11-09",
                     "release-please--branches--main"):
            c = queue.classify(self._pr(baseRefName="main", headRefName=head, title="release: develop -> main"), product, False, {}, now)
            self.assertEqual(c["group"], "release", head)
        c = queue.classify(self._pr(baseRefName="main", headRefName="feat/x"), product, False, {}, now)
        self.assertEqual(c["status"], "base")

    def test_major_bump_without_minor(self):
        product = {"repo": "o/r", "flow": "pr", "base": "develop"}
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        for t in ("Bump actions/checkout from 3 to 4", "bump x from v3 to v4", "bump y from 14.2.3 to 15.0.0"):
            self.assertEqual(queue.classify(self._pr(is_bot=True, title=t), product, False, {}, now)["status"], "major", t)
        self.assertEqual(queue.classify(self._pr(is_bot=True, title="bump z from 1.2.0 to 1.3.0"), product, False, {}, now)["status"], "ready")

    def test_review_prs_never_take_today(self):
        prs = [{"number": 5, "title": "x", "createdAt": "2026-11-01T00:00:00Z", "classification": {"group": "review"}}]
        mon = datetime(2026, 11, 9, tzinfo=timezone.utc).date()
        days = queue.train(prs, {}, mon, False, repo="o/r")["days"]
        self.assertEqual(days[0], [])
        self.assertEqual(days[1][0]["number"], 5)


class QueueActionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["release", "product", "add", "c2e", "--repo", "o/r", "--flow", "pr", "--project", "c2e"])

    def test_old_single_action_entries_are_rejected(self):
        data = release.release()
        data["queue_actions"] = {"o/r#1": {"action": "hold", "at": "x"}}
        release.save_release(data)
        with self.assertRaises(FactoryError):
            release.release()

    def test_unhold_clears_close(self):
        queue.record_queue_action("o/r#8", "close")
        queue.record_queue_action("o/r#8", "unhold")
        self.assertNotIn("o/r#8", release.release()["queue_actions"])

    def test_unhold_keeps_ok_and_snapshot_merges(self):
        queue.record_queue_action("o/r#7", "ok")
        queue.record_queue_action("o/r#7", "hold")
        queue.record_queue_action("o/r#7", "unhold")
        self.assertEqual(set(release.release()["queue_actions"]["o/r#7"]), {"ok"})
        with mock.patch.object(queue, "open_prs", return_value=[]), \
             mock.patch.object(release, "unreleased_prs", return_value=([], "sha0", [])):
            queue.queue()
            data = release.release()
            data["queue"]["products"]["other"] = {"prs": [], "train": {}, "error": None}
            release.save_release(data)
            queue.queue(product="c2e")
        self.assertIn("other", release.release()["queue"]["products"])

    def test_weekend_review_can_take_next_monday(self):
        prs = [{"number": 5, "title": "x", "createdAt": "2026-11-01T00:00:00Z", "classification": {"group": "review"}}]
        sat = datetime(2026, 11, 14, tzinfo=timezone.utc).date()
        self.assertEqual(queue.train(prs, {}, sat, False, repo="o/r")["days"][0][0]["number"], 5)

    def test_agent_sent_after_midnight_tunis_counts_today(self):
        prs = [{"number": n, "updatedAt": f"2026-11-0{n}T00:00:00Z", "classification": {"does": "fix agent"}} for n in range(1, 6)]
        queue.assign_agents(prs, {"o/r#1": {"agent": "2026-11-09T23:30:00Z"}}, "o/r", "2026-11-10")
        self.assertTrue(next(p for p in prs if p["number"] == 1)["agent_today"])

    def test_red_develop_leaves_two_agent_slots(self):
        prs = [{"number": n, "updatedAt": f"2026-11-0{n}T00:00:00Z", "classification": {"does": "rebase agent"}} for n in range(1, 5)]
        queue.assign_agents(prs, {}, "o/r", "2026-11-10", slots=2)
        self.assertEqual(sum(p["agent_today"] for p in prs), 2)

    def test_rollup_failure_counts_as_red(self):
        node = {"number": 3, "title": "fix", "baseRefName": "develop", "headRefName": "f", "isDraft": False,
                "mergeable": "MERGEABLE", "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-01T00:00:00Z",
                "author": {"login": "owner"}, "labels": {"nodes": []},
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE", "contexts": {"nodes": []}}}}]}}
        res = {"data": {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": [node, None]}}}}
        with mock.patch.object(sources, "gh_json", return_value=res):
            prs = queue.open_prs({"repo": "o/r"})
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0]["failing_contexts"], 1)


class QueuePipeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["release", "product", "add", "c2e", "--repo", "o/r", "--flow", "pr", "--project", "c2e"])

    def test_pipe_data_structure(self):
        raw_prs = [
            {"number": 1, "title": "feat: x", "baseRefName": "develop", "headRefName": "feat/x", "is_bot": False, "mergeable": "MERGEABLE", "failing_contexts": 0, "review_state": "SUCCESS", "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-01T00:00:00Z"},
            {"number": 2, "title": "bump x", "baseRefName": "develop", "headRefName": "dep/x", "is_bot": True, "mergeable": "MERGEABLE", "failing_contexts": 0, "review_state": "SUCCESS", "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-01T00:00:00Z"},
            {"number": 3, "title": "release: develop -> main", "baseRefName": "main", "headRefName": "release/2026-11-10", "is_bot": False, "mergeable": "MERGEABLE", "failing_contexts": 0, "review_state": "SUCCESS", "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-01T00:00:00Z"},
        ]
        dev_unreleased = [
            {"number": 100, "title": "prev unreleased", "mergedAt": "2026-11-05T12:00:00Z", "files": []}
        ]
        data = release.release()
        data["journal"] = [
            {"product": "c2e", "event": "shipped", "at": "2026-11-04T10:00:00Z", "sha": "abc1234", "prs": [99]},
            {"product": "c2e", "event": "started", "at": "2026-11-05T10:00:00Z", "sha": "def5678", "prs": []},
        ]
        now = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
        today_tunis = now.astimezone(release.TUNIS).date().isoformat()
        data["verdicts"] = {
            today_tunis: {
                "c2e": {
                    "verdict": "wait",
                    "gate": ["health-ping"],
                    "gate_checked": True,
                    "reasons": ["production already failing health-ping: status 500"],
                }
            }
        }
        release.save_release(data)

        with mock.patch.object(queue, "open_prs", return_value=raw_prs), \
             mock.patch.object(release, "unreleased_prs", return_value=(dev_unreleased, "sha0", [])):
            snapshot, _ = queue.queue(product="c2e", now=now)

        prod_snap = snapshot["products"]["c2e"]
        self.assertIn("pipe", prod_snap)
        pipe = prod_snap["pipe"]
        self.assertEqual(pipe["open"], 1)  # excludes release PR #3 and bot PR #2
        self.assertEqual(pipe["dev"], [{"number": 100, "title": "prev unreleased", "mergedAt": "2026-11-05T12:00:00Z"}])
        self.assertEqual(pipe["rel"], [{"number": 3, "title": "release: develop -> main"}])
        self.assertEqual(pipe["prod"], {"at": "2026-11-04T10:00:00Z", "sha": "abc1234"})
        self.assertEqual(pipe["health"], ["health-ping"])
        self.assertNotIn("error", pipe)

    def test_pipe_dev_error_wrapped_without_failing_product(self):
        with mock.patch.object(queue, "open_prs", return_value=[]), \
             mock.patch.object(release, "unreleased_prs", side_effect=Exception("unreleased fetch failed")):
            snapshot, _ = queue.queue(product="c2e")

        prod_snap = snapshot["products"]["c2e"]
        self.assertIsNone(prod_snap["error"])
        pipe = prod_snap["pipe"]
        self.assertEqual(pipe["dev"], [])
        self.assertEqual(pipe["error"], "unreleased fetch failed")
        self.assertIsNone(pipe["health"])

    def test_reclassify_updates_row_and_preserves_pipe(self):
        raw_prs = [
            {"number": 11, "title": "feat: new feature", "baseRefName": "develop", "headRefName": "feat/x",
             "is_bot": False, "mergeable": "MERGEABLE", "failing_contexts": 0, "review_state": "SUCCESS",
             "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-01T00:00:00Z"}
        ]
        with mock.patch.object(queue, "open_prs", return_value=raw_prs), \
             mock.patch.object(release, "unreleased_prs", return_value=([], "sha0", [])):
            snapshot, _ = queue.queue(product="c2e")

        pipe_before = dict(snapshot["products"]["c2e"]["pipe"])
        self.assertEqual(snapshot["products"]["c2e"]["prs"][0]["classification"]["group"], "ready")

        # Hold via record_queue_action: reclassify is automatically called under the same lock
        queue.record_queue_action("o/r#11", "hold")
        data = release.release()
        prod_snap = data["queue"]["products"]["c2e"]
        self.assertEqual(prod_snap["prs"][0]["classification"]["group"], "held")
        self.assertEqual(prod_snap["prs"][0]["classification"]["status"], "hold")
        self.assertEqual(prod_snap["pipe"], pipe_before)

        # Unhold restores classification
        queue.record_queue_action("o/r#11", "unhold")
        data2 = release.release()
        prod_snap2 = data2["queue"]["products"]["c2e"]
        self.assertEqual(prod_snap2["prs"][0]["classification"]["group"], "ready")
        self.assertEqual(prod_snap2["pipe"], pipe_before)

    def test_health_none_empty_and_failing(self):
        now = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
        today_tunis = now.astimezone(release.TUNIS).date().isoformat()

        # 1. No verdict for today -> None
        with mock.patch.object(queue, "open_prs", return_value=[]), \
             mock.patch.object(release, "unreleased_prs", return_value=([], "sha0", [])):
            snap, _ = queue.queue(product="c2e", now=now)
        self.assertIsNone(snap["products"]["c2e"]["pipe"]["health"])

        # 2. Verdict for today without gate failure -> []
        data = release.release()
        data["verdicts"] = {today_tunis: {"c2e": {"verdict": "ship-all", "ship": [1], "reasons": [], "gate_checked": True}}}
        release.save_release(data)
        with mock.patch.object(queue, "open_prs", return_value=[]), \
             mock.patch.object(release, "unreleased_prs", return_value=([], "sha0", [])):
            snap, _ = queue.queue(product="c2e", now=now)
        self.assertEqual(snap["products"]["c2e"]["pipe"]["health"], [])

        # 3. Verdict for today with gate -> failing names
        data["verdicts"] = {today_tunis: {"c2e": {"verdict": "wait", "gate": ["probe-health", "metrics-check"], "reasons": []}}}
        release.save_release(data)
        with mock.patch.object(queue, "open_prs", return_value=[]), \
             mock.patch.object(release, "unreleased_prs", return_value=([], "sha0", [])):
            snap, _ = queue.queue(product="c2e", now=now)
        self.assertEqual(snap["products"]["c2e"]["pipe"]["health"], ["probe-health", "metrics-check"])

        # a wait with nothing to ship never ran the gate: not "checks pass"
        data = release.release()
        data["verdicts"] = {today_tunis: {"c2e": {"verdict": "wait", "ship": [], "reasons": ["nothing to release"]}}}
        release.save_release(data)
        with mock.patch.object(queue, "open_prs", return_value=[]), \
             mock.patch.object(release, "unreleased_prs", return_value=([], "c0", [])):
            snap, _ = queue.queue(product="c2e", now=now)
        self.assertIsNone(snap["products"]["c2e"]["pipe"]["health"])

    def test_dev_by_product_and_shipped_filter(self):
        now = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
        data = release.release()
        data["shipped_prs"] = {"c2e": [101]}
        release.save_release(data)

        # A) When dev_by_product is provided, use it directly (unreleased_prs not called)
        with mock.patch.object(queue, "open_prs", return_value=[]), \
             mock.patch.object(release, "unreleased_prs") as mock_unrel:
            injected_dev = [{"number": 999, "title": "injected", "mergedAt": "2026-11-09T00:00:00Z"}]
            snap, _ = queue.queue(product="c2e", now=now, dev_by_product={"c2e": injected_dev})
            mock_unrel.assert_not_called()
            self.assertEqual(snap["products"]["c2e"]["pipe"]["dev"], injected_dev)

        # B) When not provided, unreleased_prs is called, but shipped_prs (101) are dropped
        unreleased_raw = [
            {"number": 101, "title": "shipped already", "mergedAt": "2026-11-08T00:00:00Z"},
            {"number": 102, "title": "not shipped", "mergedAt": "2026-11-09T00:00:00Z"},
        ]
        with mock.patch.object(queue, "open_prs", return_value=[]), \
             mock.patch.object(release, "unreleased_prs", return_value=(unreleased_raw, "sha0", [])):
            snap, _ = queue.queue(product="c2e", now=now)
            dev_numbers = [p["number"] for p in snap["products"]["c2e"]["pipe"]["dev"]]
            self.assertEqual(dev_numbers, [102])

        # C) Skip pipe dev lookup entirely when open_prs failed
        with mock.patch.object(queue, "open_prs", side_effect=Exception("gh down")), \
             mock.patch.object(release, "unreleased_prs") as mock_unrel:
            snap, _ = queue.queue(product="c2e", now=now)
            mock_unrel.assert_not_called()
            self.assertEqual(snap["products"]["c2e"]["pipe"]["dev"], [])
            self.assertNotIn("error", snap["products"]["c2e"]["pipe"])


class QueueRaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["release", "product", "add", "c2e", "--repo", "o/r", "--flow", "pr", "--project", "c2e"])

    def test_action_during_a_queue_run_is_not_lost(self):
        pr = {"number": 7, "title": "fix: x", "baseRefName": "develop", "headRefName": "f", "mergeable": "MERGEABLE",
              "failing_contexts": 0, "has_pending": False, "is_bot": False, "labels": [], "author": "owner",
              "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-09T00:00:00Z", "review_state": "SUCCESS"}
        now = datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
        with mock.patch.object(queue, "open_prs", return_value=[pr]), \
             mock.patch.object(release, "unreleased_prs", return_value=([], "c0", [])):
            queue.queue(now=now)  # a snapshot exists
            real_open = queue.open_prs

            def open_and_click(prod):
                queue.record_queue_action("o/r#7", "hold")  # User clicks Hold while the run reads GitHub
                return [pr]
            with mock.patch.object(queue, "open_prs", side_effect=open_and_click):
                queue.queue(now=now)
        rows = release.release()["queue"]["products"]["c2e"]["prs"]
        self.assertEqual(rows[0]["classification"]["status"], "hold")


class ReclassifyWeekTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["release", "product", "add", "c2e", "--repo", "o/r", "--flow", "pr", "--project", "c2e"])

    def test_friday_action_keeps_the_snapshots_week(self):
        prs = [{"number": n, "title": f"fix: {n}", "baseRefName": "develop", "headRefName": "f", "mergeable": "MERGEABLE",
                "failing_contexts": 0, "has_pending": False, "is_bot": False, "labels": [], "author": "owner",
                "createdAt": f"2026-11-0{n}T00:00:00Z", "updatedAt": "2026-11-09T00:00:00Z", "review_state": "SUCCESS"}
               for n in (1, 2, 3, 4)]
        thu = datetime(2026, 11, 12, 7, 0, tzinfo=timezone.utc)
        with mock.patch.object(queue, "open_prs", return_value=prs), \
             mock.patch.object(release, "unreleased_prs", return_value=([], "c0", [])):
            queue.queue(now=thu)
        before = release.release()["queue"]["products"]["c2e"]["train"]["days"]
        fri = datetime(2026, 11, 13, 9, 0, tzinfo=timezone.utc)
        with mock.patch.object(queue, "_now_utc", side_effect=lambda now=None: fri):
            queue.record_queue_action("o/r#4", "hold")
        after = release.release()["queue"]["products"]["c2e"]["train"]["days"]
        self.assertEqual([len(d) for d in before], [0, 0, 0, 3])
        self.assertEqual([len(d) for d in after], [0, 0, 0, 3])  # still Thursday of the snapshot's week


def make_recheck_resp(
    number=10,
    title="fix: something",
    state="OPEN",
    base="develop",
    is_draft=False,
    mergeable="MERGEABLE",
    rollup_state="SUCCESS",
    review_state="SUCCESS",
    labels=None,
    author="someone",
    head_sha="275b78e",
    dev_rollup_state="SUCCESS",
    commit_author=...,
    commit_committer=...,
    dev_ref=True,
):
    contexts = []
    if review_state:
        contexts.append({
            "__typename": "StatusContext",
            "context": "factory/review",
            "state": review_state,
        })
    c_author = commit_author if commit_author is not ... else author
    c_committer = commit_committer if commit_committer is not ... else author
    commit_dict = {
        "oid": head_sha,
        "author": {"user": {"login": c_author}} if c_author is not None else {"user": None},
        "committer": ({"user": {"login": c_committer}, "email": f"{c_committer}@users.noreply.github.com"}
                      if c_committer not in (None, "GITHUB") else {"user": None, "email": "noreply@github.com", "name": "GitHub"}),
        "signature": {"isValid": c_committer == "GITHUB"},
        "statusCheckRollup": {
            "state": rollup_state,
            "contexts": {"nodes": contexts},
        },
    }
    repo_dict = {
        "pullRequest": {
            "number": number,
            "title": title,
            "state": state,
            "baseRefName": base,
            "headRefOid": head_sha,
            "isDraft": is_draft,
            "mergeable": mergeable,
            "author": {"login": author},
            "labels": {"nodes": [{"name": l} for l in (labels or [])]},
            "commits": {
                "nodes": [{"commit": commit_dict}]
            },
        }
    }
    if dev_ref:
        repo_dict["ref"] = {
            "target": {
                "statusCheckRollup": {
                    "state": dev_rollup_state,
                }
            }
        }
    return {
        "data": {
            "repository": repo_dict
        }
    }


class QueueTodayAndRecheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["release", "product", "add", "c2e", "--repo", "o/r", "--flow", "release-branch", "--project", "c2e"])
            cli.main(["release", "product", "add", "factory", "--repo", "o/factory", "--flow", "factory", "--project", "fac"])

    def test_today_mon_thu_sat_and_blocked(self):
        # Setup snapshot in release.json
        data = release.release()
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [
                        {
                            "number": 1,
                            "title": "fix 1",
                            "classification": {"group": "ready", "does": "rebase agent"},
                            "agent_today": True,
                        },
                        {
                            "number": 2,
                            "title": "broken idle",
                            "classification": {"group": "you", "why": "stale PR"},
                        },
                    ],
                    "train": {
                        "days": [
                            [{"number": 101, "title": "fix mon", "kind": "fix"}],
                            [{"number": 102, "title": "fix tue", "kind": "fix"}],
                            [{"number": 103, "title": "fix wed", "kind": "bot"}],
                            [{"number": 104, "title": "fix thu", "kind": "feature"}],
                        ],
                        "blocked": False,
                    },
                }
            },
        }
        data["queue_actions"] = {
            "o/r#2": {"retarget": "2026-11-08T10:00:00Z"},
            "o/r#3": {"close": "2026-11-08T10:00:00Z", "applied": {"close": "2026-11-08T12:00:00Z"}},
        }
        release.save_release(data)

        # Mon (2026-11-09)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        res_mon = queue.today("c2e", now=mon)
        self.assertEqual(res_mon["product"], "c2e")
        self.assertEqual(res_mon["mode"], "shadow")
        self.assertFalse(res_mon["blocked"])
        self.assertEqual(len(res_mon["merge"]), 1)
        self.assertEqual(res_mon["merge"][0]["repo_pr"], "o/r#101")
        self.assertEqual(res_mon["merge"][0]["number"], 101)
        self.assertEqual(res_mon["merge"][0]["kind"], "fix")
        self.assertEqual(len(res_mon["agents"]), 1)
        self.assertEqual(res_mon["agents"][0]["job"], "rebase")
        self.assertEqual(res_mon["pending_actions"], [{"repo_pr": "o/r#2", "action": "retarget"}])
        self.assertEqual(len(res_mon["asks"]), 1)
        self.assertEqual(res_mon["asks"][0]["number"], 2)

        # Thu (2026-11-12)
        data["queue"]["at"] = "2026-11-12T08:00:00Z"
        release.save_release(data)
        thu = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
        res_thu = queue.today("c2e", now=thu)
        self.assertEqual(len(res_thu["merge"]), 1)
        self.assertEqual(res_thu["merge"][0]["number"], 104)

        # Sat (2026-11-14) - no merges on weekend
        data["queue"]["at"] = "2026-11-14T08:00:00Z"
        release.save_release(data)
        sat = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
        res_sat = queue.today("c2e", now=sat)
        self.assertEqual(res_sat["merge"], [])

        # Blocked train
        data["queue"]["at"] = "2026-11-09T08:00:00Z"
        data["queue"]["products"]["c2e"]["train"] = {
            "days": [[], [], [], []],
            "blocked": True,
            "develop_agent": True,
        }
        release.save_release(data)
        res_blocked = queue.today("c2e", now=mon)
        self.assertTrue(res_blocked["blocked"])
        self.assertEqual(res_blocked["merge"], [])
        self.assertTrue(any(a.get("job") == "develop-fix" for a in res_blocked["agents"]))

    def test_factory_never_merges(self):
        data = release.release()
        data["queue"] = {
            "products": {
                "factory": {
                    "train": {
                        "days": [[{"number": 99, "title": "fac fix", "kind": "fix"}]],
                    },
                    "prs": [],
                }
            }
        }
        release.save_release(data)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        res = queue.today("factory", now=mon)
        self.assertEqual(res["merge"], [])

    def test_recheck_each_failing_reason(self):
        data = release.release()
        data["products"]["c2e"]["mode"] = "live"
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [
                            [{"number": 10, "title": "fix: small bug", "kind": "fix"}],
                            [], [], [],
                        ],
                        "blocked": False,
                    },
                }
            },
        }
        release.save_release(data)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)

        # 1. PR not open
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(state="CLOSED")):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("not open", reason)

        # 1b. CI was read from a commit other than the head: refuse
        moved = make_recheck_resp()
        moved["data"]["repository"]["pullRequest"]["headRefOid"] = "newhead"
        with mock.patch.object(sources, "gh_json", return_value=moved):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("head moved", reason)

        # 2. Base is not develop
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(base="main")):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("not develop", reason)

        # 3. PR is draft
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(is_draft=True)):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("draft", reason)

        # 4. Mergeable is not MERGEABLE
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(mergeable="CONFLICTING")):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("mergeable is CONFLICTING", reason)

        # 5. Failing checks (rollup FAILURE)
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(rollup_state="FAILURE")):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("checks failing", reason)

        # 6. Pending checks (rollup PENDING)
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(rollup_state="PENDING")):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("checks pending", reason)

        # 7. Missing/failing factory/review status
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(review_state="FAILURE")):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("factory/review", reason)

        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(review_state=None)):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("factory/review", reason)

        # 8. Has no-release label
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(labels=["no-release"])):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("no-release label", reason)

        # 9. Held in queue_actions
        data = release.release()
        data["queue_actions"]["o/r#10"] = {"hold": "2026-11-09T10:00:00Z"}
        release.save_release(data)
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp()):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("held in queue_actions", reason)

        # 10. Closed in queue_actions
        data["queue_actions"]["o/r#10"] = {"close": "2026-11-09T10:00:00Z"}
        release.save_release(data)
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp()):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("closed in queue_actions", reason)

        # 11. Feature needs ok flag
        data["queue_actions"].pop("o/r#10", None)
        release.save_release(data)
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(title="feat: great stuff")):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("feature needs ok flag", reason)

        # 12. Feature with ok flag -> success
        data["queue_actions"]["o/r#10"] = {"ok": "2026-11-09T10:00:00Z"}
        release.save_release(data)
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(title="feat: great stuff")):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertTrue(ok)
            self.assertEqual(reason, "ok")

        # 13. Regular fix with no ok flag needed -> success
        data["queue_actions"].pop("o/r#10", None)
        release.save_release(data)
        with mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(title="fix: small bug")):
            ok, reason, head = queue.recheck("o/r#10", now=mon)
            self.assertTrue(ok)
            self.assertEqual(reason, "ok")

    def test_queue_done_journal_and_applied_flag(self):
        # 1. Merged
        with mock.patch.object(sources, "gh_json", return_value={"merged": True}):
            entry_merged = queue.queue_done("o/r#21", "merged", note="clean merge")
        self.assertEqual(entry_merged["event"], "queue-merged")
        self.assertEqual(entry_merged["prs"], [21])
        self.assertEqual(entry_merged["note"], "clean merge")

        # 2. Retargeted (marks applied)
        data = release.release()
        data["queue_actions"]["o/r#22"] = {"retarget": "2026-11-09T08:00:00Z"}
        release.save_release(data)

        entry_retargeted = queue.queue_done("o/r#22", "retargeted")
        self.assertEqual(entry_retargeted["event"], "queue-retargeted")
        data_after = release.release()  # tests release() validation passes!
        self.assertIn("applied", data_after["queue_actions"]["o/r#22"])
        self.assertIn("retarget", data_after["queue_actions"]["o/r#22"]["applied"])

        # 3. Closed (marks applied)
        data_after["queue_actions"]["o/r#23"] = {"close": "2026-11-09T08:00:00Z"}
        release.save_release(data_after)

        entry_closed = queue.queue_done("o/r#23", "closed")
        self.assertEqual(entry_closed["event"], "queue-closed")
        data_after_close = release.release()
        self.assertIn("close", data_after_close["queue_actions"]["o/r#23"]["applied"])

        # 4. Agent-sent
        entry_agent = queue.queue_done("o/r#24", "agent-sent")
        self.assertEqual(entry_agent["event"], "queue-agent-sent")

        # Check queue_log (not journal)
        data_final = release.release()
        qlog = data_final.get("queue_log", [])
        events = [j["event"] for j in qlog if j["product"] == "c2e"]
        self.assertIn("queue-merged", events)
        self.assertIn("queue-retargeted", events)
        self.assertIn("queue-closed", events)
        self.assertIn("queue-agent-sent", events)

    def test_cli_queue_commands(self):
        # queue-today CLI
        code, out, _ = run("release", "queue-today", "c2e")
        self.assertEqual(code, 0)
        self.assertIn("product", out)

        data = release.release()
        data["products"]["c2e"]["mode"] = "live"
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [
                            [{"number": 10, "title": "fix: bug", "kind": "fix"}],
                            [], [], [],
                        ],
                    },
                }
            },
        }
        release.save_release(data)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)

        # queue-recheck CLI ok (exit 0)
        with mock.patch.object(queue, "_now_utc", return_value=mon), \
             mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(title="fix: bug", head_sha="275b78e")):
            code, out, _ = run("release", "queue-recheck", "o/r#10")
            self.assertEqual(code, 0)
            self.assertTrue(out.get("ok"))
            self.assertEqual(out.get("head_sha"), "275b78e")

        # queue-recheck CLI fail (exit 3) with --json
        with mock.patch.object(queue, "_now_utc", return_value=mon), \
             mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(state="CLOSED", head_sha="275b78e")):
            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                code = cli.main(["--json", "release", "queue-recheck", "o/r#10"])
            self.assertEqual(code, 3)
            res = json.loads(out_buf.getvalue())
            self.assertFalse(res["ok"])
            self.assertIn("not open", res["reason"])
            self.assertEqual(res["head_sha"], "275b78e")

        # queue-recheck CLI fail (exit 3) text output prints reason
        with mock.patch.object(queue, "_now_utc", return_value=mon), \
             mock.patch.object(sources, "gh_json", return_value=make_recheck_resp(state="CLOSED", head_sha="275b78e")):
            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf):
                code = cli.main(["release", "queue-recheck", "o/r#10"])
            self.assertEqual(code, 3)
            self.assertIn("not open", out_buf.getvalue())
            self.assertIn("head 275b78e", out_buf.getvalue())

        # queue-done CLI
        with mock.patch.object(sources, "gh_json", return_value={"merged": True}):
            code, out, _ = run("release", "queue-done", "o/r#30", "merged", "--note", "merged ok")
            self.assertEqual(code, 0)
            self.assertEqual(out.get("event"), "queue-merged")

    def test_recheck_bot_green_passes(self):
        data = release.release()
        data["products"]["c2e"]["mode"] = "live"
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [[{"number": 10, "title": "bump foo from 1.0.0 to 1.1.0", "kind": "bot"}]],
                    },
                }
            },
        }
        release.save_release(data)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        resp = make_recheck_resp(
            title="bump foo from 1.0.0 to 1.1.0",
            author="dependabot[bot]",
            rollup_state="SUCCESS",
            review_state=None,
            head_sha="sha-bot-123",
        )
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=mon)
            self.assertTrue(ok)
            self.assertIn("bot: review status to be posted by the release manager", reason)
            self.assertEqual(sha, "sha-bot-123")

    def _bot_setup(self, title):
        data = release.release()
        data["products"]["c2e"]["mode"] = "live"
        data["queue"] = {"at": "2026-11-09T08:00:00Z", "products": {"c2e": {"prs": [], "at": "2026-11-09T08:00:00Z",
                         "train": {"days": [[{"number": 10, "title": title, "kind": "bot"}]]}}}}
        release.save_release(data)
        return datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)

    def test_recheck_real_dependabot_commit_shape_passes(self):
        # GitHub signs Dependabot commits: committer "GitHub <noreply@github.com>", user null, valid signature
        mon = self._bot_setup("bump foo from 1.0.0 to 1.1.0")
        resp = make_recheck_resp(title="bump foo from 1.0.0 to 1.1.0", author="dependabot[bot]",
                                 commit_author="dependabot[bot]", commit_committer="GITHUB",
                                 review_state=None, head_sha="sha-real")
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=mon)
        self.assertTrue(ok, reason)
        self.assertEqual(sha, "sha-real")

    def test_recheck_github_committer_without_valid_signature_needs_review(self):
        mon = self._bot_setup("bump foo from 1.0.0 to 1.1.0")
        resp = make_recheck_resp(title="bump foo from 1.0.0 to 1.1.0", author="dependabot[bot]",
                                 commit_author="dependabot[bot]", commit_committer="GITHUB", review_state=None)
        resp["data"]["repository"]["pullRequest"]["commits"]["nodes"][0]["commit"]["signature"] = {"isValid": False}
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, _ = queue.recheck("o/r#10", now=mon)
        self.assertFalse(ok)
        self.assertIn("factory/review", reason)

    def test_recheck_zero_major_minor_bump_needs_review(self):
        mon = self._bot_setup("bump foo from 0.3.1 to 0.4.0")
        resp = make_recheck_resp(title="bump foo from 0.3.1 to 0.4.0", author="dependabot[bot]",
                                 commit_author="dependabot[bot]", commit_committer="GITHUB", review_state=None)
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, _ = queue.recheck("o/r#10", now=mon)
        self.assertFalse(ok)
        self.assertIn("factory/review", reason)

    def test_recheck_bot_major_refused(self):
        data = release.release()
        data["products"]["c2e"]["mode"] = "live"
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [[{"number": 10, "title": "bump foo from 1.0.0 to 2.0.0", "kind": "bot"}]],
                    },
                }
            },
        }
        release.save_release(data)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        resp = make_recheck_resp(
            title="bump foo from 1.0.0 to 2.0.0",
            author="dependabot[bot]",
            rollup_state="SUCCESS",
            review_state=None,
            head_sha="sha-bot-major",
        )
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("major bump from v1 to v2", reason)
            self.assertEqual(sha, "sha-bot-major")

    def test_recheck_shadow_refused(self):
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        ok, reason, sha = queue.recheck("o/r#10", now=mon)
        self.assertFalse(ok)
        self.assertIn("shadow", reason)
        self.assertEqual(sha, "")

    def test_recheck_develop_red_refused(self):
        data = release.release()
        data["products"]["c2e"]["mode"] = "live"
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [[{"number": 10, "title": "fix: small bug", "kind": "fix"}]],
                    },
                }
            },
        }
        release.save_release(data)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        resp = make_recheck_resp(title="fix: small bug", dev_rollup_state="FAILURE", head_sha="sha-dev-red")
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertEqual(reason, "develop CI not green: FAILURE")
            self.assertEqual(sha, "sha-dev-red")

    def test_recheck_not_on_list_refused(self):
        data = release.release()
        data["products"]["c2e"]["mode"] = "live"
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [[{"number": 99, "title": "other fix", "kind": "fix"}]],
                    },
                }
            },
        }
        release.save_release(data)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        ok, reason, sha = queue.recheck("o/r#10", now=mon)
        self.assertFalse(ok)
        self.assertIn("not on today's merge list", reason)
        self.assertEqual(sha, "")

    def test_recheck_trailing_space_and_case_held_refused(self):
        data = release.release()
        data["products"]["c2e"]["mode"] = "live"
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [[{"number": 10, "title": "fix: bug", "kind": "fix"}]],
                    },
                }
            },
        }
        data["queue_actions"]["o/r#10"] = {"hold": "2026-11-09T08:00:00Z"}
        release.save_release(data)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        resp = make_recheck_resp(title="fix: bug", head_sha="sha-held")
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok1, reason1, sha1 = queue.recheck("o/r#10 ", now=mon)
            self.assertFalse(ok1)
            self.assertIn("held in queue_actions", reason1)

            ok2, reason2, sha2 = queue.recheck("O/R#10", now=mon)
            self.assertFalse(ok2)
            self.assertIn("held in queue_actions", reason2)

    def test_recheck_unknown_mergeable_retries(self):
        data = release.release()
        data["products"]["c2e"]["mode"] = "live"
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [[{"number": 10, "title": "fix: bug", "kind": "fix"}]],
                    },
                }
            },
        }
        release.save_release(data)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)

        resp_unknown = make_recheck_resp(mergeable="UNKNOWN", head_sha="sha-retry")
        resp_mergeable = make_recheck_resp(mergeable="MERGEABLE", head_sha="sha-retry")

        sleep_calls = []
        with mock.patch.object(queue, "_sleep", side_effect=lambda s: sleep_calls.append(s)), \
             mock.patch.object(sources, "gh_json", side_effect=[resp_unknown, resp_unknown, resp_mergeable]):
            ok, reason, sha = queue.recheck("o/r#10", now=mon)
            self.assertTrue(ok)
            self.assertEqual(reason, "ok")
            self.assertEqual(len(sleep_calls), 2)
            self.assertEqual(sleep_calls, [5, 5])

        sleep_calls.clear()
        with mock.patch.object(queue, "_sleep", side_effect=lambda s: sleep_calls.append(s)), \
             mock.patch.object(sources, "gh_json", side_effect=[resp_unknown, resp_unknown, resp_unknown, resp_unknown]):
            ok, reason, sha = queue.recheck("o/r#10", now=mon)
            self.assertFalse(ok)
            self.assertIn("mergeable is UNKNOWN", reason)
            self.assertEqual(len(sleep_calls), 3)
            self.assertEqual(sleep_calls, [5, 5, 5])

    def test_recheck_head_sha_returned(self):
        data = release.release()
        data["products"]["c2e"]["mode"] = "live"
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [[{"number": 10, "title": "fix: bug", "kind": "fix"}]],
                    },
                }
            },
        }
        release.save_release(data)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        resp = make_recheck_resp(title="fix: bug", head_sha="beefcafe1234")
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=mon)
            self.assertTrue(ok)
            self.assertEqual(sha, "beefcafe1234")

    def test_today_stale_snapshot_no_merges(self):
        data = release.release()
        data["queue"] = {
            "at": "2026-11-08T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [{"number": 1, "agent_today": True}],
                    "train": {
                        "days": [[{"number": 10, "title": "fix", "kind": "fix"}]],
                    },
                }
            },
        }
        release.save_release(data)
        mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        res = queue.today("c2e", now=mon)
        self.assertTrue(res.get("stale"))
        self.assertEqual(res.get("snapshot_date"), "2026-11-08")
        self.assertEqual(res["merge"], [])
        self.assertEqual(res["agents"], [])

    def test_queue_done_dedupe(self):
        now = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
        e1 = queue.queue_done("o/r#15", "agent-sent", note="first", now=now)
        e2 = queue.queue_done("o/r#15", "agent-sent", note="second", now=now + timedelta(minutes=10))
        self.assertEqual(e1["at"], e2["at"])
        qlog = release.release()["queue_log"]
        matching = [x for x in qlog if x["repo_pr"] == "o/r#15" and x["what"] == "agent-sent"]
        self.assertEqual(len(matching), 1)

    def test_queue_done_merged_unmerged_raises(self):
        with mock.patch.object(sources, "gh_json", return_value={"merged": False}):
            with self.assertRaises(FactoryError) as ctx:
                queue.queue_done("o/r#25", "merged")
            self.assertIn("is not merged", str(ctx.exception))

    def test_record_queue_action_applied_reset(self):
        queue.record_queue_action("o/r#33", "close")
        queue.queue_done("o/r#33", "closed")
        state = release.release()
        self.assertIn("close", state["queue_actions"]["o/r#33"]["applied"])

        queue.record_queue_action("o/r#33", "close")
        state2 = release.release()
        applied = state2["queue_actions"]["o/r#33"].get("applied", {})
        self.assertNotIn("close", applied)

    def test_serve_save_queue_agent_sends_relay(self):
        from factory import relay, serve
        data = release.release()
        data["products"]["c2e"]["mode"] = "live"
        data["products"]["c2e"]["thread"] = "t-c2e-thread"
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [
                        {
                            "number": 42,
                            "title": "resolve conflicts",
                            "baseRefName": "develop",
                            "is_bot": False,
                            "mergeable": "CONFLICTING",
                            "failing_contexts": 0,
                            "updatedAt": "2026-11-09T00:00:00Z",
                            "review_state": "SUCCESS",
                            "classification": {"does": "rebase agent"},
                        }
                    ],
                }
            },
        }
        release.save_release(data)

        with mock.patch.object(relay, "enqueue_message") as mock_enqueue, \
             mock.patch.object(relay, "trigger_delivery_async"):
            status_code, body = serve.save_queue({"repo_pr": "o/r#42", "action": "agent"})
            self.assertEqual(status_code, 200)
            mock_enqueue.assert_called_once()
            args, kwargs = mock_enqueue.call_args
            thread = args[0] if args else kwargs.get("thread")
            msg = args[1] if len(args) > 1 else kwargs.get("text")
            self.assertEqual(thread, "t-c2e-thread")
            self.assertTrue(msg.startswith("Release manager (factory release queue"))
            self.assertIn("o/r#42 (rebase): resolve conflicts", msg)
            self.assertIn("factory release queue-done o/r#42 agent-sent", msg)


class DevelopQueueReviewRound2Test(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["release", "product", "add", "c2e", "--repo", "o/r", "--flow", "release-branch", "--project", "c2e"])
            cli.main(["release", "product", "set", "c2e", "--mode", "live", "--thread", "t-c2e"])
        self.mon = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)

    # 1. Bot path only for verified single Dependabot bump
    def test_classify_bot_grouped_update_goes_to_review(self):
        pr = {
            "number": 1,
            "title": "Bump the npm group with 4 updates",
            "baseRefName": "develop",
            "is_bot": True,
            "mergeable": "MERGEABLE",
            "failing_contexts": 0,
            "has_pending": False,
            "review_state": None,
        }
        prod_cfg = {"repo": "o/r", "flow": "pr"}
        c = queue.classify(pr, prod_cfg, is_dev_red=False, overrides={}, now=self.mon)
        self.assertEqual(c["group"], "review")
        self.assertEqual(c["status"], "review")
        self.assertEqual(c["does"], "review loop agent")

    def test_recheck_bot_commit_author_human_requires_review(self):
        data = release.release()
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "at": "2026-11-09T08:00:00Z",
                    "prs": [],
                    "train": {"days": [[{"number": 10, "title": "bump foo from 1.0.0 to 1.1.0", "kind": "bot"}]]},
                }
            },
        }
        release.save_release(data)
        resp = make_recheck_resp(
            title="bump foo from 1.0.0 to 1.1.0",
            author="dependabot[bot]",
            commit_author="human-author",
            commit_committer="dependabot[bot]",
            rollup_state="SUCCESS",
            review_state=None,
        )
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=self.mon)
            self.assertFalse(ok)
            self.assertIn("factory/review is missing, needs SUCCESS", reason)

    def test_recheck_bot_commit_committer_human_requires_review(self):
        data = release.release()
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "at": "2026-11-09T08:00:00Z",
                    "prs": [],
                    "train": {"days": [[{"number": 10, "title": "bump foo from 1.0.0 to 1.1.0", "kind": "bot"}]]},
                }
            },
        }
        release.save_release(data)
        resp = make_recheck_resp(
            title="bump foo from 1.0.0 to 1.1.0",
            author="dependabot[bot]",
            commit_author="dependabot[bot]",
            commit_committer="human-committer",
            rollup_state="SUCCESS",
            review_state=None,
        )
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=self.mon)
            self.assertFalse(ok)
            self.assertIn("factory/review is missing, needs SUCCESS", reason)

    def test_recheck_bot_commit_author_null_user_requires_review(self):
        data = release.release()
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "at": "2026-11-09T08:00:00Z",
                    "prs": [],
                    "train": {"days": [[{"number": 10, "title": "bump foo from 1.0.0 to 1.1.0", "kind": "bot"}]]},
                }
            },
        }
        release.save_release(data)
        resp = make_recheck_resp(
            title="bump foo from 1.0.0 to 1.1.0",
            author="dependabot[bot]",
            commit_author=None,
            commit_committer="dependabot[bot]",
            rollup_state="SUCCESS",
            review_state=None,
        )
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=self.mon)
            self.assertFalse(ok)
            self.assertIn("factory/review is missing, needs SUCCESS", reason)

    def test_recheck_bot_grouped_update_requires_review(self):
        data = release.release()
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "at": "2026-11-09T08:00:00Z",
                    "prs": [],
                    "train": {"days": [[{"number": 10, "title": "Bump npm group with 4 updates", "kind": "bot"}]]},
                }
            },
        }
        release.save_release(data)
        resp = make_recheck_resp(
            title="Bump npm group with 4 updates",
            author="dependabot[bot]",
            commit_author="dependabot[bot]",
            commit_committer="dependabot[bot]",
            rollup_state="SUCCESS",
            review_state=None,
        )
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=self.mon)
            self.assertFalse(ok)
            self.assertIn("factory/review is missing, needs SUCCESS", reason)

    # 2. Per-product snapshot time
    def test_per_product_snapshot_time_freshness(self):
        data = release.release()
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "at": "2026-11-08T08:00:00Z",
                    "prs": [],
                    "train": {"days": [[{"number": 10, "title": "fix: bug", "kind": "fix"}]]},
                }
            },
        }
        release.save_release(data)
        res = queue.today("c2e", now=self.mon)
        self.assertTrue(res.get("stale"))
        self.assertEqual(res.get("merge"), [])

    # 3. No develop merge while a release is open
    def test_recheck_refuses_when_release_in_flight(self):
        data = release.release()
        data["verdicts"] = {
            "2026-11-09": {
                "c2e": {
                    "verdict": "ship",
                    "ship": [99],
                    "relayed": True,
                }
            }
        }
        data["journal"] = [
            {"at": "2026-11-09T08:00:00Z", "product": "c2e", "event": "started", "prs": [99]}
        ]
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "at": "2026-11-09T08:00:00Z",
                    "prs": [],
                    "train": {"days": [[{"number": 10, "title": "fix: bug", "kind": "fix"}]]},
                }
            },
        }
        release.save_release(data)
        ok, reason, sha = queue.recheck("o/r#10", now=self.mon)
        self.assertFalse(ok)
        self.assertEqual(reason, "release in flight")

        data["journal"].append({"at": "2026-11-09T08:30:00Z", "product": "c2e", "event": "shipped", "prs": [99]})
        release.save_release(data)
        resp = make_recheck_resp(title="fix: bug")
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=self.mon)
            self.assertTrue(ok)

    # 4. develop must be green: PENDING, missing ref or empty state
    def test_recheck_develop_pending_refused(self):
        data = release.release()
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {"c2e": {"at": "2026-11-09T08:00:00Z", "prs": [], "train": {"days": [[{"number": 10, "title": "fix: bug", "kind": "fix"}]]}}},
        }
        release.save_release(data)
        resp = make_recheck_resp(title="fix: bug", dev_rollup_state="PENDING")
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=self.mon)
            self.assertFalse(ok)
            self.assertEqual(reason, "develop CI not green: PENDING")

    def test_recheck_develop_missing_ref_refused(self):
        data = release.release()
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {"c2e": {"at": "2026-11-09T08:00:00Z", "prs": [], "train": {"days": [[{"number": 10, "title": "fix: bug", "kind": "fix"}]]}}},
        }
        release.save_release(data)
        resp = make_recheck_resp(title="fix: bug", dev_ref=False)
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=self.mon)
            self.assertFalse(ok)
            self.assertEqual(reason, "develop CI not green: missing ref")

    def test_recheck_develop_empty_state_refused(self):
        data = release.release()
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {"c2e": {"at": "2026-11-09T08:00:00Z", "prs": [], "train": {"days": [[{"number": 10, "title": "fix: bug", "kind": "fix"}]]}}},
        }
        release.save_release(data)
        resp = make_recheck_resp(title="fix: bug", dev_rollup_state=None)
        with mock.patch.object(sources, "gh_json", return_value=resp):
            ok, reason, sha = queue.recheck("o/r#10", now=self.mon)
            self.assertFalse(ok)
            self.assertEqual(reason, "develop CI not green: empty")

    # 5. Agent cap (spec: at most 3 a day per product including develop-fix; Send agent counts)
    def test_agent_cap_3_sent_returns_empty(self):
        data = release.release()
        data["queue_log"] = [
            {"at": "2026-11-09T08:00:00Z", "product": "c2e", "what": "agent-sent", "repo_pr": f"o/r#{i}"}
            for i in (1, 2, 3)
        ]
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "at": "2026-11-09T08:00:00Z",
                    "prs": [{"number": 4, "title": "fix 4", "agent_today": True, "classification": {"does": "fix agent"}}],
                    "train": {"days": [[], [], [], []]},
                }
            },
        }
        release.save_release(data)
        res = queue.today("c2e", now=self.mon)
        self.assertEqual(res["agents"], [])

    def test_agent_cap_2_sent_plus_dev_red_only_develop_fix(self):
        data = release.release()
        data["queue_log"] = [
            {"at": "2026-11-09T08:00:00Z", "product": "c2e", "what": "agent-sent", "repo_pr": f"o/r#{i}"}
            for i in (1, 2)
        ]
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "at": "2026-11-09T08:00:00Z",
                    "prs": [{"number": 4, "title": "fix 4", "agent_today": True, "classification": {"does": "fix agent"}}],
                    "train": {"develop_agent": True, "blocked": True, "days": [[], [], [], []]},
                }
            },
        }
        release.save_release(data)
        res = queue.today("c2e", now=self.mon)
        self.assertEqual(res["agents"], [{"job": "develop-fix"}])

    def test_agent_cap_pr_already_sent_today_not_listed_again(self):
        data = release.release()
        data["queue_log"] = [
            {"at": "2026-11-09T08:00:00Z", "product": "c2e", "what": "agent-sent", "repo_pr": "o/r#4"}
        ]
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "at": "2026-11-09T08:00:00Z",
                    "prs": [
                        {"number": 4, "title": "fix 4", "agent_today": True, "classification": {"does": "fix agent"}},
                        {"number": 5, "title": "fix 5", "agent_today": True, "classification": {"does": "fix agent"}},
                    ],
                    "train": {"days": [[], [], [], []]},
                }
            },
        }
        release.save_release(data)
        res = queue.today("c2e", now=self.mon)
        agent_numbers = [a.get("number") for a in res["agents"] if "number" in a]
        self.assertNotIn(4, agent_numbers)
        self.assertIn(5, agent_numbers)

    # 6. develop-fix accounting
    def test_develop_fix_queue_done_and_cli(self):
        entry = queue.queue_done("c2e", "agent-sent", develop_fix=True, now=self.mon)
        self.assertEqual(entry["product"], "c2e")
        self.assertEqual(entry["repo_pr"], "c2e:develop-fix")
        self.assertEqual(entry["event"], "queue-agent-sent")

        code, out, _ = run("release", "queue-done", "c2e", "agent-sent", "--develop-fix")
        self.assertEqual(code, 0)
        self.assertEqual(out.get("repo_pr"), "c2e:develop-fix")
        self.assertEqual(out.get("what"), "agent-sent")

    # 7. Dashboard Send agent respects cap
    def test_dashboard_send_agent_no_agent_job_400(self):
        from factory import serve
        data = release.release()
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "prs": [{"number": 10, "title": "ready fix", "classification": {"does": "merges on train day"}}],
                }
            },
        }
        release.save_release(data)
        status, body = serve.save_queue({"repo_pr": "o/r#10", "action": "agent"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "no agent job for this PR")

    def test_dashboard_send_agent_at_cap_queued_true(self):
        from factory import relay, serve
        data = release.release()
        now_iso = release._format_time(release._now_utc(None))
        data["queue_log"] = [
            {"at": now_iso, "product": "c2e", "what": "agent-sent", "repo_pr": f"o/r#{i}"}
            for i in (1, 2, 3)
        ]
        data["queue"] = {
            "at": now_iso,
            "products": {
                "c2e": {
                    "prs": [{"number": 10, "title": "conflicts", "baseRefName": "develop", "mergeable": "CONFLICTING", "classification": {"does": "rebase agent"}}],
                }
            },
        }
        release.save_release(data)
        with mock.patch.object(relay, "enqueue_message") as mock_enqueue:
            status, body = serve.save_queue({"repo_pr": "o/r#10", "action": "agent"})
            self.assertEqual(status, 200)
            self.assertEqual(body, {"queued": True})
            mock_enqueue.assert_not_called()

    def test_dashboard_send_agent_duplicate_relay_same_pr(self):
        from factory import relay, serve
        data = release.release()
        now_iso = release._format_time(release._now_utc(None))
        data["queue"] = {
            "at": now_iso,
            "products": {
                "c2e": {
                    "prs": [{"number": 10, "title": "conflicts", "baseRefName": "develop", "mergeable": "CONFLICTING", "classification": {"does": "rebase agent"}}],
                }
            },
        }
        release.save_release(data)
        with mock.patch.object(relay, "enqueue_message") as mock_enqueue, \
             mock.patch.object(relay, "trigger_delivery_async"):
            status1, body1 = serve.save_queue({"repo_pr": "o/r#10", "action": "agent"})
            self.assertEqual(status1, 200)
            mock_enqueue.assert_called_once()

            status2, body2 = serve.save_queue({"repo_pr": "o/r#10", "action": "agent"})
            self.assertEqual(status2, 200)
            self.assertEqual(body2, {"queued": True})
            self.assertEqual(mock_enqueue.call_count, 1)

    def test_serve_agent_relay_failure_keeps_the_action(self):
        from factory import relay, serve
        data = release.release()
        now_iso = release._format_time(release._now_utc(None))
        data["queue"] = {"at": now_iso, "products": {"c2e": {"prs": [{"number": 11, "title": "red", "baseRefName": "develop",
                         "classification": {"does": "fix agent"}}]}}}
        release.save_release(data)
        with mock.patch.object(relay, "enqueue_message", side_effect=RuntimeError("relay down")), \
             mock.patch.object(relay, "trigger_delivery_async"):
            status, body = serve.save_queue({"repo_pr": "o/r#11", "action": "agent"})
        self.assertEqual(status, 200)
        self.assertIn("relay down", body.get("relay_error", ""))
        self.assertIn("agent", release.release()["queue_actions"]["o/r#11"])
        self.assertNotIn("agent_relayed", release.release()["queue_actions"]["o/r#11"])  # claim released

    def test_zero_major_bot_bump_is_not_in_the_wednesday_batch(self):
        product = {"repo": "o/r", "flow": "pr", "base": "develop"}
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        pr = {"number": 1, "title": "Bump a from 0.3.1 to 0.3.2", "baseRefName": "develop", "is_bot": True,
              "mergeable": "MERGEABLE", "failing_contexts": 0, "has_pending": False,
              "updatedAt": "2026-11-08T10:00:00Z", "review_state": None}
        c = queue.classify(pr, product, False, {}, now)
        self.assertNotEqual(c["group"], "bots")
        self.assertEqual(c["does"], "review loop agent")

    # 9. queue_asked: repo#n:status and dropping closed PRs
    def test_queue_asked_re_asks_when_status_changes(self):
        data = release.release()
        data["queue_asked"] = {"c2e": ["o/r#50:stale"]}
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "at": "2026-11-09T08:00:00Z",
                    "prs": [{"number": 50, "title": "PR 50", "classification": {"group": "you", "status": "base"}}],
                    "train": {"days": [[], [], [], []]},
                }
            },
        }
        release.save_release(data)
        res = queue.today("c2e", now=self.mon)
        self.assertEqual(len(res["asks_new"]), 1)
        self.assertEqual(res["asks_new"][0]["number"], 50)

    def test_queue_asked_drops_keys_when_pr_closed(self):
        data = release.release()
        data["queue_asked"] = {"c2e": ["o/r#50:stale"]}
        data["queue"] = {
            "at": "2026-11-09T08:00:00Z",
            "products": {
                "c2e": {
                    "at": "2026-11-09T08:00:00Z",
                    "prs": [
                        {"number": 51, "title": "PR 51", "classification": {"group": "you", "status": "base"}}
                    ],
                    "train": {"days": [[], [], [], []]},
                }
            },
        }
        release.save_release(data)
        with mock.patch("factory.release.unreleased_prs", return_value=([], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.queue.queue", return_value=({}, "")), \
             mock.patch("factory.relay.enqueue_message"):
            release.plan(now=self.mon)
        st = release.release()
        asked = st.get("queue_asked", {}).get("c2e", [])
        self.assertNotIn("o/r#50:stale", asked)
        self.assertIn("o/r#51:base", asked)
