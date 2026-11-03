import contextlib
import http.server
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from factory import cli, secrets, sources, store, sweep
from factory.store import FactoryError

NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["--json", *argv])
    return code, (json.loads(out.getvalue()) if code == 0 and out.getvalue().strip() else None), err.getvalue()


def cand(source="sentry", repo="o/r", key="ABC-1", count=10, title="[Sentry] Boom", body="the body", last_seen="2026-10-27T12:00:00Z"):
    return {"source": source, "repo": repo, "key": key, "marker": f"factory-sweep: {source}:{key}",
            "count": count, "title": title, "body": body, "lastSeen": last_seen}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.environ.pop, "FACTORY_HOME", None)

    def ok(self, *argv):
        code, obj, err = run(*argv)
        self.assertEqual(code, 0, err)
        return obj

    def fails(self, *argv):
        code, _, err = run(*argv)
        self.assertEqual(code, 1)
        return err

    def set_secret(self, name, value):
        with mock.patch("sys.stdin", io.StringIO(value + "\n")):
            secrets.set_secret(name)

    def set_products(self):
        store.write("products.json", {"bk": {"name": "Acme", "repos": ["o/r", "o/r2"]}})


class SecretTest(Base):
    def test_secret_file_and_dir_modes(self):
        with mock.patch("sys.stdin", io.StringIO("sntrys_SECRET_VALUE_42\n")):
            code, obj, err = run("secret", "set", "sentry-token")
        self.assertEqual(code, 0, err)
        self.assertNotIn("SECRET_VALUE_42", err)
        self.assertNotIn("SECRET_VALUE_42", json.dumps(obj))
        p = store.path("secrets", "sentry-token")
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(store.path("secrets")).st_mode & 0o777, 0o700)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "sntrys_SECRET_VALUE_42")

    def test_no_command_prints_a_secret(self):
        self.set_secret("sentry-token", "sntrys_TOP_SECRET_99")
        self.set_secret("signoz-key", "SIGNOZ_HIDDEN_77")
        code, obj, err = run("secret", "list")
        self.assertEqual(code, 0, err)
        self.assertEqual(obj["secrets"], ["sentry-token", "signoz-key"])
        blob = err + json.dumps(obj)
        self.assertNotIn("TOP_SECRET", blob)
        self.assertNotIn("HIDDEN", blob)
        self.assertNotIn("sntrys_", blob)

    def test_missing_secret_names_the_fix(self):
        with self.assertRaises(FactoryError) as cm:
            secrets.get_secret("sentry-token")
        self.assertIn("factory secret set sentry-token", str(cm.exception))

    def test_bad_secret_name_refused(self):
        with self.assertRaises(FactoryError):
            secrets.get_secret("a/b")

    def test_secret_with_newline_or_whitespace_refused(self):
        with mock.patch("sys.stdin", io.StringIO("sntrys_SECRET\nSECOND_LINE\n")):
            with self.assertRaises(FactoryError) as cm:
                secrets.set_secret("sentry-token")
            self.assertIn("paste the key on one line", str(cm.exception))

        with mock.patch("sys.stdin", io.StringIO("sntrys_SECRET WITH SPACES\n")):
            with self.assertRaises(FactoryError) as cm:
                secrets.set_secret("sentry-token")
            self.assertIn("paste the key on one line", str(cm.exception))

    def test_secret_bad_mode_refused(self):
        self.set_secret("sentry-token", "sntrys_VAL")
        p = store.path("secrets", "sentry-token")
        os.chmod(p, 0o644)
        with self.assertRaises(FactoryError) as cm:
            secrets.get_secret("sentry-token")
        self.assertIn("want 0o600", str(cm.exception))

    def test_secret_list_ignores_tmp(self):
        self.set_secret("sentry-token", "sntrys_VAL")
        store.write_text("secrets/test.tmp", "in-flight")
        names = secrets.list_secrets()
        self.assertIn("sentry-token", names)
        self.assertNotIn("test.tmp", names)


class ConfigTest(Base):
    def test_map_refuses_unknown_repo(self):
        self.set_products()
        err = self.fails("sweep", "map", "sentry", "web", "other/x")
        self.assertIn("not a product repo", err)
        self.ok("sweep", "map", "sentry", "web", "o/r")
        self.ok("sweep", "map", "signoz", "api", "o/r2")
        cfg = sweep.sweep()
        self.assertEqual(cfg["sentry"]["projects"], {"web": "o/r"})
        self.assertEqual(cfg["signoz"]["services"], {"api": "o/r2"})

    def test_config_set_parses_values(self):
        self.ok("sweep", "config", "set", "min_events", "3")
        self.assertEqual(sweep.sweep()["min_events"], 3)
        self.ok("sweep", "config", "set", "max_per_repo", "7")
        self.assertEqual(sweep.sweep()["max_per_repo"], 7)
        self.ok("sweep", "config", "set", "signoz.default_repo", "null")
        self.assertIsNone(sweep.sweep()["signoz"]["default_repo"])
        self.ok("sweep", "config", "set", "include_performance", "true")
        self.assertIs(sweep.sweep()["include_performance"], True)
        err = self.fails("sweep", "config", "set", "bogus", "x")
        self.assertIn("unknown sweep config key", err)
        err = self.fails("sweep", "config", "set", "min_events", "0")
        self.assertIn(">= 1", err)

    def test_base_url_requires_http(self):
        err = self.fails("sweep", "config", "set", "sentry.base_url", "example.com")
        self.assertIn("must start with http:// or https://", err)
        err = self.fails("sweep", "config", "set", "signoz.base_url", "example.com")
        self.assertIn("must start with http:// or https://", err)
        self.ok("sweep", "config", "set", "sentry.base_url", "https://sentry.example.com")
        self.assertEqual(sweep.sweep()["sentry"]["base_url"], "https://sentry.example.com")

        # Source check also refuses bad base_url
        bad_cfg = {"sentry": {"base_url": "ftp://bad", "org": "acme"}, "signoz": {"base_url": "ftp://bad"}}
        with self.assertRaises(FactoryError) as cm:
            sweep._sentry_conf(bad_cfg)
        self.assertIn("must start with http:// or https://", str(cm.exception))
        with self.assertRaises(FactoryError) as cm:
            sweep._signoz_conf(bad_cfg)
        self.assertIn("must start with http:// or https://", str(cm.exception))

    def test_shape_check_refuses_corrupt_config(self):
        store.write("sweep.json", {"min_events": "five"})
        with self.assertRaises(FactoryError):
            sweep.sweep()
        store.write("sweep.json", {"sentry": {"projects": [1, 2]}})
        with self.assertRaises(FactoryError):
            sweep.sweep()


class HttpSecurityTest(unittest.TestCase):
    def test_redirect_refused_and_credentials_not_sent(self):
        received_requests = []

        class TargetHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received_requests.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
            def log_message(self, *a): pass

        target_server = http.server.HTTPServer(("127.0.0.1", 0), TargetHandler)
        target_server.timeout = 0.5
        target_port = target_server.server_port
        target_thread = threading.Thread(target=target_server.handle_request, daemon=True)
        target_thread.start()

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target_port}/leak")
                self.end_headers()
            def log_message(self, *a): pass

        redirect_server = http.server.HTTPServer(("127.0.0.1", 0), RedirectHandler)
        redirect_server.timeout = 1.0
        redirect_port = redirect_server.server_port
        redirect_thread = threading.Thread(target=redirect_server.handle_request, daemon=True)
        redirect_thread.start()

        try:
            with self.assertRaises(FactoryError) as cm:
                sweep._request("GET", f"http://127.0.0.1:{redirect_port}/start",
                               headers={"Authorization": "Bearer secret_tok"})
            self.assertIn("refusing to send credentials", str(cm.exception))
            self.assertIn(f"127.0.0.1:{target_port}", str(cm.exception))
            # Wait briefly for target thread to complete or time out
            target_thread.join(timeout=1.0)
            self.assertEqual(received_requests, [])
        finally:
            redirect_server.server_close()
            target_server.server_close()

    def test_non_json_200_raises_clear_error(self):
        class HtmlHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html><body>Login to continue</body></html>")
            def log_message(self, *a): pass

        server = http.server.HTTPServer(("127.0.0.1", 0), HtmlHandler)
        server.timeout = 1.0
        port = server.server_port
        t = threading.Thread(target=server.handle_request, daemon=True)
        t.start()
        try:
            with self.assertRaises(FactoryError) as cm:
                sweep._json("GET", f"http://127.0.0.1:{port}/api", source="sentry")
            self.assertIn("sentry returned non-JSON (login page?)", str(cm.exception))
            t.join(timeout=1.0)
        finally:
            server.server_close()

    def test_link_next_host_check(self):
        base = "https://de.sentry.io"
        h_same = {"Link": '<https://de.sentry.io/next?cursor=2>; rel="next"; results="true"'}
        self.assertEqual(sweep._link_next(h_same, base_url=base), "https://de.sentry.io/next?cursor=2")

        h_diff = {"Link": '<https://attacker.example.com/next>; rel="next"; results="true"'}
        self.assertIsNone(sweep._link_next(h_diff, base_url=base))

        h_diff_scheme = {"Link": '<http://de.sentry.io/next>; rel="next"; results="true"'}
        self.assertIsNone(sweep._link_next(h_diff_scheme, base_url=base))


class SentryFilterTest(unittest.TestCase):
    def test_filter_rules(self):
        cfg = sweep.DEFAULT_SWEEP.copy()  # min_events 5, include_performance False
        base = {"stats": {"24h": [[0, 0]]}, "firstSeen": "2026-10-20T00:00:00Z", "substatus": "ongoing"}
        # performance skipped unless include_performance
        self.assertFalse(sweep.keep_sentry({**base, "issueCategory": "performance"}, cfg, NOW))
        self.assertFalse(sweep.keep_sentry({**base, "issueCategory": "cron"}, cfg, NOW))
        # min_events: busy keeps
        self.assertTrue(sweep.keep_sentry({**base, "issueCategory": "error", "stats": {"24h": [[0, 5]]}}, cfg, NOW))
        self.assertFalse(sweep.keep_sentry({**base, "issueCategory": "error", "stats": {"24h": [[0, 4]]}}, cfg, NOW))
        # new (firstSeen within 24h) keeps
        fresh = {**base, "issueCategory": "error", "firstSeen": (NOW - timedelta(hours=1)).isoformat()}
        self.assertTrue(sweep.keep_sentry(fresh, cfg, NOW))
        # regressed keeps
        self.assertTrue(sweep.keep_sentry({**base, "issueCategory": "error", "substatus": "regressed"}, cfg, NOW))
        # performance kept with include_performance
        perf = {**base, "issueCategory": "performance", "stats": {"24h": [[0, 9]]}}
        self.assertTrue(sweep.keep_sentry(perf, {**cfg, "include_performance": True}, NOW))

    def test_frames_last_exception_value_with_in_app(self):
        # Chained exceptions: second exception has in-app frames, third has none
        event = {"entries": [{"type": "exception", "data": {"values": [
            {"stacktrace": {"frames": [
                {"filename": "first.py", "function": "f1", "lineNo": 10, "inApp": True},
            ]}},
            {"stacktrace": {"frames": [
                {"filename": "second.py", "function": "f2", "lineNo": 20, "inApp": True},
                {"filename": "second.py", "function": "f3", "lineNo": 21, "inApp": True},
            ]}},
            {"stacktrace": {"frames": [
                {"filename": "lib.py", "function": "lib", "lineNo": 30, "inApp": False},
            ]}},
        ]}}]}
        self.assertEqual(sweep.sentry_frames(event), [
            {"filename": "second.py", "function": "f2", "lineNo": 20},
            {"filename": "second.py", "function": "f3", "lineNo": 21},
        ])


class PaginationTest(unittest.TestCase):
    def test_link_next(self):
        h = {"Link": '<https://x/next?cursor=2>; rel="next"; results="true"; cursor="2"'}
        self.assertEqual(sweep._link_next(h), "https://x/next?cursor=2")
        h = {"Link": '<https://x/next>; rel="next"; results="false"'}
        self.assertIsNone(sweep._link_next(h))
        self.assertIsNone(sweep._link_next({}))

    def test_stops_at_results_false(self):
        responses = iter([
            (200, {"Link": '<https://x/n2>; rel="next"; results="true"'}, [{"id": "a"}]),
            (200, {"Link": '<https://x/n3>; rel="next"; results="false"'}, [{"id": "b"}]),
        ])
        with mock.patch.object(sweep, "_json", side_effect=lambda *a, **k: next(responses)):
            issues = sweep._sentry_issues("https://x", "org", "proj", "tok")
        self.assertEqual([i["id"] for i in issues], ["a", "b"])

    def test_caps_at_five_pages(self):
        responses = iter([(200, {"Link": '<https://x/n>; rel="next"; results="true"'}, [{"id": i}])
                          for i in range(10)])
        with mock.patch.object(sweep, "_json", side_effect=lambda *a, **k: next(responses)):
            issues = sweep._sentry_issues("https://x", "org", "proj", "tok")
        self.assertEqual(len(issues), 5)


class SignozTest(unittest.TestCase):
    def _collect(self, groups, services=None, default_repo=None):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["FACTORY_HOME"] = tmp
            self.addCleanup(os.environ.pop, "FACTORY_HOME", None)
            store.write_text("secrets/signoz-key", "KEY")
            cfg = sweep.sweep()
            cfg["signoz"]["base_url"] = "https://signoz.example.com"
            cfg["signoz"]["services"] = services or {}
            cfg["signoz"]["default_repo"] = default_repo
            sweep.save_sweep(cfg)
            with mock.patch.object(sweep, "signoz_list", return_value=groups):
                return sweep.collect_signoz(sweep.sweep(), NOW)

    def test_mapping_and_default_repo(self):
        groups = [
            {"groupID": "g1", "serviceName": "api", "exceptionType": "ValueError",
             "exceptionMessage": "boom", "exceptionCount": 10},
            {"groupID": "g2", "serviceName": "worker", "exceptionType": "TypeError",
             "exceptionMessage": "x", "exceptionCount": 10},
        ]
        cands, unmapped = self._collect(groups, services={"api": "o/api"}, default_repo="o/default")
        self.assertEqual({c["repo"] for c in cands}, {"o/api", "o/default"})
        self.assertEqual(unmapped, [])
        worker = next(c for c in cands if c["key"] == "g2")
        self.assertEqual(worker["repo"], "o/default")

    def test_unmapped_service_reported(self):
        groups = [
            {"groupID": "g1", "serviceName": "api", "exceptionType": "ValueError",
             "exceptionMessage": "boom", "exceptionCount": 10},
            {"groupID": "g3", "serviceName": "ghost", "exceptionType": "X",
             "exceptionMessage": "", "exceptionCount": 10},
        ]
        cands, unmapped = self._collect(groups, services={"api": "o/api"}, default_repo=None)
        self.assertEqual({c["repo"] for c in cands}, {"o/api"})
        self.assertEqual(unmapped, ["ghost"])

    def test_signoz_frames(self):
        self.assertEqual(sweep.signoz_frames("a\n\nb\nc\nd\ne\nf\ng\nh\ni\nj"), ["a", "b", "c", "d", "e", "f", "g", "h"])

    def test_signoz_detail_and_body_timestamp(self):
        last_seen = "1765922494401205760"
        with mock.patch.object(sweep, "_json", return_value=(200, {}, {"exceptionStacktrace": "trace"})) as mj:
            res = sweep.signoz_detail("https://signoz.example.com", "KEY", "g123", last_seen)
        mj.assert_called_once_with(
            "GET",
            "https://signoz.example.com/api/v1/errorFromGroupID?groupID=g123&timestamp=1765922494401205760",
            headers={"SIGNOZ-API-KEY": "KEY"}, source="signoz")
        self.assertEqual(res, {"exceptionStacktrace": "trace"})

        # Issue link has &timestamp=<lastSeen ns>
        group = {"groupID": "g123", "serviceName": "api", "lastSeen": last_seen, "exceptionCount": 5}
        body = sweep.signoz_body(group, ["frame1"], "https://signoz.example.com")
        self.assertIn("Link: https://signoz.example.com/error-detail?groupId=g123&timestamp=1765922494401205760", body)
        self.assertTrue(body.endswith("factory-sweep: signoz:g123"))


class DedupeTest(unittest.TestCase):
    def test_list_sweep_issues_builds_marker_map(self):
        items = [
            {"number": 1, "state": "open", "state_reason": None, "closed_at": None,
             "body": "First issue\n\nfactory-sweep: sentry:ABC-1"},
            {"number": 2, "state": "closed", "state_reason": "completed", "closed_at": "2026-10-25T10:00:00Z",
             "body": "Closed issue\n\nfactory-sweep: sentry:ABC-2"},
            {"number": 5, "state": "open", "pull_request": {}, "body": "factory-sweep: sentry:ABC-9"},
        ]
        with mock.patch.object(sources, "gh_json", return_value=items):
            res = sweep.list_sweep_issues("o/r", "sentry")
        self.assertIn("factory-sweep: sentry:ABC-1", res)
        self.assertEqual(res["factory-sweep: sentry:ABC-1"]["number"], 1)
        self.assertIn("factory-sweep: sentry:ABC-2", res)
        self.assertEqual(res["factory-sweep: sentry:ABC-2"]["number"], 2)
        self.assertEqual((res["factory-sweep: sentry:ABC-2"]["state"], res["factory-sweep: sentry:ABC-2"]["stateReason"]),
                         ("CLOSED", "completed"))
        self.assertNotIn("factory-sweep: sentry:ABC-9", res)  # a pull request is not an issue

    def test_rest_api_not_search_and_latest_closed_wins(self):
        items = [  # newest first, as the API returns them
            {"number": 9, "state": "closed", "state_reason": "not_planned", "closed_at": "2026-10-30T10:00:00Z",
             "body": "Regression of #3\nfactory-sweep: sentry:ABC-1"},
            {"number": 3, "state": "closed", "state_reason": "completed", "closed_at": "2026-10-20T10:00:00Z",
             "body": "factory-sweep: sentry:ABC-1"},
        ]
        with mock.patch.object(sources, "gh_json", return_value=items) as gj:
            res = sweep.list_sweep_issues("o/r", "sentry")
        self.assertEqual(res["factory-sweep: sentry:ABC-1"]["number"], 9)  # the dismissal stands
        self.assertTrue(gj.call_args.args[1].startswith("repos/o/r/issues?state=all"))

    def test_marker_poisoning(self):
        # Body contains fake marker in the middle or in an HTML comment, but last line is different
        items = [
            {"number": 10, "state": "OPEN", "stateReason": None, "closedAt": None,
             "body": "Line 1\n<!-- factory-sweep: sentry:ABC-POISON -->\nfactory-sweep: sentry:ABC-POISON\nLine 3\n\nfactory-sweep: sentry:ABC-REAL\n\n"},
        ]
        with mock.patch.object(sources, "gh_json", return_value=items):
            res = sweep.list_sweep_issues("o/r", "sentry")
        self.assertIn("factory-sweep: sentry:ABC-REAL", res)
        self.assertNotIn("factory-sweep: sentry:ABC-POISON", res)

    def test_read_failure_raises_and_run_files_nothing_for_that_repo(self):
        with mock.patch.object(sources, "gh_json", side_effect=FactoryError("gh: HTTP 502")):
            with self.assertRaises(FactoryError):
                sweep.list_sweep_issues("o/r", "sentry")


class UserAgentTest(unittest.TestCase):
    def test_requests_carry_a_named_user_agent(self):
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["ua"] = self.headers.get("User-Agent")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.handle_request, daemon=True).start()
        sweep._request("GET", f"http://127.0.0.1:{srv.server_port}/x")
        srv.server_close()
        self.assertEqual(seen["ua"], sweep.USER_AGENT)


class IgnoreTest(Base):
    def test_next_control_flow_signals_are_ignored_by_default(self):
        cfg = sweep.sweep()
        g = {"exceptionType": "Error", "exceptionMessage": "NEXT_HTTP_ERROR_FALLBACK;404", "exceptionCount": 2781,
             "firstSeen": "2026-10-01T00:00:00Z"}
        self.assertFalse(sweep.keep_signoz(g, cfg, NOW))
        self.assertTrue(sweep.keep_signoz({**g, "exceptionMessage": "fetch failed"}, cfg, NOW))
        issue = {"issueCategory": "error", "title": "Error: NEXT_REDIRECT", "stats": {"24h": [[0, 99]]}}
        self.assertFalse(sweep.keep_sentry(issue, cfg, NOW))
        self.ok("sweep", "config", "set", "ignore", "")   # cleared: nothing ignored
        self.assertTrue(sweep.keep_signoz(g, sweep.sweep(), NOW))


class SafetyTest(Base):
    def test_exact_nanoseconds(self):
        self.assertEqual(sweep._to_ns_str("2026-11-01T10:00:00.123456789Z"), "1793527200123456789")
        self.assertEqual(sweep._to_ns_str("2026-11-01T10:00:00Z"), "1793527200000000000")
        self.assertEqual(sweep._to_ns_str("2026-11-01T11:00:00.5+01:00"), "1793527200500000000")

    def test_error_text_never_quotes_a_non_factory_exception(self):
        e = ValueError("Invalid header value b'Bearer SECRET\\nOLD'")
        self.assertEqual(sweep._err_text(e), "ValueError")
        self.assertEqual(sweep._err_text(FactoryError("sentry: HTTP 401")), "sentry: HTTP 401")

    def test_secret_file_with_a_line_break_is_refused(self):
        os.makedirs(store.path("secrets"), mode=0o700, exist_ok=True)
        with open(store.path("secrets/sentry-token"), "w") as f:
            f.write("abc\ndef")
        os.chmod(store.path("secrets/sentry-token"), 0o600)
        with self.assertRaises(FactoryError) as cm:
            sweep.secrets.get_secret("sentry-token")
        self.assertNotIn("abc", str(cm.exception))

    def test_inline_newline_and_nested_comment(self):
        self.assertNotIn("\n", sweep._safe_inline("foo\n\n@victim # heading"))
        self.assertNotIn("<!--", sweep._clean_source_text("<<!--!-- x --->>"))

    def test_link_origin_ignores_case_and_default_port(self):
        self.assertEqual(sweep._origin("https://DE.sentry.io/x"), sweep._origin("https://de.sentry.io:443/y"))


class InjectionTest(unittest.TestCase):
    def test_title_stripped_and_capped(self):
        raw = "Error occurred\nwith newlines\rand carriage returns " + ("x" * 250)
        formatted = sweep._format_title("sentry", f"[Sentry] {raw}")
        self.assertNotIn("\n", formatted)
        self.assertNotIn("\r", formatted)
        self.assertLessEqual(len(formatted), 200)

    def test_fence_escaping_with_backticks(self):
        frames = [
            "File `foo.py`, line 10",
            "Trace with ``` triple backticks",
            "<!-- fake-html-comment -->",
        ]
        fenced = sweep._safe_fence(frames)
        # Longest run was 3 backticks, so fence must be at least 4 backticks
        self.assertTrue(fenced[0].startswith("````"))
        self.assertEqual(fenced[0], fenced[-1])
        # HTML comment tags stripped
        joined = "\n".join(fenced)
        self.assertNotIn("<!--", joined)
        self.assertNotIn("-->", joined)


class DoctorTest(Base):
    def test_doctor_ok_when_unconfigured(self):
        # sweep.json missing
        checks = sweep.doctor_checks()
        self.assertEqual(checks, [
            ("sweep sentry", True, "not configured"),
            ("sweep signoz", True, "not configured"),
        ])

        # sweep.json exists but sources empty
        sweep.save_sweep(sweep.DEFAULT_SWEEP)
        checks = sweep.doctor_checks()
        self.assertEqual(checks, [
            ("sweep sentry", True, "not configured"),
            ("sweep signoz", True, "not configured"),
        ])

    def test_doctor_fails_when_configured_and_auth_fails(self):
        cfg = sweep.sweep()
        cfg["sentry"]["base_url"] = "https://sentry.example.com"
        cfg["sentry"]["org"] = "acme"
        sweep.save_sweep(cfg)
        self.set_secret("sentry-token", "tok")

        with mock.patch.object(sweep, "_json", side_effect=FactoryError("GET organizations -> HTTP 401")):
            checks = sweep.doctor_checks()
        sentry_check = next(c for c in checks if c[0] == "sweep sentry")
        self.assertFalse(sentry_check[1])
        self.assertIn("HTTP 401", sentry_check[2])


class CandidateBodyTest(Base):
    def test_detail_failure_still_gives_a_body_and_signoz_uses_last_seen(self):
        cfg = {"sentry": {"base_url": "https://sentry.example.com", "org": "acme", "projects": {}},
               "signoz": {"base_url": "https://signoz.example.com", "services": {}, "default_repo": None}}
        c = {"source": "sentry", "key": "ABC-1", "raw": {"id": "1", "shortId": "ABC-1", "title": "Boom", "permalink": "https://x"}}
        with mock.patch.object(sweep.secrets, "get_secret", return_value="T"), \
             mock.patch.object(sweep, "_sentry_event", side_effect=TimeoutError("slow")):
            body = sweep.candidate_body(c, cfg)
        self.assertIn("factory-sweep: sentry:ABC-1", body)
        g = {"source": "signoz", "key": "g1", "lastSeen": "2026-10-27T12:00:00Z",
             "raw": {"groupID": "g1", "exceptionType": "E", "exceptionMessage": "m", "serviceName": "svc",
                     "lastSeen": "2026-10-27T12:00:00Z"}}
        with mock.patch.object(sweep.secrets, "get_secret", return_value="K"), \
             mock.patch.object(sweep, "signoz_detail", return_value={"exceptionStacktrace": "a\nb"}) as det:
            body = sweep.candidate_body(g, cfg)
        self.assertEqual(det.call_args.args[2:], ("g1", "2026-10-27T12:00:00Z"))
        self.assertIn("factory-sweep: signoz:g1", body)


class RunTest(Base):
    def setUp(self):
        super().setUp()
        self.set_products()
        # run() logic is tested here; the body builder has its own tests (SentryFilterTest, SignozTest)
        p = mock.patch.object(sweep, "candidate_body", side_effect=lambda c, cfg: c["body"])
        p.start()
        self.addCleanup(p.stop)

    def test_exact_gh_create_argv(self):
        c = cand()
        with mock.patch.object(sweep, "collect", return_value=([c], [])), \
             mock.patch.object(sweep, "list_sweep_issues", return_value={}), \
             mock.patch.object(sweep, "ensure_labels"), \
             mock.patch.object(sources, "run", return_value="https://github.com/o/r/issues/42") as sr:
            summary, text = sweep.run(source="sentry", now=NOW)
        sr.assert_called_once_with(
            "gh", "issue", "create", "--repo", "o/r", "--title", "[Sentry] Boom",
            "--body", "the body", "--label", "intake", "--label", "source:sentry")
        self.assertEqual(summary["filed"], [{"repo": "o/r", "number": 42, "source": "sentry"}])
        self.assertIn("sentry o/r ABC-1: filed #42", text)

    def test_open_match_skipped(self):
        c = cand()
        priors = {"factory-sweep: sentry:ABC-1": {"number": 9, "state": "OPEN"}}
        with mock.patch.object(sweep, "collect", return_value=([c], [])), \
             mock.patch.object(sweep, "list_sweep_issues", return_value=priors), \
             mock.patch.object(sweep, "ensure_labels"), \
             mock.patch.object(sweep, "create_issue") as ci:
            summary, text = sweep.run(source="sentry", now=NOW)
        ci.assert_not_called()
        self.assertEqual(summary["filed"], [])
        self.assertIn("already filed #9", text)

    def test_closed_match_is_regression(self):
        c = cand(last_seen="2026-10-26T12:00:00Z")
        priors = {"factory-sweep: sentry:ABC-1": {
            "number": 4, "state": "CLOSED", "stateReason": "completed", "closedAt": "2026-10-25T12:00:00Z"
        }}
        with mock.patch.object(sweep, "collect", return_value=([c], [])), \
             mock.patch.object(sweep, "list_sweep_issues", return_value=priors), \
             mock.patch.object(sweep, "ensure_labels"), \
             mock.patch.object(sources, "run", return_value="https://github.com/o/r/issues/7") as sr:
            sweep.run(source="sentry", now=NOW)
        body = sr.call_args[0][8]
        self.assertTrue(body.startswith("Regression of #4\n\n"))

    def test_closed_not_planned_skipped(self):
        c = cand()
        priors = {"factory-sweep: sentry:ABC-1": {
            "number": 4, "state": "CLOSED", "stateReason": "not_planned", "closedAt": "2026-10-25T12:00:00Z"
        }}
        with mock.patch.object(sweep, "collect", return_value=([c], [])), \
             mock.patch.object(sweep, "list_sweep_issues", return_value=priors), \
             mock.patch.object(sweep, "create_issue") as ci:
            summary, text = sweep.run(source="sentry", now=NOW)
        ci.assert_not_called()
        self.assertEqual(summary["skipped"]["dismissed"], ["sentry o/r ABC-1"])
        self.assertIn("dismissed #4", text)

    def test_closed_completed_seen_before_close_skipped(self):
        c = cand(last_seen="2026-10-24T12:00:00Z")  # seen before closedAt
        priors = {"factory-sweep: sentry:ABC-1": {
            "number": 4, "state": "CLOSED", "stateReason": "completed", "closedAt": "2026-10-25T12:00:00Z"
        }}
        with mock.patch.object(sweep, "collect", return_value=([c], [])), \
             mock.patch.object(sweep, "list_sweep_issues", return_value=priors), \
             mock.patch.object(sweep, "create_issue") as ci:
            summary, text = sweep.run(source="sentry", now=NOW)
        ci.assert_not_called()
        self.assertEqual(summary["skipped"]["already filed"], ["sentry o/r ABC-1"])
        self.assertIn("already filed #4", text)

    def test_same_marker_twice_in_run_files_once(self):
        c1 = cand(key="A", count=10)
        c2 = cand(key="A", count=5)
        with mock.patch.object(sweep, "collect", return_value=([c1, c2], [])), \
             mock.patch.object(sweep, "list_sweep_issues", return_value={}), \
             mock.patch.object(sweep, "ensure_labels"), \
             mock.patch.object(sweep, "create_issue", return_value=1) as ci:
            summary, text = sweep.run(source="sentry", now=NOW)
        self.assertEqual(ci.call_count, 1)
        self.assertEqual(len(summary["filed"]), 1)
        self.assertEqual(summary["skipped"]["already filed"], ["sentry o/r A"])

    def test_per_repo_cap_and_ordering(self):
        cfg = sweep.sweep()
        cfg["max_per_repo"] = 2
        sweep.save_sweep(cfg)
        cs = [cand(key="A", count=5, title="[Sentry] A"), cand(key="B", count=4, title="[Sentry] B"),
              cand(key="C", count=3, title="[Sentry] C")]
        with mock.patch.object(sweep, "collect", return_value=(cs, [])), \
             mock.patch.object(sweep, "list_sweep_issues", return_value={}), \
             mock.patch.object(sweep, "ensure_labels"), \
             mock.patch.object(sweep, "create_issue", side_effect=[1, 2]) as ci:
            summary, text = sweep.run(source="sentry", now=NOW)
        # biggest two count first, third is capped
        self.assertEqual(ci.call_count, 2)
        self.assertEqual([c[0][1] for c in ci.call_args_list], ["[Sentry] A", "[Sentry] B"])
        self.assertEqual(summary["skipped"]["cap"], ["sentry o/r C"])
        self.assertIn("sentry o/r C: skipped (cap)", text)

    def test_dry_run_applies_cap(self):
        cfg = sweep.sweep()
        cfg["max_per_repo"] = 2
        sweep.save_sweep(cfg)
        cs = [cand(key="A", count=5), cand(key="B", count=4), cand(key="C", count=3)]
        with mock.patch.object(sweep, "collect", return_value=(cs, [])), \
             mock.patch.object(sweep, "list_sweep_issues", return_value={}):
            summary, text = sweep.run(source="sentry", dry=True, now=NOW)
        self.assertEqual(summary["skipped"]["cap"], ["sentry o/r C"])
        self.assertIn("sentry o/r A: would file", text)
        self.assertIn("sentry o/r B: would file", text)
        self.assertIn("sentry o/r C: skipped (cap)", text)

    def test_one_source_failure_does_not_stop_other_and_history_written(self):
        csignoz = cand(source="signoz", key="g1", title="[SigNoz] X")
        with mock.patch.object(sweep, "collect", side_effect=[ValueError("sentry exploded\nsecond line"), ([csignoz], [])]), \
             mock.patch.object(sweep, "list_sweep_issues", return_value={}), \
             mock.patch.object(sweep, "ensure_labels"), \
             mock.patch.object(sweep, "create_issue", return_value=5):
            summary, text = sweep.run(source="all", now=NOW)
        # ValueError message takes exception class + first line
        self.assertEqual(summary["error"], {"sentry": "ValueError"})
        self.assertEqual(len(summary["filed"]), 1)
        self.assertIn("sentry: error: ValueError", text)
        self.assertIn("signoz o/r g1: filed #5", text)
        # History is written
        hist = sweep.sweep()["history"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["error"], {"sentry": "ValueError"})

    def test_filing_failure_on_one_candidate_continues(self):
        c1 = cand(key="A", count=10)
        c2 = cand(key="B", count=5)
        with mock.patch.object(sweep, "collect", return_value=([c1, c2], [])), \
             mock.patch.object(sweep, "list_sweep_issues", return_value={}), \
             mock.patch.object(sweep, "ensure_labels"), \
             mock.patch.object(sweep, "create_issue", side_effect=[FactoryError("GitHub down"), 42]):
            summary, text = sweep.run(source="sentry", now=NOW)
        self.assertEqual(len(summary["filed"]), 1)
        self.assertEqual(summary["filed"][0]["number"], 42)
        self.assertEqual(summary["skipped"]["factory-sweep: sentry:A"], "error: GitHub down")
        self.assertIn("sentry o/r B: filed #42", text)

    def test_dry_run_files_nothing_and_records_nothing(self):
        c = cand()
        with mock.patch.object(sweep, "collect", return_value=([c], [])), \
             mock.patch.object(sweep, "list_sweep_issues", return_value={}), \
             mock.patch.object(sweep, "ensure_labels") as el, \
             mock.patch.object(sweep, "create_issue") as ci:
            summary, text = sweep.run(source="sentry", dry=True, now=NOW)
        ci.assert_not_called()
        el.assert_not_called()
        self.assertEqual(sweep.sweep()["history"], [])
        self.assertIn("would file", text)
        self.assertTrue(summary["dry"])

    def test_history_kept_to_30(self):
        cfg = sweep.sweep()
        for _ in range(35):
            sweep._record_history(cfg, NOW, {"source": "all", "swept": 0, "filed": [], "skipped": {}, "error": {}})
        self.assertEqual(len(cfg["history"]), 30)

    def test_history_records_concurrent_config(self):
        # A concurrent command updates sweep.json (e.g. sweep map) while run() finishes
        cfg = sweep.sweep()
        # Simulate concurrent write:
        with store.lock():
            concurrent_cfg = sweep.sweep()
            concurrent_cfg["sentry"]["projects"]["new_proj"] = "o/r"
            sweep.save_sweep(concurrent_cfg)

        sweep._record_history(cfg, NOW, {"source": "all", "swept": 1, "filed": [], "skipped": {}, "error": {}})
        final_cfg = sweep.sweep()
        self.assertEqual(final_cfg["sentry"]["projects"], {"new_proj": "o/r"})
        self.assertEqual(len(final_cfg["history"]), 1)


if __name__ == "__main__":
    unittest.main()
