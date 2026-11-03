import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone

from factory import chatscan, cli, config, store, t3

NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)


class _Clock(datetime):
    """The fixtures are dated: pin the scan's clock so they never age past MAX_AGE."""
    @classmethod
    def now(cls, tz=None):
        return NOW
WS = "/w/acme-monorepo"


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_json(*argv):
    code, out, err = run_cli("--json", *argv)
    obj = json.loads(out) if code == 0 and out.strip() else None
    return code, obj, err


class ChatscanBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        clock = mock.patch.object(chatscan, "datetime", _Clock)
        clock.start()
        self.addCleanup(clock.stop)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.db_path = os.path.join(self.tmp.name, "statev2.sqlite")
        os.environ["FACTORY_T3_DB"] = self.db_path

        con = sqlite3.connect(self.db_path)
        for table, cols in t3.COLUMNS.items():
            con.execute(f"create table {table} ({', '.join(cols)})")
        con.executemany(
            "insert into projection_projects values (?,?,?)",
            [("p1", WS, None), ("p2", "/w/other", None)],
        )
        con.commit()
        con.close()

        # Set up default product and project
        with mock_stdout():
            cli.main(["product", "set", "bk", "--name", "Acme", "--workspace", WS])
            cli.main(["project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "DS"])
            cli.main(["project", "set", "ds", "--match", "acme-ds"])
            cli.main(["plan", "phase", "ds", "A", "--total", "1", "--start", "2026-10-01", "--due", "2026-12-01"])

    def tearDown(self):
        self.tmp.cleanup()
        del os.environ["FACTORY_HOME"]
        del os.environ["FACTORY_T3_DB"]

    def db(self):
        return sqlite3.connect(self.db_path)

    def add_thread(self, tid, pid="p1", title="acme-ds/feat", updated="2026-10-27T13:50:00Z",
                   archived=None, deleted=None, root=None):
        con = self.db()
        payload = {"lineage": {"rootThreadId": root or tid}}
        con.execute(
            "insert into orchestration_v2_projection_threads values (?,?,?,?,?,?,?,?)",
            (tid, pid, title, updated, updated, archived, deleted, json.dumps(payload)),
        )
        con.commit()
        con.close()

    def add_run(self, tid, status="completed", requested="2026-10-27T13:00:00Z", completed="2026-10-27T13:50:00Z"):
        con = self.db()
        con.execute(
            "insert into orchestration_v2_projection_runs values (?,?,?,?,?,?)",
            (tid, status, requested, completed, json.dumps({}), "claudeAgent"),
        )
        con.commit()
        con.close()

    def add_message(self, tid, role="assistant", text="Should I proceed?", created_at="2026-10-27T13:49:00Z"):
        con = self.db()
        payload = {"text": text}
        con.execute(
            "insert into orchestration_v2_projection_messages values (?,?,?,?)",
            (tid, role, created_at, json.dumps(payload)),
        )
        con.commit()
        con.close()


@contextlib.contextmanager
def mock_stdout():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        yield out


class ChatscanTest(ChatscanBase):
    def test_waiting_message_filed_once(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="We finished step 1. Should I run the migration now?")

        # First run: files an item
        code, out, err = run_cli("chatscan")
        self.assertEqual(code, 0, err)
        self.assertIn("filed a1: Answer acme-ds/migration: it asked you in chat", out)

        box = store.inbox()
        self.assertEqual(len(box["items"]), 1)
        self.assertEqual(box["items"][0]["id"], "a1")
        self.assertEqual(box["items"][0]["project"], "ds")

        state = store.read("chatscan.json", {})
        self.assertIn("sess-1", state)

        # Second run: files nothing
        code, out2, err2 = run_cli("chatscan")
        self.assertEqual(code, 0, err2)
        self.assertEqual(out2.strip(), "")
        box = store.inbox()
        self.assertEqual(len(box["items"]), 1)

    def test_message_naming_open_item_not_filed_but_resolved_id_is(self):
        # another thread owns open item d1; naming it means the question is already in the inbox
        store.add_ask_item(project="ds", kind="urgent", title="Q", short="", context="", options=[["a", "A"]],
                           reco=None, why="", sender="xx", thread="other")
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="Please approve this change. Next: answer d1 in the inbox.")
        self.assertEqual(run_cli("chatscan")[1].strip(), "")
        # an id that is no longer open does not hide a live question
        self.add_thread("sess-2", title="acme-ds/other")
        self.add_run("sess-2", status="completed")
        self.add_message("sess-2", role="assistant", text="d163 is done. Should I start PR 1?")
        self.assertIn("filed", run_cli("chatscan")[1])

    def test_newer_message_after_question_not_filed(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="Should I proceed?")
        self.add_message("sess-1", role="user", text="note from another agent", created_at="2026-10-27T13:59:00Z")
        self.assertEqual(run_cli("chatscan")[1].strip(), "")

    def test_agent_saying_nothing_waits_not_filed(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="I don't need a decision from you, nothing waits on it. "
                         'If you want that draft, reply "draft it".')
        self.assertEqual(run_cli("chatscan")[1].strip(), "")

    def test_nothing_with_words_between_waiting_not_filed(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="Option b is applied. Nothing on this question is waiting on you.")
        self.assertEqual(run_cli("chatscan")[1].strip(), "")
        # "nothing" in an earlier sentence does not cancel a real question.
        self.add_message("sess-1", role="assistant", text="Nothing else changed. I am waiting on you to merge it.",
                         created_at="2026-10-27T13:49:30Z")
        self.assertIn("filed a1", run_cli("chatscan")[1])

    def test_nothing_then_waiting_on_your_review_still_filed(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="Nothing is merged yet and the PR is waiting on your review, approve?")
        self.assertIn("filed a1", run_cli("chatscan")[1])

    def test_thread_with_open_item_not_filed(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="Which option do you prefer?")

        # Add open item for this thread in inbox
        store.add_ask_item(
            project="ds",
            kind="action",
            title="Existing question",
            short="Action for you",
            context="Some context",
            options=[["a", "Option A"], ["b", "Option B"]],
            reco="a",
            why="",
            sender="agent",
            thread="sess-1",
        )
        self.assertEqual(len(store.inbox()["items"]), 1)

        code, out, err = run_cli("chatscan")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "")
        # Inbox still has only the 1 existing item
        self.assertEqual(len(store.inbox()["items"]), 1)

    def test_running_thread_not_filed(self):
        for status in ("running", "starting", "queued", "waiting"):
            self.setUp()
            self.add_thread("sess-1", title="acme-ds/migration")
            self.add_run("sess-1", status=status)
            self.add_message("sess-1", role="assistant", text="Should I run this?")

            code, out, err = run_cli("chatscan")
            self.assertEqual(code, 0, err)
            self.assertEqual(out.strip(), "")
            self.assertEqual(len(store.inbox()["items"]), 0)

    def test_non_question_message_not_filed(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="Completed all tasks successfully.")

        code, out, err = run_cli("chatscan")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "")
        self.assertEqual(len(store.inbox()["items"]), 0)

    def test_subagent_delegate_thread_not_filed(self):
        # A delegate thread (role 2)
        self.add_thread("thread:delegated-task:worker-1", title="acme-ds/worker")
        self.add_run("thread:delegated-task:worker-1", status="completed")
        self.add_message("thread:delegated-task:worker-1", role="assistant", text="Should I continue?")

        # A scheduled-task thread (thread:project:...)
        self.add_thread("thread:project:p1:run1", title="acme-ds/scheduled")
        self.add_run("thread:project:p1:run1", status="completed")
        self.add_message("thread:project:p1:run1", role="assistant", text="Should I continue?")

        code, out, err = run_cli("chatscan")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "")
        self.assertEqual(len(store.inbox()["items"]), 0)

    def test_dry_run_writes_nothing(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="What is your decision?")

        code, out, err = run_cli("chatscan", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("would file: Answer acme-ds/migration: it asked you in chat", out)

        # Nothing written to inbox or chatscan.json
        self.assertEqual(len(store.inbox()["items"]), 0)
        state = store.read("chatscan.json", {})
        self.assertEqual(state, {})

    def test_no_project_and_no_chatscan_project_skipped(self):
        # Title matches no factory project
        self.add_thread("sess-1", title="other-unmatched-repo/feature")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="What is your approval?")

        # Without config chatscan.project: skipped, prints why
        code, out, err = run_cli("chatscan")
        self.assertEqual(code, 0, err)
        self.assertIn("no visible project and chatscan.project unset", out)
        self.assertEqual(len(store.inbox()["items"]), 0)

        # Set config chatscan.project: now it files successfully to that fallback project
        with mock_stdout():
            cli.main(["config", "set", "chatscan.project", "ds"])

        code, out, err = run_cli("chatscan")
        self.assertEqual(code, 0, err)
        self.assertIn("filed a1: Answer other-unmatched-repo/feature: it asked you in chat", out)
        box = store.inbox()
        self.assertEqual(len(box["items"]), 1)
        self.assertEqual(box["items"][0]["project"], "ds")

    def test_hidden_project_falls_back_to_chatscan_project(self):
        # "ds" resolves from the title but has no phases: the page would hide its items
        with mock_stdout():
            cli.main(["project", "add", "dz", "--product", "bk", "--kind", "build", "--name", "DZ"])
            cli.main(["project", "set", "dz", "--match", "acme-dz"])
        self.add_thread("sess-h", title="acme-dz/feat")
        self.add_run("sess-h", status="completed")
        self.add_message("sess-h", role="assistant", text="Should I ship it?")
        code, out, err = run_cli("chatscan")
        self.assertIn("no visible project", out)
        with mock_stdout():
            cli.main(["config", "set", "chatscan.project", "ds"])
        code, out, err = run_cli("chatscan")
        self.assertEqual(store.inbox()["items"][0]["project"], "ds")

    def test_filed_item_has_thread_set_so_relay_can_deliver(self):
        self.add_thread("sess-xyz", title="acme-ds/feat")
        self.add_run("sess-xyz", status="completed")
        self.add_message("sess-xyz", role="assistant", text="Please approve the deployment plan.")

        code, out, err = run_cli("chatscan")
        self.assertEqual(code, 0, err)

        box = store.inbox()
        self.assertEqual(len(box["items"]), 1)
        item = box["items"][0]
        self.assertEqual(item["thread"], "sess-xyz")
        self.assertEqual(item["kind"], "action")
        self.assertEqual(item["from"], "scan")
        self.assertEqual(item["reco"], "a")
        self.assertEqual(
            item["options"],
            [
                ["a", "Reply (write your answer in the note)"],
                ["b", "Not needed: carry on without me"],
            ],
        )

    def test_cli_verbose_and_plain_output(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="running")
        self.add_message("sess-1", role="assistant", text="Should I run this?")

        # Plain output: skipped running thread is silent
        code, out, err = run_cli("chatscan")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "")

        # Verbose output: shows skipped reason
        code, out_v, err_v = run_cli("chatscan", "--verbose")
        self.assertEqual(code, 0, err_v)
        self.assertIn("skipped Answer acme-ds/migration: it asked you in chat: run active (running)", out_v)

    def test_cli_json_output(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="Do you want me to proceed?")

        code, obj, err = run_json("chatscan", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(obj["would_file"]), 1)
        self.assertEqual(obj["would_file"][0]["thread"], "sess-1")
        self.assertEqual(obj["would_file"][0]["project"], "ds")

    def test_context_whitespace_collapsed_and_truncated(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        long_text = "start " + ("word " * 100) + "\n\nLine 1\n\nLine 2   multiple spaces?   "
        self.add_message("sess-1", role="assistant", text=long_text)

        code, out, err = run_cli("chatscan")
        self.assertEqual(code, 0, err)

        item = store.inbox()["items"][0]
        self.assertNotIn("\n\n", item["context"])
        self.assertNotIn("   ", item["context"])
        self.assertTrue(len(item["context"]) <= 400)
        self.assertTrue(item["context"].endswith("Line 1 Line 2 multiple spaces?"))


class ChatscanCloseTest(ChatscanBase):
    def test_unanswered_scan_item_closes_when_session_moves_on(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="Should I proceed?")
        self.assertIn("filed", run_cli("chatscan")[1])
        self.add_message("sess-1", role="user", text="yes go", created_at="2026-10-27T13:55:00Z")
        out = run_cli("chatscan")[1]
        self.assertIn("closed", out)
        self.assertEqual(store.inbox()["items"], [])

    def test_idle_compact_message_neither_hides_nor_closes(self):
        self.add_thread("sess-1", title="acme-ds/migration")
        self.add_run("sess-1", status="completed")
        self.add_message("sess-1", role="assistant", text="Should I proceed?", created_at="2026-10-27T12:00:00Z")
        con = self.db()
        con.execute("insert into orchestration_v2_projection_messages values (?,?,?,?)",
                    ("sess-1", "user", "2026-10-27T12:58:00Z",
                     json.dumps({"text": "/compact", "idleCompaction": "beforeCacheExpires"})))
        con.commit()
        con.close()
        self.assertIn("filed", run_cli("chatscan")[1])
        out = run_cli("chatscan")[1]
        self.assertNotIn("closed", out)
        self.assertEqual(len(store.inbox()["items"]), 1)

