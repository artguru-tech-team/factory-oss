import contextlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from factory import cli, queue, release, sources, store
from factory.store import FactoryError


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--json", *argv])
    return code, (json.loads(out.getvalue()) if code == 0 and out.getvalue().strip() else None), err.getvalue()


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)

    def ok(self, *argv):
        code, obj, err = run(*argv)
        self.assertEqual(code, 0, err)
        return obj

    def test_product_add_defaults_to_shadow(self):
        self.ok(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        state = release.release()
        prod = state["products"]["app"]
        self.assertEqual(prod["mode"], "shadow")
        self.assertEqual(prod["base"], "develop")
        self.assertEqual(prod["head"], "main")
        self.assertEqual(prod["repo"], "acme/app")
        self.assertEqual(prod["flow"], "release-branch")
        self.assertEqual(prod["project"], "c2e")
        self.assertIsNone(prod["thread"])
        self.assertEqual(prod["red_paths"], [])
        self.assertEqual(prod["checks"], [])

    def test_bad_flow_rejected(self):
        code, _, err = run(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "invalid-flow",
            "--project", "c2e",
        )
        self.assertNotEqual(code, 0)
        for allowed in ("release-branch", "gitflow", "pr", "factory"):
            self.assertIn(allowed, err)

    def test_bad_shape_refused(self):
        store.write(release.RELEASE_REL, {"products": []})
        with self.assertRaises(FactoryError):
            release.release()

    def test_product_add_existing_refused(self):
        self.ok(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        code, _, err = run(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        self.assertNotEqual(code, 0)
        self.assertIn("app", err)

    def test_product_set_unknown_refused(self):
        code, _, err = run(
            "release", "product", "set", "nonexistent",
            "--mode", "live",
        )
        self.assertNotEqual(code, 0)
        self.assertIn("nonexistent", err)

    def test_product_set_updates_mode_thread_red_path(self):
        self.ok(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        self.ok(
            "release", "product", "set", "app",
            "--mode", "live",
            "--thread", "t3-123",
            "--red-path", "apps/payments/**", "infra/**",
        )
        state = release.release()
        prod = state["products"]["app"]
        self.assertEqual(prod["mode"], "live")
        self.assertEqual(prod["thread"], "t3-123")
        self.assertEqual(prod["red_paths"], ["apps/payments/**", "infra/**"])

    @mock.patch.object(sources, "gh_json")
    def test_unreleased_from_compare(self, mock_gh):
        product = {
            "repo": "acme/app",
            "flow": "release-branch",
            "base": "develop",
            "head": "main",
        }

        def fake_gh(cmd, path):
            self.assertEqual(cmd, "api")
            if path == "repos/acme/app/commits/develop":
                return {"sha": "c3"}
            if path == "repos/acme/app/compare/main...c3":
                return {
                    "commits": [
                        {"sha": "c1"},
                        {"sha": "c2"},
                        {"sha": "c3"},
                    ]
                }
            if path == "repos/acme/app/commits/c1/pulls":
                return [{
                    "number": 11,
                    "merged_at": "2026-11-01T10:00:00Z",
                    "base": {"ref": "develop"},
                }]
            if path == "repos/acme/app/commits/c2/pulls":
                return [{
                    "number": 11,
                    "merged_at": "2026-11-01T10:00:00Z",
                    "base": {"ref": "develop"},
                }]
            if path == "repos/acme/app/commits/c3/pulls":
                return [{
                    "number": 12,
                    "merged_at": "2026-11-02T10:00:00Z",
                    "base": {"ref": "develop"},
                }]
            if path == "repos/acme/app/pulls/11":
                return {
                    "title": "PR eleven",
                    "merged_at": "2026-11-01T10:00:00Z",
                    "labels": [{"name": "backend"}, {"name": "ui"}],
                    "additions": 100,
                    "deletions": 20,
                    "changed_files": 1,
                }
            if path == "repos/acme/app/pulls/11/files?per_page=100&page=1":
                return [{"filename": "src/app.py", "patch": "@@ -1 +1 @@\n+test"}]
            if path == "repos/acme/app/pulls/12":
                return {
                    "title": "PR twelve",
                    "merged_at": "2026-11-02T10:00:00Z",
                    "labels": [],
                    "additions": 5,
                    "deletions": 3,
                    "changed_files": 2,
                }
            if path == "repos/acme/app/pulls/12/files?per_page=100&page=1":
                return [
                    {"filename": "docs/readme.md", "patch": "@@ -1 +1 @@\n+doc"},
                    {"filename": "binary.png", "patch": None},
                ]
            raise AssertionError(f"unexpected gh_json call: {cmd} {path}")

        mock_gh.side_effect = fake_gh
        prs, base_sha, unmapped = release.unreleased_prs(product)
        self.assertEqual(base_sha, "c3")
        self.assertEqual(unmapped, [])
        self.assertEqual([p["number"] for p in prs], [11, 12])
        self.assertEqual(prs[0], {
            "number": 11,
            "title": "PR eleven",
            "mergedAt": "2026-11-01T10:00:00Z",
            "labels": ["backend", "ui"],
            "files": ["src/app.py"],
            "changed": 120,
            "filesComplete": True,
            "patch": "@@ -1 +1 @@\n+test",
        })
        self.assertEqual(prs[1], {
            "number": 12,
            "title": "PR twelve",
            "mergedAt": "2026-11-02T10:00:00Z",
            "labels": [],
            "files": ["docs/readme.md", "binary.png"],
            "changed": 8,
            "filesComplete": True,
            "patch": "@@ -1 +1 @@\n+doc",
        })

    @mock.patch.object(sources, "run")
    @mock.patch.object(sources, "gh_json")
    def test_factory_compares_live_head(self, mock_gh, mock_run):
        product = {
            "repo": "owner/factory",
            "flow": "factory",
            "base": "develop",
            "head": "main",
        }
        mock_run.return_value = "live-head-sha\n"
        def fake_gh(cmd, path):
            if path == "repos/owner/factory/commits/develop":
                return {"sha": "develop-tip-sha"}
            if path == "repos/owner/factory/compare/live-head-sha...develop-tip-sha":
                return {"commits": []}
            raise AssertionError(f"unexpected path: {path}")
        mock_gh.side_effect = fake_gh

        prs, base_sha, unmapped = release.unreleased_prs(product)
        self.assertEqual(prs, [])
        self.assertEqual(base_sha, "develop-tip-sha")
        self.assertEqual(unmapped, [])
        mock_run.assert_called_once_with(
            "git", "-C", os.path.expanduser("~/factory-live"), "rev-parse", "HEAD"
        )

    @mock.patch.object(sources, "gh_json")
    def test_pr_different_base_excluded(self, mock_gh):
        product = {
            "repo": "acme/app",
            "flow": "release-branch",
            "base": "develop",
            "head": "main",
        }

        def fake_gh(cmd, path):
            if path == "repos/acme/app/commits/develop":
                return {"sha": "c3"}
            if path == "repos/acme/app/compare/main...c3":
                return {
                    "commits": [
                        {"sha": "c1"},
                        {"sha": "c2"},
                        {"sha": "c3"},
                    ]
                }
            if path == "repos/acme/app/commits/c1/pulls":
                # merged into different base branch
                return [{
                    "number": 21,
                    "merged_at": "2026-11-01T10:00:00Z",
                    "base": {"ref": "main"},
                }]
            if path == "repos/acme/app/commits/c2/pulls":
                # not merged
                return [{
                    "number": 22,
                    "merged_at": None,
                    "base": {"ref": "develop"},
                }]
            if path == "repos/acme/app/commits/c3/pulls":
                # valid PR
                return [{
                    "number": 23,
                    "merged_at": "2026-11-01T12:00:00Z",
                    "base": {"ref": "develop"},
                }]
            if path == "repos/acme/app/pulls/23":
                return {
                    "title": "PR twenty-three",
                    "merged_at": "2026-11-01T12:00:00Z",
                    "labels": [],
                    "additions": 1,
                    "deletions": 1,
                    "changed_files": 1,
                }
            if path == "repos/acme/app/pulls/23/files?per_page=100&page=1":
                return [{"filename": "x.py", "patch": "@@ -1 +1 @@"}]
            raise AssertionError(f"unexpected call: {path}")

        mock_gh.side_effect = fake_gh
        prs, base_sha, unmapped = release.unreleased_prs(product)
        self.assertEqual(base_sha, "c3")
        self.assertEqual([p["number"] for p in prs], [23])

    @mock.patch.object(sources, "gh_json")
    def test_compare_truncated_raises(self, mock_gh):
        product = {
            "repo": "acme/app",
            "flow": "release-branch",
            "base": "develop",
            "head": "main",
        }
        mock_gh.side_effect = lambda cmd, path: (
            {"sha": "c_tip"} if "commits/develop" in path else {
                "total_commits": 300,
                "commits": [{"sha": f"c{i}"} for i in range(250)],
            }
        )
        with self.assertRaises(FactoryError) as ctx:
            release.unreleased_prs(product)
        self.assertIn("compare truncated: 250 of 300 commits", str(ctx.exception))

    @mock.patch.object(sources, "gh_json")
    def test_unreleased_prs_reads_develop_tip_and_returns_plain_tuple(self, mock_gh):
        product = {
            "repo": "org/repo",
            "flow": "release-branch",
            "base": "develop",
            "head": "main",
        }
        def fake_gh(cmd, path):
            if path == "repos/org/repo/commits/develop":
                return {"sha": "tip_sha_1234567"}
            if path == "repos/org/repo/compare/main...tip_sha_1234567":
                return {"commits": []}
            raise AssertionError(f"unexpected path: {path}")
        mock_gh.side_effect = fake_gh
        result = release.unreleased_prs(product)
        self.assertIs(type(result), tuple)
        prs, base_sha, unmapped = result
        self.assertEqual(prs, [])
        self.assertEqual(base_sha, "tip_sha_1234567")
        self.assertEqual(unmapped, [])

    def test_rollback_sets_hold(self):
        self.ok(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        t0 = datetime(2026, 11, 5, 6, 30, tzinfo=timezone.utc)
        entry = release.record(
            "app", "rolled-back", prs=[10, 11], sha="sha123", note="bad migration", now=t0
        )
        self.assertEqual(entry["event"], "rolled-back")
        self.assertEqual(entry["prs"], [10, 11])
        self.assertEqual(entry["sha"], "sha123")
        self.assertEqual(entry["note"], "bad migration")
        self.assertEqual(entry["at"], "2026-11-05T06:30:00Z")

        state = release.release()
        self.assertIn("app", state["holds"])
        self.assertEqual(state["holds"]["app"], {
            "since": "2026-11-05T06:30:00Z",
            "issue": "bad migration",
        })
        self.assertEqual(len(state["journal"]), 1)
        self.assertEqual(state["journal"][0], entry)

        with self.assertRaises(FactoryError):
            release.record("unknown", "rolled-back", note="x")

        with self.assertRaises(FactoryError):
            release.record("app", "invalid-event", note="x")

        # CLI record
        rec_obj = self.ok(
            "release", "record", "app", "started",
            "--pr", "10", "11",
            "--sha", "c123",
            "--note", "cutting branch",
        )
        self.assertEqual(rec_obj["event"], "started")
        self.assertEqual(rec_obj["prs"], [10, 11])
        self.assertEqual(rec_obj["sha"], "c123")
        self.assertEqual(rec_obj["note"], "cutting branch")

        code, _, err = run("release", "record", "app", "held", "--note", "manual hold")
        self.assertEqual(code, 0, err)

        # Journal trims to 500 entries
        state = release.release()
        state["journal"] = [{"at": "2026-11-05T00:00:00Z", "product": "app", "event": "held", "prs": [], "sha": None, "note": str(i)} for i in range(505)]
        release.save_release(state)
        release.record("app", "held", note="one more")
        state = release.release()
        self.assertEqual(len(state["journal"]), 500)
        self.assertEqual(state["journal"][-1]["note"], "one more")

    def test_release_accepts_older_file_without_shipped_prs(self):
        old_data = {
            "products": {},
            "paused": {},
            "holds": {},
            "journal": [],
            "verdicts": {},
            "stale_asked": {},
        }
        store.write(release.RELEASE_REL, old_data)
        data = release.release()
        self.assertIn("shipped_prs", data)
        self.assertEqual(data["shipped_prs"], {})

    def test_record_shipped_and_rolled_back_adds_to_shipped_prs(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        release.record("c2e", "shipped", prs=[1, 2, 2, 3])
        st = release.release()
        self.assertEqual(st["shipped_prs"]["c2e"], [1, 2, 3])

        release.record("c2e", "rolled-back", prs=[3, 4, 5])
        st = release.release()
        self.assertEqual(st["shipped_prs"]["c2e"], [1, 2, 3, 4, 5])

        # Test capping at 2000
        st["shipped_prs"]["c2e"] = list(range(1999))
        release.save_release(st)
        release.record("c2e", "shipped", prs=[1999, 2000, 2001])
        st = release.release()
        self.assertEqual(len(st["shipped_prs"]["c2e"]), 2000)
        self.assertEqual(st["shipped_prs"]["c2e"][-3:], [1999, 2000, 2001])

    def test_record_shipped_drops_held_prs_after_last_started(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        # Prior release: held PR 9 before last started
        release.record("c2e", "held", prs=[9], note="prior hold")

        # Current release started
        release.record("c2e", "started", prs=[9, 10, 11, 12], sha="s0")

        # PR 11 has a cherry-pick conflict, so it is held after started
        release.record("c2e", "held", prs=[11], note="cherry-pick conflict")

        # Shipped is called with the full list
        entry = release.record("c2e", "shipped", prs=[9, 10, 11, 12], sha="s1")

        # PR 11 must be dropped from entry["prs"] and from shipped_prs; PR 9 must NOT be dropped
        self.assertEqual(entry["prs"], [9, 10, 12])
        st = release.release()
        self.assertEqual(st["shipped_prs"]["c2e"], [9, 10, 12])
        self.assertNotIn(11, st["shipped_prs"]["c2e"])

    def test_record_rolled_back_drops_held_prs_after_last_started(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        release.record("c2e", "started", prs=[10, 11], sha="s0")
        release.record("c2e", "held", prs=[11], note="cherry-pick conflict")
        entry = release.record("c2e", "rolled-back", prs=[10, 11], note="issue")
        self.assertEqual(entry["prs"], [10])
        self.assertEqual(release.release()["shipped_prs"]["c2e"], [10])

    def test_rollback_failed_pauses(self):
        self.ok(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        t0 = datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc)
        entry = release.record(
            "app", "rollback-failed", prs=[12], sha="sha456", note="revert conflict", now=t0
        )
        self.assertEqual(entry["event"], "rollback-failed")
        state = release.release()
        self.assertTrue(state["paused"].get("app"))
        self.assertNotIn("app", state["holds"])
        self.assertEqual(len(state["journal"]), 1)
        self.assertEqual(state["journal"][0]["event"], "rollback-failed")

    def test_unhold_refused_before_24h(self):
        self.ok(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        t0 = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)
        release.record("app", "rolled-back", note="broken watch", now=t0)

        t_under = t0 + timedelta(hours=10)
        with self.assertRaises(FactoryError) as ctx:
            release.unhold("app", now=t_under)
        self.assertIn("hours remain", str(ctx.exception))
        self.assertIn("14", str(ctx.exception))

        with self.assertRaises(FactoryError) as ctx_nohold:
            release.unhold("other-product", now=t_under)
        self.assertIn("no hold on other-product", str(ctx_nohold.exception))

        t_after = t0 + timedelta(hours=24, minutes=1)
        res = release.unhold("app", now=t_after)
        self.assertEqual(res["since"], "2026-11-05T06:00:00Z")
        state = release.release()
        self.assertNotIn("app", state["holds"])

        with self.assertRaises(FactoryError) as ctx_again:
            release.unhold("app", now=t_after)
        self.assertIn("no hold on app", str(ctx_again.exception))

        # CLI unhold under 24h fails with fixed clock
        fixed_now = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
        release.record("app", "rolled-back", note="broken watch", now=fixed_now)
        with mock.patch("factory.release._now_utc", return_value=fixed_now + timedelta(hours=10)):
            code, _, err = run("release", "unhold", "app")
        self.assertNotEqual(code, 0)
        self.assertIn("hours remain", err)

        # CLI unhold with old hold succeeds with fixed clock
        with store.lock():
            st = release.release()
            st["holds"]["app"] = {
                "since": (fixed_now - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "issue": "old hold",
            }
            release.save_release(st)
        with mock.patch("factory.release._now_utc", return_value=fixed_now):
            unheld = self.ok("release", "unhold", "app")
        self.assertEqual(unheld["issue"], "old hold")

    def test_pause_all_and_resume(self):
        self.ok(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        res = self.ok("release", "pause")
        self.assertEqual(res, {"*": True})
        state = release.release()
        self.assertTrue(state["paused"].get("*"))

        res = self.ok("release", "resume")
        self.assertEqual(res, {})
        state = release.release()
        self.assertNotIn("*", state["paused"])

        res = self.ok("release", "resume")
        self.assertEqual(res, {})

        res = self.ok("release", "pause", "app")
        self.assertEqual(res.get("app"), True)
        state = release.release()
        self.assertTrue(state["paused"].get("app"))

        res = self.ok("release", "resume", "app")
        self.assertNotIn("app", res)
        state = release.release()
        self.assertNotIn("app", state["paused"])

        res = self.ok("release", "resume", "app")
        self.assertNotIn("app", res)

    def test_status_json_for_product(self):
        self.ok(
            "release", "product", "add", "app",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        res = self.ok("release", "status", "app")
        self.assertEqual(res, {
            "paused": False,
            "hold": None,
            "verdict": None,
            "mode": "shadow",
            "project": "c2e",
            "lastStarted": None,
            "journal": [],
        })
        self.assertEqual(set(res.keys()), {"paused", "hold", "verdict", "mode", "project", "lastStarted", "journal"})

        t0 = datetime(2026, 11, 5, 10, 0, 0, tzinfo=timezone.utc)
        release.record("app", "rolled-back", note="broken deploy", now=t0)
        res = self.ok("release", "status", "app")
        self.assertFalse(res["paused"])
        self.assertIsNotNone(res["hold"])
        self.assertEqual(res["hold"]["issue"], "broken deploy")
        self.assertIsNone(res["verdict"])
        self.assertEqual(len(res["journal"]), 1)
        self.assertEqual(res["journal"][0]["event"], "rolled-back")

        self.ok("release", "pause")
        res = self.ok("release", "status", "app")
        self.assertTrue(res["paused"])

        tz_tunis = ZoneInfo("Africa/Tunis")
        today_fixed = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
        today_tunis = today_fixed.astimezone(tz_tunis).strftime("%Y-%m-%d")
        with store.lock():
            data = release.release()
            data["verdicts"][today_tunis] = {
                "app": {
                    "verdict": "ship-all",
                    "ship": [101, 102],
                    "held": [],
                    "reasons": ["2 PRs, all green"],
                    "score": "green",
                }
            }
            release.save_release(data)

        with mock.patch("factory.release._now_utc", return_value=today_fixed):
            res = self.ok("release", "status", "app")
        self.assertTrue(res["paused"])
        self.assertEqual(res["verdict"]["verdict"], "ship-all")
        self.assertEqual(res["verdict"]["ship"], [101, 102])

        code, _, err = run("release", "status", "app")
        self.assertEqual(code, 0, err)

        code, _, err = run("release", "status")
        self.assertEqual(code, 0, err)

    def test_status_unknown_product_raises(self):
        with self.assertRaises(FactoryError) as ctx:
            release.status("unknown_prod")
        self.assertIn("unknown product 'unknown_prod'", str(ctx.exception))
        code, _, err = run("release", "status", "unknown_prod")
        self.assertNotEqual(code, 0)
        self.assertIn("unknown product 'unknown_prod'", err)

    def test_status_json_project_and_last_started(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e-proj",
        )
        res = self.ok("release", "status", "c2e")
        self.assertEqual(res["project"], "c2e-proj")
        self.assertIsNone(res["lastStarted"])

        release.record("c2e", "started", prs=[101], sha="s1", note="prev=s0", now=datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc))
        release.record("c2e", "shipped", prs=[101], sha="s1", now=datetime(2026, 11, 2, 7, 30, tzinfo=timezone.utc))
        release.record("c2e", "started", prs=[102], sha="s2", note="prev=s1", now=datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc))

        res2 = self.ok("release", "status", "c2e")
        self.assertEqual(res2["project"], "c2e-proj")
        self.assertIsNotNone(res2["lastStarted"])
        self.assertEqual(res2["lastStarted"]["event"], "started")
        self.assertEqual(res2["lastStarted"]["sha"], "s2")
        self.assertEqual(res2["lastStarted"]["note"], "prev=s1")

    def test_pause_resume_unregistered_raises(self):
        with self.assertRaises(FactoryError):
            release.pause("nonexistent")
        with self.assertRaises(FactoryError):
            release.resume("nonexistent")
        code, _, err = run("release", "pause", "nonexistent")
        self.assertNotEqual(code, 0)
        self.assertIn("unknown product 'nonexistent'", err)
        code, _, err = run("release", "resume", "nonexistent")
        self.assertNotEqual(code, 0)
        self.assertIn("unknown product 'nonexistent'", err)

    def test_unhold_corrupt_since_raises(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        with store.lock():
            st = release.release()
            st["holds"]["c2e"] = {"since": None, "issue": "missing since"}
            release.save_release(st)
        with self.assertRaises(FactoryError) as ctx:
            release.unhold("c2e")
        self.assertIn("corrupt hold on c2e: missing since", str(ctx.exception))

        with store.lock():
            st = release.release()
            st["holds"]["c2e"] = {"since": "not-a-valid-iso-date", "issue": "bad date"}
            release.save_release(st)
        with self.assertRaises(FactoryError) as ctx:
            release.unhold("c2e")
        self.assertIn("corrupt hold on c2e: unparsable since", str(ctx.exception))


class ReleaseScoreTest(unittest.TestCase):
    def test_default_red_defined(self):
        self.assertEqual(
            release.DEFAULT_RED,
            [
                "**/migrations/**",
                "prisma/**",
                "supabase/migrations/**",
                "**/auth/**",
                "**/middleware.*",
                ".github/workflows/**",
                "helm/**",
                "charts/**",
                "**/payout*",
                "**/wallet*",
                "**/payment*",
                "**/checkout*",
                "**/fee[s_./-]*",
            ],
        )

    def test_migration_is_red(self):
        pr = {
            "number": 1,
            "title": "Migration PR",
            "files": ["libs/prisma-schema/prisma/migrations/20261101_x/migration.sql", "tests/foo_test.py"],
            "changed": 20,
            "filesComplete": True,
            "labels": [],
            "patch": "",
        }
        color, reasons = release.score_pr(pr)
        self.assertEqual(color, "red")
        self.assertIn("migration", reasons)

    def test_product_red_path(self):
        pr = {
            "number": 2,
            "title": "Payment UI",
            "files": ["apps/payments/a.ts", "tests/foo_test.py"],
            "changed": 20,
            "filesComplete": True,
            "labels": [],
            "patch": "",
        }
        color, reasons = release.score_pr(pr, red_paths=["apps/payments/**"])
        self.assertEqual(color, "red")
        self.assertIn("red path apps/payments/**", reasons)

    def test_big_diff_is_yellow(self):
        pr_401 = {
            "number": 3,
            "title": "Big diff",
            "files": ["src/app.py", "tests/app_test.py"],
            "changed": 401,
            "filesComplete": True,
            "labels": [],
            "patch": "",
        }
        color_401, reasons_401 = release.score_pr(pr_401)
        self.assertEqual(color_401, "yellow")
        self.assertIn("over 400 changed lines", reasons_401)

        pr_400 = {
            "number": 4,
            "title": "Border diff",
            "files": ["src/app.py", "tests/app_test.py"],
            "changed": 400,
            "filesComplete": True,
            "labels": [],
            "patch": "",
        }
        color_400, reasons_400 = release.score_pr(pr_400)
        self.assertEqual(color_400, "green")
        self.assertEqual(reasons_400, [])

    def test_no_test_file_is_yellow(self):
        pr = {
            "number": 5,
            "title": "No test",
            "files": ["src/app.py"],
            "changed": 50,
            "filesComplete": True,
            "labels": [],
            "patch": "",
        }
        color, reasons = release.score_pr(pr)
        self.assertEqual(color, "yellow")
        self.assertIn("no test file changed", reasons)

    def test_new_env_var_is_yellow(self):
        pr = {
            "number": 6,
            "title": "New env var",
            "files": ["src/config.ts", "tests/config_test.ts"],
            "changed": 10,
            "filesComplete": True,
            "labels": [],
            "patch": (
                "@@ -1 +1,4 @@\n"
                "+  const k = getEnv(\"NEW_KEY\");\n"
                "+  const proc = process.env.PROC_KEY;\n"
                "+  const secret = getEnv(MY_SECRET);\n"
            ),
        }
        color, reasons = release.score_pr(pr)
        self.assertEqual(color, "yellow")
        self.assertIn("new env var NEW_KEY", reasons)
        self.assertIn("new env var PROC_KEY", reasons)
        self.assertIn("new env var MY_SECRET", reasons)

    def test_lockfile_is_yellow(self):
        lockfiles = (
            "package-lock.json",
            "bun.lock",
            "bun.lockb",
            "yarn.lock",
            "pnpm-lock.yaml",
            "requirements.txt",
            "poetry.lock",
            "go.sum",
        )
        for lockfile in lockfiles:
            pr = {
                "number": 7,
                "title": f"Bump {lockfile}",
                "files": [f"subdir/{lockfile}", "tests/foo_test.py"],
                "changed": 10,
                "filesComplete": True,
                "labels": [],
                "patch": "",
            }
            color, reasons = release.score_pr(pr)
            self.assertEqual(color, "yellow")
            self.assertIn("dependency bump", reasons)

    def test_truncated_files_is_red(self):
        pr = {
            "number": 8,
            "title": "Truncated PR",
            "files": ["src/app.py", "tests/app_test.py"],
            "changed": 100,
            "filesComplete": False,
            "labels": [],
            "patch": "",
        }
        color, reasons = release.score_pr(pr)
        self.assertEqual(color, "red")
        self.assertIn("file list incomplete", reasons)

    def test_risk_critical_label_is_red(self):
        pr = {
            "number": 9,
            "title": "Critical PR",
            "files": ["src/app.py", "tests/app_test.py"],
            "changed": 10,
            "filesComplete": True,
            "labels": ["risk:critical", "bug"],
            "patch": "",
        }
        color, reasons = release.score_pr(pr)
        self.assertEqual(color, "red")
        self.assertIn("risk-review critical", reasons)

    def test_four_yellows_make_red(self):
        four_yellows = [("yellow", ["a"]), ("yellow", ["b"]), ("yellow", ["c"]), ("yellow", ["d"])]
        self.assertEqual(release.release_score(four_yellows), "red")

        three_yellows = [("yellow", ["a"]), ("yellow", ["b"]), ("yellow", ["c"])]
        self.assertEqual(release.release_score(three_yellows), "yellow")

        empty = []
        self.assertEqual(release.release_score(empty), "green")

        all_green = [("green", []), ("green", [])]
        self.assertEqual(release.release_score(all_green), "green")

        one_red = [("green", []), ("red", ["migration"])]
        self.assertEqual(release.release_score(one_red), "red")

    def test_money_path_is_red(self):
        for f in ("services/payout.py", "wallet/balance.ts", "checkout/cart.js", "fee_calc.py"):
            pr = {
                "number": 10,
                "title": "Money",
                "files": [f, "tests/test_money.py"],
                "changed": 10,
                "filesComplete": True,
                "labels": [],
                "patch": "",
            }
            color, reasons = release.score_pr(pr)
            self.assertEqual(color, "red")
            self.assertIn("money path", reasons)

    def test_feedback_is_not_money(self):
        feed_files = [
            "feedback.tsx",
            "src/components/feedback/Form.tsx",
            "apps/web/feed/list.ts",
            "src/utils/feeds.ts",
            "tests/test_feed.py",
        ]
        pr_green = {
            "number": 101,
            "title": "Feedback and feeds",
            "files": feed_files,
            "changed": 20,
            "filesComplete": True,
            "labels": [],
            "patch": "",
        }
        color, reasons = release.score_pr(pr_green)
        self.assertEqual(color, "green")
        self.assertEqual(reasons, [])

        for fee_path in ("apps/payments/fees.ts", "src/fee.ts"):
            pr_red = {
                "number": 102,
                "title": "Fee update",
                "files": [fee_path, "tests/test_money.py"],
                "changed": 10,
                "filesComplete": True,
                "labels": [],
                "patch": "",
            }
            color_red, reasons_red = release.score_pr(pr_red)
            self.assertEqual(color_red, "red")
            self.assertIn("money path", reasons_red)

    def test_auth_and_infra_red(self):
        pr_auth = {
            "number": 11,
            "title": "Auth",
            "files": ["src/auth/token.ts", "middleware.ts", "tests/test_auth.ts"],
            "changed": 10,
            "filesComplete": True,
            "labels": [],
            "patch": "",
        }
        color, reasons = release.score_pr(pr_auth)
        self.assertEqual(color, "red")
        self.assertIn("auth", reasons)

        pr_infra = {
            "number": 12,
            "title": "Infra",
            "files": [".github/workflows/deploy.yml", "helm/values.yaml", "charts/app/Chart.yaml", "tests/test_ci.py"],
            "changed": 10,
            "filesComplete": True,
            "labels": [],
            "patch": "",
        }
        color, reasons = release.score_pr(pr_infra)
        self.assertEqual(color, "red")
        self.assertIn("infra", reasons)

    def test_mixed_red_and_yellow_reasons_and_dedup(self):
        pr = {
            "number": 13,
            "title": "Mixed",
            "files": [
                "migrations/001.sql",
                "prisma/migrations/002.sql",
                "package-lock.json",
            ],
            "changed": 500,
            "filesComplete": False,
            "labels": ["risk:critical"],
            "patch": "@@ -1 +1,2 @@\n+ process.env.FOO\n",
        }
        color, reasons = release.score_pr(pr)
        self.assertEqual(color, "red")
        # All red reasons present
        self.assertIn("file list incomplete", reasons)
        self.assertIn("risk-review critical", reasons)
        self.assertIn("migration", reasons)
        # Migration only once
        self.assertEqual(reasons.count("migration"), 1)
        # All yellow reasons present
        self.assertIn("over 400 changed lines", reasons)
        self.assertIn("no test file changed", reasons)
        self.assertIn("new env var FOO", reasons)
        self.assertIn("dependency bump", reasons)
        # No duplicates
        self.assertEqual(len(reasons), len(set(reasons)))


def make_pr(
    number: int,
    merged_at: str = "2026-11-01T10:00:00Z",
    color: str = "green",
    labels: list[str] | None = None,
    score_reasons: list[str] | None = None,
    title: str = "",
    files: list[str] | None = None,
) -> dict:
    return {
        "number": number,
        "title": title or f"PR #{number}",
        "mergedAt": merged_at,
        "labels": list(labels) if labels else [],
        "files": list(files) if files is not None else ["src/app.py", "tests/test_app.py"],
        "changed": 10,
        "filesComplete": True,
        "patch": "",
        "color": color,
        "scoreReasons": list(score_reasons) if score_reasons else [],
    }


class ReleaseDecideTest(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Africa/Tunis")
        self.mon = datetime(2026, 11, 2, 7, 30, tzinfo=self.tz)
        self.fri = datetime(2026, 11, 6, 7, 30, tzinfo=self.tz)
        self.sat = datetime(2026, 11, 7, 7, 30, tzinfo=self.tz)
        self.empty_state = {"paused": {}, "holds": {}, "journal": []}

    def test_no_release_label_is_held(self):
        pr1 = make_pr(1, "2026-11-01T10:00:00Z", color="green", labels=["no-release"])
        pr2 = make_pr(2, "2026-11-01T11:00:00Z", color="green")
        res = release.decide([pr1, pr2], self.empty_state, "c2e", True, self.mon)
        self.assertEqual(res["verdict"], "cherry-pick")
        self.assertEqual(res["ship"], [2])
        self.assertEqual(res["held"], [{"number": 1, "reason": "no-release label"}])
        self.assertEqual(res["score"], "green")

        # When only no-release PRs exist, verdict is wait with "nothing to release"
        res_none = release.decide([pr1], self.empty_state, "c2e", True, self.mon)
        self.assertEqual(res_none["verdict"], "wait")
        self.assertEqual(res_none["ship"], [])
        self.assertEqual(res_none["held"], [{"number": 1, "reason": "no-release label"}])
        self.assertEqual(res_none["reasons"], ["nothing to release"])
        self.assertEqual(res_none["score"], "green")

    def test_no_release_held_turns_ship_all_into_cherry_pick(self):
        pr1 = make_pr(1, "2026-11-01T10:00:00Z", color="green", labels=["no-release"])
        pr2 = make_pr(2, "2026-11-01T11:00:00Z", color="green")
        res = release.decide([pr1, pr2], self.empty_state, "c2e", True, self.mon)
        self.assertEqual(res["verdict"], "cherry-pick")
        self.assertEqual(res["ship"], [2])
        self.assertEqual(res["held"], [{"number": 1, "reason": "no-release label"}])
        self.assertEqual(res["score"], "green")

    def test_cap_overflow_is_cherry_pick(self):
        prs = [
            make_pr(i, f"2026-11-01T{i:02d}:00:00Z", color="green")
            for i in range(1, 13)
        ]
        res = release.decide(prs, self.empty_state, "c2e", True, self.mon)
        self.assertEqual(res["verdict"], "cherry-pick")
        self.assertEqual(res["ship"], list(range(1, 11)))
        self.assertEqual(res["held"], [
            {"number": 11, "reason": "over the 10-PR cap"},
            {"number": 12, "reason": "over the 10-PR cap"},
        ])
        self.assertEqual(res["score"], "green")

    def test_friday_partial_is_cherry_pick(self):
        pr1 = make_pr(1, "2026-11-05T01:00:00Z", color="red", labels=["fix"], score_reasons=["migration"])
        pr2 = make_pr(2, "2026-11-05T02:00:00Z", color="green", labels=["fix"])
        pr3 = make_pr(3, "2026-11-05T03:00:00Z", color="yellow", labels=["feature"])
        res = release.decide([pr1, pr2, pr3], self.empty_state, "c2e", True, self.fri)
        self.assertEqual(res["verdict"], "cherry-pick")
        self.assertEqual(res["ship"], [2])
        self.assertEqual(res["held"], [
            {"number": 1, "reason": "Friday: only green bug/fix PRs"},
            {"number": 3, "reason": "Friday: only green bug/fix PRs"},
        ])
        self.assertEqual(res["score"], "green")

    def test_decide_unparsable_journal_at_skipped(self):
        pr = make_pr(1, color="green")
        state = {
            "paused": {},
            "holds": {},
            "journal": [
                {"at": "invalid-timestamp", "product": "c2e", "event": "rolled-back"},
                {"at": "garbage-date", "product": "c2e", "event": "shipped"},
            ],
        }
        res = release.decide([pr], state, "c2e", True, self.mon)
        self.assertEqual(res["verdict"], "ship-all")
        self.assertEqual(res["ship"], [1])

    def test_unmapped_commits_forces_cherry_pick(self):
        pr = make_pr(1, color="green")
        state = {"paused": {}, "holds": {}, "journal": []}
        res = release.decide([pr], state, "c2e", True, self.mon, unmapped=2)
        self.assertEqual(res["verdict"], "cherry-pick")
        self.assertEqual(res["ship"], [1])
        self.assertIn("2 commit(s) on develop without a PR: not shipped", res["reasons"])

    def test_unmapped_commits_names_up_to_3_short_shas(self):
        pr = make_pr(1, color="green")
        state = {"paused": {}, "holds": {}, "journal": []}
        # 1 commit
        res1 = release.decide([pr], state, "c2e", True, self.mon, unmapped=["1111111aaa"])
        self.assertEqual(res1["verdict"], "cherry-pick")
        self.assertIn("1 commit(s) on develop without a PR (1111111): not shipped", res1["reasons"])

        # 2 commits
        res2 = release.decide([pr], state, "c2e", True, self.mon, unmapped=["1111111aaa", "2222222bbb"])
        self.assertIn("2 commit(s) on develop without a PR (1111111, 2222222): not shipped", res2["reasons"])

        # 3 commits
        res3 = release.decide([pr], state, "c2e", True, self.mon, unmapped=["1111111aaa", "2222222bbb", "3333333ccc"])
        self.assertIn("3 commit(s) on develop without a PR (1111111, 2222222, 3333333): not shipped", res3["reasons"])

        # 4 commits (truncated after 3 with ...)
        res4 = release.decide([pr], state, "c2e", True, self.mon, unmapped=["1111111aaa", "2222222bbb", "3333333ccc", "4444444ddd"])
        self.assertIn("4 commit(s) on develop without a PR (1111111, 2222222, 3333333, ...): not shipped", res4["reasons"])

    def _factory_state(self):
        return {
            "products": {"factory": {"repo": "acme/factory", "flow": "factory",
                                     "base": "main", "head": "main", "mode": "live"}},
            "paused": {}, "holds": {}, "journal": [],
        }

    def test_factory_single_red_ships_solo(self):
        res = release.decide([make_pr(1, color="red")], self._factory_state(), "factory", True, self.mon)
        self.assertEqual(res["verdict"], "solo-red")
        self.assertEqual(res["ship"], [1])
        self.assertNotIn("stall", res)

    def test_factory_red_with_others_waits_and_flags_stall(self):
        prs = [make_pr(1, "2026-11-01T10:00:00Z", color="green"), make_pr(2, "2026-11-01T11:00:00Z", color="red")]
        res = release.decide(prs, self._factory_state(), "factory", True, self.mon)
        self.assertEqual(res["verdict"], "wait")
        self.assertEqual(res["ship"], [])
        self.assertEqual(res["stall"], 2)
        self.assertIn("red #2 cannot ship alone", res["reasons"][0])

    def test_factory_four_yellows_ship_all_of_main(self):
        prs = [make_pr(i, f"2026-11-01T1{i}:00:00Z", color="yellow") for i in range(1, 5)]
        res = release.decide(prs, self._factory_state(), "factory", True, self.mon)
        self.assertEqual(res["verdict"], "ship-all")
        self.assertEqual(res["ship"], [1, 2, 3, 4])
        self.assertEqual(res["held"], [])
        self.assertIn("4 or more yellows make this release red", res["reasons"])

    def test_factory_over_cap_ships_all_of_main(self):
        prs = [make_pr(i, f"2026-11-01T10:{i:02d}:00Z", color="green") for i in range(1, 13)]
        res = release.decide(prs, self._factory_state(), "factory", True, self.mon)
        self.assertEqual(res["verdict"], "ship-all")
        self.assertEqual(len(res["ship"]), 12)
        self.assertIn("over the 10-PR cap", res["reasons"])

    def test_factory_friday_non_fix_past_cap_waits(self):
        fri = self.mon + timedelta(days=4)
        prs = [make_pr(i, f"2026-11-01T10:{i:02d}:00Z", color="green", labels=["fix"]) for i in range(1, 11)]
        prs.append(make_pr(99, "2026-11-01T11:00:00Z", color="green"))
        res = release.decide(prs, self._factory_state(), "factory", True, fri)
        self.assertEqual(res["verdict"], "wait")
        self.assertIn("#99 is not a green bug/fix PR", res["reasons"][0])

    def test_factory_no_release_or_unmapped_waits_without_stall(self):
        held = make_pr(2, "2026-11-01T11:00:00Z", color="green")
        held["labels"] = ["no-release"]
        prs = [make_pr(1, "2026-11-01T10:00:00Z", color="green"), held]
        res = release.decide(prs, self._factory_state(), "factory", True, self.mon)
        self.assertEqual(res["verdict"], "wait")
        self.assertIn("#2 has the no-release label", res["reasons"][0])
        self.assertNotIn("stall", res)
        res2 = release.decide([make_pr(1, color="green")], self._factory_state(), "factory", True, self.mon, unmapped=1)
        self.assertEqual(res2["verdict"], "wait")
        self.assertNotIn("stall", res2)

    def test_paused_waits(self):
        pr = make_pr(1, color="green")
        # paused for product
        state = {"paused": {"c2e": True}, "holds": {}, "journal": []}
        res = release.decide([pr], state, "c2e", True, self.mon)
        self.assertEqual(res["verdict"], "wait")
        self.assertEqual(res["ship"], [])
        self.assertEqual(res["reasons"], ["paused"])

        # paused wildcard
        state_star = {"paused": {"*": True}, "holds": {}, "journal": []}
        res_star = release.decide([pr], state_star, "c2e", True, self.mon)
        self.assertEqual(res_star["verdict"], "wait")
        self.assertEqual(res_star["ship"], [])
        self.assertEqual(res_star["reasons"], ["paused"])

    def test_hold_waits(self):
        pr = make_pr(1, color="green")
        state = {
            "paused": {},
            "holds": {"c2e": {"since": "2026-11-01T06:00:00Z", "issue": "payments degraded"}},
            "journal": [],
        }
        res = release.decide([pr], state, "c2e", True, self.mon)
        self.assertEqual(res["verdict"], "wait")
        self.assertEqual(res["ship"], [])
        self.assertEqual(res["reasons"], ["on hold: payments degraded"])

    def test_rollback_24h_waits(self):
        pr = make_pr(1, color="green")
        # self.mon is 2026-11-02 07:30:00+01:00 = 2026-11-02 06:30:00 UTC
        # 23h59 ago = 2026-11-01 06:31:00 UTC -> under 24h -> waits
        state_23h59 = {
            "paused": {},
            "holds": {},
            "journal": [{"at": "2026-11-01T06:31:00Z", "product": "c2e", "event": "rolled-back", "prs": [99], "sha": None, "note": "bad"}],
        }
        res_wait = release.decide([pr], state_23h59, "c2e", True, self.mon)
        self.assertEqual(res_wait["verdict"], "wait")
        self.assertEqual(res_wait["ship"], [])
        self.assertEqual(res_wait["reasons"], ["rolled back under 24h ago"])

        # 24h01 ago = 2026-11-01 06:29:00 UTC -> not under 24h -> does not wait
        state_24h01 = {
            "paused": {},
            "holds": {},
            "journal": [{"at": "2026-11-01T06:29:00Z", "product": "c2e", "event": "rolled-back", "prs": [99], "sha": None, "note": "bad"}],
        }
        res_pass = release.decide([pr], state_24h01, "c2e", True, self.mon)
        self.assertEqual(res_pass["verdict"], "ship-all")
        self.assertEqual(res_pass["ship"], [1])

    def test_already_shipped_today_waits(self):
        pr = make_pr(1, color="green")
        # shipped today in Africa/Tunis (e.g. 05:00 UTC on 2026-11-02 = 06:00 Africa/Tunis)
        state_shipped = {
            "paused": {},
            "holds": {},
            "journal": [{"at": "2026-11-02T05:00:00Z", "product": "c2e", "event": "shipped", "prs": [99], "sha": "abc", "note": "ok"}],
        }
        res_shipped = release.decide([pr], state_shipped, "c2e", True, self.mon)
        self.assertEqual(res_shipped["verdict"], "wait")
        self.assertEqual(res_shipped["ship"], [])
        self.assertEqual(res_shipped["reasons"], ["already released today"])

        # started today in Africa/Tunis
        state_started = {
            "paused": {},
            "holds": {},
            "journal": [{"at": "2026-11-02T06:00:00Z", "product": "c2e", "event": "started", "prs": [99], "sha": "abc", "note": "running"}],
        }
        res_started = release.decide([pr], state_started, "c2e", True, self.mon)
        self.assertEqual(res_started["verdict"], "wait")
        self.assertEqual(res_started["ship"], [])
        self.assertEqual(res_started["reasons"], ["already released today"])

        # shipped yesterday does not wait
        state_yesterday = {
            "paused": {},
            "holds": {},
            "journal": [{"at": "2026-11-01T08:00:00Z", "product": "c2e", "event": "shipped", "prs": [99], "sha": "abc", "note": "ok"}],
        }
        res_yesterday = release.decide([pr], state_yesterday, "c2e", True, self.mon)
        self.assertEqual(res_yesterday["verdict"], "ship-all")
        self.assertEqual(res_yesterday["ship"], [1])

    def test_weekend_waits(self):
        pr = make_pr(1, color="green")
        res_sat = release.decide([pr], self.empty_state, "c2e", True, self.sat)
        self.assertEqual(res_sat["verdict"], "wait")
        self.assertEqual(res_sat["ship"], [])
        self.assertEqual(res_sat["reasons"], ["weekend"])

        sun = datetime(2026, 11, 8, 7, 30, tzinfo=self.tz)
        res_sun = release.decide([pr], self.empty_state, "c2e", True, sun)
        self.assertEqual(res_sun["verdict"], "wait")
        self.assertEqual(res_sun["ship"], [])
        self.assertEqual(res_sun["reasons"], ["weekend"])

    def test_ci_red_waits(self):
        pr = make_pr(1, color="green")
        res = release.decide([pr], self.empty_state, "c2e", ci_green=False, now=self.mon)
        self.assertEqual(res["verdict"], "wait")
        self.assertEqual(res["ship"], [])
        self.assertEqual(res["reasons"], ["CI is not green"])

    def test_cap_10_oldest_first(self):
        prs = [
            make_pr(i, f"2026-11-01T{i:02d}:00:00Z", color="green")
            for i in range(1, 13)
        ]
        res = release.decide(prs, self.empty_state, "c2e", True, self.mon)
        self.assertEqual(res["verdict"], "cherry-pick")
        self.assertEqual(res["ship"], list(range(1, 11)))
        self.assertEqual(res["held"], [
            {"number": 11, "reason": "over the 10-PR cap"},
            {"number": 12, "reason": "over the 10-PR cap"},
        ])

    def test_friday_only_green_fixes(self):
        pr1 = make_pr(1, "2026-11-05T01:00:00Z", color="green", labels=["bug"])
        pr2 = make_pr(2, "2026-11-05T02:00:00Z", color="green", labels=["fix"])
        pr3 = make_pr(3, "2026-11-05T03:00:00Z", color="green", labels=["feature"])
        pr4 = make_pr(4, "2026-11-05T04:00:00Z", color="yellow", labels=["bug"])
        pr5 = make_pr(5, "2026-11-05T05:00:00Z", color="red", labels=["fix"], score_reasons=["migration"])
        res = release.decide([pr1, pr2, pr3, pr4, pr5], self.empty_state, "c2e", True, self.fri)
        self.assertEqual(res["verdict"], "cherry-pick")
        self.assertEqual(res["ship"], [1, 2])
        self.assertEqual(res["held"], [
            {"number": 3, "reason": "Friday: only green bug/fix PRs"},
            {"number": 4, "reason": "Friday: only green bug/fix PRs"},
            {"number": 5, "reason": "Friday: only green bug/fix PRs"},
        ])

    def test_friday_all_red_waits(self):
        pr1 = make_pr(1, "2026-11-05T01:00:00Z", color="red", labels=["bug"], score_reasons=["migration"])
        pr2 = make_pr(2, "2026-11-05T02:00:00Z", color="red", labels=["fix"], score_reasons=["infra"])
        pr3 = make_pr(3, "2026-11-05T03:00:00Z", color="green", labels=["feature"])
        res = release.decide([pr1, pr2, pr3], self.empty_state, "c2e", True, self.fri)
        self.assertEqual(res["verdict"], "wait")
        self.assertEqual(res["ship"], [])
        self.assertEqual(res["reasons"], ["Friday: no green bug/fix PR"])
        self.assertEqual(res["held"], [])
        self.assertEqual(res["score"], "red")

    def test_all_green_ships_all(self):
        prs = [
            make_pr(1, "2026-11-01T01:00:00Z", color="green"),
            make_pr(2, "2026-11-01T02:00:00Z", color="green"),
            make_pr(3, "2026-11-01T03:00:00Z", color="green"),
        ]
        res = release.decide(prs, self.empty_state, "c2e", True, self.mon)
        self.assertEqual(res["verdict"], "ship-all")
        self.assertEqual(res["ship"], [1, 2, 3])
        self.assertEqual(res["held"], [])
        self.assertEqual(res["score"], "green")
        self.assertEqual(res["reasons"], ["3 PRs, all green"])

    def test_one_red_ships_solo_on_monday(self):
        pr1 = make_pr(1, "2026-11-01T01:00:00Z", color="green")
        pr2 = make_pr(2, "2026-11-01T02:00:00Z", color="red", score_reasons=["migration"])
        pr3 = make_pr(3, "2026-11-01T03:00:00Z", color="yellow", score_reasons=["over 400 changed lines"])
        res = release.decide([pr1, pr2, pr3], self.empty_state, "c2e", True, self.mon)
        self.assertEqual(res["verdict"], "solo-red")
        self.assertEqual(res["ship"], [2])
        self.assertEqual(res["held"], [
            {"number": 1, "reason": "solo-red day: the red PR ships alone"},
            {"number": 3, "reason": "solo-red day: the red PR ships alone"},
        ])
        self.assertEqual(res["score"], "red")
        self.assertEqual(res["reasons"], ["solo red: #2 (migration)"])

    def test_two_reds_ship_oldest_solo(self):
        pr1 = make_pr(1, "2026-11-01T01:00:00Z", color="green")
        pr2 = make_pr(2, "2026-11-01T02:00:00Z", color="red", score_reasons=["migration"])
        pr3 = make_pr(3, "2026-11-01T03:00:00Z", color="red", score_reasons=["money path"])
        res = release.decide([pr1, pr2, pr3], self.empty_state, "c2e", True, self.mon)
        self.assertEqual(res["verdict"], "solo-red")
        self.assertEqual(res["ship"], [2])
        self.assertEqual(res["held"], [
            {"number": 1, "reason": "solo-red day: the red PR ships alone"},
            {"number": 3, "reason": "second red: only the oldest red ships solo"},
        ])
        self.assertEqual(res["score"], "red")
        self.assertEqual(res["reasons"], ["solo red: #2 (migration)"])

    def test_four_yellows_cherry_pick_three(self):
        pr1 = make_pr(1, "2026-11-01T01:00:00Z", color="yellow", score_reasons=["over 400 changed lines"])
        pr2 = make_pr(2, "2026-11-01T02:00:00Z", color="green")
        pr3 = make_pr(3, "2026-11-01T03:00:00Z", color="yellow", score_reasons=["no test file changed"])
        pr4 = make_pr(4, "2026-11-01T04:00:00Z", color="yellow", score_reasons=["dependency bump"])
        pr5 = make_pr(5, "2026-11-01T05:00:00Z", color="yellow", score_reasons=["new env var FOO"])
        pr6 = make_pr(6, "2026-11-01T06:00:00Z", color="green")
        res = release.decide([pr1, pr2, pr3, pr4, pr5, pr6], self.empty_state, "c2e", True, self.mon)
        self.assertEqual(res["verdict"], "cherry-pick")
        self.assertEqual(res["ship"], [1, 2, 3, 4, 6])
        self.assertEqual(res["held"], [
            {"number": 5, "reason": "4 yellows make the release red; held for tomorrow"},
        ])
        self.assertEqual(res["score"], "yellow")


class ReleasePlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
        self.tz = ZoneInfo("Africa/Tunis")
        self.mon = datetime(2026, 11, 2, 7, 30, tzinfo=self.tz)

    def ok(self, *argv):
        code, obj, err = run(*argv)
        self.assertEqual(code, 0, err)
        return obj

    def test_plan_relays_live_ship_verdict(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        pr = make_pr(101, "2026-11-01T10:00:00Z", color="green", title="Feature A")
        with mock.patch("factory.release.unreleased_prs", return_value=([pr], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            verdicts = release.plan(now=self.mon)
            self.assertEqual(verdicts["c2e"]["verdict"], "ship-all")
            self.assertEqual(verdicts["c2e"]["ship"], [101])
            mock_enqueue.assert_called_once()
            args, kwargs = mock_enqueue.call_args
            thread = args[0] if args else kwargs.get("thread")
            text = args[1] if len(args) > 1 else kwargs.get("text")
            kind = kwargs.get("kind")
            self.assertEqual(thread, "t-c2e-thread")
            self.assertEqual(kind, "release")
            self.assertTrue(text.startswith("Release manager (factory release plan"))
            self.assertIn("c2e", text)
            self.assertIn("#101", text)
            self.assertIn("Feature A", text)
            self.assertIn("Follow the release-manager skill for c2e.", text)

        state = release.release()
        today_str = self.mon.astimezone(self.tz).strftime("%Y-%m-%d")
        self.assertTrue(state["verdicts"][today_str]["c2e"]["relayed"])

    def test_plan_shadow_does_not_relay(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        pr = make_pr(101, "2026-11-01T10:00:00Z", color="green", title="Feature A")
        with mock.patch("factory.release.unreleased_prs", return_value=([pr], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            verdicts = release.plan(now=self.mon)
            self.assertEqual(verdicts["c2e"]["verdict"], "ship-all")
            mock_enqueue.assert_not_called()

        state = release.release()
        today_str = self.mon.astimezone(self.tz).strftime("%Y-%m-%d")
        self.assertFalse(state["verdicts"][today_str]["c2e"]["relayed"])

    def test_plan_one_repo_fails_others_continue(self):
        self.ok(
            "release", "product", "add", "prod1",
            "--repo", "org/prod1",
            "--flow", "release-branch",
            "--project", "p1",
        )
        self.ok(
            "release", "product", "add", "prod2",
            "--repo", "org/prod2",
            "--flow", "release-branch",
            "--project", "p2",
        )
        def mock_unreleased(prod):
            if prod["repo"] == "org/prod1":
                raise FactoryError("GitHub rate limit exceeded")
            return ([make_pr(201, "2026-11-01T10:00:00Z", color="green")], "basesha", [])

        with mock.patch("factory.release.unreleased_prs", side_effect=mock_unreleased), \
             mock.patch("factory.release.ci_green", return_value=True):
            verdicts = release.plan(now=self.mon)

        self.assertEqual(verdicts["prod1"]["verdict"], "wait")
        self.assertEqual(verdicts["prod1"]["ship"], [])
        self.assertEqual(verdicts["prod1"]["held"], [])
        self.assertEqual(verdicts["prod1"]["score"], "green")
        self.assertEqual(verdicts["prod1"]["reasons"], ["could not read: GitHub rate limit exceeded"])

        self.assertEqual(verdicts["prod2"]["verdict"], "ship-all")
        self.assertEqual(verdicts["prod2"]["ship"], [201])

    def test_plan_is_idempotent_per_day(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        pr = make_pr(101, "2026-11-01T10:00:00Z", color="green")
        with mock.patch("factory.release.unreleased_prs", return_value=([pr], "basesha", [])) as mock_unreleased, \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            verdicts1 = release.plan(now=self.mon)
            self.assertEqual(mock_enqueue.call_count, 1)
            self.assertEqual(mock_unreleased.call_count, 1)

            # Second call on the same day must not recompute or resend
            verdicts2 = release.plan(now=self.mon + timedelta(hours=3))
            self.assertEqual(mock_enqueue.call_count, 1)
            self.assertEqual(mock_unreleased.call_count, 1)
            self.assertEqual(verdicts1["c2e"]["verdict"], verdicts2["c2e"]["verdict"])
            self.assertEqual(verdicts1["c2e"]["at"], verdicts2["c2e"]["at"])

        state = release.release()
        today_str = self.mon.astimezone(self.tz).strftime("%Y-%m-%d")
        self.assertEqual(state["verdicts"][today_str]["c2e"]["at"], verdicts1["c2e"]["at"])

    def test_queue_frozen_after_queue_relay(self):
        # When queue_relayed is set, the verdict is frozen for the day
        self.ok("release", "product", "add", "c2e", "--repo", "org/c2e", "--flow", "pr",
                "--project", "c2e-proj", "--thread", "t-c2e")
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        pr = make_pr(101, "2026-11-01T10:00:00Z", color="green")
        today = {"merge": [], "agents": [{"repo_pr": "org/c2e#5", "number": 5, "title": "x", "job": "fix"}],
                 "pending_actions": [], "asks": [], "blocked": False, "mode": "live", "snapshot_at": None}
        with mock.patch("factory.release.unreleased_prs", return_value=([], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.queue.queue", return_value=({}, "")), \
             mock.patch("factory.queue.today", return_value=today), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            first = release.plan(now=self.mon)
            self.assertEqual(first["c2e"]["verdict"], "wait")
            self.assertEqual(mock_enqueue.call_count, 1)  # the queue message
            self.assertTrue(first["c2e"].get("queue_relayed"))

        # Second call with new PRs: verdict is frozen because queue_relayed is set
        with mock.patch("factory.release.unreleased_prs", return_value=([pr], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.queue.queue", return_value=({}, "")), \
             mock.patch("factory.queue.today", return_value=today), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue2:
            second = release.plan(now=self.mon + timedelta(hours=3))
            self.assertEqual(second["c2e"]["verdict"], "wait")
            self.assertEqual(second["c2e"]["at"], first["c2e"]["at"])
            mock_enqueue2.assert_not_called()

    def test_plan_passes_dev_by_product_to_queue(self):
        self.ok("release", "product", "add", "c2e", "--repo", "org/c2e", "--flow", "pr",
                "--project", "c2e-proj", "--thread", "t-c2e")
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        pr = make_pr(101, "2026-11-01T10:00:00Z", color="green")
        with mock.patch("factory.release.unreleased_prs", return_value=([pr], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.queue.queue", return_value=({}, "")) as mock_queue, \
             mock.patch("factory.relay.enqueue_message"):
            release.plan(now=self.mon)
            _, kw = mock_queue.call_args
            self.assertEqual([p["number"] for p in kw["dev_by_product"]["c2e"]], [101])

    def test_factory_no_relay(self):
        self.ok("release", "product", "add", "fac", "--repo", "org/fac", "--flow", "factory",
                "--project", "fac-proj", "--thread", "t-fac")
        self.ok("release", "product", "set", "fac", "--mode", "live")
        res = release._queue_today_if_any("fac", now=self.mon)
        self.assertIsNone(res)

    def test_friday_no_relay(self):
        self.ok("release", "product", "add", "c2e", "--repo", "org/c2e", "--flow", "pr",
                "--project", "c2e-proj", "--thread", "t-c2e")
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        fri = self.mon + timedelta(days=4)  # Friday
        res = release._queue_today_if_any("c2e", now=fri)
        self.assertIsNone(res)

    def test_asks_only_day_relays(self):
        self.ok("release", "product", "add", "c2e", "--repo", "org/c2e", "--flow", "pr",
                "--project", "c2e-proj", "--thread", "t-c2e")
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        today = {"merge": [], "agents": [], "pending_actions": [],
                 "asks": [{"number": 50, "title": "stale PR", "classification": {"group": "you", "why": "broken"}}],
                 "asks_new": [{"number": 50, "title": "stale PR", "classification": {"group": "you", "why": "broken"}}],
                 "blocked": False, "mode": "live", "snapshot_at": None}
        with mock.patch("factory.config.get", side_effect=lambda k, default=None: "https://factory.example.com" if k == "site.url" else default), \
             mock.patch("factory.release.unreleased_prs", return_value=([], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.queue.queue", return_value=({}, "")), \
             mock.patch("factory.queue.today", return_value=today), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            verdicts = release.plan(now=self.mon)
            self.assertEqual(verdicts["c2e"]["verdict"], "wait")
            mock_enqueue.assert_called_once()
            args, kwargs = mock_enqueue.call_args
            text = args[1] if len(args) > 1 else kwargs.get("text")
            self.assertIn("Waiting on you:", text)
            self.assertIn("#50 stale PR", text)
            self.assertIn("https://factory.example.com/#/automation", text)

        state = release.release()
        self.assertIn("org/c2e#50", state.get("queue_asked", {}).get("c2e", []))

    def test_asks_dedupe_across_days(self):
        self.ok("release", "product", "add", "c2e", "--repo", "org/c2e", "--flow", "pr",
                "--project", "c2e-proj", "--thread", "t-c2e")
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        data = release.release()
        data["queue_asked"] = {"c2e": ["org/c2e#50"]}
        data["queue"] = {
            "at": "2026-11-03T07:00:00Z",
            "products": {
                "c2e": {
                    "prs": [
                        {"number": 50, "title": "stale PR", "classification": {"group": "you"}}
                    ],
                    "train": {"days": [[], [], [], []]},
                }
            }
        }
        release.save_release(data)
        tue = self.mon + timedelta(days=1)
        res = queue.today("c2e", now=tue)
        self.assertEqual(len(res.get("asks", [])), 1)
        self.assertEqual(res.get("asks_new"), [])

        q_today = release._queue_today_if_any("c2e", now=tue)
        self.assertIsNone(q_today)

    def test_plan_relays_one_queue_message_on_non_ship_day(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        data = release.release()
        data["queue"] = {
            "at": "2026-11-02T07:00:00Z",
            "products": {
                "c2e": {
                    "prs": [
                        {
                            "number": 11,
                            "title": "broken build",
                            "classification": {"group": "fix", "does": "fix agent"},
                            "agent_today": True,
                        },
                        {
                            "number": 13,
                            "title": "stale pr",
                            "classification": {"group": "you", "why": "broken 60 days"},
                        },
                    ],
                    "train": {
                        "days": [
                            [{"number": 10, "title": "fix monday", "kind": "fix"}],
                            [], [], [],
                        ],
                        "blocked": False,
                    },
                }
            },
        }
        data["queue_actions"] = {
            "org/c2e#12": {"retarget": "2026-11-01T10:00:00Z"},
        }
        release.save_release(data)

        # Non-ship verdict (e.g. no unreleased PRs -> wait)
        with mock.patch("factory.release.unreleased_prs", return_value=([], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.queue.queue", return_value=({}, "")), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            verdicts = release.plan(now=self.mon)
            self.assertEqual(verdicts["c2e"]["verdict"], "wait")
            mock_enqueue.assert_called_once()
            args, kwargs = mock_enqueue.call_args
            thread = args[0] if args else kwargs.get("thread")
            text = args[1] if len(args) > 1 else kwargs.get("text")
            self.assertEqual(thread, "t-c2e-thread")
            self.assertTrue(text.startswith("Release manager (factory release queue"))
            self.assertIn("Merges today:", text)
            self.assertIn("org/c2e#10 fix monday [fix]", text)
            self.assertIn("factory release queue-recheck", text)
            self.assertIn("queue-done", text)
            self.assertIn("Agent jobs:", text)
            self.assertIn("org/c2e#11 (fix): broken build", text)
            self.assertIn("Pending actions:", text)
            self.assertIn("org/c2e#12: retarget", text)
            self.assertIn("Waiting on you:", text)
            self.assertIn("#13 stale pr (broken 60 days)", text)

    def test_plan_appends_queue_section_on_ship_day(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        data = release.release()
        data["queue"] = {
            "at": "2026-11-02T07:00:00Z",
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [
                            [{"number": 20, "title": "queued fix", "kind": "fix"}],
                            [], [], [],
                        ],
                        "blocked": False,
                    },
                }
            },
        }
        release.save_release(data)

        pr = make_pr(101, "2026-11-01T10:00:00Z", color="green", title="Feature A")
        with mock.patch("factory.release.unreleased_prs", return_value=([pr], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.queue.queue", return_value=({}, "")), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            verdicts = release.plan(now=self.mon)
            self.assertEqual(verdicts["c2e"]["verdict"], "ship-all")
            mock_enqueue.assert_called_once()
            args, kwargs = mock_enqueue.call_args
            text = args[1] if len(args) > 1 else kwargs.get("text")
            self.assertTrue(text.startswith("Release manager (factory release plan"))
            self.assertIn("Merge these only after the watch window ends.", text)
            self.assertIn("Merges today:", text)
            self.assertIn("org/c2e#20 queued fix [fix]", text)
            self.assertIn("factory release queue-recheck", text)
            self.assertIn("queue-done", text)

    def test_plan_shadow_relays_no_queue_message(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        # c2e defaults to shadow mode
        data = release.release()
        data["queue"] = {
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [[{"number": 20, "title": "fix", "kind": "fix"}]],
                    },
                }
            }
        }
        release.save_release(data)

        with mock.patch("factory.release.unreleased_prs", return_value=([], "basesha", [])), \
             mock.patch("factory.queue.queue", return_value=({}, "")), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            verdicts = release.plan(now=self.mon)
            self.assertEqual(verdicts["c2e"]["verdict"], "wait")
            mock_enqueue.assert_not_called()

    def test_plan_queue_error_does_not_change_verdict(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        pr = make_pr(101, "2026-11-01T10:00:00Z", color="green", title="Feature A")
        with mock.patch("factory.release.unreleased_prs", return_value=([pr], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.queue.queue", side_effect=Exception("GitHub API down")), \
             mock.patch("factory.relay.enqueue_message"):
            verdicts = release.plan(now=self.mon)
            self.assertEqual(verdicts["c2e"]["verdict"], "ship-all")
            self.assertEqual(verdicts["c2e"]["ship"], [101])
            self.assertEqual(verdicts["c2e"]["queue_error"], "GitHub API down")

    def test_plan_queue_message_dedupe(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        data = release.release()
        data["queue"] = {
            "at": "2026-11-02T07:00:00Z",
            "products": {
                "c2e": {
                    "prs": [],
                    "train": {
                        "days": [[{"number": 10, "title": "fix mon", "kind": "fix"}]],
                    },
                }
            }
        }
        release.save_release(data)

        with mock.patch("factory.release.unreleased_prs", return_value=([], "basesha", [])), \
             mock.patch("factory.queue.queue", return_value=({}, "")), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            verdicts1 = release.plan(now=self.mon)
            self.assertEqual(mock_enqueue.call_count, 1)

            # Second plan call on same day must not re-relay queue message
            verdicts2 = release.plan(now=self.mon + timedelta(hours=2))
            self.assertEqual(mock_enqueue.call_count, 1)

    def test_factory_waits_while_others_in_flight(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
        )
        self.ok(
            "release", "product", "add", "factory",
            "--repo", "org/factory",
            "--flow", "factory",
            "--project", "factory-proj",
        )
        # Without only, factory is skipped
        with mock.patch("factory.release.unreleased_prs", return_value=([], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True):
            all_verdicts = release.plan(now=self.mon)
            self.assertIn("c2e", all_verdicts)
            self.assertNotIn("factory", all_verdicts)

        # Record started for c2e today
        release.record("c2e", "started", prs=[101], now=self.mon)

        # Plan for factory while c2e is in flight
        factory_verdict = release.plan(now=self.mon, only="factory")
        self.assertEqual(factory_verdict["factory"]["verdict"], "wait")
        self.assertEqual(factory_verdict["factory"]["reasons"], ["other releases still in flight"])

        # Now c2e finishes with shipped
        release.record("c2e", "shipped", prs=[101], sha="abc", now=self.mon + timedelta(minutes=20))
        pr_factory = make_pr(501, "2026-11-01T10:00:00Z", color="green")
        with mock.patch("factory.release.unreleased_prs", return_value=([pr_factory], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True):
            factory_verdict2 = release.plan(now=self.mon + timedelta(minutes=30), only="factory")
            self.assertEqual(factory_verdict2["factory"]["verdict"], "ship-all")
            self.assertEqual(factory_verdict2["factory"]["ship"], [501])

    def test_stale_pr_asks_once(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        self.ok(
            "release", "product", "add", "shadow_prod",
            "--repo", "org/shadow_prod",
            "--flow", "release-branch",
            "--project", "shadow-proj",
            "--thread", "t-shadow-thread",
        )
        # Pause c2e and shadow_prod so verdict is wait (paused)
        release.pause("c2e")
        release.pause("shadow_prod")
        # Mon Nov 2, merged 4 working days ago = Tuesday Oct 27
        # Tue Oct 27 -> Mon Nov 2: Wed 28 (1), Thu 29 (2), Fri 30 (3), Mon Nov 2 (4) -> 4 working days
        pr_stale = make_pr(42, "2026-10-27T10:00:00Z", color="green", title="Old PR")
        with mock.patch("factory.release.unreleased_prs", return_value=([pr_stale], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.store.add_ask_item") as mock_ask:
            release.plan(now=self.mon)
            # Live product c2e asks once; shadow product raises no ask
            mock_ask.assert_called_once()
            _, kwargs = mock_ask.call_args
            self.assertEqual(kwargs.get("project"), "c2e-proj")
            self.assertEqual(kwargs.get("kind"), "applied")
            self.assertEqual(kwargs.get("title"), "Ship or hold PR #42 on c2e: waiting 4 days")
            self.assertEqual(kwargs.get("short"), "Applied by default")
            self.assertEqual(kwargs.get("context"), "paused")
            self.assertEqual(kwargs.get("reco"), "wait")
            self.assertEqual(kwargs.get("why"), "Release manager keeps waiting by default")
            self.assertEqual(kwargs.get("sender"), "release")
            self.assertEqual(kwargs.get("thread"), "t-c2e-thread")

            state = release.release()
            today_str = self.mon.astimezone(self.tz).strftime("%Y-%m-%d")
            self.assertEqual(state["stale_asked"]["c2e#42"], today_str)
            self.assertNotIn("shadow_prod#42", state.get("stale_asked", {}))

            # Running again does not ask again (deduped)
            release.plan(now=self.mon)
            self.assertEqual(mock_ask.call_count, 1)

    def test_factory_stall_asks_urgent_once(self):
        self.ok(
            "release", "product", "add", "factory",
            "--repo", "acme/factory",
            "--flow", "factory",
            "--project", "fac-release-mgr",
            "--thread", "t-fac",
        )
        self.ok("release", "product", "set", "factory", "--mode", "live")
        prs = [make_pr(1, "2026-11-02T06:00:00Z", files=["a.py", "tests/test_a.py"]),
                make_pr(2, "2026-11-02T06:30:00Z", files=[".github/workflows/ci.yml"])]
        with mock.patch("factory.release.unreleased_prs", return_value=(prs, "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue, \
             mock.patch("factory.store.add_ask_item") as mock_ask:
            out = release.plan(now=self.mon, only="factory")
            self.assertEqual(out["factory"]["verdict"], "wait")
            mock_ask.assert_called_once()
            _, kwargs = mock_ask.call_args
            self.assertEqual(kwargs.get("kind"), "urgent")
            self.assertEqual(kwargs.get("title"), "Release factory by hand: red PR #2 cannot ship alone")
            mock_enqueue.assert_not_called()
            release.plan(now=self.mon, only="factory")
            self.assertEqual(mock_ask.call_count, 1)

    def test_gate_hold_asks_urgent_once_and_does_not_relay(self):
        self.ok("release", "product", "add", "c2e", "--repo", "o/c2e", "--flow", "pr",
                "--project", "c2e-proj", "--thread", "t-c2e")
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        prs = [make_pr(1, "2026-11-08T06:00:00Z", files=["a.py", "tests/test_a.py"])]
        bad = {"ok": False, "checks": [{"name": "home", "ok": False, "detail": "status 502"}]}
        with mock.patch("factory.release.unreleased_prs", return_value=(prs, "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.release.check", return_value=bad), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue, \
             mock.patch("factory.store.add_ask_item") as mock_ask:
            out = release.plan(now=self.mon, only="c2e")
            self.assertEqual(out["c2e"]["verdict"], "wait")
            self.assertEqual(out["c2e"]["gate"], ["home"])
            mock_ask.assert_called_once()
            _, kwargs = mock_ask.call_args
            self.assertEqual(kwargs.get("kind"), "urgent")
            self.assertEqual(kwargs.get("title"), "Check c2e production: home failing, release held")
            mock_enqueue.assert_not_called()
            release.plan(now=self.mon, only="c2e")
            self.assertEqual(mock_ask.call_count, 1)

    def test_stale_pr_concurrent_run_does_not_double_ask(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        release.pause("c2e")
        pr_stale = make_pr(42, "2026-10-27T10:00:00Z", color="green", title="Old PR")

        orig_lock = store.lock

        @contextlib.contextmanager
        def mock_lock_preempt():
            with orig_lock():
                st = release.release()
                st.setdefault("stale_asked", {})["c2e#42"] = "2026-11-02"
                release.save_release(st)
                yield

        with mock.patch("factory.release.unreleased_prs", return_value=([pr_stale], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.store.lock", side_effect=mock_lock_preempt), \
             mock.patch("factory.store.add_ask_item") as mock_ask:
            release.plan(now=self.mon)
            mock_ask.assert_not_called()

    def test_enqueue_failure_does_not_double_send(self):
        self.ok(
            "release", "product", "add", "prodA",
            "--repo", "org/prodA",
            "--flow", "release-branch",
            "--project", "pA",
            "--thread", "t-a",
        )
        self.ok("release", "product", "set", "prodA", "--mode", "live")
        self.ok(
            "release", "product", "add", "prodB",
            "--repo", "org/prodB",
            "--flow", "release-branch",
            "--project", "pB",
            "--thread", "t-b",
        )
        self.ok("release", "product", "set", "prodB", "--mode", "live")

        prA = make_pr(101, "2026-11-01T10:00:00Z", color="green")
        prB = make_pr(201, "2026-11-01T10:00:00Z", color="green")

        def mock_unreleased(prod):
            if prod["repo"] == "org/prodA":
                return ([prA], "basesha", [])
            return ([prB], "basesha", [])

        enqueued_threads = []
        b_calls = 0

        def mock_enqueue(thread, msg, kind="message"):
            nonlocal b_calls
            if thread == "t-b":
                b_calls += 1
                if b_calls == 1:
                    raise RuntimeError("Slack API timeout")
            enqueued_threads.append(thread)

        with mock.patch("factory.release.unreleased_prs", side_effect=mock_unreleased), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.relay.enqueue_message", side_effect=mock_enqueue):
            # First run: A succeeds, B raises
            v1 = release.plan(now=self.mon)
            self.assertEqual(enqueued_threads, ["t-a"])
            self.assertTrue(v1["prodA"]["relayed"])
            self.assertFalse(v1["prodB"]["relayed"])
            self.assertEqual(v1["prodB"].get("relayError"), "Slack API timeout")

            state1 = release.release()
            today_str = self.mon.astimezone(self.tz).strftime("%Y-%m-%d")
            self.assertTrue(state1["verdicts"][today_str]["prodA"]["relayed"])
            self.assertFalse(state1["verdicts"][today_str]["prodB"]["relayed"])
            self.assertEqual(state1["verdicts"][today_str]["prodB"]["relayError"], "Slack API timeout")

            # Second run: A is already relayed so skipped, B retries and succeeds
            v2 = release.plan(now=self.mon)
            self.assertEqual(enqueued_threads, ["t-a", "t-b"])
            self.assertTrue(v2["prodA"]["relayed"])
            self.assertTrue(v2["prodB"]["relayed"])

            state2 = release.release()
            self.assertTrue(state2["verdicts"][today_str]["prodA"]["relayed"])
            self.assertTrue(state2["verdicts"][today_str]["prodB"]["relayed"])

            # Assert A was enqueued exactly once and B exactly once after the retry
            self.assertEqual(enqueued_threads.count("t-a"), 1)
            self.assertEqual(enqueued_threads.count("t-b"), 1)

    def test_plan_value_error_one_repo_others_continue(self):
        self.ok(
            "release", "product", "add", "prod1",
            "--repo", "org/prod1",
            "--flow", "release-branch",
            "--project", "p1",
        )
        self.ok(
            "release", "product", "add", "prod2",
            "--repo", "org/prod2",
            "--flow", "release-branch",
            "--project", "p2",
        )
        pr2 = make_pr(201, "2026-11-01T10:00:00Z", color="green")

        def mock_gh_json(cmd, endpoint, *args, **kwargs):
            if "org/prod1" in endpoint:
                raise ValueError("JSON decode error: line 1 column 1")
            return []

        def mock_unreleased(prod):
            if prod["repo"] == "org/prod1":
                return sources.gh_json("api", f"repos/{prod['repo']}/pulls")
            return ([pr2], "basesha", [])

        with mock.patch("factory.sources.gh_json", side_effect=mock_gh_json), \
             mock.patch("factory.release.unreleased_prs", side_effect=mock_unreleased), \
             mock.patch("factory.release.ci_green", return_value=True):
            verdicts = release.plan(now=self.mon)

        self.assertEqual(verdicts["prod1"]["verdict"], "wait")
        self.assertEqual(verdicts["prod1"]["ship"], [])
        self.assertEqual(verdicts["prod1"]["reasons"], ["could not read: JSON decode error: line 1 column 1"])
        self.assertEqual(verdicts["prod2"]["verdict"], "ship-all")
        self.assertEqual(verdicts["prod2"]["ship"], [201])

    def test_ci_green_reads_100_check_runs(self):
        prod = {"repo": "org/repo", "base": "main"}
        with mock.patch("factory.sources.gh_json") as mock_gh:
            mock_gh.side_effect = [
                {"state": "pending", "total_count": 0},
                {"check_runs": [{"conclusion": "success"}]},
            ]
            res = release.ci_green(prod)
            self.assertTrue(res)
            self.assertEqual(mock_gh.call_count, 2)
            check_runs_url = mock_gh.call_args_list[1][0][1]
            self.assertEqual(check_runs_url, "repos/org/repo/commits/main/check-runs?per_page=100&page=1")

    def test_ci_green_status_success_check_run_failure(self):
        prod = {"repo": "org/repo", "base": "main"}
        with mock.patch("factory.sources.gh_json") as mock_gh:
            mock_gh.side_effect = [
                {"state": "success", "total_count": 1},
                {"check_runs": [{"conclusion": "failure"}]},
            ]
            res = release.ci_green(prod)
            self.assertFalse(res)

    def test_ci_green_no_statuses_no_check_runs(self):
        prod = {"repo": "org/repo", "base": "main"}
        with mock.patch("factory.sources.gh_json") as mock_gh:
            mock_gh.side_effect = [
                {"state": "pending", "total_count": 0},
                {"check_runs": []},
            ]
            res = release.ci_green(prod)
            self.assertTrue(res)

    def test_ci_green_judges_base_sha_paginates_and_accepts_neutral(self):
        prod = {"repo": "org/repo", "base": "develop"}
        calls = []
        def fake_gh(cmd, path):
            calls.append(path)
            if path == "repos/org/repo/commits/mysha123/status":
                return {"state": "success"}
            if path == "repos/org/repo/commits/mysha123/check-runs?per_page=100&page=1":
                return {"check_runs": [{"conclusion": "success"}] * 50 + [{"conclusion": "neutral"}] * 50}
            if path == "repos/org/repo/commits/mysha123/check-runs?per_page=100&page=2":
                return {"check_runs": [{"conclusion": "skipped"}] * 20}
            raise AssertionError(f"unexpected path: {path}")
        with mock.patch("factory.sources.gh_json", side_effect=fake_gh):
            self.assertTrue(release.ci_green(prod, "mysha123"))
        self.assertEqual(calls, [
            "repos/org/repo/commits/mysha123/status",
            "repos/org/repo/commits/mysha123/check-runs?per_page=100&page=1",
            "repos/org/repo/commits/mysha123/check-runs?per_page=100&page=2",
        ])

    def test_cli_release_plan_dry_run_and_json(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        pr = make_pr(101, "2026-11-01T10:00:00Z", color="green")
        tuesday = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)  # the verdict depends on the weekday
        with mock.patch("factory.release.unreleased_prs", return_value=([pr], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.release._now_utc", return_value=tuesday), \
             mock.patch("factory.relay.enqueue_message") as mock_enqueue:
            # dry run JSON
            code, out, err = run("release", "plan", "--dry-run")
            self.assertEqual(code, 0, err)
            self.assertIn("c2e", out)
            self.assertEqual(out["c2e"]["verdict"], "ship-all")
            mock_enqueue.assert_not_called()

            # dry run text
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = cli.main(["release", "plan", "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("c2e: ship-all (1 PR, all green)", stdout.getvalue())

        # release.json should not have verdicts recorded from dry-run
        state = release.release()
        self.assertEqual(state.get("verdicts"), {})

    def test_shipped_prs_are_not_listed_again(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
        )
        # Previous shipped event for c2e including PR 101
        prev_date = self.mon - timedelta(days=5)
        release.record("c2e", "shipped", prs=[101], sha="s1", now=prev_date)

        pr101 = make_pr(101, "2026-10-25T10:00:00Z", color="green", title="Feature Old")
        pr102 = make_pr(102, "2026-11-01T10:00:00Z", color="green", title="Feature New")

        with mock.patch("factory.release.unreleased_prs", return_value=([pr101, pr102], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True):
            verdicts = release.plan(now=self.mon)

        c2e_verdict = verdicts["c2e"]
        self.assertEqual(c2e_verdict["verdict"], "ship-all")
        self.assertEqual(c2e_verdict["ship"], [102])
        self.assertNotIn(101, c2e_verdict["ship"])
        self.assertEqual(c2e_verdict["held"], [])

    def test_shipped_prs_excluded_after_600_journal_events(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
        )
        prev_date = self.mon - timedelta(days=5)
        release.record("c2e", "shipped", prs=[101], sha="s1", now=prev_date)

        # Record 600 journal events to exceed journal size (500)
        for i in range(600):
            release.record("c2e", "held", note=f"held {i}")

        pr101 = make_pr(101, "2026-10-25T10:00:00Z", color="green", title="Feature Old")
        pr102 = make_pr(102, "2026-11-01T10:00:00Z", color="green", title="Feature New")

        with mock.patch("factory.release.unreleased_prs", return_value=([pr101, pr102], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True):
            verdicts = release.plan(now=self.mon)

        c2e_verdict = verdicts["c2e"]
        self.assertEqual(c2e_verdict["verdict"], "ship-all")
        self.assertEqual(c2e_verdict["ship"], [102])
        self.assertNotIn(101, c2e_verdict["ship"])
        self.assertEqual(c2e_verdict["held"], [])

    def test_base_sha_exposed_and_stored_in_verdict(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
        )
        with mock.patch("factory.sources.gh_json") as mock_gh:
            def fake_gh(cmd, path):
                if path == "repos/org/c2e/commits/develop":
                    return {"sha": "c2_head_sha"}
                if path == "repos/org/c2e/compare/main...c2_head_sha":
                    return {
                        "commits": [
                            {"sha": "c1", "parents": [{}, {}]},
                            {"sha": "c2_head_sha", "parents": [{}]},
                        ]
                    }
                if "commits/c1/pulls" in path:
                    return []
                if "commits/c2_head_sha/pulls" in path:
                    return [{"number": 101, "merged_at": "2026-11-01T10:00:00Z", "base": {"ref": "develop"}}]
                if path == "repos/org/c2e/pulls/101":
                    return {"number": 101, "title": "Feat", "merged_at": "2026-11-01T10:00:00Z", "changed_files": 0}
                if "pulls/101/files" in path:
                    return []
                if "status" in path:
                    return {"total_count": 0}
                if "check-runs" in path:
                    return {"check_runs": []}
                return {}
            mock_gh.side_effect = fake_gh

            prs, base_sha, unmapped = release.unreleased_prs({"repo": "org/c2e", "base": "develop", "head": "main"})
            self.assertEqual(base_sha, "c2_head_sha")

            verdicts = release.plan(now=self.mon)
            self.assertEqual(verdicts["c2e"]["baseSha"], "c2_head_sha")

            msg = release.plan_message("c2e", verdicts["c2e"], prs)
            self.assertIn("Cut from: c2_head_sha", msg)

    def test_plan_relay_claims_stored_verdict(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
            "--thread", "t-c2e-thread",
        )
        self.ok("release", "product", "set", "c2e", "--mode", "live")
        pr101 = make_pr(101, "2026-11-01T10:00:00Z", color="green", title="Feature A")
        pr102 = make_pr(102, "2026-11-01T10:00:00Z", color="red", title="Feature B")

        today_str = self.mon.astimezone(self.tz).strftime("%Y-%m-%d")
        enqueued_msgs = []

        orig_lock = store.lock
        b_overwrote = False

        @contextlib.contextmanager
        def mock_lock_overwrite():
            nonlocal b_overwrote
            with orig_lock():
                st = release.release()
                if not b_overwrote and st.get("verdicts", {}).get(today_str, {}).get("c2e"):
                    # B overwrites A's verdict before A claims relay
                    st["verdicts"][today_str]["c2e"] = {
                        "verdict": "solo-red",
                        "ship": [102],
                        "held": [{"number": 101, "reason": "solo-red day"}],
                        "reasons": ["solo red: #102"],
                        "score": "red",
                        "at": "2026-11-02T07:30:00Z",
                        "relayed": False,
                    }
                    release.save_release(st)
                    b_overwrote = True
                yield

        def mock_enqueue(thread, msg, kind="release"):
            enqueued_msgs.append(msg)

        # A computed a green verdict for pr101
        with mock.patch("factory.release.unreleased_prs", return_value=([pr101], "basesha", [])), \
             mock.patch("factory.release.ci_green", return_value=True), \
             mock.patch("factory.store.lock", side_effect=mock_lock_overwrite), \
             mock.patch("factory.relay.enqueue_message", side_effect=mock_enqueue):
            release.plan(now=self.mon)

        self.assertEqual(len(enqueued_msgs), 1)
        self.assertIn("Verdict: solo-red", enqueued_msgs[0])
        self.assertIn("#102", enqueued_msgs[0])

    def test_has_release_in_flight_relayed_verdict(self):
        self.ok(
            "release", "product", "add", "c2e",
            "--repo", "org/c2e",
            "--flow", "release-branch",
            "--project", "c2e-proj",
        )
        self.ok(
            "release", "product", "add", "factory",
            "--repo", "acme/factory",
            "--flow", "factory",
            "--project", "fac-release-mgr",
        )
        today_date = self.mon.astimezone(self.tz).date()
        today_str = self.mon.astimezone(self.tz).strftime("%Y-%m-%d")

        verdicts = {
            today_str: {
                "c2e": {
                    "verdict": "ship-all",
                    "ship": [101],
                    "held": [],
                    "relayed": True,
                }
            }
        }
        # In-flight with relayed verdict and no terminal event
        self.assertTrue(release._has_release_in_flight([], today_date, verdicts=verdicts))

        # When c2e has a terminal shipped event today, it is no longer in flight
        shipped_entry = {
            "at": self.mon.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "product": "c2e",
            "event": "shipped",
            "prs": [101],
        }
        self.assertFalse(release._has_release_in_flight([shipped_entry], today_date, verdicts=verdicts))


class CheckHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health-ok":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        elif self.path == "/health-500":
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Internal Server Error")
        elif self.path == "/health-302":
            self.send_response(302)
            self.send_header("Location", "https://auth.example.com/login")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


class ReleaseCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)

        self.server = http.server.HTTPServer(("127.0.0.1", 0), CheckHandler)
        self.server_port = self.server.server_port
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server_thread.join)
        self.addCleanup(self.server.shutdown)

        code, _, err = run(
            "release", "product", "add", "c2e",
            "--repo", "acme/app",
            "--flow", "release-branch",
            "--project", "c2e",
        )
        self.assertEqual(code, 0, err)

    def ok(self, *argv):
        code, obj, err = run(*argv)
        self.assertEqual(code, 0, err)
        return obj

    def test_http_200_ok(self):
        url = f"http://127.0.0.1:{self.server_port}/health-ok"
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "ping",
            "--kind", "http",
            "--url", url,
        )
        self.assertEqual(code, 0, err)

        res = release.check("c2e")
        self.assertTrue(res["ok"])
        self.assertEqual(len(res["checks"]), 1)
        c = res["checks"][0]
        self.assertEqual(c["name"], "ping")
        self.assertEqual(c["kind"], "http")
        self.assertTrue(c["ok"])
        self.assertEqual(c["value"], 200)
        self.assertIsNone(c["baseline"])
        self.assertEqual(c["detail"], "")

        code, obj, err = run("release", "check", "c2e")
        self.assertEqual(code, 0, err)
        self.assertTrue(obj["ok"])

    def test_http_500_fails_exit_3(self):
        url = f"http://127.0.0.1:{self.server_port}/health-500"
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "ping",
            "--kind", "http",
            "--url", url,
        )
        self.assertEqual(code, 0, err)

        res = release.check("c2e")
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["checks"]), 1)
        c = res["checks"][0]
        self.assertEqual(c["name"], "ping")
        self.assertEqual(c["kind"], "http")
        self.assertFalse(c["ok"])
        self.assertEqual(c["value"], 500)
        self.assertIsNone(c["baseline"])
        self.assertIn("500", c["detail"])

        code, _, _ = run("release", "check", "c2e")
        self.assertEqual(code, 3)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli_code = cli.main(["release", "check", "c2e"])
        self.assertEqual(cli_code, 3)
        self.assertIn("FAIL ping (http): value=500 baseline=None", stdout.getvalue())

    def test_errors_over_2x_fails(self):
        cmd = f'{sys.executable} -c "print(30)"'
        base_cmd = f'{sys.executable} -c "print(10)"'
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "errs",
            "--kind", "errors",
            "--cmd", cmd,
            "--baseline-cmd", base_cmd,
        )
        self.assertEqual(code, 0, err)

        res = release.check("c2e")
        self.assertFalse(res["ok"])
        c = res["checks"][0]
        self.assertFalse(c["ok"])
        self.assertEqual(c["value"], 30)
        self.assertEqual(c["baseline"], 10)
        self.assertIn("30 > 29.5 (baseline 10)", c["detail"])
        self.assertTrue(release._judge_errors(29, 10)[0])  # 2x + 3 sqrt(10) = 29.5

        code, _, _ = run("release", "check", "c2e")
        self.assertEqual(code, 3)

    def test_business_drop_ignored_under_10_baseline(self):
        cmd = f'{sys.executable} -c "print(3)"'
        base_cmd = f'{sys.executable} -c "print(8)"'
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "orders",
            "--kind", "business",
            "--cmd", cmd,
            "--baseline-cmd", base_cmd,
        )
        self.assertEqual(code, 0, err)

        res = release.check("c2e")
        self.assertTrue(res["ok"])
        c = res["checks"][0]
        self.assertTrue(c["ok"])
        self.assertEqual(c["value"], 3)
        self.assertEqual(c["baseline"], 8)
        self.assertEqual(c["detail"], "")

        code, obj, err = run("release", "check", "c2e")
        self.assertEqual(code, 0, err)
        self.assertTrue(obj["ok"])

    def test_business_drop_fails(self):
        cmd = f'{sys.executable} -c "print(9)"'
        base_cmd = f'{sys.executable} -c "print(20)"'
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "orders",
            "--kind", "business",
            "--cmd", cmd,
            "--baseline-cmd", base_cmd,
        )
        self.assertEqual(code, 0, err)

        res = release.check("c2e")
        self.assertFalse(res["ok"])
        c = res["checks"][0]
        self.assertFalse(c["ok"])
        self.assertEqual(c["value"], 9)
        self.assertEqual(c["baseline"], 20)
        self.assertIn("0.5 * baseline", c["detail"])

        code, _, _ = run("release", "check", "c2e")
        self.assertEqual(code, 3)

    def test_check_add_and_bad_cmd_fails_loud(self):
        # Duplicate check name rejected
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "dup",
            "--kind", "http",
            "--url", "http://example.com",
        )
        self.assertEqual(code, 0, err)
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "dup",
            "--kind", "http",
            "--url", "http://example.com/2",
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        # http check rejects --cmd
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "bad-http",
            "--kind", "http",
            "--url", "http://example.com",
            "--cmd", "echo 1",
        )
        self.assertEqual(code, 1)

        # errors check requires both --cmd and --baseline-cmd
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "incomplete-errors",
            "--kind", "errors",
            "--cmd", "echo 1",
        )
        self.assertEqual(code, 1)

        # unknown product rejected
        code, _, err = run(
            "release", "check-add", "nonexistent",
            "--name", "chk",
            "--kind", "http",
            "--url", "http://example.com",
        )
        self.assertEqual(code, 1)
        self.assertIn("unknown product", err)

        # http check rejects invalid url schemes
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "bad-scheme",
            "--kind", "http",
            "--url", "ftp://example.com",
        )
        self.assertEqual(code, 1)
        self.assertIn("http:// or https://", err)

        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "no-scheme",
            "--kind", "http",
            "--url", "example.com",
        )
        self.assertEqual(code, 1)
        self.assertIn("http:// or https://", err)

        # unbalanced quote in --cmd rejected
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "bad-quote",
            "--kind", "errors",
            "--cmd", 'echo "unclosed',
            "--baseline-cmd", "echo 10",
        )
        self.assertEqual(code, 1)
        self.assertIn("bad --cmd:", err)

        # Command that prints "abc" fails loudly
        bad_cmd = f'{sys.executable} -c "print(\x27abc\x27)"'
        base_cmd = f'{sys.executable} -c "print(10)"'
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "broken-signal",
            "--kind", "errors",
            "--cmd", bad_cmd,
            "--baseline-cmd", base_cmd,
        )
        self.assertEqual(code, 0, err)

        res = release.check("c2e")
        self.assertFalse(res["ok"])
        broken_check = [c for c in res["checks"] if c["name"] == "broken-signal"][0]
        self.assertFalse(broken_check["ok"])
        self.assertIn("output not an integer", broken_check["detail"])
        self.assertIn("abc", broken_check["detail"])

        code, _, _ = run("release", "check", "c2e")
        self.assertEqual(code, 3)

    def test_no_checks_configured(self):
        res = release.check("c2e")
        self.assertEqual(res, {"ok": True, "checks": []})

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["release", "check", "c2e"])
        self.assertEqual(code, 0)
        self.assertIn("no checks configured", stdout.getvalue())

    def test_errors_baseline_zero(self):
        # baseline 0, now 5 -> passes
        cmd = f'{sys.executable} -c "print(5)"'
        base_cmd = f'{sys.executable} -c "print(0)"'
        self.ok(
            "release", "check-add", "c2e",
            "--name", "err0-pass",
            "--kind", "errors",
            "--cmd", cmd,
            "--baseline-cmd", base_cmd,
        )
        res = release.check("c2e")
        self.assertTrue(res["ok"])

        # baseline 0, now 6 -> fails
        cmd6 = f'{sys.executable} -c "print(6)"'
        self.ok(
            "release", "check-add", "c2e",
            "--name", "err0-fail",
            "--kind", "errors",
            "--cmd", cmd6,
            "--baseline-cmd", base_cmd,
        )
        res = release.check("c2e")
        self.assertFalse(res["ok"])
        c_fail = [c for c in res["checks"] if c["name"] == "err0-fail"][0]
        self.assertFalse(c_fail["ok"])
        self.assertIn("6 > 5 (baseline 0)", c_fail["detail"])

    def test_cmd_nonzero_exit(self):
        cmd = f'{sys.executable} -c "import sys; print(\x27boom\x27, file=sys.stderr); sys.exit(2)"'
        base_cmd = f'{sys.executable} -c "print(10)"'
        self.ok(
            "release", "check-add", "c2e",
            "--name", "exit-fail",
            "--kind", "errors",
            "--cmd", cmd,
            "--baseline-cmd", base_cmd,
        )
        res = release.check("c2e")
        self.assertFalse(res["ok"])
        c = res["checks"][0]
        self.assertFalse(c["ok"])
        self.assertIn("command exited 2: boom", c["detail"])

    def test_http_connection_refused(self):
        # Port 1 is not running an HTTP server -> URLError
        self.ok(
            "release", "check-add", "c2e",
            "--name", "bad-port",
            "--kind", "http",
            "--url", "http://127.0.0.1:1/nope",
        )
        res = release.check("c2e")
        self.assertFalse(res["ok"])
        c = res["checks"][0]
        self.assertFalse(c["ok"])
        self.assertIsNone(c["value"])
        self.assertTrue(c["detail"])

    def test_http_redirect_fails(self):
        url = f"http://127.0.0.1:{self.server_port}/health-302"
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "ping",
            "--kind", "http",
            "--url", url,
        )
        self.assertEqual(code, 0, err)

        res = release.check("c2e")
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["checks"]), 1)
        c = res["checks"][0]
        self.assertEqual(c["name"], "ping")
        self.assertEqual(c["kind"], "http")
        self.assertFalse(c["ok"])
        self.assertEqual(c["value"], 302)
        self.assertIsNone(c["baseline"])
        self.assertEqual(c["detail"], "redirect 302 to https://auth.example.com/login")

        code, _, _ = run("release", "check", "c2e")
        self.assertEqual(code, 3)

    def test_cmd_failure_skips_baseline_cmd(self):
        marker = os.path.join(self.tmp.name, "baseline_ran.txt")
        cmd = f'{sys.executable} -c "import sys; sys.exit(1)"'
        base_cmd = f'{sys.executable} -c "import pathlib; pathlib.Path({marker!r}).touch()"'
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "skip-base",
            "--kind", "errors",
            "--cmd", cmd,
            "--baseline-cmd", base_cmd,
        )
        self.assertEqual(code, 0, err)

        res = release.check("c2e")
        self.assertFalse(res["ok"])
        self.assertFalse(os.path.exists(marker))
        c = res["checks"][0]
        self.assertFalse(c["ok"])
        self.assertIsNone(c["value"])
        self.assertIsNone(c["baseline"])
        self.assertIn("cmd error", c["detail"])

    def test_malformed_check_in_product_config_names_product_and_check(self):
        with store.lock():
            data = release.release()
            data["products"]["c2e"]["checks"] = [{"name": "bad-check", "kind": "unsupported"}]
            release.save_release(data)

        with self.assertRaises(FactoryError) as cm:
            release.release()
        self.assertIn("bad-check", str(cm.exception))
        self.assertIn("c2e", str(cm.exception))

    def test_baseline_cmd_failing_makes_check_fail(self):
        cmd = f'{sys.executable} -c "print(10)"'
        bad_base = f'{sys.executable} -c "import sys; sys.exit(1)"'
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "base-fails",
            "--kind", "errors",
            "--cmd", cmd,
            "--baseline-cmd", bad_base,
        )
        self.assertEqual(code, 0, err)

        res = release.check("c2e")
        self.assertFalse(res["ok"])
        c = res["checks"][0]
        self.assertFalse(c["ok"])
        self.assertEqual(c["value"], 10)
        self.assertIsNone(c["baseline"])
        self.assertIn("baseline_cmd error", c["detail"])

        code, _, _ = run("release", "check", "c2e")
        self.assertEqual(code, 3)

    def test_command_timeout_fails(self):
        cmd = f'{sys.executable} -c "import time; time.sleep(1)"'
        base_cmd = f'{sys.executable} -c "print(10)"'
        code, _, err = run(
            "release", "check-add", "c2e",
            "--name", "slow-cmd",
            "--kind", "errors",
            "--cmd", cmd,
            "--baseline-cmd", base_cmd,
        )
        self.assertEqual(code, 0, err)

        with mock.patch("factory.release.SIGNAL_TIMEOUT", 0.05):
            res = release.check("c2e")
        self.assertFalse(res["ok"])
        c = res["checks"][0]
        self.assertFalse(c["ok"])
        self.assertIsNone(c["value"])
        self.assertIsNone(c["baseline"])
        self.assertIn("timed out", c["detail"])

        with mock.patch("factory.release.SIGNAL_TIMEOUT", 0.05):
            code, _, _ = run("release", "check", "c2e")
        self.assertEqual(code, 3)

    def test_watch_passes_after_window(self):
        cmd = f'{sys.executable} -c "print(10)"'
        self.ok(
            "release", "check-add", "c2e",
            "--name", "orders",
            "--kind", "business",
            "--cmd", cmd,
            "--baseline-cmd", cmd,
        )

        sim_time = 0.0
        def fake_monotonic():
            return sim_time
        def fake_sleep(secs):
            nonlocal sim_time
            sim_time += secs

        with mock.patch("factory.release._monotonic", side_effect=fake_monotonic), \
             mock.patch("factory.release._sleep", side_effect=fake_sleep):
            code, obj, err = run("release", "watch", "c2e", "--minutes", "30", "--every", "300")
        self.assertEqual(code, 0, err)
        self.assertEqual(obj, {"result": "pass", "samples": 6})

        log_path = store.path("release-watch-c2e.log")
        self.assertTrue(os.path.exists(log_path))
        with open(log_path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[-1]["sample"], 6)
        self.assertTrue(lines[-1]["ok"])

    def test_watch_exports_its_start_to_check_commands(self):
        self.addCleanup(os.environ.pop, "FACTORY_WATCH_SINCE", None)
        os.environ.pop("FACTORY_WATCH_SINCE", None)
        cmd = f'{sys.executable} -c "import os; print(10 if os.environ.get(\'FACTORY_WATCH_SINCE\') else 0)"'
        self.ok("release", "check-add", "c2e", "--name", "since", "--kind", "business",
                "--cmd", cmd, "--baseline-cmd", f'{sys.executable} -c "print(10)"')
        sim_time = 0.0
        def fake_sleep(secs):
            nonlocal sim_time
            sim_time += secs
        with mock.patch("factory.release._monotonic", side_effect=lambda: sim_time), \
             mock.patch("factory.release._sleep", side_effect=fake_sleep):
            code, obj, err = run("release", "watch", "c2e", "--minutes", "10", "--every", "300")
        self.assertEqual(code, 0, err)
        self.assertEqual(obj["result"], "pass")
        self.assertNotIn("FACTORY_WATCH_SINCE", os.environ)  # restored after the watch

    def test_watch_three_http_failures_rolls_back(self):
        self.ok(
            "release", "check-add", "c2e",
            "--name", "ping",
            "--kind", "http",
            "--url", "http://127.0.0.1:1/down",
        )

        sim_time = 0.0
        def fake_monotonic():
            return sim_time
        def fake_sleep(secs):
            nonlocal sim_time
            sim_time += secs

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("factory.release._monotonic", side_effect=fake_monotonic), \
             mock.patch("factory.release._sleep", side_effect=fake_sleep), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--json", "release", "watch", "c2e", "--minutes", "30", "--every", "300"])
        self.assertEqual(code, 3)
        obj = json.loads(out.getvalue())
        self.assertEqual(obj["result"], "rollback")
        self.assertEqual(obj["kind"], "http")
        self.assertEqual(obj["samples"], 3)
        self.assertIn("last", obj)
        self.assertFalse(obj["last"]["ok"])

        log_path = store.path("release-watch-c2e.log")
        with open(log_path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 3)

    def test_watch_two_signal_failures_rolls_back(self):
        cmd = f'{sys.executable} -c "print(10)"'
        base_cmd = f'{sys.executable} -c "print(1)"'
        self.ok(
            "release", "check-add", "c2e",
            "--name", "err-spike",
            "--kind", "errors",
            "--cmd", cmd,
            "--baseline-cmd", base_cmd,
        )

        sim_time = 0.0
        def fake_monotonic():
            return sim_time
        def fake_sleep(secs):
            nonlocal sim_time
            sim_time += secs

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("factory.release._monotonic", side_effect=fake_monotonic), \
             mock.patch("factory.release._sleep", side_effect=fake_sleep), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--json", "release", "watch", "c2e", "--minutes", "30", "--every", "300"])
        self.assertEqual(code, 3)
        obj = json.loads(out.getvalue())
        self.assertEqual(obj["result"], "rollback")
        self.assertEqual(obj["kind"], "signal")
        self.assertEqual(obj["samples"], 2)
        self.assertIn("last", obj)

        log_path = store.path("release-watch-c2e.log")
        with open(log_path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 2)

    def test_watch_http_streak_resets_on_success(self):
        self.ok(
            "release", "check-add", "c2e",
            "--name", "ping",
            "--kind", "http",
            "--url", "http://127.0.0.1:1/fake",
        )

        sim_time = 0.0
        def fake_monotonic():
            return sim_time
        def fake_sleep(secs):
            nonlocal sim_time
            sim_time += secs

        check_stream = [
            {"name": "ping", "kind": "http", "ok": False, "value": None, "baseline": None, "detail": "fail 1"},
            {"name": "ping", "kind": "http", "ok": False, "value": None, "baseline": None, "detail": "fail 2"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": False, "value": None, "baseline": None, "detail": "fail 3"},
            {"name": "ping", "kind": "http", "ok": False, "value": None, "baseline": None, "detail": "fail 4"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
        ]
        with mock.patch("factory.release._monotonic", side_effect=fake_monotonic), \
             mock.patch("factory.release._sleep", side_effect=fake_sleep), \
             mock.patch("factory.release._run_http_check", side_effect=check_stream):
            code, obj, err = run("release", "watch", "c2e", "--minutes", "30", "--every", "300")
        self.assertEqual(code, 0, err)
        self.assertEqual(obj, {"result": "pass", "samples": 6})

    def test_watch_deadline_with_open_streak_keeps_sampling(self):
        self.ok(
            "release", "check-add", "c2e",
            "--name", "ping",
            "--kind", "http",
            "--url", "http://127.0.0.1:1/fake",
        )

        # Variant 1: last in-window sample has 1 http failure, next sample passes
        # -> pass with samples = window samples + 1 (6 + 1 = 7)
        sim_time = 0.0
        def fake_monotonic():
            return sim_time
        def fake_sleep(secs):
            nonlocal sim_time
            sim_time += secs

        check_stream_recover = [
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": False, "value": None, "baseline": None, "detail": "fail 1"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
        ]
        with mock.patch("factory.release._monotonic", side_effect=fake_monotonic), \
             mock.patch("factory.release._sleep", side_effect=fake_sleep), \
             mock.patch("factory.release._run_http_check", side_effect=check_stream_recover):
            code, obj, err = run("release", "watch", "c2e", "--minutes", "30", "--every", "300")
        self.assertEqual(code, 0, err)
        self.assertEqual(obj, {"result": "pass", "samples": 7})

        log_path = store.path("release-watch-c2e.log")
        with open(log_path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 7)
        self.assertEqual(lines[-1]["sample"], 7)
        self.assertTrue(lines[-1]["ok"])
        self.assertEqual(lines[-1]["http_streak"], 0)

        # Clean up log for variant 2
        if os.path.exists(log_path):
            os.remove(log_path)

        # Variant 2: next two fail -> rollback kind http
        sim_time = 0.0
        check_stream_fail = [
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": False, "value": None, "baseline": None, "detail": "fail 1"},
            {"name": "ping", "kind": "http", "ok": False, "value": None, "baseline": None, "detail": "fail 2"},
            {"name": "ping", "kind": "http", "ok": False, "value": None, "baseline": None, "detail": "fail 3"},
        ]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("factory.release._monotonic", side_effect=fake_monotonic), \
             mock.patch("factory.release._sleep", side_effect=fake_sleep), \
             mock.patch("factory.release._run_http_check", side_effect=check_stream_fail), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--json", "release", "watch", "c2e", "--minutes", "30", "--every", "300"])
        self.assertEqual(code, 3)
        obj = json.loads(out.getvalue())
        self.assertEqual(obj["result"], "rollback")
        self.assertEqual(obj["kind"], "http")
        self.assertEqual(obj["samples"], 8)
        self.assertIn("last", obj)

        with open(log_path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 8)
        self.assertEqual(lines[-1]["sample"], 8)
        self.assertFalse(lines[-1]["ok"])
        self.assertEqual(lines[-1]["http_streak"], 3)

    def test_watch_alternating_failures_end_after_grace(self):
        self.ok(
            "release", "check-add", "c2e",
            "--name", "ping",
            "--kind", "http",
            "--url", "http://127.0.0.1:1/fake",
        )
        self.ok(
            "release", "check-add", "c2e",
            "--name", "err",
            "--kind", "errors",
            "--cmd", f'{sys.executable} -c "print(0)"',
            "--baseline-cmd", f'{sys.executable} -c "print(0)"',
        )

        sim_time = 0.0
        def fake_monotonic():
            return sim_time
        def fake_sleep(secs):
            nonlocal sim_time
            sim_time += secs

        # Variant 1: alternating failures ending with open http streak
        # 6 in-window samples + exactly 2 grace samples -> 8 samples
        http_stream_1 = [
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": False, "value": None, "baseline": None, "detail": "fail 1"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": False, "value": None, "baseline": None, "detail": "fail 2"},
        ]
        signal_stream_1 = [
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": False, "value": 10, "baseline": 1, "detail": "spike 1"},
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
        ]

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("factory.release._monotonic", side_effect=fake_monotonic), \
             mock.patch("factory.release._sleep", side_effect=fake_sleep), \
             mock.patch("factory.release._run_http_check", side_effect=http_stream_1), \
             mock.patch("factory.release._run_signal_check", side_effect=signal_stream_1), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--json", "release", "watch", "c2e", "--minutes", "30", "--every", "300"])

        self.assertEqual(code, 3)
        obj = json.loads(out.getvalue())
        self.assertEqual(obj["result"], "rollback")
        self.assertEqual(obj["kind"], "http")
        self.assertEqual(obj["reason"], "still failing after the watch window")
        self.assertEqual(obj["samples"], 8)
        self.assertIn("last", obj)

        log_path = store.path("release-watch-c2e.log")
        with open(log_path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 8)
        self.assertEqual(lines[-1]["sample"], 8)
        self.assertEqual(lines[-1]["http_streak"], 1)
        self.assertEqual(lines[-1]["signal_streak"], 0)

        # Clean up log for variant 2
        if os.path.exists(log_path):
            os.remove(log_path)

        # Variant 2: alternating failures ending with open signal streak
        sim_time = 0.0
        http_stream_2 = [
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
            {"name": "ping", "kind": "http", "ok": False, "value": None, "baseline": None, "detail": "fail 1"},
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
        ]
        signal_stream_2 = [
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": False, "value": 10, "baseline": 1, "detail": "spike 1"},
            {"name": "err", "kind": "errors", "ok": True, "value": 0, "baseline": 0, "detail": "ok"},
            {"name": "err", "kind": "errors", "ok": False, "value": 10, "baseline": 1, "detail": "spike 2"},
        ]

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("factory.release._monotonic", side_effect=fake_monotonic), \
             mock.patch("factory.release._sleep", side_effect=fake_sleep), \
             mock.patch("factory.release._run_http_check", side_effect=http_stream_2), \
             mock.patch("factory.release._run_signal_check", side_effect=signal_stream_2), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--json", "release", "watch", "c2e", "--minutes", "30", "--every", "300"])

        self.assertEqual(code, 3)
        obj = json.loads(out.getvalue())
        self.assertEqual(obj["result"], "rollback")
        self.assertEqual(obj["kind"], "signal")
        self.assertEqual(obj["reason"], "still failing after the watch window")
        self.assertEqual(obj["samples"], 8)
        self.assertIn("last", obj)

        with open(log_path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 8)
        self.assertEqual(lines[-1]["sample"], 8)
        self.assertEqual(lines[-1]["http_streak"], 0)
        self.assertEqual(lines[-1]["signal_streak"], 1)

    def test_watch_sleep_clamped_inside_window(self):
        self.ok(
            "release", "check-add", "c2e",
            "--name", "ping",
            "--kind", "http",
            "--url", "http://127.0.0.1:1/fake",
        )

        sim_time = 0.0
        sleep_durations = []
        def fake_monotonic():
            return sim_time
        def fake_sleep(secs):
            nonlocal sim_time
            sleep_durations.append(secs)
            sim_time += secs

        check_stream = [
            {"name": "ping", "kind": "http", "ok": True, "value": 200, "baseline": None, "detail": "ok"},
        ]
        with mock.patch("factory.release._monotonic", side_effect=fake_monotonic), \
             mock.patch("factory.release._sleep", side_effect=fake_sleep), \
             mock.patch("factory.release._run_http_check", side_effect=check_stream):
            code, obj, err = run("release", "watch", "c2e", "--minutes", "1", "--every", "300")
        self.assertEqual(code, 0, err)
        self.assertEqual(obj, {"result": "pass", "samples": 1})
        self.assertEqual(sleep_durations, [60.0])
        self.assertEqual(sim_time, 60.0)

    def test_watch_no_checks_refused(self):
        code, _, err = run("release", "watch", "c2e", "--minutes", "30")
        self.assertEqual(code, 1)
        self.assertIn("no checks configured for c2e", err)
        self.assertFalse(os.path.exists(store.path("release-watch-c2e.log")))


if __name__ == "__main__":
    unittest.main()
