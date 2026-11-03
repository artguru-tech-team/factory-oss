"""Serve the built page and the answers API on 127.0.0.1.

GET  /                 the built page (~/.factory/site/factory.html)
GET  /api/answers      saved answers of the open inbox items
GET  /api/inbox/open   open inbox item ids ({"open": [ids...]})
POST /api/answers      {"<item id>": {"choice": "a", "note": "..."}}: merged into answers.json

Every request needs ?k=KEY (~/.factory/.key). Requests that came through the Cloudflare tunnel
must also carry a valid Access JWT (access_jwt.py); local ones skip that check.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import access_jwt, autopilot, board, relay, store, t3

MAX_BODY = 64 * 1024
MAX_NOTE = 4000
MISSING_KEY = (b"<!doctype html><meta charset=utf-8><title>Missing key</title><body style='font:16px system-ui;"
               b"padding:2rem;background:#08090a;color:#d0d6e0'><p>Open the dashboard's public URL and sign in "
               b"with Cloudflare Access. From this machine, add <code>?k=</code> and the key in "
               b"<code>~/.factory/.key</code>.</p>")


def csp(page):
    """Only the page's own inline script may run. If a domain zone has Cloudflare's Google tag
    gateway on, which injects GTM (and through it Microsoft Clarity) into every HTML response;
    this header keeps those scripts and their requests off the dashboard."""
    hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(m).digest()).decode()
                      for m in re.findall(rb"<script>(.*?)</script>", page, re.S)) or "'none'"
    return ("default-src 'none'; script-src %s; style-src 'unsafe-inline'; font-src data:; img-src data:; "
            "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'" % hashes)


def save_answers(patch):
    """Validate and merge answers; returns (status, body)."""
    if not isinstance(patch, dict) or not 0 < len(patch) <= 50:
        return 400, {"error": "expected {id: {choice, note}}"}
    now = store.now_iso()
    relay_queue = []
    with store.lock():
        box = store.inbox()
        items = {it["id"]: it for it in box.get("items", []) if isinstance(it, dict) and "id" in it}
        resolved = {it["id"]: it for it in box.get("resolved", []) if isinstance(it, dict) and "id" in it}
        data = store.read("answers.json", {})
        for k, v in patch.items():
            it = items.get(k)
            if not it:
                res = resolved.get(k)
                if res:
                    ans = res.get("answer") or data.get(k)
                    answered_at = ans.get("at") if isinstance(ans, dict) else None
                    resolved_at = res.get("resolvedAt")
                    return 409, {
                        "error": "already handled",
                        "id": k,
                        "answeredAt": answered_at,
                        "resolvedAt": resolved_at,
                    }
                return 404, {"error": f"no open inbox item {k!r}"}
            if not isinstance(v, dict) or not isinstance(v.get("choice"), str) or v["choice"] not in {o[0] for o in it["options"]}:
                return 400, {"error": f"{k}: choice must be one of the item's options"}
            note = v.get("note") or ""
            if not isinstance(note, str) or len(note) > MAX_NOTE:
                return 400, {"error": f"{k}: note must be text under {MAX_NOTE} characters"}
            data[k] = {"choice": v["choice"], "note": note, "at": now}
            if it.get("thread"):
                relay_queue.append((it, v["choice"], note, now))
        store.write("answers.json", data)
        enqueued_any = False
        if relay_queue:
            for it, choice, note, at in relay_queue:
                try:
                    relay.enqueue(it, choice, note, at)
                    enqueued_any = True
                except Exception as e:
                    sys.stderr.write(f"relay enqueue failed: {e}\n")
    if enqueued_any:
        try:
            relay.trigger_delivery_async()
        except Exception as e:
            sys.stderr.write(f"relay trigger failed: {e}\n")
    return 200, open_answers()


def open_answers():
    ids = {it["id"] for it in store.read("inbox.json", {"items": []})["items"]}
    return {k: v for k, v in store.read("answers.json", {}).items() if k in ids}


def open_inbox():
    box = store.inbox()
    open_ids = [it["id"] for it in box.get("items", []) if isinstance(it, dict) and "id" in it]
    closed = {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    data = store.read("answers.json", {})
    for res in box.get("resolved", []):
        if not isinstance(res, dict) or not res.get("id"):
            continue
        k = res["id"]
        resolved_at = res.get("resolvedAt")
        ans = res.get("answer") or data.get(k)
        answered_at = ans.get("at") if isinstance(ans, dict) else None
        ts = resolved_at or answered_at
        if ts and isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    closed[k] = {"resolvedAt": resolved_at, "answeredAt": answered_at}
            except Exception:
                pass
    return {"open": open_ids, "closed": closed}
def live_agent_threads():
    """Return set of live T3 thread IDs from current agents data."""
    with closing(t3.connect()) as con:
        rows, _ = t3.agents(store.products(), store.projects(), con=con)
    return {r["thread"] for r in rows if r.get("thread")}


def is_subagent_thread(thread):
    return isinstance(thread, str) and t3.role_of(thread)[0] == "3"


def save_message(patch):
    """Validate thread and text; enqueues message; returns (status, body)."""
    if not isinstance(patch, dict):
        return 400, {"error": "expected {thread, text}"}
    if "retry" in patch:
        retry_id = patch.get("retry")
        if not isinstance(retry_id, str):
            return 400, {"error": "retry id must be a string"}
        entry = next((x for x in relay.load_outbox() if x.get("id") == retry_id), None)
        if not entry or entry.get("kind") not in ("message", "autopilot", "answer"):
            return 400, {"error": f"unknown message {retry_id!r}"}
        if is_subagent_thread(entry.get("thread") or ""):
            return 400, {"error": "subagents cannot receive messages; message the parent"}
        if entry.get("thread") not in live_agent_threads():
            return 400, {"error": "unknown or non-T3 thread"}
        with store.lock():
            outbox = relay.load_outbox()
            target = next((x for x in outbox if x.get("id") == retry_id), None)
            # only a finished attempt can be retried: retrying one in flight would send it twice
            if not target or target.get("state") not in ("failed", "stale", "queued"):
                return 400, {"error": "message is being sent or already sent"}
            target["state"] = "queued"
            target["tries"] = 0
            target["confirmTries"] = 0
            target["sends"] = target.get("sends", 0) + 1
            target["bodyId"] = f"{target['id']}-r{target['sends']}"
            target.pop("lastError", None)
            target.pop("claimedAt", None)
            store.write(relay.RELAY_REL, outbox)
        relay.trigger_delivery_async()
        return 200, entry
    thread = patch.get("thread")
    if not isinstance(thread, str):
        return 400, {"error": "unknown or non-T3 thread"}
    if is_subagent_thread(thread):
        return 400, {"error": "subagents cannot receive messages; message the parent"}
    if thread not in live_agent_threads():
        return 400, {"error": "unknown or non-T3 thread"}
    text = patch.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        return 400, {"error": "text must be 1..2000 characters"}
    entry = relay.enqueue_message(thread, text.strip())
    relay.trigger_delivery_async()
    return 200, entry


def save_proto(patch):
    """Validate or send back a prototype's latest version; returns (status, body)."""
    if not isinstance(patch, dict):
        return 400, {"error": "expected {id, action, reason}"}
    pid = patch.get("id")
    action = patch.get("action")
    if not isinstance(pid, str) or action not in ("validate", "sendback"):
        return 400, {"error": "id and action (validate|sendback) are required"}
    seen = patch.get("v")
    if type(seen) is not int:
        return 400, {"error": "v (the version on screen) is required"}
    reason = patch.get("reason") or ""
    if not isinstance(reason, str):
        return 400, {"error": "reason must be text"}
    reason = reason.strip()
    if len(reason) > 2000:
        return 400, {"error": "reason must be at most 2000 characters"}
    if action == "sendback" and not reason:
        return 400, {"error": "a reason is required to send back"}
    with store.lock():
        protos = store.protos()
        proto = store.find_proto(protos, pid)
        if not proto:
            return 404, {"error": f"no prototype {pid!r}"}
        latest = proto["versions"][0]
        if latest.get("v") != seen:
            # an agent added a newer version after the page was built: never decide on one nobody has opened
            return 409, {"error": f"v{latest.get('v')} replaced v{seen}", "version": latest}
        if latest.get("status") != "waiting":
            # the page was built before this decision (another tab, or a reload before the next build)
            return 409, {"error": f"v{latest.get('v')} is already {latest.get('status')}", "version": latest}
        date = datetime.now(timezone.utc).strftime("%d %b %Y")
        if action == "validate":
            latest["status"] = "validated"
            latest["decisions"] = [f"Validated on {date}"]
        else:
            latest["status"] = "sent-back"
            latest["sent_back_reason"] = reason
        store.save_protos(protos)
        thread = latest.get("thread")
        feature = proto.get("feature") or ""
        v = latest.get("v")
        if action == "validate":
            text = f"Prototype {feature} v{v}: validated"
        else:
            text = f"Prototype {feature} v{v}: sent back: {reason}"
    if not thread:
        return 200, {"delivered": False, "why": "not in T3", "version": latest}
    try:
        if is_subagent_thread(thread) or thread not in live_agent_threads():
            return 200, {"delivered": False, "why": "not in T3", "version": latest}
        relay.enqueue_message(thread, text)
    except Exception as e:  # the decision is saved (T3 unreadable, outbox corrupt): say so instead of a 500
        sys.stderr.write(f"factory serve: proto relay failed: {type(e).__name__}\n")
        return 200, {"delivered": False, "why": f"message not queued ({type(e).__name__})", "version": latest}
    relay.trigger_delivery_async()
    return 200, {"delivered": True, "version": latest}


def save_autopilot(patch):
    """POST /api/autopilot: toggle / save / add / send / pause / resume. Returns (status, body)."""
    if not isinstance(patch, dict):
        return 400, {"error": "expected a JSON object"}
    action = patch.get("action")
    if action in ("pause", "resume"):
        with store.lock():
            cfg = store.autopilot()
            cfg["paused"] = action == "pause"
            cfg["pausedAt"] = store.now_iso() if cfg["paused"] else None
            store.save_autopilot(cfg)
        return 200, {"paused": cfg["paused"], "pausedAt": cfg["pausedAt"]}
    if action == "toggle":
        pid, enabled = patch.get("project"), patch.get("enabled")
        if not isinstance(pid, str) or not isinstance(enabled, bool):
            return 400, {"error": "project (string) and enabled (boolean) are required"}
        with store.lock():
            cfg = store.autopilot()
            entry = cfg.get("projects", {}).get(pid)
            if entry is None:
                return 404, {"error": f"no autopilot project {pid!r}"}
            entry["enabled"] = enabled
            store.save_autopilot(cfg)
        return 200, {"project": pid, "enabled": enabled}
    if action == "save":
        pid = patch.get("project")
        if not isinstance(pid, str):
            return 400, {"error": "project is required"}
        goal, every = patch.get("goal"), patch.get("every_hours")
        if goal is not None and (not isinstance(goal, str) or not goal.strip()):
            return 400, {"error": "goal must be non-empty text"}
        if every is not None and (err := autopilot.every_error(every)):
            return 400, {"error": err}
        with store.lock():
            cfg = store.autopilot()
            entry = cfg.get("projects", {}).get(pid)
            if entry is None:
                return 404, {"error": f"no autopilot project {pid!r}"}
            if goal is not None:
                entry["goal"] = goal.strip()
                entry["goal_source"] = "page"
            if every is not None:
                entry["every_hours"] = every
            store.save_autopilot(cfg)
        return 200, entry
    if action == "add":
        pid = patch.get("project")
        thread = patch.get("thread")
        goal = patch.get("goal")
        every = patch.get("every_hours", autopilot.DEFAULT_EVERY)
        enabled = patch.get("enabled", False)
        if not isinstance(pid, str):
            return 400, {"error": "project is required"}
        if not isinstance(thread, str):
            return 400, {"error": "thread is required"}
        if not isinstance(goal, str) or not goal.strip():
            return 400, {"error": "goal must be non-empty text"}
        if (err := autopilot.every_error(every)):
            return 400, {"error": err}
        if not isinstance(enabled, bool):
            return 400, {"error": "enabled must be a boolean"}
        try:
            store.find_project(pid)
        except store.FactoryError as e:
            return 400, {"error": str(e)}
        if not autopilot.drivable(thread):
            return 400, {"error": f"thread {autopilot.NOT_DRIVABLE}"}
        try:
            if thread not in autopilot.agent_rows(store.products(), store.projects()):
                return 400, {"error": "thread is not a live T3 thread"}
        except Exception as e:
            return 400, {"error": f"cannot read T3: {type(e).__name__}"}
        with store.lock():
            cfg = store.autopilot()
            if pid in cfg.get("projects", {}):
                return 409, {"error": f"project {pid!r} is already on autopilot"}
            if (other := autopilot.thread_taken(cfg, thread, pid)):
                return 409, {"error": f"that session already runs autopilot for {other!r}"}
            cfg["projects"][pid] = {"thread": thread, "goal": goal.strip(), "every_hours": every,
                                    "enabled": enabled, "max_per_day": autopilot.DEFAULT_MAX_PER_DAY,
                                    "goal_source": "page", "last_nudge_at": None, "history": []}
            store.save_autopilot(cfg)
        return 200, cfg["projects"][pid]
    if action == "send":
        pid = patch.get("project")
        if not isinstance(pid, str):
            return 400, {"error": "project is required"}
        try:
            res = autopilot.nudge_project(pid, allow_waiting=patch.get("confirmed") is True)
        except store.FactoryError as e:
            return 404, {"error": str(e)}
        return 200, res
    return 400, {"error": f"unknown action {action!r}"}


def save_board(patch):
    """POST /api/board: promote / close / send_back / auto_promote / refresh. Returns (status, body)."""
    if not isinstance(patch, dict):
        return 400, {"error": "expected a JSON object"}
    action = patch.get("action")
    if action == "auto_promote":
        enabled = patch.get("enabled")
        if not isinstance(enabled, bool):
            return 400, {"error": "enabled (boolean) is required"}
        with store.lock():
            data = board.set_auto_promote(enabled)
        return 200, {"auto_promote": data["auto_promote"]}
    if action == "refresh":
        data = board.collect(store.products())
        data["auto_promote"] = board.auto_promote()
        return 200, data
    repo, number, text = patch.get("repo"), patch.get("number"), patch.get("text")
    try:
        result = board.act(action, repo, number, text)
    except board.BoardError as e:  # a validation/state refusal (400/409/502), not a gh failure
        return e.status, {"error": str(e)}
    return 200, result


def board_data():
    """GET /api/board: the cached board (cards, fetchedAt, error, repoErrors) plus auto_promote."""
    cached = store.read("board-cache.json", {"cards": [], "fetchedAt": None})
    return {"cards": cached.get("cards", []), "fetchedAt": cached.get("fetchedAt"),
            "error": cached.get("error"), "repoErrors": cached.get("repoErrors", {}),
            "auto_promote": board.auto_promote()}


def board_timeline(query):
    repo = query.get("repo", [None])[0]
    raw = query.get("number", [None])[0]
    if not repo or not raw:
        return 400, {"error": "repo and number are required"}
    try:
        number = int(raw)
    except (ValueError, TypeError):
        return 400, {"error": "number must be a positive whole number"}
    try:
        return 200, board.timeline(repo, number)
    except board.BoardError as e:
        return e.status, {"error": str(e)}


PROTO_PATH = re.compile(r"/protos/([a-z0-9-]+)/v(\d+)\.html")


class Handler(BaseHTTPRequestHandler):
    server_version = "factory"
    timeout = 15  # a client that sends less than its Content-Length cannot hold a thread

    def _send(self, code, body, ctype, headers=()):
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, no-transform")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _gate(self):
        """Returns the URL path when the request may go on, else answers it and returns None."""
        if not access_jwt.allowed(self.headers):
            self._json(403, {"error": "Cloudflare Access login required"})
            return None
        url = urlparse(self.path)
        # through the tunnel, the Access JWT checked above is the login: no key in the links the owner opens.
        # From this machine (no Cloudflare headers, so no JWT) the key is still required.
        if self.headers.get("Cf-Ray") or self.headers.get("Cf-Connecting-Ip"):
            return url.path
        q = parse_qs(url.query)
        given = q.get("k", [""])[0].encode()
        token = q.get("t", [""])[0].encode()
        if token and PROTO_PATH.fullmatch(url.path) and hmac.compare_digest(
                token, store.proto_token(url.path.lstrip("/")).encode()):
            return url.path
        if not given or not hmac.compare_digest(given, store.key().encode()):
            if url.path.startswith("/api/") or self.command != "GET":
                self._json(403, {"error": "missing or wrong key"})
            else:
                self._send(401, MISSING_KEY, "text/html; charset=utf-8", [("Content-Security-Policy", csp(MISSING_KEY))])
            return None
        return url.path

    def do_GET(self):
        self._safe(self._get)

    def do_POST(self):
        self._safe(self._post)

    def _safe(self, fn):
        try:
            fn()
        except store.FactoryError as e:  # e.g. a corrupt state file: say so instead of dropping the connection
            self._json(500, {"error": str(e)})
        except Exception as e:  # hand-edited state with a missing field: still an answer, never a dropped request
            sys.stderr.write(f"factory serve: {type(e).__name__} on {urlparse(self.path).path}\n")
            self._json(500, {"error": f"server error ({type(e).__name__}); see the server log"})

    def _get(self):
        p = self._gate()
        if p is None:
            return
        if p == "/api/answers":
            return self._json(200, open_answers())
        if p == "/api/inbox/open":
            return self._json(200, open_inbox())
        if p == "/api/messages":
            url = urlparse(self.path)
            thread = parse_qs(url.query).get("thread", [None])[0]
            if not thread:
                return self._json(400, {"error": "thread parameter required"})
            return self._json(200, relay.thread_history(thread))
        if p == "/api/board/timeline":
            url = urlparse(self.path)
            self._json(*board_timeline(parse_qs(url.query)))
            return
        if p == "/api/board":
            return self._json(200, board_data())
        if p in ("/", "/factory.html"):
            try:
                with open(store.path("site", "factory.html"), "rb") as f:
                    page = f.read()
                return self._send(200, page, "text/html; charset=utf-8", [("Content-Security-Policy", csp(page))])
            except FileNotFoundError:
                return self._json(503, {"error": "page not built yet: run factory build"})
        m = PROTO_PATH.fullmatch(p)
        if m:
            rel = os.path.join("protos", m.group(1), f"v{int(m.group(2))}.html")
            try:
                with open(store.path(rel), "rb") as f:
                    content = f.read()
                return self._send(200, content, "text/html; charset=utf-8",
                                  [("Content-Security-Policy", "sandbox allow-scripts")])
            except FileNotFoundError:
                return self._json(404, {"error": "not found"})
        return self._json(404, {"error": "not found"})

    def _post(self):
        p = self._gate()
        if p is None:
            return
        if p not in ("/api/answers", "/api/messages", "/api/protos", "/api/autopilot", "/api/board"):
            return self._json(404, {"error": "not found"})
        # no key in the URL, so the Access cookie is the only credential: refuse what another page can forge.
        # A cross-site JSON POST needs a preflight, which this server never grants (no OPTIONS).
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"error": "send application/json"})
        if self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
            return self._json(403, {"error": "cross-site request refused"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            return self._json(413, {"error": "body too large or empty"})
        try:
            patch = json.loads(self.rfile.read(length))
        except (ValueError, RecursionError):
            return self._json(400, {"error": "body is not JSON"})
        if p == "/api/answers":
            self._json(*save_answers(patch))
        elif p == "/api/messages":
            self._json(*save_message(patch))
        elif p == "/api/autopilot":
            self._json(*save_autopilot(patch))
        elif p == "/api/board":
            self._json(*save_board(patch))
        else:
            self._json(*save_proto(patch))

    def log_message(self, fmt, *args):
        # never log the query string: it carries the key
        sys.stderr.write("%s %s %s\n" % (self.log_date_time_string(), getattr(self, "command", "-"),
                                          urlparse(getattr(self, "path", "") or "").path))


def run(port=3045):
    store.key()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"factory: serving http://127.0.0.1:{port}/?k=<key in {store.path('.key')}>", file=sys.stderr)
    srv.serve_forever()
