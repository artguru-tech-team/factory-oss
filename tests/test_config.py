import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from factory import access_jwt, build, cli, config, skills, sources, store, t3, tokens
from tests import datasource_env

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)


def run_cli(*argv):
    return cli.main(list(argv))


def run_json(*argv):
    a = cli.parser().parse_args(list(argv))
    a.json = True
    obj, text = a.fn(a)
    return obj, text


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)

    def test_config_get_set_list_round_trip(self):
        # Starts with empty config
        obj, text = run_json("config", "list")
        self.assertEqual(obj, {})
        self.assertEqual(text, "no config")

        # Set string value
        code = run_cli("config", "set", "access.team", "https://example.cloudflareaccess.com")
        self.assertEqual(code, 0)
        self.assertEqual(config.get("access.team"), "https://example.cloudflareaccess.com")

        # Get via CLI
        val, text = run_json("config", "get", "access.team")
        self.assertEqual(val, "https://example.cloudflareaccess.com")
        self.assertEqual(text, "https://example.cloudflareaccess.com")

        # Set JSON array value
        code = run_cli("config", "set", "kit.bases", "--as-json", '["feat/migration", "develop"]')
        self.assertEqual(code, 0)
        self.assertEqual(config.get("kit.bases"), ["feat/migration", "develop"])

        val, text = run_json("config", "get", "kit.bases")
        self.assertEqual(val, ["feat/migration", "develop"])
        self.assertEqual(json.loads(text), ["feat/migration", "develop"])

        # Set number and boolean
        run_cli("config", "set", "settings.timeout", "--as-json", "30")
        self.assertEqual(config.get("settings.timeout"), 30)

        run_cli("config", "set", "settings.enabled", "--as-json", "true")
        self.assertEqual(config.get("settings.enabled"), True)

        # List all config
        cfg, text = run_json("config", "list")
        self.assertEqual(cfg["access"]["team"], "https://example.cloudflareaccess.com")
        self.assertEqual(cfg["kit"]["bases"], ["feat/migration", "develop"])
        self.assertEqual(cfg["settings"]["timeout"], 30)
        self.assertEqual(cfg["settings"]["enabled"], True)
        self.assertIn("access.team=https://example.cloudflareaccess.com", text)
        self.assertIn('kit.bases=["feat/migration", "develop"]', text)

        # Missing key raises error
        with self.assertRaises(store.FactoryError):
            run_json("config", "get", "nonexistent.key")

        # Without --as-json a value stays a string, even when it looks like JSON
        run_cli("config", "set", "kit.ref", "2026.10")
        self.assertEqual(config.get("kit.ref"), "2026.10")

    def test_config_set_refuses_clobber_and_bad_shapes(self):
        config.set_value("access.team", "https://t.cloudflareaccess.com")
        with self.assertRaises(store.FactoryError):
            config.set_value("access", "oops")  # would wipe team
        with self.assertRaises(store.FactoryError):
            config.set_value("access.team.x", "y")  # team is a value, not a group
        self.assertEqual(config.get("access.team"), "https://t.cloudflareaccess.com")
        with self.assertRaises(store.FactoryError):
            config.set_value("kit.bases", "not json", as_json=True)
        config.set_value("kit.bases", "develop")  # a string, not a list
        with self.assertRaises(store.FactoryError):
            sources.kit_bases()
        with open(store.path("config.json"), "w") as f:
            json.dump(["not", "an", "object"], f)
        with self.assertRaises(store.FactoryError):
            config.load()

    def test_doctor_reports_access_and_kit(self):
        run_cli("product", "set", "testprod", "--name", "Test Product")
        run_cli("project", "add", "ds", "--product", "testprod", "--kind", "build", "--name", "DS")
        p = store.find_project("ds")
        p["source"] = "ds-kit"
        store.save_project(p)
        with mock.patch.object(sources, "run", return_value="abc123"):
            checks = {name: (ok, detail) for name, ok, detail in sources.doctor_checks()}
            self.assertEqual(checks["access"], (True, "not configured: tunnelled requests are refused"))
            self.assertFalse(checks["ds kit"][0])
            config.set_value("access.team", "http://t.cloudflareaccess.com")
            config.set_value("kit.repo", self.tmp.name)
            checks = {name: (ok, detail) for name, ok, detail in sources.doctor_checks()}
            self.assertFalse(checks["access"][0])
            self.assertTrue(checks["ds kit"][0])

    def test_access_refuses_on_corrupt_config(self):
        with open(store.path("config.json"), "w") as f:
            f.write("{bad")
        self.assertFalse(access_jwt.allowed({"Cf-Ray": "r", "Cf-Access-Jwt-Assertion": "x"}))
        self.assertTrue(access_jwt.allowed({}))

    def test_access_jwt_accepts_valid_token(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        team = "https://team.cloudflareaccess.com"
        token = jwt.encode({"iss": team, "aud": "aud-1"}, key, algorithm="RS256")
        signing = mock.MagicMock()
        signing.get_signing_key_from_jwt.return_value.key = key.public_key()
        headers = {"Cf-Ray": "r", "Cf-Access-Jwt-Assertion": token}
        with mock.patch.object(access_jwt, "_get_jwks", return_value=signing):
            config.set_value("access.aud", "aud-1")
            for value in (team, team + "/"):  # a trailing slash must not lock everyone out
                config.set_value("access.team", value)
                self.assertTrue(access_jwt.allowed(headers), value)
            config.set_value("access.team", "team.cloudflareaccess.com")  # no scheme: refused
            self.assertFalse(access_jwt.allowed(headers))
            config.set_value("access.team", team)
            config.set_value("access.aud", "other")
            self.assertFalse(access_jwt.allowed(headers))

    def test_access_jwt_unconfigured_and_local(self):
        # In unconfigured state (no access.team or access.aud in config)
        self.assertIsNone(config.get("access.team"))
        self.assertIsNone(config.get("access.aud"))

        # Local requests (no Cf-Ray or Cf-Connecting-Ip headers) are allowed
        self.assertTrue(access_jwt.allowed({}))
        self.assertTrue(access_jwt.allowed({"User-Agent": "curl/8.0"}))
        self.assertTrue(access_jwt.allowed({"Host": "127.0.0.1:3045"}))

        # Tunnelled requests (carrying CF headers) are refused fail-closed when unconfigured
        self.assertFalse(access_jwt.allowed({"Cf-Ray": "ray-12345"}))
        self.assertFalse(access_jwt.allowed({"Cf-Connecting-Ip": "198.51.100.1"}))
        self.assertFalse(access_jwt.allowed({
            "Cf-Ray": "ray-12345",
            "Cf-Connecting-Ip": "198.51.100.1",
            "Cf-Access-Jwt-Assertion": "some-token",
        }))

        # When configured, still fails closed on invalid tokens
        config.set_value("access.team", "https://test.cloudflareaccess.com")
        config.set_value("access.aud", "test-audience-tag")
        self.assertTrue(access_jwt.allowed({}))
        self.assertFalse(access_jwt.allowed({"Cf-Ray": "ray-12345"}))
        self.assertFalse(access_jwt.allowed({
            "Cf-Ray": "ray-12345",
            "Cf-Access-Jwt-Assertion": "invalid.jwt.token",
        }))

    def test_kit_source_disabled_and_factory_build_succeeds(self):
        # With empty config, kit source is disabled
        self.assertFalse(sources.kit_repo())
        self.assertEqual(sources.show("UNITS.md"), "")
        self.assertEqual(sources.kit_prs(), [])
        self.assertEqual(sources.scope_history(), [])
        self.assertEqual(sources.ds_kit(), {})
        with self.assertRaises(store.FactoryError):
            sources.ds_notes_items("any_project")

        # Set up a product and a project with source="ds-kit" in temp FACTORY_HOME
        run_cli("product", "set", "testprod", "--name", "Test Product", "--repo", "org/repo")
        run_cli("project", "add", "ds", "--product", "testprod", "--kind", "build", "--name", "DS Project")
        p = store.find_project("ds")
        p["source"] = "ds-kit"
        store.save_project(p)

        # Mock external datasources (T3, tokens, gh)
        nowhere = os.path.join(self.tmp.name, "nowhere")
        usage = os.path.join(self.tmp.name, "usage-scan-cache.json")
        with open(usage, "w") as f:
            json.dump({"version": 9, "sessions": [], "models": [], "files": {}}, f)

        with mock.patch.dict(os.environ, datasource_env(nowhere, FACTORY_USAGE_CACHE=usage)), \
             mock.patch.object(t3, "connect", return_value=mock.MagicMock()), \
             mock.patch.object(t3, "agents", return_value=([], {})), \
             mock.patch.object(t3, "thread_attribution_map", return_value=({}, {})), \
             mock.patch.object(t3, "unknown_statuses", return_value={}), \
             mock.patch.object(tokens, "load", return_value=({"sessions": [], "models": [], "files": {}}, NOW)), \
             mock.patch.object(sources, "prs_for", return_value={}), \
             mock.patch.object(skills, "collect", return_value=([], [], 0.001)), \
             mock.patch.object(sources, "run_lane", return_value=([], [])):
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                code = run_cli("build")
            self.assertEqual(code, 0)
            self.assertIn("kit.repo is unset", out.getvalue())
            self.assertTrue(os.path.exists(store.path("site", "factory.html")))

    def test_owner_name_and_kit_search_defaults_and_overrides(self):
        from factory import relay
        self.assertEqual(config.owner(), "the owner")
        self.assertEqual(relay._owner_title(), "The owner")
        self.assertEqual(sources.kit_search_base(), "develop")
        self.assertEqual(sources.kit_search(), "ds- in:head")

        config.set_value("owner.name", "alice")
        self.assertEqual(config.get("owner.name"), "alice")
        self.assertEqual(relay._owner_title(), "Alice")

        config.set_value("owner.name", "5", as_json=True)
        self.assertEqual(config.owner(), "the owner")
        with open(store.path("config.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(config.owner(), "the owner")
        self.assertEqual(relay._owner_title(), "The owner")
        with open(store.path("config.json"), "w") as f:
            f.write("{}")

        config.set_value("kit.search_base", "main")
        self.assertEqual(sources.kit_search_base(), "main")

        config.set_value("kit.search", "feature- in:head")
        self.assertEqual(sources.kit_search(), "feature- in:head")
