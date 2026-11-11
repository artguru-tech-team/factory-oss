import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from factory import cli, public_serve, store


EXPECTED_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com https://*.clarity.ms; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://*.clarity.ms https://c.bing.com https://*.google-analytics.com https://www.googletagmanager.com; "
    "font-src 'self' data:; "
    "connect-src 'self' https://*.clarity.ms https://*.google-analytics.com https://*.analytics.google.com https://www.googletagmanager.com; "
    "frame-ancestors 'self'"
)


class TestPublicServe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FACTORY_HOME"] = self.tmp.name
        self.public_dir = store.path("site-public")
        os.makedirs(self.public_dir, exist_ok=True)

        self.index_content = b"<!doctype html><html><body>Office</body></html>\n"
        self.feed_content = b'{"builtAt": "2026-11-09T00:00:00Z", "agents": []}\n'

        with open(os.path.join(self.public_dir, "index.html"), "wb") as f:
            f.write(self.index_content)
        with open(os.path.join(self.public_dir, "office-public.json"), "wb") as f:
            f.write(self.feed_content)

        handler_cls = public_serve.make_handler(self.public_dir)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()
        del os.environ["FACTORY_HOME"]

    def req(self, path, method="GET", body=None, headers=None):
        r = urllib.request.Request(self.base + path, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_get_root(self):
        status, body, headers = self.req("/")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.index_content)
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(headers.get("Cache-Control"), "public, max-age=300")
        self.assertEqual(headers.get("Content-Length"), str(len(self.index_content)))
        self.assertEqual(headers.get("Server"), "factory")  # no Python version, no trailing space

        # GET /index.html -> 404 (one name per file)
        status, _, _ = self.req("/index.html")
        self.assertEqual(status, 404)

    def test_get_feed(self):
        status, body, headers = self.req("/office-public.json")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.feed_content)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(headers.get("Cache-Control"), "public, max-age=60")
        self.assertEqual(headers.get("Content-Length"), str(len(self.feed_content)))

    def test_query_string_ignored(self):
        status, body, _ = self.req("/?x=1")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.index_content)

        status, body, _ = self.req("/office-public.json?ts=12345")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.feed_content)

    def test_404_on_other_paths(self):
        paths = [
            "/../config.json",
            "/%2e%2e/.key",
            "/office.html",
            "/api/office",
            "/site/factory.html",
            "/other",
        ]
        for p in paths:
            status, _, headers = self.req(p)
            self.assertEqual(status, 404, f"Expected 404 for {p}")
            self.assertEqual(headers.get("Content-Security-Policy"), EXPECTED_CSP)
            self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

    def test_methods_405_and_head(self):
        # POST and PUT -> 405 with Allow: GET, HEAD
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            status, _, headers = self.req("/", method=m, body=b"data")
            self.assertEqual(status, 405, f"Expected 405 for {m}")
            self.assertEqual(headers.get("Allow"), "GET, HEAD")
            self.assertEqual(headers.get("Content-Security-Policy"), EXPECTED_CSP)

        # HEAD / -> 200 with no body
        status, body, headers = self.req("/", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertEqual(headers.get("Content-Length"), str(len(self.index_content)))

        # HEAD /office-public.json -> 200 with no body
        status, body, headers = self.req("/office-public.json", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertEqual(headers.get("Content-Length"), str(len(self.feed_content)))

    def test_security_headers_on_all_responses(self):
        # 200
        _, _, h200 = self.req("/")
        self.assertEqual(h200.get("Content-Security-Policy"), EXPECTED_CSP)
        self.assertEqual(h200.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(h200.get("Referrer-Policy"), "no-referrer")

        # 404
        _, _, h404 = self.req("/nonexistent")
        self.assertEqual(h404.get("Content-Security-Policy"), EXPECTED_CSP)
        self.assertEqual(h404.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(h404.get("Referrer-Policy"), "no-referrer")

        # 405
        _, _, h405 = self.req("/", method="POST", body=b"x")
        self.assertEqual(h405.get("Content-Security-Policy"), EXPECTED_CSP)
        self.assertEqual(h405.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(h405.get("Referrer-Policy"), "no-referrer")

    def test_missing_files_503(self):
        # Remove index.html
        os.remove(os.path.join(self.public_dir, "index.html"))
        status, body, headers = self.req("/")
        self.assertEqual(status, 503)
        self.assertEqual(body.strip(), b"office not installed")
        self.assertEqual(headers.get("Content-Type"), "text/plain; charset=utf-8")
        self.assertEqual(headers.get("Content-Security-Policy"), EXPECTED_CSP)

        # Remove office-public.json
        os.remove(os.path.join(self.public_dir, "office-public.json"))
        status, body, headers = self.req("/office-public.json")
        self.assertEqual(status, 503)
        self.assertEqual(body.strip(), b"feed not built yet")
        self.assertEqual(headers.get("Content-Type"), "text/plain; charset=utf-8")
        self.assertEqual(headers.get("Content-Security-Policy"), EXPECTED_CSP)

    def test_dynamic_file_reading_per_request(self):
        # Update index.html
        new_content = b"<!doctype html><html><body>Office v2</body></html>\n"
        with open(os.path.join(self.public_dir, "index.html"), "wb") as f:
            f.write(new_content)

        status, body, headers = self.req("/")
        self.assertEqual(status, 200)
        self.assertEqual(body, new_content)
        self.assertEqual(headers.get("Content-Length"), str(len(new_content)))

    def test_cli_parser_and_execution(self):
        p = cli.parser()

        # default args
        args = p.parse_args(["office", "serve-public"])
        self.assertEqual(args.cmd, "office")
        self.assertEqual(args.sub, "serve-public")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 3046)

        # custom args
        args_custom = p.parse_args(["office", "serve-public", "--host", "0.0.0.0", "--port", "8080"])
        self.assertEqual(args_custom.host, "0.0.0.0")
        self.assertEqual(args_custom.port, 8080)

        # test cli.office_serve_public calls public_serve.serve
        with mock.patch.object(public_serve, "serve") as mock_serve:
            ret, text = cli.office_serve_public(args)
            self.assertIsNone(ret)
            self.assertEqual(text, "")
            mock_serve.assert_called_once_with(host="127.0.0.1", port=3046)


    def test_unreadable_file_503_not_dropped(self):
        os.chmod(os.path.join(self.public_dir, "index.html"), 0)
        try:
            if os.access(os.path.join(self.public_dir, "index.html"), os.R_OK):
                self.skipTest("running as a user that ignores file modes")
            status, body, headers = self.req("/")
            self.assertEqual(status, 503)
            self.assertEqual(body, b"temporarily unavailable\n")
            self.assertNotIn(self.public_dir.encode(), body)
        finally:
            os.chmod(os.path.join(self.public_dir, "index.html"), 0o600)

    def test_stdlib_errors_carry_security_headers_and_no_python_version(self):
        import socket
        port = self.srv.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(b"GET / HTTP/1.1\r\nX-Long: " + b"a" * 70000 + b"\r\n\r\n")
            raw = s.recv(65536).decode("latin-1")
        self.assertIn(" 431 ", raw.splitlines()[0])
        self.assertIn("Content-Security-Policy: " + EXPECTED_CSP, raw)
        self.assertIn("X-Content-Type-Options: nosniff", raw)
        self.assertNotIn("Python/", raw)

    def test_handler_has_socket_timeout(self):
        self.assertEqual(public_serve.make_handler(self.public_dir).timeout, 10)


class TestFrameAncestors(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"FACTORY_HOME": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _set(self, value):
        from factory import config
        config.set_value("office.public_frame_ancestors", json.dumps(value), as_json=True)

    def test_default_is_self(self):
        self.assertEqual(public_serve.frame_ancestors(), "'self'")

    def test_list_and_string(self):
        self._set(["https://example.com", "https://*.example.com", "http://localhost:8080"])
        self.assertEqual(public_serve.frame_ancestors(),
                         "https://example.com https://*.example.com http://localhost:8080")
        self._set("https://a.example https://b.example")
        self.assertEqual(public_serve.frame_ancestors(), "https://a.example https://b.example")

    def test_bad_values_never_reach_the_header(self):
        self._set(["https://ok.example", 5, "https://x.example; script-src *", "https://y.example\r\nSet-Cookie: a=1", "https://z.example\n"])
        self.assertEqual(public_serve.frame_ancestors(), "https://ok.example")
        self._set([7, "*"])
        self.assertEqual(public_serve.frame_ancestors(), "'self'")
        self.assertTrue(public_serve.csp().endswith("frame-ancestors 'self'"))

    def test_broken_config_falls_back(self):
        with open(os.path.join(self.tmp.name, "config.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(public_serve.frame_ancestors(), "'self'")
