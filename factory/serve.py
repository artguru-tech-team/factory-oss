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
from urllib.parse import parse_qs, unquote, urlparse

from . import access_jwt, assign, autopilot, board, comms, config, office, queue, relay, release, roadmap, store, t3

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
                      for m in re.findall(rb"<script(?![^>]*\bsrc\b)[^>]*>(.*?)</script>", page, re.S | re.I)) or "'none'"
    return ("default-src 'none'; script-src %s; style-src 'unsafe-inline'; font-src data:; img-src data:; "
            "connect-src 'self'; frame-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'" % hashes)


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
    ids = {it["id"] for it in store.read("inbox.json", {"items": []})["items"] if isinstance(it, dict) and "id" in it}
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
def live_agent_rows():
    """The current agents rows that name a T3 thread, by thread id."""
    with closing(t3.connect()) as con:
        rows, _ = t3.agents(store.products(), store.projects(), con=con)
    return {r["thread"]: r for r in rows if r.get("thread")}


def live_agent_threads():
    """Return set of live T3 thread IDs from current agents data."""
    return set(live_agent_rows())


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
        if not entry or entry.get("kind") not in ("message", "autopilot", "answer", "release"):
            return 400, {"error": f"unknown message {retry_id!r}"}
        if is_subagent_thread(entry.get("thread") or ""):
            return 400, {"error": "subagents cannot receive messages; message the parent"}
        # an idle dispatcher drops out of the agents rows after 24 h: open in T3 is enough to retry
        if entry.get("thread") not in live_agent_threads() and relay.is_thread_live(entry.get("thread")) is not True:
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
    text = text.strip()
    item_id = patch.get("item")
    if item_id is not None:
        # a message sent from a Waiting on you row quotes the question it is about
        if not isinstance(item_id, str):
            return 400, {"error": "item must be an inbox item id"}
        it = next((x for x in store.inbox().get("items", []) if isinstance(x, dict) and x.get("id") == item_id), None)
        if not it:
            return 404, {"error": f"no open inbox item {item_id!r}"}
        # ponytail: checked outside the lock, so two tabs linking one question at once can both send; the
        # second is told the winner in itemThread. Single owner, so rare; one lock over check+enqueue+link fixes it.
        if it.get("thread") and it["thread"] != thread:
            return 400, {"error": f"{item_id} belongs to another session; message that one", "itemThread": it["thread"]}
        title = str(it.get("title") or "")
        text = f"About {item_id} ({title[:120]}): {text}"
    entry = relay.enqueue_message(thread, text)
    if item_id is not None:
        item_thread = it.get("thread")
        if patch.get("link") is True and not item_thread:
            try:  # the message is queued: a failed link must not make the page resend it
                item_thread = link_item(item_id, thread)
            except Exception as e:
                sys.stderr.write(f"link {item_id} to {thread}: {e}\n")
        # the row badge shows the thread the item really has, not the one the page asked for
        entry = {**entry, "itemThread": item_thread}
    relay.trigger_delivery_async()
    return 200, entry


def link_item(item_id, thread):
    """Record thread on an open inbox item asked outside T3 (never replaces one). An answer saved
    before the link was never relayed, so it is relayed now, in the same lock. Returns the item's
    thread afterwards (None when the item is gone)."""
    with store.lock():
        box = store.inbox()
        it = next((x for x in box.get("items", []) if isinstance(x, dict) and x.get("id") == item_id), None)
        if not it or it.get("thread"):
            return it.get("thread") if it else None
        it["thread"] = thread
        store.write("inbox.json", box)
        ans = store.read("answers.json", {}).get(item_id)
        if isinstance(ans, dict) and ans.get("choice"):
            relay.enqueue(it, ans["choice"], ans.get("note") or "", ans.get("at") or store.now_iso())
        return thread


def save_assign(patch):
    """Assign an agent to a project: {project, mode:"new", provider, brief} asks the dispatcher session to
    launch one; {project, mode:"existing", thread, brief} links a free live session and briefs it.
    Returns (status, relay entry) like save_message, so Retry reuses /api/messages."""
    if not isinstance(patch, dict):
        return 400, {"error": "expected {project, mode, brief, provider|thread}"}
    brief = patch.get("brief")
    if not isinstance(brief, str) or not brief.strip() or len(brief.strip()) > 2000:
        return 400, {"error": "brief must be 1..2000 characters"}
    brief = brief.strip()
    mode = patch.get("mode")
    if mode not in ("new", "existing"):
        return 400, {"error": "mode must be new or existing"}
    pid = patch.get("project")
    try:
        project = store.find_project(pid) if isinstance(pid, str) else None
    except store.FactoryError:
        project = None
    if not project:
        return 404, {"error": f"no project {pid!r}"}
    rows = live_agent_rows()
    if mode == "new":
        provider = patch.get("provider")
        if provider not in assign.PROVIDERS:
            return 400, {"error": f"provider must be one of {', '.join(assign.PROVIDERS)}"}
        disp = assign.dispatcher()
        if not disp:
            return 409, {"error": "the dispatcher session is offline", "fix": assign.FIX}
        asked = assign.live_pending(assign.read_pending(), pid, datetime.now(timezone.utc))  # a corrupt file fails here, before anything is queued
        if asked and patch.get("force") is not True:
            return 409, {"error": f"an agent is already starting for this project (asked {asked})", "pending": True}
        entry = relay.enqueue_message(disp, assign.dispatch_text(project, provider, brief))
        with store.lock():
            assign.add_pending(pid, provider, entry["id"], datetime.now(timezone.utc))
    else:
        thread = patch.get("thread")
        if not isinstance(thread, str) or thread not in rows:
            return 400, {"error": "unknown or non-T3 thread"}
        if is_subagent_thread(thread):
            return 400, {"error": "subagents cannot receive messages; message the parent"}
        if thread == config.get("assign.dispatcher"):
            return 400, {"error": "the dispatcher session launches agents; it cannot be one"}
        # the same project again is a re-send (Edit brief after a failed delivery), not a conflict
        if rows[thread].get("project") not in (None, pid):
            return 409, {"error": f"thread already works on project {rows[thread]['project']!r}"}
        with store.lock():
            linked = store.thread_links().get(thread)
            if linked not in (None, pid) and linked in {p["id"] for p in store.projects()}:  # another tab linked it since the rows were read
                return 409, {"error": f"thread already works on project {linked!r}"}
            store.set_thread_link(thread, pid)
        entry = relay.enqueue_message(thread, brief)
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


def roadmap_data(query):
    """GET /api/roadmap?product=P: items with flags, live agents and progress, sources, cap."""
    try:
        return 200, roadmap.view(query.get("product", [None])[0])
    except store.FactoryError as e:
        return 500, {"error": str(e)}


ROADMAP_FIELDS = ("id", "lane", "before", "id_in", "id_out", "product", "title", "due", "note", "project")


def save_roadmap(patch):
    if not isinstance(patch, dict):
        return 400, {"error": "expected a JSON object"}
    bad = [k for k in ROADMAP_FIELDS if patch.get(k) is not None and not isinstance(patch[k], str)]
    if bad:
        return 400, {"error": f"must be strings: {', '.join(bad)}"}
    action = patch.get("action")
    if action not in ("move", "swap", "add", "set"):
        return 400, {"error": "action must be move, swap, add, or set"}
    try:
        if action == "move":
            iid = patch.get("id")
            lane = patch.get("lane")
            if not iid or not lane:
                return 400, {"error": "id and lane are required"}
            before = patch.get("before")
            item = roadmap.move(iid, lane, before=before)
            return 200, {"ok": True, "item": item}
        elif action == "swap":
            id_in = patch.get("id_in")
            id_out = patch.get("id_out")
            if not id_in or not id_out:
                return 400, {"error": "id_in and id_out are required"}
            before = patch.get("before")
            res = roadmap.swap(id_in, id_out, before=before)
            return 200, {"ok": True, **res}
        elif action == "add":
            prod = patch.get("product")
            lane = patch.get("lane")
            title = patch.get("title")
            if not prod or not lane or not title:
                return 400, {"error": "product, lane, and title are required"}
            item = roadmap.add(prod, lane, title)
            return 200, {"ok": True, "item": item}
        elif action == "set":
            iid = patch.get("id")
            if not iid:
                return 400, {"error": "id is required"}
            item = roadmap.update(iid, title=patch.get("title"), due=patch.get("due"),
                                  note=patch.get("note"), project=patch.get("project"))
            return 200, {"ok": True, "item": item}
    except roadmap.RoadmapError as e:
        if e.code == "now_full":
            return 409, {"error": "now_full", "now": e.now}
        elif e.code == "notion_error":
            return 502, {"error": str(e)}
        elif e.code == "notion_partial":
            return 502, {"error": str(e), "partial": True}
        elif e.code == "readonly":
            return 423, {"error": str(e)}
        return 400, {"error": str(e)}
    except store.FactoryError as e:
        return 400, {"error": str(e)}

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


def _product_meta(name, p):
    """Display fields the dashboard reads for a product (name, colour, short id, flow label)."""
    return {"name": comms.product_name(name), "color": p.get("color") or "", "short": p.get("short") or name,
            "flow_label": p.get("flow_label") or ""}


def queue_data():
    """GET /api/queue: release.json["queue"] snapshot plus products config names, flows and modes."""
    rel = release.release()
    snap = rel.get("queue")
    if not snap:
        snap = {"at": None, "products": {}}
    else:
        snap = dict(snap)

    prods_cfg = {}
    for name, p in rel.get("products", {}).items():
        prods_cfg[name] = {
            **_product_meta(name, p),
            "repo": p.get("repo"),
            "flow": p.get("flow"),
            "mode": p.get("mode"),
        }
    snap["product_configs"] = prods_cfg

    return snap


def comms_data():
    """GET /api/comms: changelogs, jobs, due, and rules summary per comms product in the release config."""
    rel = release.release()
    configured_products = rel.get("products", {})
    with store.lock():
        c_data = comms.comms()

    all_changelogs = c_data.get("changelogs", {})
    all_jobs = c_data.get("jobs", {})

    products = {}
    for prod, cfg in comms.comms_products().items():
        prod_data = _product_meta(prod, configured_products[prod])
        if prod not in c_data.get("since", {}):
            prod_data["not_started"] = True

        # 1. changelogs (newest first, at most 10)
        prod_cls = [
            cl for cl in all_changelogs.values()
            if isinstance(cl, dict) and cl.get("product") == prod
        ]
        prod_cls.sort(key=lambda x: x.get("at") or "", reverse=True)
        prod_data["changelogs"] = prod_cls[:10]

        # 2. jobs (all statuses, newest first)
        prod_j = [
            j for j in all_jobs.values()
            if isinstance(j, dict) and j.get("product") == prod
        ]
        prod_j.sort(key=lambda x: x.get("updated") or x.get("at") or x.get("id") or "", reverse=True)
        prod_data["jobs"] = prod_j
        prod_data["deferred"] = c_data.get("deferred", {}).get(prod, {})

        # 3. rules summary (internal list, big definition, Friday 10:00 Tunis, language order)
        internal_scopes_combined = sorted(comms.INTERNAL_SCOPES | cfg.get("internal_scopes", set()))
        internal_list = sorted(set(comms.INTERNAL_PREFIXES) | set(internal_scopes_combined) | {"no-comms", "dependencies", "admin", "agent"})
        prod_data["rules"] = {
            "internal_list": internal_list,
            "big_definition": "A PR linked to a roadmap FEAT row, or labelled 'big'. Everything else is small.",
            "friday_timing": "Friday 10:00 Tunis",
            "language_order": cfg.get("langs", []),
            "audiences": cfg.get("audiences", []),
        }

        # 4. the current due() list and window facts. A failure for one product must not break the others: return error for that product.
        try:
            prod_data["due"] = comms.due(prod, write=False)
        except Exception as e:
            prod_data["error"] = str(e)
            prod_data["due"] = []

        try:
            prod_data["window"] = comms.window(prod)
        except Exception as e:
            prod_data.setdefault("error", str(e))
            prod_data["window"] = {
                "not_started": True,
                "cutoff": None,
                "until": None,
                "pending": [],
                "big_pending": [],
                "unsynced": [],
            }

        products[prod] = prod_data

    return {"products": products}


def save_queue(patch):
    """POST /api/queue: {repo_pr, action}. Returns (status, body)."""
    if not isinstance(patch, dict):
        return 400, {"error": "expected a JSON object"}
    repo_pr = patch.get("repo_pr")
    action = patch.get("action")
    if not isinstance(repo_pr, str) or not repo_pr.strip():
        return 400, {"error": "repo_pr is required"}
    if not isinstance(action, str) or not action.strip():
        return 400, {"error": "action is required"}

    action_clean = action.strip()
    repo_pr_clean = repo_pr.strip()

    if action_clean == "agent":
        rel = release.release()
        try:
            repo, num = queue._parse_repo_pr(repo_pr_clean)
        except store.FactoryError as e:
            return 400, {"error": str(e)}

        prod_name = None
        prod_cfg = None
        for pname, p in rel.get("products", {}).items():
            if p.get("repo", "").lower() == repo.lower():
                prod_name = pname
                prod_cfg = p
                break
        if not prod_cfg:
            return 400, {"error": f"repo {repo!r} does not belong to any configured product"}

        canonical_key = f"{prod_cfg.get('repo')}#{num}"
        snap = rel.get("queue", {}).get("products", {}).get(prod_name, {})
        prs = snap.get("prs", [])
        pr_obj = next((p for p in prs if p.get("number") == num), None)
        planned_job = (pr_obj.get("classification") or {}).get("does") if pr_obj else None
        if planned_job not in ("rebase agent", "fix agent", "review loop agent"):
            return 400, {"error": "no agent job for this PR"}

        try:
            res = queue.record_queue_action(repo_pr_clean, action_clean)
        except store.FactoryError as e:
            return 400, {"error": str(e)}

        now_utc = release._now_utc(None)
        today_tunis = now_utc.astimezone(release.TUNIS).date()
        iso_now = release._format_time(now_utc)

        cur_rel = release.release()
        sent_keys_today = set()
        sent_count = 0
        for entry in cur_rel.get("queue_log", []):
            if entry.get("product") == prod_name and entry.get("what") in ("agent-sent", "queue-agent-sent"):
                entry_at = entry.get("at")
                if entry_at and release._parse_utc(entry_at).astimezone(release.TUNIS).date() == today_tunis:
                    sent_count += 1
                    if entry.get("repo_pr"):
                        sent_keys_today.add(entry.get("repo_pr"))

        actions_data = cur_rel.get("queue_actions") or {}
        already_relayed_today = False
        for k, flags in actions_data.items():
            if isinstance(flags, dict) and "agent_relayed" in flags:
                m = queue.REPO_PR_RE.match(k)
                if m and m.group(1).lower() == prod_cfg.get("repo", "").lower():
                    relayed_at = flags.get("agent_relayed")
                    if relayed_at and release._try_parse_utc(relayed_at) and release._try_parse_utc(relayed_at).astimezone(release.TUNIS).date() == today_tunis:
                        if k == canonical_key:
                            already_relayed_today = True
                        if k not in sent_keys_today:
                            sent_count += 1
                            sent_keys_today.add(k)

        if sent_count >= 3 or already_relayed_today:
            return 200, {"queued": True}

        # claim the relay under the lock first, so a double click cannot relay twice
        with store.lock():
            cur = release.release()
            pr_flags = cur.setdefault("queue_actions", {}).setdefault(canonical_key, {})
            prev = release._try_parse_utc(pr_flags.get("agent_relayed") or "")
            if prev and prev.astimezone(release.TUNIS).date() == today_tunis:
                return 200, {"queued": True}
            pr_flags["agent_relayed"] = iso_now
            release.save_release(cur)

        if prod_cfg.get("mode") == "live" and prod_cfg.get("flow") != "factory":
            thread = prod_cfg.get("thread")
            if thread:
                job = "fix"
                title = ""
                if pr_obj:
                    title = pr_obj.get("title", "")
                    does = planned_job
                    if "rebase" in does:
                        job = "rebase"
                    elif "fix" in does:
                        job = "fix"
                    elif "review" in does:
                        job = "review"
                    elif does:
                        job = does
                d_str = today_tunis.strftime("%Y-%m-%d")
                title_part = f": {title}" if title else ""
                msg = (
                    f"Release manager (factory release queue, {d_str}):\n"
                    f"Product: {prod_name}\n"
                    f"Agent jobs:\n"
                    f"  - {canonical_key} ({job}){title_part}\n"
                    f"    once done: factory release queue-done {canonical_key} agent-sent"
                )
                try:
                    relay.enqueue_message(thread, msg, kind="release")
                    relay.trigger_delivery_async()
                except Exception as e:
                    # release the claim so today's list still offers this job; the action stays recorded
                    with store.lock():
                        cur = release.release()
                        flags = (cur.get("queue_actions") or {}).get(canonical_key)
                        if isinstance(flags, dict) and flags.get("agent_relayed") == iso_now:
                            flags.pop("agent_relayed", None)
                            release.save_release(cur)
                    return 200, {**res, "relay_error": str(e)}

        return 200, res

    try:
        res = queue.record_queue_action(repo_pr_clean, action_clean)
    except store.FactoryError as e:
        return 400, {"error": str(e)}

    return 200, res


PROTO_PATH = re.compile(r"/protos/([a-z0-9-]+)/v(\d+)\.html")
THREAD_MSGS_PATH = re.compile(r"^/api/threads/([^/]+)/messages$")
MACHINE_AGENTS_PATH = re.compile(r"^/api/machines/([a-z0-9-]{1,32})/agents$")
MAX_MACHINE_BODY = 1024 * 1024
# string-or-null fields a remote machine may send (no per-row tokens: the Office never reads them)
ALLOWED_AGENT_KEYS = (
    "hc", "who", "provider", "model", "via", "product", "project",
    "state", "t3Status", "since", "task", "thread", "parent",
)
KNOWN_STATES = {"waiting", "running", "stalled", "unknown", "quota", "failed", "done"}


def save_machine_agents(machine_id, patch, headers):
    """Validate and store pushed agents from a remote machine."""
    loc_id = office.machine_info()["id"]
    if machine_id == "vps" or machine_id == loc_id:
        return 400, {"error": f"cannot push for local machine {machine_id!r}"}

    token_header = headers.get("X-Factory-Machine-Token")
    stored_hash = office.machines_doc().get(machine_id, {}).get("tokenHash")
    given_hash = hashlib.sha256(token_header.strip().encode("utf-8")).hexdigest() if token_header else ""
    # constant-time compare even when there is nothing to match (unknown machine or no token)
    ok_hash = stored_hash if isinstance(stored_hash, str) and stored_hash and token_header else "0" * 64
    if not hmac.compare_digest(given_hash, ok_hash):
        return 403, {"error": "unknown machine or bad token"}

    if not isinstance(patch, dict) or not isinstance(patch.get("agents"), list):
        return 400, {"error": "expected dict with 'agents' list"}

    raw_agents = patch["agents"]
    if len(raw_agents) > 300:
        return 400, {"error": "at most 300 agent rows allowed"}
    if not all(isinstance(r, dict) for r in raw_agents):
        return 400, {"error": "agent rows must be objects"}

    clean_rows = []
    for r in raw_agents:
        if not isinstance(r.get("hc"), str) or not r["hc"].strip():
            continue  # a row without a handle cannot be shown or linked
        # only strings (cut to 500) or null: anything else would break the Office for every building
        row = {k: (r[k][:500] if isinstance(r.get(k), str) else None) for k in ALLOWED_AGENT_KEYS}
        if row["state"] not in KNOWN_STATES:
            row["state"] = "unknown"
        # handles and thread ids are only unique per machine: prefix them with the machine id
        for k in ("hc", "parent", "thread"):
            if row[k]:
                row[k] = f"{machine_id}:{row[k]}"
        row["canReceive"] = False
        row["machine"] = machine_id
        clean_rows.append(row)

    with store.lock():
        machines = office.machines_doc()
        if machine_id not in machines:
            return 403, {"error": "unknown machine or bad token"}
        machines[machine_id]["lastPushAt"] = store.now_iso()
        machines[machine_id]["agents"] = clean_rows
        store.write("machines.json", machines)

    return 200, {"ok": True, "accepted": len(clean_rows)}



def _no_constant(name):
    raise ValueError(f"{name} is not JSON")  # NaN / Infinity would be saved and then fail to write


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
            if not access_jwt.is_person(self.headers) and not (
                    self.command == "POST" and MACHINE_AGENTS_PATH.fullmatch(url.path)):
                self._json(403, {"error": "a service token may only push machine agents"})
                return None
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
        if p == "/api/roadmap":
            url = urlparse(self.path)
            self._json(*roadmap_data(parse_qs(url.query)))
            return
        if p == "/api/board":
            return self._json(200, board_data())
        if p == "/api/queue":
            return self._json(200, queue_data())
        if p == "/api/comms":
            return self._json(200, comms_data())
        if p == "/api/office":
            office_file = store.path("site", "office.json")
            try:
                with open(office_file, "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except FileNotFoundError:
                return self._json(503, {"error": "not built yet: run factory build"})
            box = store.inbox()
            open_items = []
            for it in box.get("items", []):
                if isinstance(it, dict) and "id" in it:
                    open_items.append({
                        k: it.get(k) for k in ("id", "kind", "title", "context", "options", "reco", "project", "from", "thread", "at")
                    })
            doc["inbox"] = open_items
            # answered but not yet resolved by the agent: the Office shows these as answered, never as open again
            doc["answers"] = open_answers()

            now_dt = datetime.now(timezone.utc)
            loc_machine = doc.get("machine") or office.machine_info()
            machines_list, remote_agents = office.get_machines_and_agents(
                loc_machine, doc.get("builtAt"), now_dt
            )
            doc["machines"] = machines_list
            for a in doc.get("agents", []):
                a.setdefault("machine", loc_machine["id"])  # office.json built before machines existed
            doc["agents"] = doc.get("agents", []) + remote_agents
            return self._json(200, doc)
        m = THREAD_MSGS_PATH.fullmatch(p)
        if m:
            raw_tid = unquote(m.group(1))
            if not re.fullmatch(r"[A-Za-z0-9_.:/%-]{1,1024}", raw_tid):  # delegate ids carry a literal %
                return self._json(404, {"error": "thread not found"})
            limit = parse_qs(urlparse(self.path).query).get("limit", [40])[0]  # thread_messages parses and clamps it
            with closing(t3.connect()) as con:
                if not t3.thread_exists(con, raw_tid):
                    return self._json(404, {"error": f"thread {raw_tid!r} not found"})
                msgs = t3.thread_messages(con, raw_tid, limit=limit)
            return self._json(200, msgs)
        if p in ("/", "/factory.html"):
            try:
                with open(store.path("site", "factory.html"), "rb") as f:
                    page = f.read()
                return self._send(200, page, "text/html; charset=utf-8", [("Content-Security-Policy", csp(page))])
            except FileNotFoundError:
                return self._json(503, {"error": "page not built yet: run factory build"})
        if p == "/office/":  # the bundle fetches relative api/...: only /office resolves them to /api/...
            q = urlparse(self.path).query
            return self._send(301, b"", "text/plain; charset=utf-8", [("Location", "/office" + ("?" + q if q else ""))])
        if p == "/office":
            try:
                with open(store.path("site", "office.html"), "rb") as f:
                    page = f.read()
                return self._send(200, page, "text/html; charset=utf-8", [("Content-Security-Policy", csp(page))])
            except FileNotFoundError:
                hint = b"<!doctype html><p>Office not installed: run <code>factory office install &lt;file.html&gt;</code></p>\n"
                return self._send(404, hint, "text/html; charset=utf-8", [("Content-Security-Policy", csp(hint))])
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
        m_push = MACHINE_AGENTS_PATH.fullmatch(p)
        if p not in ("/api/answers", "/api/messages", "/api/protos", "/api/autopilot", "/api/board", "/api/assign", "/api/queue", "/api/roadmap") and not m_push:
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
        max_body = MAX_MACHINE_BODY if m_push else MAX_BODY
        if not 0 < length <= max_body:
            return self._json(413, {"error": "body too large or empty"})
        try:
            patch = json.loads(self.rfile.read(length), parse_constant=_no_constant)
        except (ValueError, RecursionError):
            return self._json(400, {"error": "body is not JSON"})
        if m_push:
            self._json(*save_machine_agents(m_push.group(1), patch, self.headers))
        elif p == "/api/answers":
            self._json(*save_answers(patch))
        elif p == "/api/messages":
            self._json(*save_message(patch))
        elif p == "/api/autopilot":
            self._json(*save_autopilot(patch))
        elif p == "/api/board":
            self._json(*save_board(patch))
        elif p == "/api/roadmap":
            self._json(*save_roadmap(patch))
        elif p == "/api/assign":
            self._json(*save_assign(patch))
        elif p == "/api/queue":
            self._json(*save_queue(patch))
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
