import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from factory import board, cli, config, fixlane, relay, sources, store
from factory.store import FactoryError

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


class FixLaneTestBase(unittest.TestCase):
    def setUp(self):
        fixlane._labels_ready.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)

        self.bin_dir = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin_dir, exist_ok=True)
        self.gh_bin = os.path.join(self.bin_dir, "gh")
        shutil.copyfile(os.path.join(os.path.dirname(__file__), "fake_gh_board.py"), self.gh_bin)
        os.chmod(self.gh_bin, 0o755)

        self.old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.bin_dir + ":" + self.old_path
        self.addCleanup(self._restore_path)

        # Create temporary git clone
        self.clone_dir = os.path.join(self.tmp.name, "clone-r")
        os.makedirs(self.clone_dir, exist_ok=True)
        subprocess.run(["git", "init", "-b", "develop", self.clone_dir], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.clone_dir, "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", self.clone_dir, "config", "user.email", "test@example.com"], check=True)
        with open(os.path.join(self.clone_dir, "app.py"), "w", encoding="utf-8") as f:
            f.write("print('hello world')\n")
        subprocess.run(["git", "-C", self.clone_dir, "add", "app.py"], check=True)
        subprocess.run(["git", "-C", self.clone_dir, "commit", "-m", "init"], check=True)
        # The lane fetches exactly origin/<base>: give the clone a real (bare) origin.
        origin = os.path.join(self.tmp.name, "origin.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.clone_dir, origin], check=True)
        subprocess.run(["git", "-C", self.clone_dir, "remote", "add", "origin", origin], check=True)

        self.spool_dir = os.path.join(self.tmp.name, "spool")
        self.tasks_dir = os.path.join(self.spool_dir, "tasks")
        os.makedirs(self.tasks_dir, exist_ok=True)
        os.makedirs(os.path.join(self.spool_dir, "out", "push"), exist_ok=True)

        self.rules_path = os.path.join(self.tmp.name, "rules.json")
        self.rules = {
            "bases": ["develop", "staging"],
            "repos": {
                "r": {
                    "path": self.clone_dir,
                    "github": "o/r",
                    "level": 2,
                    "bases": ["develop"],
                }
            }
        }
        self._write_rules(self.rules)

        store.write("products.json", {"bk": {"name": "Acme", "repos": ["o/r"]}})

        config.set_value("fixlane.enabled", "true")
        config.set_value("fixlane.rules", self.rules_path)
        config.set_value("fixlane.tasks", self.tasks_dir)
        config.set_value("fixlane.spool", self.spool_dir)
        config.set_value("fixlane.review_thread", "thread-review-loop-42")
        config.set_value("fixlane.agent_user", str(os.getuid()))
        config.set_value("fixlane.group", str(os.getgid()))

    def _restore_path(self):
        os.environ["PATH"] = self.old_path

    def _write_rules(self, data):
        with open(self.rules_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def read_gh_actions(self):
        log_path = os.path.join(self.tmp.name, "gh-actions.log")
        if not os.path.exists(log_path):
            return []
        with open(log_path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


class FixLaneRunTest(FixLaneTestBase):
    def test_disabled_is_noop(self):
        config.set_value("fixlane.enabled", "false")
        res, text = fixlane.run(now=NOW)
        self.assertFalse(res.get("enabled"))
        self.assertEqual(res.get("picked"), [])
        self.assertIn("disabled", text)
        self.assertEqual(os.listdir(self.tasks_dir), [])
        self.assertEqual(self.read_gh_actions(), [])

    def test_dry_run_writes_nothing(self):
        res, text = fixlane.run(dry=True, now=NOW)
        self.assertTrue(res.get("dry"))
        self.assertEqual(res.get("picked"), [139])
        self.assertIn("would pick: o/r#139", text)
        self.assertEqual(os.listdir(self.tasks_dir), [])
        self.assertEqual(self.read_gh_actions(), [])
        state = fixlane.load_state()
        self.assertEqual(state.get("tasks", {}), {})

    def test_pick_and_atomic_task_dir_and_src_tar(self):
        res, text = fixlane.run(dry=False, now=NOW)
        self.assertEqual(len(res["picked"]), 1)
        task_info = res["picked"][0]
        task_id = task_info["id"]
        self.assertTrue(task_id.startswith("r-139-202611041200"))
        self.assertIn(f"Fix lane started: {task_id}", text)

        # Atomic creation: temp dir is gone, final dir exists
        self.assertFalse(os.path.exists(os.path.join(self.tasks_dir, f".{task_id}")))
        task_dir = os.path.join(self.tasks_dir, task_id)
        self.assertTrue(os.path.isdir(task_dir))
        # Task dir mode 0o3770: sticky, so the agent cannot replace the factory's task.json or src.tar
        d_mode = os.stat(task_dir).st_mode & 0o7777
        self.assertEqual(d_mode, 0o3770)

        # task.json contents and mode 0o640
        task_json_path = os.path.join(task_dir, "task.json")
        self.assertTrue(os.path.isfile(task_json_path))
        self.assertEqual(os.stat(task_json_path).st_mode & 0o777, 0o640)
        with open(task_json_path, encoding="utf-8") as f:
            tdata = json.load(f)
        self.assertEqual(tdata["id"], task_id)
        self.assertEqual(tdata["repo"], "r")
        self.assertEqual(tdata["number"], 139)
        self.assertEqual(tdata["title"], "Auth token refresh race condition causes random logouts")
        self.assertEqual(tdata["base"], "develop")
        self.assertEqual(len(tdata["base_sha"]), 40)
        self.assertIn("factory", tdata["labels"])
        self.assertIn("https://github.com/o/r/issues/139", tdata["url"])

        # src.tar has the base tree, no .git, mode 0o640
        src_tar = os.path.join(task_dir, "src.tar")
        self.assertTrue(os.path.isfile(src_tar))
        self.assertEqual(os.stat(src_tar).st_mode & 0o777, 0o640)
        with tarfile.open(src_tar) as tar:
            names = tar.getnames()
            self.assertIn("app.py", names)
            self.assertFalse(any(n == ".git" or n.startswith(".git/") for n in names))

        # GitHub mutations: label building and comment posted
        actions = self.read_gh_actions()
        label_edit = next((a for a in actions if a[:2] == ["issue", "edit"] and "--add-label" in a and "building" in a), None)
        self.assertIsNotNone(label_edit)
        self.assertIn("139", label_edit)
        self.assertIn("o/r", label_edit)

        comment = next((a for a in actions if a[:2] == ["issue", "comment"] and "--body" in a), None)
        self.assertIsNotNone(comment)
        body_idx = comment.index("--body") + 1
        self.assertEqual(comment[body_idx], f"Fix lane started: {task_id}")

        # Stored in ~/.factory/fixlane.json
        state = fixlane.load_state()
        self.assertIn(task_id, state["tasks"])
        self.assertEqual(state["tasks"][task_id]["state"], "running")
        self.assertTrue(state["tasks"][task_id]["labelled"])
        self.assertTrue(state["tasks"][task_id]["commented"])

    def test_labels_and_comments_posted_once_and_skips_existing_task(self):
        res1, _ = fixlane.run(dry=False, now=NOW)
        self.assertEqual(len(res1["picked"]), 1)
        actions_after_first = len(self.read_gh_actions())

        # Second run should skip issue 139 because it already has a task dir and state
        res2, text2 = fixlane.run(dry=False, now=NOW)
        self.assertEqual(res2["picked"], [])
        self.assertEqual(len(self.read_gh_actions()), actions_after_first)
        self.assertEqual(text2, "no issues to pick")

    def test_filters_ready_level_oldest_first_and_daily_cap(self):
        issues = [
            {"number": 10, "title": "Oldest ready", "state": "open", "labels": [{"name": "factory"}],
             "created_at": "2026-11-01T08:00:00Z", "html_url": "https://github.com/o/r/issues/10", "repository_url": "https://api.github.com/repos/o/r"},
            {"number": 20, "title": "Newer ready", "state": "open", "labels": [{"name": "factory"}],
             "created_at": "2026-11-02T08:00:00Z", "html_url": "https://github.com/o/r/issues/20", "repository_url": "https://api.github.com/repos/o/r"},
            {"number": 30, "title": "Newest ready", "state": "open", "labels": [{"name": "factory"}],
             "created_at": "2026-11-03T08:00:00Z", "html_url": "https://github.com/o/r/issues/30", "repository_url": "https://api.github.com/repos/o/r"},
            {"number": 40, "title": "Level 1 repo", "state": "open", "labels": [{"name": "factory"}],
             "created_at": "2026-10-30T08:00:00Z", "html_url": "https://github.com/o/l1/issues/40", "repository_url": "https://api.github.com/repos/o/l1"},
            {"number": 50, "title": "Has building", "state": "open", "labels": [{"name": "factory"}, {"name": "building"}],
             "created_at": "2026-10-30T08:00:00Z", "html_url": "https://github.com/o/r/issues/50", "repository_url": "https://api.github.com/repos/o/r"},
            {"number": 60, "title": "Has needs-decision", "state": "open", "labels": [{"name": "factory"}, {"name": "needs-decision"}],
             "created_at": "2026-10-30T08:00:00Z", "html_url": "https://github.com/o/r/issues/60", "repository_url": "https://api.github.com/repos/o/r"},
            {"number": 70, "title": "Has stuck", "state": "open", "labels": [{"name": "factory"}, {"name": "stuck"}],
             "created_at": "2026-10-30T08:00:00Z", "html_url": "https://github.com/o/r/issues/70", "repository_url": "https://api.github.com/repos/o/r"},
            {"number": 80, "title": "Linked PR", "state": "open", "labels": [{"name": "factory"}],
             "created_at": "2026-10-30T08:00:00Z", "html_url": "https://github.com/o/r/issues/80", "repository_url": "https://api.github.com/repos/o/r"},
        ]
        open_by_closed = {("o/r", 80): [{"number": 999, "repo": "o/r", "url": "pr_url", "isDraft": False}]}

        rules = {
            "bases": ["develop"],
            "repos": {
                "r": {"path": self.clone_dir, "github": "o/r", "level": 2, "bases": ["develop"]},
                "l1": {"path": self.clone_dir, "github": "o/l1", "level": 1, "bases": ["develop"]},
            }
        }
        self._write_rules(rules)
        config.set_value("fixlane.max_per_day", "2")

        with mock.patch.object(board, "search_issues", return_value=(issues, {}, {"o/r", "o/l1"})), \
             mock.patch.object(board, "_collect_pr_index", return_value=(open_by_closed, {})):
            res, _ = fixlane.run(dry=True, now=NOW)
            # Oldest first and capped at 2: issues 10 and 20 (30 is capped; 40, 50, 60, 70, 80 skipped)
            self.assertEqual(res["picked"], [10, 20])

    def test_run_task_dir_no_exist_ok(self):
        # Item 1/11: os.mkdir without exist_ok fails if tmp dir exists, candidate logged and skipped
        task_id = "r-139-202611041200"
        tmp_dir = os.path.join(self.tasks_dir, f".{task_id}")
        os.makedirs(tmp_dir, exist_ok=True)
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            res, _ = fixlane.run(now=NOW)
        self.assertEqual(res["picked"], [])
        self.assertIn("File exists", stderr.getvalue())

    def test_run_candidate_isolation_and_cap_successes(self):
        # Item 4 & 5: fetch/archive errors isolated; cap counts only successes
        config.set_value("fixlane.max_per_day", "1")
        issues = [
            {"number": 1, "title": "Broken repo issue", "state": "open", "labels": [{"name": "factory"}],
             "created_at": "2026-11-01T08:00:00Z", "html_url": "https://github.com/o/broken/issues/1", "repository_url": "https://api.github.com/repos/o/broken"},
            {"number": 2, "title": "Good repo issue", "state": "open", "labels": [{"name": "factory"}],
             "created_at": "2026-11-02T08:00:00Z", "html_url": "https://github.com/o/r/issues/2", "repository_url": "https://api.github.com/repos/o/r"},
        ]
        rules = {
            "bases": ["develop"],
            "repos": {
                "broken": {"path": os.path.join(self.tmp.name, "nonexistent"), "github": "o/broken", "level": 2, "bases": ["develop"]},
                "r": {"path": self.clone_dir, "github": "o/r", "level": 2, "bases": ["develop"]},
            }
        }
        self._write_rules(rules)
        with mock.patch.object(board, "search_issues", return_value=(issues, {}, {"o/broken", "o/r"})), \
             mock.patch.object(board, "_collect_pr_index", return_value=({}, {})):
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                res, _ = fixlane.run(dry=False, now=NOW)
            # Broken repo was logged and skipped; good repo was picked
            self.assertIn("fixlane: o/broken#1:", stderr.getvalue())
            self.assertEqual(len(res["picked"]), 1)
            self.assertEqual(res["picked"][0]["number"], 2)

    def test_get_ready_issues_skips_repo_errors(self):
        # Item 6: skip repos present in repo_errors
        issues = [
            {"number": 1, "title": "Repo with error", "state": "open", "labels": [{"name": "factory"}],
             "created_at": "2026-11-01T08:00:00Z", "html_url": "https://github.com/o/err/issues/1", "repository_url": "https://api.github.com/repos/o/err"},
            {"number": 2, "title": "Healthy repo", "state": "open", "labels": [{"name": "factory"}],
             "created_at": "2026-11-02T08:00:00Z", "html_url": "https://github.com/o/r/issues/2", "repository_url": "https://api.github.com/repos/o/r"},
        ]
        rules = {
            "bases": ["develop"],
            "repos": {
                "err": {"path": self.clone_dir, "github": "o/err", "level": 2, "bases": ["develop"]},
                "r": {"path": self.clone_dir, "github": "o/r", "level": 2, "bases": ["develop"]},
            }
        }
        self._write_rules(rules)
        with mock.patch.object(board, "search_issues", return_value=(issues, {"o/err": "500 server error"}, {"o/r"})), \
             mock.patch.object(board, "_collect_pr_index", return_value=({}, {})):
            candidates = fixlane.get_ready_issues(rules, now=NOW)
            self.assertEqual([c["number"] for c in candidates], [2])

    def test_run_fails_pick_when_group_missing_or_chown_fails(self):
        # Round 2 item D: if group fixlane.group does not exist or chown fails, fail that pick
        # (log, remove the tmp dir, no state entry, no gh calls)
        config.set_value("fixlane.group", "nonexistent_group_xyz")
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            res, _ = fixlane.run(dry=False, now=NOW)
        self.assertEqual(res["picked"], [])
        self.assertIn("fixlane: o/r#139:", stderr.getvalue())
        self.assertIn("nonexistent_group_xyz", stderr.getvalue())
        self.assertEqual(len(self.read_gh_actions()), 0)
        self.assertEqual(os.listdir(self.tasks_dir), [])
        st = fixlane.load_state()
        self.assertEqual(st.get("tasks", {}), {})

        # When chown fails
        config.set_value("fixlane.group", str(os.getgid()))
        stderr = io.StringIO()
        with mock.patch("os.chown", side_effect=PermissionError("chown denied")), mock.patch("sys.stderr", stderr):
            res, _ = fixlane.run(dry=False, now=NOW)
        self.assertEqual(res["picked"], [])
        self.assertIn("chown denied", stderr.getvalue())
        self.assertEqual(len(self.read_gh_actions()), 0)
        self.assertEqual(os.listdir(self.tasks_dir), [])

        # Group exists but this process is not in it (setgid would be dropped silently)
        stderr = io.StringIO()
        with mock.patch.object(fixlane, "_resolve_group_gid", return_value=99999), mock.patch("sys.stderr", stderr):
            res, _ = fixlane.run(dry=False, now=NOW)
        self.assertEqual(res["picked"], [])
        self.assertIn("not in group", stderr.getvalue())
        self.assertEqual(len(self.read_gh_actions()), 0)
        self.assertEqual(os.listdir(self.tasks_dir), [])

    def test_has_task_dir_safe_open(self):
        # Round 2 item E: In has_task_dir, read task.json with safe open (O_NOFOLLOW|O_NONBLOCK, regular file)
        task_id = "untrusted-task-1"
        tdir = os.path.join(self.tasks_dir, task_id)
        os.makedirs(tdir, exist_ok=True)
        t_json = os.path.join(tdir, "task.json")

        # 1. Symlink task.json is safely ignored
        target = os.path.join(self.tmp.name, "real_task.json")
        with open(target, "w", encoding="utf-8") as f:
            json.dump({"repo": "o/r", "number": 139}, f)
        os.symlink(target, t_json)

        # Empty state so has_task_dir checks task.json on disk
        self.assertFalse(fixlane.has_task_dir(self.tasks_dir, "o/r", 139, state={"tasks": {}}))

        # 2. Regular task.json file is read safely
        os.unlink(t_json)
        with open(t_json, "w", encoding="utf-8") as f:
            json.dump({"repo": "o/r", "number": 139}, f)
        self.assertTrue(fixlane.has_task_dir(self.tasks_dir, "o/r", 139, state={"tasks": {}}))

    def test_has_task_in_state_stuck_grace_window(self):
        # Round 2 item F: has_task_in_state: a stuck task within its 24 h grace window blocks a re-pick
        st = {
            "tasks": {
                "t1": {
                    "id": "t1",
                    "repo": "r",
                    "github": "o/r",
                    "number": 139,
                    "state": "stuck",
                    "collectedAt": (NOW - timedelta(hours=5)).isoformat(),
                }
            }
        }
        # Within 24h grace window: blocks re-pick
        self.assertTrue(fixlane.has_task_in_state(st, "o/r", 139, now=NOW))

        # Past 24h grace window: allows re-pick
        st["tasks"]["t1"]["collectedAt"] = (NOW - timedelta(hours=25)).isoformat()
        self.assertFalse(fixlane.has_task_in_state(st, "o/r", 139, now=NOW))


class FixLaneCollectTest(FixLaneTestBase):
    def setUp(self):
        super().setUp()
        self.out_push_dir = os.path.join(self.spool_dir, "out", "push")

    def _seed_running_task(self, task_id="r-139-202611041000", number=139, started_at="2026-11-04T10:00:00Z"):
        with store.lock():
            st = fixlane.load_state()
            st["tasks"][task_id] = {
                "id": task_id,
                "repo": "r",
                "github": "o/r",
                "number": number,
                "title": "Auth token refresh",
                "state": "running",
                "labelled": True,
                "commented": True,
                "base": "develop",
                "base_sha": "abc123",
                "startedAt": started_at,
                "url": f"https://github.com/o/r/issues/{number}",
            }
            fixlane.save_state(st)
        task_dir = os.path.join(self.tasks_dir, task_id)
        os.makedirs(task_dir, exist_ok=True)
        return task_id

    def test_collect_pushed(self):
        # Item 2 & 3: Validate push result with gh pr view and autopilot relay
        task_id = self._seed_running_task()
        result_file = os.path.join(self.out_push_dir, f"{task_id}.json.result.json")
        pr_url = "https://github.com/o/r/pull/99"
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"pushed": True, "pr": 99, "head_sha": "sha_head_123"}, f)

        res, text = fixlane.collect(now=NOW)
        self.assertEqual(len(res["collected"]), 1)
        self.assertEqual(res["collected"][0]["outcome"], "pushed")

        # Comment PR link on issue
        actions = self.read_gh_actions()
        comment = next((a for a in actions if a[:2] == ["issue", "comment"] and a[2] == "139"), None)
        self.assertIsNotNone(comment)
        body = comment[comment.index("--body") + 1]
        self.assertEqual(body, pr_url)

        # Relay message queued with kind="autopilot" and exact text
        outbox = relay.load_outbox()
        msg = next((m for m in outbox if m.get("thread") == "thread-review-loop-42"), None)
        self.assertIsNotNone(msg)
        self.assertEqual(msg.get("kind"), "autopilot")
        expected_relay = "factory fixlane: gate PR https://github.com/o/r/pull/99 for o/r#139 is open; run the review loop on it (factory/review, then the merge ask)."
        self.assertEqual(msg["text"], expected_relay)

        # State updated
        st = fixlane.load_state()
        self.assertEqual(st["tasks"][task_id]["state"], "pushed")

        # Idempotence: subsequent collect does nothing
        res2, _ = fixlane.collect(now=NOW)
        self.assertEqual(res2["collected"], [])
        self.assertEqual(len(self.read_gh_actions()), len(actions))
        self.assertEqual(len(relay.load_outbox()), len(outbox))

    def test_collect_push_mismatch_refused(self):
        # Item 2: Gate result mismatch stops with refused and reason
        task_id = self._seed_running_task()
        result_file = os.path.join(self.out_push_dir, f"{task_id}.json.result.json")
        # head_sha in result does not match fake_gh_board sha_head_123
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"pushed": True, "pr": 99, "head_sha": "wrong_sha"}, f)

        res, _ = fixlane.collect(now=NOW)
        self.assertEqual(len(res["collected"]), 1)
        self.assertEqual(res["collected"][0]["outcome"], "refused")
        self.assertEqual(res["collected"][0]["reasons"], ["gate result does not match this task"])

        st = fixlane.load_state()
        self.assertEqual(st["tasks"][task_id]["state"], "refused")

    def test_collect_refused_or_rejected(self):
        task_id = self._seed_running_task()
        result_file = os.path.join(self.out_push_dir, f"{task_id}.json.result.json")
        long_reason = "X" * 300
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({
                "pushed": False,
                "reasons": ["patch does not apply", long_reason, "r3", "r4", "r5", "r6"]
            }, f)

        res, _ = fixlane.collect(now=NOW)
        self.assertEqual(len(res["collected"]), 1)
        self.assertEqual(res["collected"][0]["outcome"], "refused")

        # Reasons cut to 5, each cut to 200 chars
        reasons = res["collected"][0]["reasons"]
        self.assertEqual(len(reasons), 5)
        self.assertLessEqual(len(reasons[1]), 200)

        # Labels updated: remove building, add stuck
        actions = self.read_gh_actions()
        edit = next((a for a in actions if a[:2] == ["issue", "edit"] and "--remove-label" in a and "--add-label" in a), None)
        self.assertIsNotNone(edit)
        self.assertIn("building", edit)
        self.assertIn("stuck", edit)

        # Comment with reasons
        comment = next((a for a in actions if a[:2] == ["issue", "comment"] and a[2] == "139"), None)
        self.assertIsNotNone(comment)
        body = comment[comment.index("--body") + 1]
        self.assertIn("patch does not apply", body)

        st = fixlane.load_state()
        self.assertEqual(st["tasks"][task_id]["state"], "refused")

    def test_collect_failed_txt(self):
        task_id = self._seed_running_task()
        failed_path = os.path.join(self.tasks_dir, task_id, "failed.txt")
        with open(failed_path, "w", encoding="utf-8") as f:
            f.write("Agent build failed @someone see #1 ```breakout```")

        res, _ = fixlane.collect(now=NOW)
        self.assertEqual(len(res["collected"]), 1)
        self.assertEqual(res["collected"][0]["outcome"], "failed")

        actions = self.read_gh_actions()
        edit = next((a for a in actions if a[:2] == ["issue", "edit"] and "stuck" in a), None)
        self.assertIsNotNone(edit)
        # the repo may lack the lane labels: they are created before the first edit that adds one
        create = next((k for k, x in enumerate(actions) if x[:3] == ["label", "create", "stuck"]), None)
        self.assertIsNotNone(create)
        self.assertLess(create, actions.index(edit))

        comment = next((a for a in actions if a[:2] == ["issue", "comment"] and a[2] == "139"), None)
        self.assertIsNotNone(comment)
        body = comment[comment.index("--body") + 1]
        self.assertIn("Agent build failed", body)
        fence = "`" * 3
        self.assertEqual(body.count(fence), 2)
        inner = body.split(fence)[1]
        self.assertIn("@someone", inner)
        self.assertIn("#1", inner)

        st = fixlane.load_state()
        self.assertEqual(st["tasks"][task_id]["state"], "failed")

    def test_collect_failed_txt_security_rejections(self):
        # Item 1: symlink, wrong uid, nlink != 1 rejected
        task_id = self._seed_running_task()
        failed_path = os.path.join(self.tasks_dir, task_id, "failed.txt")

        # 1. Symlink rejected
        target = os.path.join(self.tasks_dir, "some_target.txt")
        with open(target, "w") as f:
            f.write("secret data")
        os.symlink(target, failed_path)

        res, _ = fixlane.collect(now=NOW)
        self.assertEqual(res["collected"][0]["outcome"], "failed")
        self.assertIn("failed.txt rejected", res["collected"][0]["reasons"][0])
        os.unlink(failed_path)

        # 2. Wrong UID rejected
        config.set_value("fixlane.agent_user", str(os.getuid() + 999))
        with open(failed_path, "w") as f:
            f.write("failed msg")
        st = fixlane.load_state()
        st["tasks"][task_id]["state"] = "running"
        fixlane.save_state(st)

        res, _ = fixlane.collect(now=NOW)
        self.assertEqual(res["collected"][0]["outcome"], "failed")
        self.assertIn("failed.txt rejected", res["collected"][0]["reasons"][0])
        config.set_value("fixlane.agent_user", str(os.getuid()))
        os.unlink(failed_path)

        # 3. Hardlink count != 1 rejected
        with open(failed_path, "w") as f:
            f.write("failed msg")
        link_path = os.path.join(self.tasks_dir, "failed_link.txt")
        os.link(failed_path, link_path)
        st = fixlane.load_state()
        st["tasks"][task_id]["state"] = "running"
        fixlane.save_state(st)

        res, _ = fixlane.collect(now=NOW)
        self.assertEqual(res["collected"][0]["outcome"], "failed")
        self.assertIn("hardlink count 2 != 1", res["collected"][0]["reasons"][0])
        os.unlink(link_path)

    def test_collect_6h_timeout(self):
        seven_hours_ago = (NOW - timedelta(hours=7)).isoformat()
        task_id = self._seed_running_task(started_at=seven_hours_ago)

        res, _ = fixlane.collect(now=NOW)
        self.assertEqual(len(res["collected"]), 1)
        self.assertEqual(res["collected"][0]["outcome"], "stuck")

        actions = self.read_gh_actions()
        edit = next((a for a in actions if a[:2] == ["issue", "edit"] and "stuck" in a), None)
        self.assertIsNotNone(edit)

        comment = next((a for a in actions if a[:2] == ["issue", "comment"] and a[2] == "139"), None)
        self.assertIsNotNone(comment)
        body = comment[comment.index("--body") + 1]
        self.assertIn("no result in 6 h", body)

        st = fixlane.load_state()
        self.assertEqual(st["tasks"][task_id]["state"], "stuck")

    def test_collect_recovers_stuck_by_timeout_within_24h(self):
        # Item 9: collect checks result files for tasks stuck by timeout < 24h ago
        task_id = self._seed_running_task()
        two_hours_ago = (NOW - timedelta(hours=2)).isoformat()
        with store.lock():
            st = fixlane.load_state()
            st["tasks"][task_id].update({
                "state": "stuck",
                "reasons": ["no result in 6 h"],
                "collectedAt": two_hours_ago,
            })
            fixlane.save_state(st)

        result_file = os.path.join(self.out_push_dir, f"{task_id}.json.result.json")
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"pushed": True, "pr": 99, "head_sha": "sha_head_123"}, f)

        res, _ = fixlane.collect(now=NOW)
        self.assertEqual(len(res["collected"]), 1)
        self.assertEqual(res["collected"][0]["outcome"], "pushed")

        # Removes label stuck and re-adds nothing else
        actions = self.read_gh_actions()
        remove_stuck = next((a for a in actions if a[:2] == ["issue", "edit"] and "--remove-label" in a and "stuck" in a), None)
        self.assertIsNotNone(remove_stuck)
        self.assertNotIn("--add-label", remove_stuck)

        st = fixlane.load_state()
        self.assertEqual(st["tasks"][task_id]["state"], "pushed")

    def test_collect_retries_missing_label_and_comment(self):
        # Item 4: collect() retries missing label/comment
        task_id = self._seed_running_task()
        with store.lock():
            st = fixlane.load_state()
            st["tasks"][task_id]["labelled"] = False
            st["tasks"][task_id]["commented"] = False
            fixlane.save_state(st)

        fixlane.collect(now=NOW)
        actions = self.read_gh_actions()
        self.assertTrue(any(a[:2] == ["issue", "edit"] and "building" in a for a in actions))
        self.assertTrue(any(a[:2] == ["issue", "comment"] and f"Fix lane started: {task_id}" in a for a in actions))

        st = fixlane.load_state()
        self.assertTrue(st["tasks"][task_id]["labelled"])
        self.assertTrue(st["tasks"][task_id]["commented"])

    def test_collect_gh_pr_view_transient_error_leaves_running(self):
        # Round 2 item A: gh pr view raising (network, 502, rate limit) must NOT stop the task:
        # log fixlane: <repo>#<n>: pr check failed: <err> to stderr and leave it running for next collect.
        task_id = self._seed_running_task()
        result_file = os.path.join(self.out_push_dir, f"{task_id}.json.result.json")
        with open(result_file, "w", encoding="utf-8") as f:
            json.dump({"pushed": True, "pr": 99, "head_sha": "sha_head_123"}, f)

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"FAKE_GH_PR_VIEW_FAIL": "1"}), mock.patch("sys.stderr", stderr):
            res, _ = fixlane.collect(now=NOW)

        self.assertIn("fixlane: o/r#139: pr check failed:", stderr.getvalue())
        self.assertEqual(res["collected"], [])
        st = fixlane.load_state()
        self.assertEqual(st["tasks"][task_id]["state"], "running")

        # Next collect without failure succeeds and marks pushed
        res2, _ = fixlane.collect(now=NOW)
        self.assertEqual(len(res2["collected"]), 1)
        self.assertEqual(res2["collected"][0]["outcome"], "pushed")
        st2 = fixlane.load_state()
        self.assertEqual(st2["tasks"][task_id]["state"], "pushed")

    def test_collect_task_isolation_continues_others(self):
        # Round 2 item B: collect: each task in its own try/except; log fixlane: <repo>#<n>: <err> to stderr; continue with the others.
        task1 = self._seed_running_task(task_id="r-101-202611041000", number=101)
        task2 = self._seed_running_task(task_id="r-102-202611041000", number=102)

        result_file2 = os.path.join(self.out_push_dir, f"{task2}.json.result.json")
        with open(result_file2, "w", encoding="utf-8") as f:
            json.dump({"pushed": True, "pr": 102, "head_sha": "sha_head_123"}, f)

        orig_isfile = os.path.isfile
        def custom_isfile(path):
            if f"{task1}.json.result.json" in path:
                raise OSError("simulated disk error for task1")
            return orig_isfile(path)

        stderr = io.StringIO()
        with mock.patch("os.path.isfile", side_effect=custom_isfile), mock.patch("sys.stderr", stderr):
            res, _ = fixlane.collect(now=NOW)

        self.assertIn("fixlane: o/r#101: simulated disk error for task1", stderr.getvalue())
        collected_ids = [c["id"] for c in res["collected"]]
        self.assertIn(task2, collected_ids)


class FixLaneDoctorAndCliTest(FixLaneTestBase):
    def test_doctor_not_configured(self):
        config.set_value("fixlane.rules", "")
        self.assertEqual(fixlane.doctor(), "not configured")

    def test_doctor_ok_when_configured(self):
        self.assertEqual(fixlane.doctor(), "ok")

    def test_doctor_fails_when_rules_missing(self):
        config.set_value("fixlane.rules", os.path.join(self.tmp.name, "missing-rules.json"))
        with self.assertRaises(FactoryError) as cm:
            fixlane.doctor()
        self.assertIn("cannot read fixlane rules", str(cm.exception))

    def test_doctor_fails_when_tasks_dir_unwritable(self):
        unwritable_dir = os.path.join(self.tmp.name, "unwritable_tasks")
        os.makedirs(unwritable_dir, exist_ok=True)
        os.chmod(unwritable_dir, 0o400)
        config.set_value("fixlane.tasks", unwritable_dir)
        try:
            with self.assertRaises(FactoryError) as cm:
                fixlane.doctor()
            self.assertIn("not writable", str(cm.exception))
        finally:
            os.chmod(unwritable_dir, 0o700)

    def test_doctor_enabled_requires_review_thread_and_push_dir(self):
        # Item 12: when fixlane.enabled is true, require review_thread and push dir readable
        config.set_value("fixlane.review_thread", "")
        with self.assertRaises(FactoryError) as cm:
            fixlane.doctor()
        self.assertIn("fixlane.review_thread is not configured", str(cm.exception))

        # Push dir missing
        config.set_value("fixlane.review_thread", "th-1")
        push_dir = os.path.join(self.spool_dir, "out", "push")
        shutil.rmtree(push_dir)
        with self.assertRaises(FactoryError) as cm:
            fixlane.doctor()
        self.assertIn("push dir", str(cm.exception))

        # When enabled is false, does not require them
        config.set_value("fixlane.enabled", "false")
        self.assertEqual(fixlane.doctor(), "ok")

    def test_cli_status_and_json(self):
        fixlane.run(dry=False, now=NOW)
        obj, text = fixlane.status()
        self.assertEqual(len(obj["tasks"]), 1)
        self.assertIn("r-139-202611041200", text)
        self.assertIn("running", text)

        code, out, err = self._run_cli("fixlane", "status")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out["tasks"]), 1)

    def test_cli_retry_command(self):
        # Round 2 item C:
        # 1. Running task refuses retry
        fixlane.run(dry=False, now=NOW)
        code, out, err = self._run_cli("fixlane", "retry", "o/r#139")
        self.assertNotEqual(code, 0)
        self.assertIn("running", err)

        # 2. Pushed task with open PR refuses retry
        st = fixlane.load_state()
        task_id = next(iter(st["tasks"].keys()))
        st["tasks"][task_id]["state"] = "pushed"
        st["tasks"][task_id]["pr"] = 99
        st["tasks"][task_id]["pr_url"] = "https://github.com/o/r/pull/99"
        fixlane.save_state(st)
        code, out, err = self._run_cli("fixlane", "retry", "o/r#139")
        self.assertNotEqual(code, 0)
        self.assertIn("close or merge PR https://github.com/o/r/pull/99 and delete its branch first", err)

        # 3. Pushed task with closed PR succeeds
        with mock.patch.dict(os.environ, {"FAKE_GH_PR_STATE": "CLOSED"}):
            code, out, err = self._run_cli("fixlane", "retry", "o/r#139")
            self.assertEqual(code, 0, err)
            self.assertEqual(out["state"], "retired")

        # 4. Stuck or failed task removes stuck and building labels, marks state retired
        st = fixlane.load_state()
        st["tasks"][task_id]["state"] = "stuck"
        fixlane.save_state(st)
        actions_before = len(self.read_gh_actions())
        code, out, err = self._run_cli("fixlane", "retry", "o/r#139")
        self.assertEqual(code, 0, err)
        self.assertEqual(out["state"], "retired")
        actions = self.read_gh_actions()[actions_before:]
        remove_labels = next((a for a in actions if a[:2] == ["issue", "edit"] and "--remove-label" in a and "stuck" in a and "building" in a), None)
        self.assertIsNotNone(remove_labels)

        st = fixlane.load_state()
        task = st["tasks"][task_id]
        self.assertEqual(task["state"], "retired")

        # Because it's retired, has_task_* allows it to be picked again
        self.assertFalse(fixlane.has_task_in_state(st, "o/r", 139, now=NOW))
        self.assertFalse(fixlane.has_task_dir(self.tasks_dir, "o/r", 139, state=st))

    def test_doctor_group_checks(self):
        # Round 2 item D: when fixlane.enabled, require group to exist and user to be a member (os.getgroups())
        config.set_value("fixlane.group", "nonexistent_group_xyz")
        with self.assertRaises(FactoryError) as cm:
            fixlane.doctor()
        self.assertIn("does not exist", str(cm.exception))

        # Group exists but user is not a member
        with mock.patch.object(fixlane, "_resolve_group_gid", return_value=99999), \
             mock.patch("os.getgroups", return_value=[1, 2, 3]), \
             mock.patch("os.getgid", return_value=1), \
             mock.patch("os.getegid", return_value=1):
            with self.assertRaises(FactoryError) as cm:
                fixlane.doctor()
            self.assertIn("not in group", str(cm.exception))

        # When enabled is false, does not require group
        config.set_value("fixlane.enabled", "false")
        config.set_value("fixlane.group", "nonexistent_group_xyz")
        self.assertEqual(fixlane.doctor(), "ok")

    def test_factory_doctor_output(self):
        checks = sources.doctor_checks()
        fl_check = next((c for c in checks if c[0] == "fixlane"), None)
        self.assertIsNotNone(fl_check)
        self.assertTrue(fl_check[1])
        self.assertEqual(fl_check[2], "ok")

        # When unconfigured
        config.set_value("fixlane.rules", "")
        checks = sources.doctor_checks()
        fl_check = next((c for c in checks if c[0] == "fixlane"), None)
        self.assertIsNotNone(fl_check)
        self.assertTrue(fl_check[1])
        self.assertEqual(fl_check[2], "not configured")

    def test_exclusive_flock_busy(self):
        # Item 7: Exclusive flock on fixlane.lock returns locked: true and text fixlane busy
        import fcntl
        lock_file = store.path("fixlane.lock")
        store.mkdir(store.home())
        with open(lock_file, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            res_run, text_run = fixlane.run(now=NOW)
            self.assertEqual(res_run, {"locked": True})
            self.assertEqual(text_run, "fixlane busy")

            res_coll, text_coll = fixlane.collect(now=NOW)
            self.assertEqual(res_coll, {"locked": True})
            self.assertEqual(text_coll, "fixlane busy")
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def test_resolve_base_and_level_logic(self):
        # Item 13: repo bases intersected with global bases; non-list -> None; non-int level -> 1
        rcfg = {"bases": ["feature", "develop"]}
        rules = {"bases": ["develop", "main"]}
        base = fixlane.resolve_base(rcfg, rules)
        self.assertEqual(base, "develop")

        # Non-list repo bases
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            base_bad = fixlane.resolve_base({"bases": "develop"}, rules, repo="o/r")
        self.assertIsNone(base_bad)
        self.assertIn("fixlane: o/r: bases is not a list", stderr.getvalue())

    def test_get_spool_dir_and_match_repo_rule(self):
        # Item 14: rstrip("/") and exact github match priority
        config.set_value("fixlane.spool", "")
        spool = fixlane.get_spool_dir("/var/spool/fix-gate/tasks/")
        self.assertEqual(spool, "/var/spool/fix-gate")

        rules = {
            "repos": {
                "r": {"github": "other/r"},
                "exact": {"github": "o/r"},
            }
        }
        gate_name, rcfg = fixlane.match_repo_rule("o/r", rules)
        self.assertEqual(gate_name, "exact")

    def _run_cli(self, *argv):
        import contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--json", *argv])
        val = out.getvalue().strip()
        data = json.loads(val) if code == 0 and val else None
        return code, data, err.getvalue()


if __name__ == "__main__":
    unittest.main()
