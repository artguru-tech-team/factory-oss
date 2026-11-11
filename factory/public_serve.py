"""Public static server for site-public.

Serves exactly two routes:
  /                   -> index.html (text/html; charset=utf-8)
  /office-public.json -> office-public.json (application/json)

Independent from factory/serve.py to prevent private route exposure.
"""
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import config, store


# An origin for frame-ancestors: scheme, optional "*." wildcard, host, optional port. Anything else is
# dropped, so a config value can never add directives or headers.
_ORIGIN = re.compile(r"^https?://(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*(:[0-9]{1,5})?$")


def frame_ancestors() -> str:
    try:
        origins = config.get("office.public_frame_ancestors")
    except Exception:  # a broken config.json must not take the public page down
        origins = None
    if isinstance(origins, str):
        origins = origins.split()
    ok = [o for o in origins if isinstance(o, str) and _ORIGIN.fullmatch(o)] if isinstance(origins, list) else []
    return " ".join(ok) or "'self'"


def csp() -> str:
    fa = frame_ancestors()
    return (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://www.googletagmanager.com https://*.clarity.ms; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https://*.clarity.ms https://c.bing.com https://*.google-analytics.com https://www.googletagmanager.com; "
        "font-src 'self' data:; "
        "connect-src 'self' https://*.clarity.ms https://*.google-analytics.com https://*.analytics.google.com https://www.googletagmanager.com; "
        f"frame-ancestors {fa}"
    )


ROUTES = {
    "/": {
        "filename": "index.html",
        "content_type": "text/html; charset=utf-8",
        "cache_control": "public, max-age=300",
        "missing_msg": b"office not installed\n",
    },
    "/office-public.json": {
        "filename": "office-public.json",
        "content_type": "application/json",
        "cache_control": "public, max-age=60",
        "missing_msg": b"feed not built yet\n",
    },
}


def make_handler(root: str) -> type[BaseHTTPRequestHandler]:
    resolved_root = os.path.abspath(root)

    class PublicHandler(BaseHTTPRequestHandler):
        def version_string(self):
            return "factory"  # no Python version in the Server header
        timeout = 10  # a client that stalls mid-request gives its thread back

        def send_error(self, code, message=None, explain=None):
            # stdlib errors (400, 414, 431) get the same headers as every other response
            # (an HTTP/0.9 request line gets a bare body: the stdlib writes no headers for it)
            self.close_connection = True
            self._send(code, f"{code}\n".encode(), "text/plain; charset=utf-8")

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype, cache_control="no-store", extra_headers=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", csp())
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in extra_headers:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_405(self):
            self._send(
                405,
                b"method not allowed\n",
                "text/plain; charset=utf-8",
                extra_headers=[("Allow", "GET, HEAD")],
            )

        def do_GET(self):
            self._handle()

        def do_HEAD(self):
            self._handle()

        def __getattr__(self, name):
            if name.startswith("do_"):  # every method but GET and HEAD
                return self._send_405
            raise AttributeError(name)

        def _handle(self):
            path = urlparse(self.path).path
            route = ROUTES.get(path)
            if not route:
                return self._send(404, b"not found\n", "text/plain; charset=utf-8")
            filepath = os.path.join(resolved_root, route["filename"])
            try:
                with open(filepath, "rb") as f:
                    content = f.read()
            except (FileNotFoundError, IsADirectoryError):
                return self._send(503, route["missing_msg"], "text/plain; charset=utf-8")
            except OSError:
                return self._send(503, b"temporarily unavailable\n", "text/plain; charset=utf-8")
            return self._send(200, content, route["content_type"], route["cache_control"])

    return PublicHandler


def serve(host: str = "127.0.0.1", port: int = 3046) -> None:
    root = store.path("site-public")
    handler_cls = make_handler(root)
    srv = ThreadingHTTPServer((host, port), handler_cls)
    print(f"factory: serving public office http://{host}:{port}/", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
