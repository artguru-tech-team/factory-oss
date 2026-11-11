import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

from factory import assign, cli, config, relay, serve, store

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


ROWS = [
    {"hc": "1A01", "thread": "free-1", "canReceive": True, "project": None, "state": "done", "task": "free one", "since": iso(NOW)},
    {"hc": "2B14", "thread": "busy-1", "canReceive": True, "project": "pp", "state": "running", "task": "busy", "since": iso(NOW - timedelta(hours=2))},
    {"hc": "3C99", "thread": "thread:provider:claudeAgent:sub-1", "canReceive": False, "project": None, "state": "running", "task": "sub", "since": iso(NOW)},
    {"hc": "4D00", "thread": "disp-1", "canReceive": True, "project": None, "state": "done", "task": "dispatcher", "since": iso(NOW)},
    {"hc": "5X99", "thread": None, "canReceive": False, "project": None, "state": "running", "task": "outside T3", "since": iso(NOW)},
]


class AssignBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_HOME")
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["product", "set", "bk", "--name", "Acme", "--workspace", "/w"])
            cli.main(["project", "add", "ds", "--product", "bk", "--kind", "build", "--name", "Design system"])
            cli.main(["project", "add", "pp", "--product", "bk", "--kind", "build", "--name", "Payments"])
        self.rows = [dict(r) for r in ROWS]
        for target in ("factory.t3.connect", "factory.relay.trigger_delivery_async"):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("factory.t3.agents", side_effect=lambda *a, **k: (self.rows, {}))
        p.start()
        self.addCleanup(p.stop)
        # T3 has these threads open; "idle-disp" is idle for days, so it is not in the agents rows
        self.open_threads = {"free-1", "busy-1", "disp-1", "idle-disp"}
        p = mock.patch("factory.relay.is_thread_live", side_effect=lambda t: t in self.open_threads)
        p.start()
        self.addCleanup(p.stop)
        self.key = store.key()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def post(self, body):
        r = urllib.request.Request(f"{self.base}/api/assign?k={self.key}", data=json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class AssignApiTest(AssignBase):
    def new(self, **kw):
        return self.post({"project": "ds", "mode": "new", "provider": "claude", "brief": "Do the thing", **kw})

    def existing(self, **kw):
        return self.post({"project": "ds", "mode": "existing", "thread": "free-1", "brief": "Take over", **kw})

    def test_new_asks_the_dispatcher_and_records_pending(self):
        config.set_value("assign.dispatcher", "disp-1")
        code, entry = self.new(provider="opencode")
        self.assertEqual(code, 200)
        self.assertEqual((entry["thread"], entry["state"]), ("disp-1", "queued"))
        for needle in ("ds", "Design system", "opencode", "Do the thing", "factory agent assign ds --thread <new thread id>"):
            self.assertIn(needle, entry["raw"])
        pend = store.read(assign.PENDING_REL, {})["ds"]
        self.assertEqual((pend["provider"], pend["msgId"]), ("opencode", entry["id"]))
        self.assertEqual(store.thread_links(), {})  # the dispatcher links the new thread, not the page
        self.assertEqual(relay.load_outbox()[0]["id"], entry["id"])

    def test_new_offline_is_409_with_the_fix(self):
        code, body = self.new()  # unset
        self.assertEqual((code, body["fix"]), (409, "factory config set assign.dispatcher <thread>"))
        config.set_value("assign.dispatcher", "not-live")
        self.assertEqual(self.new()[0], 409)
        config.set_value("assign.dispatcher", "thread:provider:claudeAgent:sub-1")  # a subagent cannot dispatch
        self.assertEqual(self.new()[0], 409)
        self.assertEqual(relay.load_outbox(), [])
        self.assertEqual(store.read(assign.PENDING_REL, {}), {})

    def test_new_idle_dispatcher_is_online(self):
        config.set_value("assign.dispatcher", "idle-disp")  # open in T3, quiet for days
        self.assertEqual(self.new()[0], 200)

    def test_new_twice_is_409_unless_forced(self):
        config.set_value("assign.dispatcher", "disp-1")
        self.assertEqual(self.new()[0], 200)
        code, body = self.new()
        self.assertEqual((code, body.get("pending")), (409, True))
        self.assertIn("already starting", body["error"])
        self.assertEqual(len(relay.load_outbox()), 1)
        self.assertEqual(self.new(force=True)[0], 200)  # the owner asked for a second agent
        self.assertEqual(len(relay.load_outbox()), 2)

    def test_retry_for_an_idle_dispatcher(self):
        config.set_value("assign.dispatcher", "idle-disp")  # open in T3, not in the agents rows
        _, entry = self.new()
        with store.lock():
            box = relay.load_outbox()
            box[0]["state"] = "failed"
            store.write(relay.RELAY_REL, box)
        self.assertEqual(serve.save_message({"retry": entry["id"]})[0], 200)
        self.open_threads.discard("idle-disp")  # gone from T3: no retry
        with store.lock():
            box = relay.load_outbox()
            box[0]["state"] = "failed"
            store.write(relay.RELAY_REL, box)
        self.assertEqual(serve.save_message({"retry": entry["id"]})[0], 400)

    def test_existing_link_to_a_removed_project_does_not_block(self):
        with store.lock():
            store.set_thread_link("free-1", "removed")
        self.assertEqual(self.existing()[0], 200)
        self.assertEqual(store.thread_links(), {"free-1": "ds"})

    def test_new_corrupt_pending_queues_nothing(self):
        config.set_value("assign.dispatcher", "disp-1")
        with open(store.path(assign.PENDING_REL), "w") as f:
            f.write("{not json")
        self.assertEqual(self.new()[0], 500)
        self.assertEqual(relay.load_outbox(), [])  # no dispatcher message without its pending row

    def test_existing_links_and_briefs(self):
        code, entry = self.existing()
        self.assertEqual((code, entry["thread"], entry["raw"]), (200, "free-1", "Take over"))
        self.assertEqual(store.thread_links(), {"free-1": "ds"})

    def test_existing_resend_to_the_same_project(self):
        # Edit brief after a failed delivery: the link stands, the rows now say "ds"; a re-send is not a conflict
        self.assertEqual(self.existing()[0], 200)
        self.rows[0]["project"] = "ds"
        code, entry = self.existing(brief="Take over, edited")
        self.assertEqual((code, entry["raw"]), (200, "Take over, edited"))
        self.assertEqual(len(relay.load_outbox()), 2)

    def test_existing_link_written_since_the_rows_is_409(self):
        with store.lock():
            store.set_thread_link("free-1", "pp")  # another tab, after this request read the rows
        code, body = self.existing()
        self.assertEqual(code, 409)
        self.assertIn("'pp'", body["error"])
        self.assertEqual(store.thread_links(), {"free-1": "pp"})
        self.assertEqual(relay.load_outbox(), [])

    def test_existing_refusals_write_nothing(self):
        self.assertEqual(self.existing(thread="nope")[0], 400)
        self.assertEqual(self.existing(thread="5X99")[0], 400)  # outside T3
        code, body = self.existing(thread="thread:provider:claudeAgent:sub-1")
        self.assertEqual(code, 400)
        self.assertIn("subagents", body["error"])
        code, body = self.existing(thread="busy-1")  # already on another project
        self.assertEqual(code, 409)
        self.assertIn("already works on project 'pp'", body["error"])
        config.set_value("assign.dispatcher", "disp-1")
        code, body = self.existing(thread="disp-1")
        self.assertEqual(code, 400)
        self.assertIn("dispatcher", body["error"])
        self.assertEqual(store.thread_links(), {})
        self.assertEqual(relay.load_outbox(), [])

    def test_validation_4xx(self):
        config.set_value("assign.dispatcher", "disp-1")
        self.assertEqual(self.post([])[0], 400)
        self.assertEqual(self.new(brief="")[0], 400)
        self.assertEqual(self.new(brief="  \n ")[0], 400)
        self.assertEqual(self.new(brief="x" * 2001)[0], 400)
        self.assertEqual(self.new(brief=5)[0], 400)
        self.assertEqual(self.new(mode="other")[0], 400)
        self.assertEqual(self.new(provider="gpt")[0], 400)
        self.assertEqual(self.new(provider=None)[0], 400)
        self.assertEqual(self.new(project="nope")[0], 404)
        self.assertEqual(self.new(project=None)[0], 404)
        self.assertEqual(self.new(brief="x" * 2000)[0], 200)
        self.assertEqual(len(relay.load_outbox()), 1)

    def test_retry_reuses_the_messages_endpoint(self):
        config.set_value("assign.dispatcher", "disp-1")
        _, entry = self.new()
        with store.lock():
            box = relay.load_outbox()
            box[0]["state"] = "failed"
            store.write(relay.RELAY_REL, box)
        self.assertEqual(serve.save_message({"retry": entry["id"]})[0], 200)
        box = relay.load_outbox()
        self.assertEqual(len(box), 1)  # the same entry, not a second dispatcher message
        self.assertEqual((box[0]["id"], box[0]["state"], box[0]["sends"]), (entry["id"], "queued", 1))


class AssignDataTest(AssignBase):
    def test_block(self):
        d = assign.data(self.rows, NOW)
        self.assertEqual((d["dispatcher"], d["providers"]), ("offline", ["claude", "antigravity", "opencode"]))
        self.assertEqual([f["thread"] for f in d["free"]], ["free-1", "disp-1"])  # dispatcher not yet known
        self.assertNotIn("busy-1", [f["thread"] for f in d["free"]])
        config.set_value("assign.dispatcher", "disp-1")
        with mock.patch("factory.relay.is_thread_live", return_value=None):  # T3 unreadable: still never "free"
            self.assertNotIn("disp-1", [f["thread"] for f in assign.data(self.rows, NOW)["free"]])
        config.set_value("assign.dispatcher", "")
        config.set_value("assign.dispatcher", "disp-1")
        d = assign.data(self.rows, NOW)
        self.assertEqual(d["dispatcher"], "online")
        self.assertEqual(d["free"], [{"thread": "free-1", "hc": "1A01", "title": "free one", "state": "done"}])
        config.set_value("assign.dispatcher", "gone")
        self.assertEqual(assign.data(self.rows, NOW)["dispatcher"], "offline")

    def test_pending_lifecycle(self):
        def put(minutes, project="ds"):
            store.write(assign.PENDING_REL, {project: {"provider": "claude", "at": iso(NOW - timedelta(minutes=minutes)), "msgId": "m"}})
        put(5)
        self.assertEqual(assign.data(self.rows, NOW)["pending"]["ds"]["stale"], False)
        put(31)
        self.assertEqual(assign.data(self.rows, NOW)["pending"]["ds"]["stale"], True)
        put(24 * 60 + 1)
        self.assertEqual(assign.data(self.rows, NOW)["pending"], {})  # dropped after 24 h
        # another agent on the project taking a turn does not answer the ask: only the dispatcher's link does
        put(5)
        self.rows[1].update(project="ds", since=iso(NOW))
        self.assertIn("ds", assign.data(self.rows, NOW)["pending"])
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["agent", "assign", "ds", "--thread", "new-1"])
        self.assertEqual(assign.data(self.rows, NOW)["pending"], {})
        # an unreadable entry is skipped, never breaks the build
        store.write(assign.PENDING_REL, {"ds": "junk", "pp": {"at": "not a date"}})
        self.assertEqual(assign.data(self.rows, NOW)["pending"], {})

    def test_build_carries_the_block(self):
        from factory import build
        with mock.patch.object(build, "agents_data", return_value=(self.rows, {}, {}, {}, [])), \
             mock.patch.object(build.sources, "prs_for", return_value={}), \
             mock.patch.object(build.t3, "thread_handles", return_value={}):
            data = build.collect(NOW)
        self.assertEqual(data["assign"]["dispatcher"], "offline")
        self.assertEqual(data["assign"]["free"][0]["thread"], "free-1")
