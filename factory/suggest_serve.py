"""Public write service for suggestions and votes.

Separate from factory/serve.py and factory/public_serve.py to prevent private route exposure.
Serves:
  GET/HEAD /suggest/ideas.json
  POST     /suggest/idea   (201 carries "look": {fit, effort, source} from suggest_look, null when
                            the idea is held or the first look is off, capped or failed)
  POST     /suggest/vote
"""
import base64
import hashlib
import hmac
import ipaddress
import json
import re
import sys
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import public_serve, secrets, suggest_look, suggestions


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(s: str) -> bytes:
    pad = len(s) % 4
    if pad:
        s += "=" * (4 - pad)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def make_vote_token(product: str, uid: str, exp: int, secret_key: str) -> str:
    """Helper to generate a valid vote token for testing and products."""
    payload = {"product": product, "uid": uid, "exp": exp}
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_b64 = b64url_encode(payload_json)
    sig = hmac.new(secret_key.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    sig_b64 = b64url_encode(sig)
    return f"{payload_b64}.{sig_b64}"


def _reject_constant(name):
    raise ValueError(name)


def parse_and_verify_token(token: str) -> tuple[dict | None, str | None]:
    """Verify vote token format, key, signature, and expiry.
    Returns (payload, None) on success or (None, "invalid token") on ANY failure, so the reply
    never tells a caller which products have a key.
    """
    bad = (None, "invalid token")
    if not isinstance(token, str):
        return bad
    parts = token.split(".")
    if len(parts) != 2:
        return bad
    payload_b64, sig_b64 = parts

    try:
        payload = json.loads(b64url_decode(payload_b64), parse_constant=_reject_constant)
        received_sig = b64url_decode(sig_b64)
    except Exception:
        return bad
    if not isinstance(payload, dict):
        return bad

    product = payload.get("product")
    uid = payload.get("uid")
    exp = payload.get("exp")
    if not isinstance(product, str) or not isinstance(uid, str):
        return bad
    now = time.time()
    # the range test alone rejects NaN and infinities, and comparing a huge int with a float never
    # overflows (math.isfinite would raise OverflowError on it before the signature is checked)
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or not now < exp <= now + 3660:
        return bad

    try:
        key = secrets.get_secret("suggest-vote-key-" + product)
    except Exception:
        return bad
    if not key:
        return bad

    expected_sig = hmac.new(key.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected_sig, received_sig):
        return bad
    return payload, None


def rate_key(ip: str) -> str:
    """IPv6 clients get a whole /64 each, so one client cannot dodge the limit by rotating addresses."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(ip + "/64", strict=False))
    return str(addr)


def _bad_text_char(c: str) -> bool:
    """Control and format characters are rejected, except newline, tab and the two joiners that
    Persian and emoji sequences need."""
    cat = unicodedata.category(c)
    if cat == "Cc":
        return c not in "\n\t"
    return cat == "Cf" and c not in "\u200c\u200d"


class SuggestHandler(BaseHTTPRequestHandler):
    def version_string(self):
        return "factory"

    timeout = 10

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        body = json.dumps({"error": str(message or code)}).encode("utf-8")
        self._send(code, body, "application/json", cache_control="no-store")

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype, cache_control="no-store", extra_headers=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", public_serve.csp())
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra_headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, code, obj, cache_control="no-store", extra_headers=()):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json", cache_control=cache_control, extra_headers=extra_headers)

    def _send_405(self, allow):
        self.close_connection = True
        self._send_json(405, {"error": "method not allowed"}, extra_headers=[("Allow", allow)])

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            if path == "/suggest/ideas.json":
                shown = set(suggestions.config_list("office.publicProducts"))
                ideas = [i for i in suggestions.public_ideas() if i["product"] in shown]
                return self._send_json(200, {"ideas": ideas}, cache_control="public, max-age=30")
            if path in ("/suggest/idea", "/suggest/vote"):
                return self._send_405("POST")
            return self._send_json(404, {"error": "not found"})
        except Exception:
            return self._send_json(500, {"error": "internal error"})

    do_HEAD = do_GET  # _send skips the body for HEAD

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/suggest/idea":
            return self._handle_post_idea()
        if path == "/suggest/vote":
            return self._handle_post_vote()
        if path == "/suggest/ideas.json":
            return self._send_405("GET, HEAD")
        return self._send_json(404, {"error": "not found"})

    def __getattr__(self, name):
        if name.startswith("do_"):
            def _other():
                path = urlparse(self.path).path
                if path == "/suggest/ideas.json":
                    return self._send_405("GET, HEAD")
                if path in ("/suggest/idea", "/suggest/vote"):
                    return self._send_405("POST")
                return self._send_json(404, {"error": "not found"})
            return _other
        raise AttributeError(name)

    def _read_json_body(self) -> tuple[dict | None, int | None]:
        """Validate Content-Type and Content-Length, read body, parse JSON.
        Returns (data, None) on success or (None, http_status) on error.
        """
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._send_json(400, {"error": "Content-Type must be application/json"})
            return None, 400

        raw_len = self.headers.get("Content-Length")
        if not raw_len or not (raw_len.isascii() and raw_len.isdigit()):
            self._send_json(411, {"error": "length required"})
            return None, 411

        content_length = int(raw_len)
        if content_length > 2048:
            self._send_json(413, {"error": "payload too large"})
            return None, 413

        try:
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except Exception:
            self._send_json(400, {"error": "invalid json"})
            return None, 400

        if not isinstance(data, dict):
            self._send_json(400, {"error": "invalid json"})
            return None, 400

        return data, None

    def _handle_post_idea(self):
        try:
            data, status = self._read_json_body()
            if status is not None:
                return

            # Honeypot: website field
            if data.get("website"):
                return self._send_json(200, {"id": None})

            # Product validation
            product = data.get("product")
            if not isinstance(product, str) or product not in suggestions.config_list("office.publicProducts"):
                return self._send_json(400, {"error": "invalid product"})

            # Text validation
            text = data.get("text")
            if not isinstance(text, str):
                return self._send_json(400, {"error": "text must be a string"})
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            if any(_bad_text_char(c) for c in text):
                return self._send_json(400, {"error": "text has control characters"})
            text_stripped = text.strip()
            if not (10 <= len(text_stripped) <= 500):
                return self._send_json(400, {"error": "text must be 10-500 characters"})

            # Email validation (optional)
            email = data.get("email")
            if email is not None and email != "":
                if not isinstance(email, str):
                    return self._send_json(400, {"error": "invalid email"})
                email_clean = email.strip()
                if len(email_clean) > 254 or not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", email_clean):
                    return self._send_json(400, {"error": "invalid email"})
            else:
                email_clean = None

            # IP hashing and rate limiting
            cf_ip = self.headers.get("CF-Connecting-IP")
            ip = cf_ip.strip() if cf_ip else self.client_address[0]
            ip_hash = hmac.new(suggestions.suggest_salt(), rate_key(ip).encode("utf-8"), hashlib.sha256).hexdigest()

            added = suggestions.add_idea_limited(product, text_stripped, email_clean, ip_hash, limit=5)
            if added is None:
                return self._send_json(429, {"error": "rate limit exceeded"})
            idea_id, screened = added  # clean ideas are public at once, bad ones are held
            public = screened == 1
            look = suggest_look.first_look(product, text_stripped) if public else None  # held ideas never reach the model
            return self._send_json(
                201,
                {"id": idea_id, "product": product, "text": text_stripped, "status": "under-review", "public": public, "look": look},
            )
        except Exception:
            return self._send_json(500, {"error": "internal error"})

    def _handle_post_vote(self):
        try:
            data, status = self._read_json_body()
            if status is not None:
                return

            idea_id = data.get("idea_id")
            token = data.get("token")
            on = data.get("on")

            if not isinstance(idea_id, int) or isinstance(idea_id, bool) or not 1 <= idea_id <= suggestions.MAX_ID:
                return self._send_json(400, {"error": "bad request"})
            if not isinstance(token, str) or not isinstance(on, bool):
                return self._send_json(400, {"error": "bad request"})

            payload, err = parse_and_verify_token(token)
            if err:
                return self._send_json(401, {"error": err})

            idea = suggestions.get_idea(idea_id)
            if (
                not idea
                or idea.get("hidden") == 1
                or idea.get("screened") != 1
                or idea.get("product") not in suggestions.config_list("office.publicProducts")
            ):
                return self._send_json(404, {"error": "idea not found"})

            if idea.get("product") != payload.get("product"):
                return self._send_json(401, {"error": "invalid token"})

            account_hash = hmac.new(
                suggestions.suggest_salt(),
                f"{payload['product']}:{payload['uid']}".encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

            votes = suggestions.set_vote(idea_id, account_hash, on, limit=60)
            if votes is None:
                return self._send_json(429, {"error": "rate limit exceeded"})
            return self._send_json(200, {"idea_id": idea_id, "votes": votes, "on": on})
        except Exception:
            return self._send_json(500, {"error": "internal error"})


def serve(host: str = "127.0.0.1", port: int = 3047) -> None:
    suggestions.suggest_salt()  # create it before any request thread can race on it
    srv = ThreadingHTTPServer((host, port), SuggestHandler)
    print(f"factory: serving suggestions http://{host}:{port}/", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
