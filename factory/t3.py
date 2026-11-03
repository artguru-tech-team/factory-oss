"""Read the T3 agent registry (~/.t3/userdata/statev2.sqlite), read-only.

The schema is internal to T3, so every column read here is listed in COLUMNS and checked
before any query; a change fails loudly with the missing table or column.
"""
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from .store import FactoryError, iso

COLUMNS = {
    "projection_projects": ["project_id", "workspace_root", "deleted_at"],
    "orchestration_v2_projection_threads": ["thread_id", "project_id", "title", "created_at", "updated_at",
                                            "archived_at", "deleted_at", "payload_json"],
    "orchestration_v2_projection_runs": ["thread_id", "status", "requested_at", "completed_at", "payload_json",
                                         "provider_instance_id"],
    "orchestration_v2_projection_provider_sessions": ["thread_id", "model", "driver", "updated_at"],
    "orchestration_v2_projection_provider_threads": ["thread_id", "driver", "payload_json"],
    "orchestration_v2_projection_runtime_requests": ["thread_id", "status"],
    "orchestration_v2_projection_subagents": ["child_thread_id", "status", "started_at", "completed_at",
                                              "payload_json", "driver", "provider_instance_id"],
    "orchestration_v2_projection_messages": ["thread_id", "role", "created_at", "payload_json"],
    "orchestration_v2_projection_turn_items": ["thread_id", "ordinal", "type", "payload_json"],
}
# Activity windows: done, failed and stalled threads silent for DONE_WINDOW drop off; a "running" run whose thread
# has been silent for SILENT is shown as stalled, never as a ghost "running" row.
DONE_WINDOW = timedelta(hours=24)
SILENT = timedelta(hours=2)
CLOSED_REQUESTS = ("resolved", "cancelled", "canceled", "expired", "failed", "rejected")
ORDER = ["waiting", "running", "stalled", "unknown", "quota", "failed", "done"]
# Every run and subagent status of T3 0.1.3 (OrchestrationV2RunStatus and the OrchestrationV2Subagent
# status literals in its bundle). A status outside these is shown as "unknown" (never as done) and
# reported by factory doctor and the build.
ACTIVE = ("preparing", "queued", "starting", "pending", "running", "waiting")
FAILED = ("failed", "interrupted", "rolled_back")
DONE = ("completed", "cancelled", "idle")
KNOWN_RUN = ACTIVE + FAILED + DONE
PROVIDERS = {"claudeAgent": "Claude", "antigravity": "Antigravity", "opencode": "OpenCode", "codex": "Codex"}


class T3SchemaError(FactoryError):
    pass


def db_path():
    return os.environ.get("FACTORY_T3_DB") or os.path.expanduser("~/.t3/userdata/statev2.sqlite")


def connect(path=None):
    path = path or db_path()
    if not os.path.exists(path):
        raise FactoryError(f"T3 database not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    check_schema(con)
    return con


def check_schema(con):
    missing = []
    for table, cols in COLUMNS.items():
        have = {r[1] for r in con.execute(f"pragma table_info('{table}')")}
        if not have:
            missing.append(f"table {table}")
            continue
        missing += [f"{table}.{c}" for c in cols if c not in have]
    if missing:
        raise T3SchemaError("T3 schema changed, factory cannot read: " + ", ".join(missing)
                            + " (update factory/t3.py COLUMNS and the queries)")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def handles(roles, created=None, taken=()):
    """{thread id: handle}: role digit + the shortest sha1 prefix (3+ characters) not taken yet.

    Older threads pick first (by creation time), so a newer thread can never change the handle of
    one that already had it while both are listed. `taken` holds handles already given elsewhere."""
    out, taken, created = {}, set(taken), created or {}
    for tid in sorted(roles, key=lambda t: (created.get(t) or "", t)):
        h = hashlib.sha1(tid.encode()).hexdigest().upper()
        n = 3
        while roles[tid] + h[:n] in taken:
            n += 1
        out[tid] = roles[tid] + h[:n]
        taken.add(out[tid])
    return out


def handles_for(threads, taken=()):
    """Handles for a {thread id: info} map from load_threads: the one place every view computes them."""
    return handles({tid: role_of(tid)[0] for tid in threads}, {tid: t["created"] for tid, t in threads.items()}, taken)


def thread_handles(con=None):
    """Return {thread_id: handle} for live T3 threads using handles()."""
    own = con is None
    con = con or connect()
    try:
        roots, threads = load_threads(con)
        return handles_for(threads)
    finally:
        if own:
            con.close()


def role_of(thread_id):
    if thread_id.startswith("thread:delegated-task:"):
        return "2", "Delegate", "T3 delegate"
    if thread_id.startswith("thread:provider:"):
        return "3", "Subagent", "T3 subagent"
    return "1", "Thread", "T3 thread"


def product_of(root, products):
    for key, prod in products.items():
        for ws in prod.get("workspaces", []):
            ws = ws.rstrip("/")
            if root == ws or root.startswith(ws + "/"):
                return key
    return None


def project_of(texts, product, projects):
    texts = [t.lower() for t in texts if t]
    for p in projects:
        if p["product"] == product and any(m.lower() in t for m in p.get("match", []) for t in texts):
            return p["id"]
    return None


def load_threads(con, include_gone=False):
    roots = {pid: root for pid, root, deleted in con.execute(
        "select project_id, workspace_root, deleted_at from projection_projects") if not deleted}
    threads = {}
    for tid, pid, title, created, updated, archived, deleted, payload in con.execute(
            "select thread_id, project_id, title, created_at, updated_at, archived_at, deleted_at, payload_json "
            "from orchestration_v2_projection_threads"):
        gone = bool(archived or deleted)
        if gone and not include_gone:
            continue
        threads[tid] = {"pid": pid, "title": title or "", "created": created, "updated": ts(updated),
                        "payload": json.loads(payload or "{}"), "gone": gone}
    return roots, threads


def _root_title(thread_info, threads):
    root_id = (thread_info["payload"].get("lineage") or {}).get("rootThreadId")
    return threads.get(root_id, {}).get("title", "") if root_id else ""


def thread_product(thread_info, threads, roots, products):
    """The product of a thread: its workspace first; for a session started outside every product
    workspace (e.g. the parent folder of all repos), the first segment of its own or its root
    session's title when that names a product (sessions are named <product>/<type>/<feature>)."""
    product = product_of(roots.get(thread_info["pid"], ""), products)
    if product:
        return product
    for title in (thread_info["title"], _root_title(thread_info, threads)):
        head = (title or "").split("/", 1)[0].strip().lower()
        if "/" in (title or "") and head in products:
            return head
    return None


def thread_project(thread_info, threads, roots, products, projects):
    product = thread_product(thread_info, threads, roots, products)
    if not product:
        return None
    return project_of([_root_title(thread_info, threads), thread_info["title"]], product, projects)


def thread_by_native_id(con, native_id):
    for tid, payload in con.execute("select thread_id, payload_json from orchestration_v2_projection_provider_threads"):
        if native_id and (json.loads(payload or "{}").get("nativeThreadRef") or {}).get("nativeId") == native_id:
            return tid
    return None


def thread_from_cwd(threads, cwd):
    """The thread whose worktree holds cwd, longest path first; None when two tie."""
    cwd = os.path.normpath(cwd)
    hits = sorted(((len(wt), tid) for tid, t in threads.items()
                   if (wt := t["payload"].get("worktreePath")) and (cwd + "/").startswith(os.path.normpath(wt) + "/")),
                  reverse=True)
    if hits and (len(hits) == 1 or hits[0][0] > hits[1][0]):
        return hits[0][1]
    return None


# the agent's own environment names its provider session; T3 maps it to the thread. A native
# subagent (Claude's Task tool) runs in its parent's process, so it resolves to the parent.
NATIVE_ENV = ("CLAUDE_CODE_SESSION_ID", "ANTIGRAVITY_CONVERSATION_ID")


def whoami(products, projects, thread_id=None, cwd=None, con=None):
    """The caller's handle, thread id, title and project, as the page shows them."""
    own = con is None
    con = con or connect()
    try:
        roots, threads = load_threads(con)
        sessions = [os.environ[v] for v in NATIVE_ENV if os.environ.get(v)]
        tid = thread_id or next(filter(None, (thread_by_native_id(con, x) for x in sessions)), None)
        # a session T3 has not recorded must not borrow the thread of the worktree it runs in
        if not tid and not sessions:
            tid = thread_from_cwd(threads, cwd or os.getcwd())
        if not tid:
            raise FactoryError("cannot tell which T3 thread this is: pass --thread <id>")
        if tid not in threads:
            raise FactoryError(f"no live T3 thread {tid!r} (archived, deleted or unknown)")
        hcs = handles_for(threads)
        t = threads[tid]
        return {"handle": hcs[tid], "thread": tid, "title": t["title"],
                "project": thread_project(t, threads, roots, products, projects)}
    finally:
        if own:
            con.close()


def unknown_statuses(rows):
    """{status: count} of the agents() rows shown as "Unknown T3 status": the build warning and
    `factory doctor` count exactly what the page shows (an old row in a new status never alarms)."""
    out = {}
    for r in rows:
        if r["state"] == "unknown":
            out[r["t3Status"]] = out.get(r["t3Status"], 0) + 1
    return out


def agents(products, projects, now=None, con=None):
    """One row per T3 thread that is active, waiting, failed, or was busy in the last 24 h.

    Returns (rows, sessions) where sessions maps an agent handle to the Claude session ids of its
    thread, for token attribution."""
    now = now or datetime.now(timezone.utc)
    con = con or connect()
    roots, threads = load_threads(con)
    last_run = {}
    for tid, status, req, done, payload, inst in con.execute(
            "select thread_id, status, requested_at, completed_at, payload_json, provider_instance_id "
            "from orchestration_v2_projection_runs order by requested_at"):
        last_run[tid] = {"status": status, "requested": ts(req), "completed": ts(done),
                         "model": (json.loads(payload or "{}").get("modelSelection") or {}).get("model"), "instance": inst}
    session_model, driver_of = {}, {}
    for tid, model, driver, _ in con.execute(
            "select thread_id, model, driver, updated_at from orchestration_v2_projection_provider_sessions order by updated_at"):
        session_model[tid], driver_of[tid] = model, driver
    native = {}
    for tid, driver, payload in con.execute(
            "select thread_id, driver, payload_json from orchestration_v2_projection_provider_threads"):
        driver_of.setdefault(tid, driver)
        ref = json.loads(payload or "{}").get("nativeThreadRef") or {}
        if ref.get("nativeId"):
            native.setdefault(tid, []).append(ref["nativeId"])
    # native subagents (Claude's Task tool) have no run rows: their subagent row carries the state
    for tid, status, started, done, payload, driver, inst in con.execute(
            "select child_thread_id, status, started_at, completed_at, payload_json, driver, provider_instance_id "
            "from orchestration_v2_projection_subagents"):
        if tid and tid not in last_run:
            last_run[tid] = {"status": status, "requested": ts(started), "completed": ts(done),
                             "model": json.loads(payload or "{}").get("model"), "instance": inst}
            driver_of.setdefault(tid, driver)
    waiting = {tid for tid, st in con.execute(
        "select thread_id, status from orchestration_v2_projection_runtime_requests") if st not in CLOSED_REQUESTS}

    rows, sessions = [], {}
    hcs = handles_for(threads)
    for tid, t in threads.items():
        run = last_run.get(tid)
        product = thread_product(t, threads, roots, products)
        if not run or not product:
            continue
        silent = now - (t["updated"] or now)
        # a run left in status waiting on a dead thread falls through to stalled, so it leaves the page
        if tid in waiting or (run["status"] == "waiting" and silent <= DONE_WINDOW):
            state, since = "waiting", t["updated"]
        elif run["status"] in ACTIVE:
            state, since = ("stalled" if silent > SILENT else "running"), run["requested"]
        elif run["status"] in FAILED:
            state, since = "failed", run["completed"] or t["updated"]
        elif run["status"] in DONE:
            state, since = "done", run["completed"] or t["updated"]
        else:
            state, since = "unknown", run["completed"] or t["updated"]
        if state in ("done", "failed", "stalled", "unknown") and silent > DONE_WINDOW:
            continue
        _, who, via = role_of(tid)
        driver = driver_of.get(tid) or (run["instance"] or "").split("_")[0]
        hc = hcs[tid]
        project = thread_project(t, threads, roots, products, projects)
        lineage = (t.get("payload") or {}).get("lineage") or {}
        parent_tid = lineage.get("parentThreadId") or lineage.get("rootThreadId")
        parent_hc = hcs.get(parent_tid) if parent_tid else None
        rows.append({"hc": hc, "who": who, "provider": PROVIDERS.get(driver, driver or "unknown"),
                     "model": run["model"] or session_model.get(tid) or "", "via": via, "product": product,
                     "project": project, "state": state,
                     "t3Status": run["status"],
                     "since": iso(since or now),
                     "tokens": None, "task": t["title"], "thread": tid,
                     "parent": parent_hc})
        sessions[hc] = native.get(tid, [])
    rows.sort(key=lambda r: r["since"], reverse=True)
    rows.sort(key=lambda r: ORDER.index(r["state"]))
    return rows, sessions


def thread_attribution_map(con, products, projects):
    """Map every native session id in T3 to its thread info and handle, for historical token attribution."""
    roots, threads = load_threads(con, include_gone=True)

    # live threads get exactly the Agents tab handles; archived and deleted ones pick from what is left
    hcs = handles_for({tid: t for tid, t in threads.items() if not t["gone"]})
    hcs.update(handles_for({tid: t for tid, t in threads.items() if t["gone"]}, hcs.values()))

    driver_of = {}
    native = {}
    for tid, driver, payload in con.execute(
            "select thread_id, driver, payload_json from orchestration_v2_projection_provider_threads"):
        driver_of.setdefault(tid, driver)
        ref = json.loads(payload or "{}").get("nativeThreadRef") or {}
        if ref.get("nativeId"):
            native.setdefault(ref["nativeId"], tid)

    session_model = {}
    for tid, model, driver, _ in con.execute(
            "select thread_id, model, driver, updated_at from orchestration_v2_projection_provider_sessions order by updated_at"):
        session_model[tid] = model
        driver_of.setdefault(tid, driver)

    mapped = {}
    for nid, tid in native.items():
        t = threads.get(tid)
        if not t:
            continue
        _, who, via = role_of(tid)
        driver = driver_of.get(tid)
        hc = hcs.get(tid)
        project = thread_project(t, threads, roots, products, projects)
        lineage = (t.get("payload") or {}).get("lineage") or {}
        parent_tid = lineage.get("parentThreadId") or lineage.get("rootThreadId")
        mapped[nid] = {
            "thread": tid,
            "hc": hc,
            "who": who,
            "provider": PROVIDERS.get(driver, driver or "unknown"),
            "model": session_model.get(tid) or "",
            "via": via,
            "project": project,
            "parent": hcs.get(parent_tid) if parent_tid else None,
        }
    return mapped, hcs


def _dropped_notice(payload_json):
    """True when a message or notice says a hook dropped the prompt before it (cache-tax and the like)."""
    try:
        payload = json.loads(payload_json or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    text = payload.get("text") or payload.get("message") or ""
    return isinstance(text, str) and text.startswith("Prompt dropped by a hook")


def thread_user_messages(con, thread_id):
    """Return list of text strings from user messages in thread_id.

    Checks orchestration_v2_projection_messages (written at queue time)
    and orchestration_v2_projection_turn_items (turn start).
    Optionally enforces createdBy == 'agent' and creationSource == 'mcp'
    when present on the matched row. Leaves out prompts a hook dropped.
    """
    texts = []
    seen = set()

    def _extract(payload_json):
        try:
            payload = json.loads(payload_json or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        created_by = payload.get("createdBy")
        creation_source = payload.get("creationSource")
        if created_by is not None and created_by != "agent":
            return None
        if creation_source is not None and creation_source != "mcp":
            return None
        t = payload.get("text")
        return t if isinstance(t, str) else None

    # 1. projection_messages (written at queue time), in order, so a prompt a hook dropped can be spotted:
    # T3 still writes the user row, then the provider answers "Prompt dropped by a hook: ...".
    dropped = set()
    last_user = None
    for role, payload_json in con.execute(
        "select role, payload_json from orchestration_v2_projection_messages where thread_id = ? "
        "order by created_at, rowid", (thread_id,)
    ):
        if role == "user":
            last_user = _extract(payload_json)
            if last_user and last_user not in seen:
                seen.add(last_user)
                texts.append(last_user)
        elif role == "assistant" and last_user:
            if _dropped_notice(payload_json):
                dropped.add(last_user)
            last_user = None

    # 2. turn_items (turn start). A drop can live only here: the item right after the user_message
    # (ordinal + 1) is a system_notice ("message") or assistant_message ("text") naming the hook.
    items = {}
    for ordinal, typ, payload_json in con.execute(
        "select ordinal, type, payload_json from orchestration_v2_projection_turn_items where thread_id = ? "
        "and type in ('user_message', 'system_notice', 'assistant_message')", (thread_id,)
    ):
        items[ordinal] = (typ, payload_json)
    for ordinal, (typ, payload_json) in sorted(items.items()):
        if typ != "user_message":
            continue
        t = _extract(payload_json)
        if not t:
            continue
        nxt = items.get(ordinal + 1)
        if nxt and nxt[0] != "user_message" and _dropped_notice(nxt[1]):
            dropped.add(t)
        if t not in seen:
            seen.add(t)
            texts.append(t)

    # sqlite3.Error propagates: a locked or busy read must not look like "not in the thread"
    return [t for t in texts if t not in dropped]

