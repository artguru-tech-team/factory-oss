import hashlib
import hmac
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from factory import config as config_mod
from factory import build, cli, office, office_public, store


class TestOfficePublic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev_home = os.environ.get("FACTORY_HOME")
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if self.prev_home is not None:
            os.environ["FACTORY_HOME"] = self.prev_home
        else:
            os.environ.pop("FACTORY_HOME", None)
        self.tmp.cleanup()

    def test_only_allowlisted_fields(self):
        salt = b"test-salt-bytes-1234567890123456"
        data = {
            "builtAt": "2026-11-09T00:00:00Z",
            "products": {
                "web": {
                    "name": "Web",
                    "repo": "private/web-repo",
                    "workspace": "/srv/workspace",
                }
            },
            "projects": [{"id": "p1", "name": "Secret Project"}],
            "machine": {"id": "box-1", "label": "Secret Host"},
            "tokens": {"total": 123456},
            "agents": [
                {
                    "hc": "1A01",
                    "who": "Agent One",
                    "thread": "orch-1",
                    "parent": None,
                    "product": "web",
                    "project": "p1",
                    "state": "running",
                    "since": "2026-11-09T00:00:00Z",
                    "provider": "claude",
                    "model": "claude-3-opus",
                    "via": "factory CLI",
                    "t3Status": "active",
                    "task": "web/feat/cool-thing",
                    "canReceive": True,
                    "disabledHint": None,
                }
            ],
        }
        feed = office_public.build_public_feed(data, salt)

        self.assertEqual(set(feed.keys()), {"builtAt", "products", "agents"})
        self.assertEqual(set(feed["products"]["web"].keys()), {"name"})
        self.assertEqual(feed["products"]["web"]["name"], "Web")

        self.assertEqual(len(feed["agents"]), 1)
        agent = feed["agents"][0]
        self.assertEqual(
            set(agent.keys()),
            {"id", "parent", "product", "state", "provider", "since", "label"},
        )

    def test_labels_from_table(self):
        salt = b"test-salt-bytes-1234567890123456"
        data = {
            "builtAt": "2026-11-09T00:00:00Z",
            "products": {
                "web": {"name": "Web"},
                "app": {"name": "App"},
            },
            "agents": [
                {
                    "hc": "1A01",
                    "product": "web",
                    "state": "running",
                    "task": "web/feat/x",
                },
                {
                    "hc": "1B02",
                    "product": "app",
                    "state": "running",
                    "task": "c2e/fix/y",
                },
                {
                    "hc": "1C03",
                    "product": "web",
                    "state": "running",
                    "task": "platform/wip/z",
                },
                {
                    "hc": "1D04",
                    "product": "web",
                    "state": "running",
                    "task": "plain",
                },
                {
                    "hc": "1E05",
                    "product": "web",
                    "state": "running",
                    "task": "",
                },
                {
                    "hc": "1F06",
                    "product": "web",
                    "state": "running",
                    "task": "web/refactor/cleanup",
                },
                {
                    "hc": "1G07",
                    "product": "web",
                    "state": "running",
                    "task": "web/chore/dep",
                },
                {
                    "hc": "1H08",
                    "product": "web",
                    "state": "running",
                    "task": "web/docs/api",
                },
                {
                    "hc": "1I09",
                    "product": "web",
                    "state": "running",
                    "task": "web/perf/speed",
                },
                {
                    "hc": "1J10",
                    "product": "web",
                    "state": "running",
                    "task": "web/test/e2e",
                },
            ],
        }
        feed = office_public.build_public_feed(data, salt)
        labels = [a["label"] for a in feed["agents"]]
        self.assertEqual(labels[0], "Building a feature for Web")
        self.assertEqual(labels[1], "Fixing a bug on App")
        self.assertEqual(labels[2], "Working on Web")
        self.assertEqual(labels[3], "Working on Web")
        self.assertEqual(labels[4], "Working on Web")
        self.assertEqual(labels[5], "Cleaning up code in Web")
        self.assertEqual(labels[6], "Maintenance on Web")
        self.assertEqual(labels[7], "Writing docs for Web")
        self.assertEqual(labels[8], "Speeding up Web")
        self.assertEqual(labels[9], "Writing tests for Web")

    def test_dropped_rows(self):
        salt = b"test-salt-bytes-1234567890123456"
        data = {
            "builtAt": "2026-11-09T00:00:00Z",
            "products": {
                "web": {"name": "Web"},
            },
            "agents": [
                # Dropped states
                {"hc": "F01", "product": "web", "state": "failed", "task": "web/feat/a"},  # no since: dropped
                {"hc": "Q02", "product": "web", "state": "quota", "task": "web/feat/b"},
                {"hc": "U03", "product": "web", "state": "unknown", "task": "web/feat/c"},
                # Probe task dropped
                {"hc": "P04", "product": "web", "state": "running", "task": "probe"},
                {"hc": "P05", "product": "web", "state": "running", "task": "  probe  "},
                # Product missing from products dropped
                {"hc": "M06", "product": "missing_prod", "state": "running", "task": "missing/feat/d"},
                # Waiting kept as waiting
                {"hc": "W07", "product": "web", "state": "waiting", "task": "web/feat/e"},
                # Running kept as running
                {"hc": "R08", "product": "web", "state": "running", "task": "web/feat/f"},
                # Stalled kept as stalled
                {"hc": "S09", "product": "web", "state": "stalled", "task": "web/feat/g"},
                # Done long ago: dropped
                {"hc": "D10", "product": "web", "state": "done", "since": "2026-11-08T17:59:59Z", "task": "web/feat/h"},
            ],
        }
        feed = office_public.build_public_feed(data, salt)
        states = [a["state"] for a in feed["agents"]]
        self.assertEqual(states, ["waiting", "running", "stalled"])

    def test_odd_rows_do_not_break_the_feed(self):
        salt = b"test-salt-bytes-1234567890123456"
        data = {
            "builtAt": "2026-11-09T00:00:00Z",
            "products": {"web": {"name": None}},
            "agents": [
                {"hc": "X1", "product": "web", "state": "running", "task": 7, "provider": ["x"],
                 "parent": ["y"], "since": {"host": "vps-1"}},
                {"thread": 42, "hc": "X2", "product": ["web"], "state": "running"},
                {"hc": "X3", "product": "web", "state": "running", "since": "not a date"},
                {"hc": "X3", "product": "web", "state": "running"},
                "not a row",
            ],
        }
        feed = office_public.build_public_feed(data, salt)
        self.assertEqual(feed["products"], {"web": {"name": "web"}})
        self.assertEqual(len(feed["agents"]), 2)  # X2 has a list product, the second X3 is a duplicate
        first = feed["agents"][0]
        self.assertEqual((first["label"], first["provider"], first["parent"], first["since"]),
                         ("Working on web", "AI agent", None, None))
        self.assertIsNone(feed["agents"][1]["since"])
        self.assertNotIn("vps-1", json.dumps(feed))

    def test_recently_finished_agents_stay_idle(self):
        salt = b"test-salt-bytes-1234567890123456"
        data = {
            "builtAt": "2026-11-09T12:00:00Z",
            "products": {"web": {"name": "Web"}},
            "agents": [
                {"hc": "F0", "product": "web", "state": "failed", "since": "2026-11-09T09:00:00Z", "task": "web/fix/z"},
                {"hc": "D0", "product": "web", "state": "done", "since": "2026-11-09T12:00:00Z", "task": "web/fix/y"},
                {"hc": "D1", "product": "web", "state": "done", "since": "2026-11-09T11:00:00Z", "task": "web/fix/a"},
                {"hc": "F2", "product": "web", "state": "failed", "since": "2026-11-09T06:00:00Z", "task": "web/feat/secret-name"},
                {"hc": "D3", "product": "web", "state": "done", "since": "2026-11-09T05:59:59Z", "task": "web/feat/c"},
                {"hc": "D4", "product": "web", "state": "done", "since": "2026-11-09T13:00:00Z", "task": "web/feat/d"},
                {"hc": "F5", "product": "web", "state": "failed", "since": "not a date", "task": "web/feat/e"},
            ],
        }
        agents = office_public.build_public_feed(data, salt)["agents"]
        self.assertEqual([a["state"] for a in agents], ["idle"] * 4)
        # newest first, failed or not: input order (failed rows first) must not survive
        self.assertEqual([a["since"] for a in agents], ["2026-11-09T12:00:00Z", "2026-11-09T11:00:00Z",
                                                        "2026-11-09T09:00:00Z", "2026-11-09T06:00:00Z"])
        self.assertEqual({a["label"] for a in agents}, {"Between tasks on Web"})  # failed looks like done
        self.assertEqual({tuple(sorted(a)) for a in agents}, {tuple(sorted(agents[0]))})

    def test_idle_capped_per_room_newest_kept(self):
        salt = b"test-salt-bytes-1234567890123456"
        rows = [{"hc": f"W{i}", "product": "web", "state": "failed", "since": f"2026-11-09T1{i}:00:00Z"} for i in range(6)]
        rows += [{"hc": "A1", "product": "api", "state": "done", "since": "2026-11-09T10:00:00Z"},
                 {"hc": "R1", "product": "web", "state": "running", "parent": "W0"}]
        data = {"builtAt": "2026-11-09T16:00:00Z", "products": {"web": {"name": "Web"}, "api": {"name": "Api"}}, "agents": rows}
        agents = office_public.build_public_feed(data, salt)["agents"]
        web_idle = [a["since"] for a in agents if a["product"] == "web" and a["state"] == "idle"]
        self.assertEqual(web_idle, [f"2026-11-09T1{i}:00:00Z" for i in (5, 4, 3, 2)])
        self.assertEqual([a["product"] for a in agents if a["state"] == "idle"].count("api"), 1)
        self.assertIsNone(next(a for a in agents if a["state"] == "running")["parent"])  # W0 was capped out

    def test_probe_variants_dropped(self):
        salt = b"test-salt-bytes-1234567890123456"
        rows = [{"hc": f"P{i}", "product": "web", "state": "running", "task": t}
                for i, t in enumerate(["probe", "probe ag2", " probe-4", "PROBE x", "probe_ag2", "probes/feat/x", "web/fix/probe"])]
        data = {"builtAt": "2026-11-09T16:00:00Z", "products": {"web": {"name": "Web"}}, "agents": rows}
        self.assertEqual(len(office_public.build_public_feed(data, salt)["agents"]), 2)  # probes/feat/x, web/fix/probe

    def test_odd_timestamps_drop_the_row_not_the_office(self):
        salt = b"test-salt-bytes-1234567890123456"
        data = {"builtAt": "2026-11-09T12:00:00Z", "products": {"web": {"name": "Web"}},
                "agents": [{"hc": "O1", "product": "web", "state": "done", "since": "0001-01-01T00:30:00+01:00"},
                           {"hc": "R1", "product": "web", "state": "running", "since": "2026-11-09T11:00:00Z"}]}
        agents = office_public.build_public_feed(data, salt)["agents"]
        self.assertEqual([a["state"] for a in agents], ["running"])
        data["builtAt"] = 12345  # not a string: idle rows dropped, live rows kept
        data["agents"][0]["since"] = "2026-11-09T11:00:00Z"
        self.assertEqual([a["state"] for a in office_public.build_public_feed(data, salt)["agents"]], ["running"])

    def test_since_is_normalised(self):
        salt = b"test-salt-bytes-1234567890123456"
        data = {"builtAt": "2026-11-09T00:00:00Z", "products": {"web": {"name": "Web"}},
                "agents": [{"hc": "S1", "product": "web", "state": "running", "since": "2026-11-09T06:32:03+05:30"}]}
        self.assertEqual(office_public.build_public_feed(data, salt)["agents"][0]["since"], "2026-11-09T01:02:03Z")

    def test_ids_stable_and_hashed(self):
        salt = b"test-salt-bytes-1234567890123456"
        data = {
            "builtAt": "2026-11-09T00:00:00Z",
            "products": {"web": {"name": "Web"}},
            "agents": [
                {"hc": "1A01", "thread": "orch-thread-1", "product": "web", "state": "running"},
                {"hc": "1B02", "product": "web", "state": "running"},
            ],
        }
        feed1 = office_public.build_public_feed(data, salt)
        feed2 = office_public.build_public_feed(data, salt)

        # Same thread -> same id across calls
        id1_first = feed1["agents"][0]["id"]
        id1_second = feed2["agents"][0]["id"]
        self.assertEqual(id1_first, id1_second)

        # 10 hex characters
        self.assertTrue(re.fullmatch(r"[0-9a-f]{10}", id1_first))

        # Row with no thread uses hc
        id2_first = feed1["agents"][1]["id"]
        self.assertTrue(re.fullmatch(r"[0-9a-f]{10}", id2_first))
        expected_id2 = hmac.new(salt, b"1B02", hashlib.sha256).hexdigest()[:10]
        self.assertEqual(id2_first, expected_id2)

        # Neither thread id nor hc appears in json output
        feed_str = json.dumps(feed1)
        self.assertNotIn("orch-thread-1", feed_str)
        self.assertNotIn("1A01", feed_str)
        self.assertNotIn("1B02", feed_str)

    def test_parent_mapped(self):
        salt = b"test-salt-bytes-1234567890123456"
        data = {
            "builtAt": "2026-11-09T00:00:00Z",
            "products": {"web": {"name": "Web"}},
            "agents": [
                # Top level row with parent None
                {"hc": "P01", "thread": "t-parent", "parent": None, "product": "web", "state": "running"},
                # Subagent row with parent != own hc
                {"hc": "C02", "thread": "t-child", "parent": "P01", "product": "web", "state": "running"},
                # Top level row with parent == own hc
                {"hc": "P03", "thread": "t-self-parent", "parent": "P03", "product": "web", "state": "running"},
            ],
        }
        feed = office_public.build_public_feed(data, salt)
        self.assertEqual(len(feed["agents"]), 3)
        parent_id = feed["agents"][0]["id"]
        self.assertIsNone(feed["agents"][0]["parent"])
        self.assertEqual(feed["agents"][1]["parent"], parent_id)
        self.assertIsNone(feed["agents"][2]["parent"])

    def test_provider_names(self):
        salt = b"test-salt-bytes-1234567890123456"
        data = {
            "builtAt": "2026-11-09T00:00:00Z",
            "products": {"web": {"name": "Web"}},
            "agents": [
                {"hc": "A1", "product": "web", "state": "running", "provider": "claude", "model": "claude-3-5"},
                {"hc": "A2", "product": "web", "state": "running", "provider": "opencode", "model": "qwen"},
                {"hc": "A3", "product": "web", "state": "running", "provider": "weird", "model": "custom-model"},
                {"hc": "A4", "product": "web", "state": "running", "provider": "Antigravity"},
                {"hc": "A5", "product": "web", "state": "running", "provider": "Codex"},
            ],
        }
        feed = office_public.build_public_feed(data, salt)
        providers = [a["provider"] for a in feed["agents"]]
        self.assertEqual(providers, ["Claude", "OpenCode", "AI agent", "Antigravity", "Codex"])
        feed_str = json.dumps(feed)
        self.assertNotIn("model", feed_str)
        self.assertNotIn("claude-3-5", feed_str)
        self.assertNotIn("qwen", feed_str)
        self.assertNotIn("custom-model", feed_str)

    def test_no_private_value_leaks(self):
        salt = b"test-salt-bytes-1234567890123456"
        data = {
            "builtAt": "2026-11-09T00:00:00Z",
            "products": {
                "web": {
                    "name": "Web",
                    "repo": "private-org/secret-repo",
                    "workspace": "/opt/secret/path",
                }
            },
            "projects": [
                {
                    "id": "bk-paypal-fr",
                    "name": "PayPal FR Project",
                    "next": ["top up Pandora +$70"],
                    "prs": ["org/repo#12"],
                }
            ],
            "machine": {"id": "vps-box-99", "label": "secret-machine-label"},
            "tokens": {"total": 987654321},
            "levers": [{"title": "Secret Lever Title", "options": []}],
            "inbox": {"items": [{"title": "Secret Inbox Item Title"}]},
            "answers": {"ans1": {"choice": "a"}},
            "prs": {"org/repo#12": {"title": "Secret PR"}},
            "log": [{"text": "Secret Log Entry"}],
            "provColors": {"Claude": "var(--secret-prov-color)"},
            "relayConfigured": True,
            "warnings": ["Secret build warning"],
            "agents": [
                {
                    "hc": "SECRET_HC_01",
                    "who": "Secret Agent Name",
                    "thread": "secret-t3-thread-999",
                    "parent": None,
                    "product": "web",
                    "project": "bk-paypal-fr",
                    "state": "running",
                    "since": "2026-11-09T00:00:00Z",
                    "provider": "claude",
                    "model": "claude-3-opus-secret",
                    "via": "factory CLI secret",
                    "t3Status": "active_secret",
                    "task": "web/feat/secret-roadmap",
                    "canReceive": True,
                    "disabledHint": "Secret hint",
                }
            ],
        }
        feed = office_public.build_public_feed(data, salt)
        feed_str = json.dumps(feed)

        # Every string in the input, except the few that are public by design, must stay out.
        public = {"web", "Web", "running", "claude", "a", "2026-11-09T00:00:00Z"}

        def strings(v):
            if isinstance(v, str):
                yield v
            elif isinstance(v, dict):
                for x in v.values():
                    yield from strings(x)
            elif isinstance(v, list):
                for x in v:
                    yield from strings(x)

        input_strings = set(strings(data)) - public
        self.assertIn("top up Pandora +$70", input_strings)
        for val in input_strings:
            self.assertNotIn(val, feed_str)

        # Output keys are an allowlist at every level
        self.assertEqual(set(feed), {"builtAt", "products", "agents"})
        for p in feed["products"].values():
            self.assertEqual(set(p), {"name"})
        for a in feed["agents"]:
            self.assertEqual(set(a), {"id", "parent", "product", "state", "provider", "since", "label"})

    def test_salt_file_created_600(self):
        salt_file = store.path(".public-salt")
        self.assertFalse(os.path.exists(salt_file))

        salt1 = office_public.public_salt()
        self.assertTrue(os.path.exists(salt_file))
        self.assertIsInstance(salt1, bytes)

        # Mode must be 0o600
        mode = os.stat(salt_file).st_mode & 0o777
        self.assertEqual(mode, 0o600)

        # 64 hex chars
        with open(salt_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
        self.assertEqual(len(content), 64)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{64}", content))

        # Second call returns the exact same bytes
        salt2 = office_public.public_salt()
        self.assertEqual(salt1, salt2)

    def test_render_writes_public_feed(self):
        data = {
            "builtAt": "2026-10-27T12:00:00Z",
            "products": {"web": {"name": "Web"}},
            "projects": [],
            "agents": [
                {
                    "hc": "1A01",
                    "product": "web",
                    "state": "running",
                    "task": "web/feat/x",
                }
            ],
            "tokens": {},
            "levers": [],
            "answers": {},
            "prs": {},
            "log": [],
            "provColors": {},
            "warnings": [],
        }
        build.render(data)
        public_path = store.path("site-public", "office-public.json")
        self.assertTrue(os.path.exists(public_path))
        with open(public_path, "r", encoding="utf-8") as f:
            feed = json.load(f)
        self.assertEqual(set(feed.keys()), {"builtAt", "products", "agents"})

    def _render_data(self):
        return {
            "builtAt": "2026-10-27T12:00:00Z",
            "products": {"web": {"name": "Web"}, "stealth": {"name": "Stealth X"}},
            "projects": [], "tokens": {}, "levers": [], "answers": {}, "prs": {}, "log": [],
            "provColors": {}, "warnings": [],
            "agents": [
                {"hc": "1A01", "product": "web", "state": "running", "task": "web/feat/x"},
                {"hc": "1A02", "product": "stealth", "state": "running", "task": "stealth/feat/y"},
            ],
        }

    def _public_feed(self):
        with open(store.path("site-public", "office-public.json"), "r", encoding="utf-8") as f:
            return json.load(f)

    def test_render_publishes_only_allowlisted_products(self):
        config_mod.store.write(config_mod.CONFIG_REL, {"office": {"publicProducts": ["web"]}})
        data = self._render_data()
        build.render(data)
        feed = self._public_feed()
        self.assertEqual(feed["products"], {"web": {"name": "Web"}})
        self.assertEqual([a["product"] for a in feed["agents"]], ["web"])
        self.assertNotIn("Stealth X", json.dumps(feed))
        self.assertNotIn("stealth", json.dumps(feed))

    def test_render_without_allowlist_publishes_nothing_and_warns(self):
        data = self._render_data()
        build.render(data)
        feed = self._public_feed()
        self.assertEqual(feed["products"], {})
        self.assertEqual(feed["agents"], [])
        self.assertTrue(any("office.publicProducts" in w for w in data["warnings"]))

    def test_malformed_salt_is_replaced(self):
        store.write_text(".public-salt", "short\n")
        salt = office_public.public_salt()
        self.assertRegex(salt.decode(), r"^[0-9a-f]{64}$")

    def test_row_without_thread_or_hc_dropped(self):
        data = {"builtAt": "2026-10-27T12:00:00Z", "products": {"web": {"name": "Web"}},
                "agents": [{"product": "web", "state": "running", "task": "web/feat/x"}]}
        self.assertEqual(office_public.build_public_feed(data, b"k" * 32)["agents"], [])

    def test_render_public_feed_failure_warns_and_continues(self):
        data = {
            "builtAt": "2026-10-27T12:00:00Z",
            "products": {"web": {"name": "Web"}},
            "projects": [],
            "agents": [],
            "tokens": {},
            "levers": [],
            "answers": {},
            "prs": {},
            "log": [],
            "provColors": {},
            "warnings": [],
        }
        store.write(os.path.join("site-public", "office-public.json"),
                    {"builtAt": "old", "products": {"gone": {"name": "Gone"}}, "agents": [{"id": "x"}]})
        with mock.patch("factory.office_public.build_public_feed", side_effect=RuntimeError("boom")):
            out = build.render(data)
        with open(store.path("site-public", "office-public.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"builtAt": "2026-10-27T12:00:00Z", "products": {}, "agents": []})
        with open(store.path("site", "factory.html"), encoding="utf-8") as f:
            self.assertIn("public office feed: boom", f.read())
        self.assertTrue(os.path.exists(store.path("site", "factory.html")))
        self.assertTrue(os.path.exists(store.path("site", "office.json")))
        self.assertEqual(out, store.path("site", "factory.html"))
        self.assertTrue(any("public office feed: boom" in w for w in data.get("warnings", [])))

    def test_render_removes_old_feed_when_write_fails(self):
        data = {"builtAt": "2026-10-27T12:00:00Z", "products": {}, "projects": [], "agents": [], "warnings": []}
        feed_path = store.path("site-public", "office-public.json")
        store.write(os.path.join("site-public", "office-public.json"), {"builtAt": "old", "products": {}, "agents": []})
        real_write = store.write

        def failing(rel, obj):
            if rel.startswith("site-public"):
                raise OSError("disk full")
            return real_write(rel, obj)

        with mock.patch("factory.store.write", side_effect=failing):
            build.render(data)
        self.assertFalse(os.path.exists(feed_path))
        self.assertTrue(any("public office feed not written: disk full" in w for w in data["warnings"]))

    def test_install_public_ok(self):
        valid_file = os.path.join(self.tmp.name, "public.html")
        html_bytes = b"<!doctype html><html><head><title>Public Office</title></head><body>Hello</body></html>"
        with open(valid_file, "wb") as f:
            f.write(html_bytes)

        res = office.install_public(valid_file)
        self.assertTrue(res["installed"])
        expected_path = store.path("site-public", "index.html")
        self.assertEqual(res["path"], expected_path)
        self.assertEqual(res["size"], len(html_bytes))

        # Check site-public/index.html was written correctly
        self.assertTrue(os.path.exists(expected_path))
        with open(expected_path, "rb") as f:
            self.assertEqual(f.read(), html_bytes)

        # Check site/office.html was untouched
        self.assertFalse(os.path.exists(store.path("site", "office.html")))

    def test_install_public_rejects_private_bundle(self):
        # file containing api/messages
        f1 = os.path.join(self.tmp.name, "bad1.html")
        with open(f1, "wb") as f:
            f.write(b"<!doctype html><html><script>fetch('/api/messages')</script></html>")
        with self.assertRaises(store.FactoryError) as cm:
            office.install_public(f1)
        self.assertIn("api/messages", str(cm.exception))

        # file containing api/office
        f2 = os.path.join(self.tmp.name, "bad2.html")
        with open(f2, "wb") as f:
            f.write(b"<!doctype html><html><script>const endpoint = 'api/office';</script></html>")
        with self.assertRaises(store.FactoryError) as cm:
            office.install_public(f2)
        self.assertIn("api/office", str(cm.exception))

        # ensure site-public/index.html was not written
        self.assertFalse(os.path.exists(store.path("site-public", "index.html")))

    def test_install_public_checks_size_and_doctype(self):
        # Nonexistent file
        with self.assertRaises(store.FactoryError) as cm:
            office.install_public(os.path.join(self.tmp.name, "nonexistent.html"))
        self.assertIn("file not found", str(cm.exception))

        # Non-HTML (missing doctype)
        bad_file = os.path.join(self.tmp.name, "bad.txt")
        with open(bad_file, "wb") as f:
            f.write(b"<html>no doctype</html>")
        with self.assertRaises(store.FactoryError) as cm:
            office.install_public(bad_file)
        self.assertIn("<!doctype html", str(cm.exception))

        # Over 8 MB
        big_file = os.path.join(self.tmp.name, "big.html")
        with open(big_file, "wb") as f:
            f.write(b"<!doctype html>" + b"0" * (8 * 1024 * 1024 + 10))
        with self.assertRaises(store.FactoryError) as cm:
            office.install_public(big_file)
        self.assertIn("file too large", str(cm.exception))

    def test_install_cli_public_flag(self):
        valid_file = os.path.join(self.tmp.name, "public.html")
        with open(valid_file, "wb") as f:
            f.write(b"<!doctype html><html><title>Public</title></html>")

        # Call cli.office_install with public=True
        args = mock.MagicMock(file=valid_file, public=True)
        res, text = cli.office_install(args)
        self.assertTrue(res["installed"])
        expected_path = store.path("site-public", "index.html")
        self.assertEqual(res["path"], expected_path)
        self.assertEqual(text, f"ok: installed public page {expected_path} ({res['size']} bytes)")
        self.assertTrue(os.path.exists(expected_path))
        self.assertFalse(os.path.exists(store.path("site", "office.html")))

        # Check parser accepts --public
        p = cli.parser()
        parsed = p.parse_args(["office", "install", "--public", valid_file])
        self.assertTrue(parsed.public)
        self.assertEqual(parsed.file, valid_file)

        parsed_no_flag = p.parse_args(["office", "install", valid_file])
        self.assertFalse(parsed_no_flag.public)


if __name__ == "__main__":
    unittest.main()
