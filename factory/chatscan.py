"""File chat-only questions into the inbox.

An agent session that ends its turn asking the owner something in chat, without `factory ask`,
waits for an answer that never comes: the owner works from the inbox. Every few minutes this scan
files one inbox item per such message, with `thread` set, so the owner's answer is relayed back.
"""
import argparse
import json
import re
from datetime import datetime, timezone

from . import autopilot, config, store, t3

# Keep in step with the agents' Stop hook that blocks ending a turn on a chat-only question.
ASKS = re.compile(
    r"\?\s*$|need .{0,40}from you|your (call|answer|approval|go|decision)|\bapprove\b|\bsay \"|reply \"|reply with"
    r"|tell me (which|if|whether|\d)|want me to|should i\b|waiting (on|for) you|which (one|option)",
    re.I,
)
# An agent that says nothing waits on the owner is not waiting, whatever else its message offers.
NOT_WAITING = re.compile(r"(don't|do not|doesn't|no) need (a |an |any )?\w* ?from you|nothing\b[^.?!\n;:]{0,60}?\b(waits|is waiting) on you\b|"
                         r"nothing (to do|needed) (for|from) you", re.I)
INBOX_ID = re.compile(r"\b[ad]\d{1,5}\b")
MIN_IDLE = 10 * 60  # give the owner time to answer in the session before the question goes to the inbox
ACTIVE = ("running", "starting", "queued", "waiting")
MAX_AGE = 7 * 86400
OPTIONS = [["a", "Reply (write your answer in the note)"], ["b", "Not needed: carry on without me"]]


def text_of(payload):
    text = payload.get("text")
    if isinstance(text, str):
        return text
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _last_message(con, tid, id_col):
    """(role, message key, created_at, text) of the thread's newest message of any role, or None.
    T3's own idle /compact (a user-role message about an hour after the last turn) is not the session
    moving on, so it is left out."""
    m = con.execute(f"select role, {id_col}, created_at, payload_json from orchestration_v2_projection_messages "
                    "where thread_id=? and json_extract(payload_json, '$.idleCompaction') is null "
                    "order by created_at desc, rowid desc limit 1", (tid,)).fetchone()
    if not m:
        return None
    try:
        payload = json.loads(m[3] or "{}")
    except (TypeError, ValueError):
        payload = {}
    return m[0], str(m[1]), m[2], text_of(payload).strip()


def _visible(pid):
    """The project id if the page draws it (an item on a hidden project is never seen), else None."""
    if not isinstance(pid, str) or not pid.strip():
        return None
    try:
        return pid.strip() if not store.why_hidden(store.find_project(pid.strip())) else None
    except store.FactoryError:
        return None


def _run_lane(product, projects):
    """The product's one visible run-lane project, so its sessions' questions land under the right product."""
    lanes = [p["id"] for p in projects if product and p.get("product") == product and p.get("kind") == "run"]
    return _visible(lanes[0]) if len(lanes) == 1 else None


def _skip_reason(con, tid, th, now, open_threads, open_ids, filed_keys, id_col):
    """Why this thread gets no item, or (None, key, text) when it waits on the owner in chat."""
    if not autopilot.drivable(tid):
        return "not a top-level session", None, None
    if not th.get("updated") or now.timestamp() - th["updated"].timestamp() > MAX_AGE:
        return "silent for over 7 days", None, None
    r = con.execute("select status from orchestration_v2_projection_runs where thread_id=? "
                    "order by rowid desc limit 1", (tid,)).fetchone()
    if r and r[0] in ACTIVE:
        return f"run active ({r[0]})", None, None
    last = _last_message(con, tid, id_col)
    if not last or last[0] != "assistant":
        return "newest message is not the agent's", None, None
    _, key, created, text = last
    if now.timestamp() - t3.ts(created).timestamp() < MIN_IDLE:
        return "asked less than 10 minutes ago", None, None
    tail = text[-600:]
    if not tail or not ASKS.search(tail) or NOT_WAITING.search(tail):
        return "does not end on a question", None, None
    if set(INBOX_ID.findall(tail)) & open_ids:
        return "names an open inbox item", None, None
    if tid in open_threads:
        return "already has an open inbox item", None, None
    if filed_keys.get(tid) == key:
        return "message already filed", None, None
    return None, key, text


def run(dry_run=False, verbose=False, now=None, con=None):
    """Returns (summary, text). summary: filed, would_file, skipped lists of dicts."""
    now = now or datetime.now(timezone.utc)
    own = con is None
    con = con or t3.connect()
    try:
        roots, threads = t3.load_threads(con)
        items = [it for it in store.inbox().get("items", []) if isinstance(it, dict)]
        open_threads = {it.get("thread") for it in items}
        open_ids = {it.get("id") for it in items}
        filed_keys = store.read("chatscan.json", {})
        products, projects = store.products(), store.projects()
        cols = {c[1] for c in con.execute("pragma table_info('orchestration_v2_projection_messages')")}
        id_col = "message_id" if "message_id" in cols else "rowid"

        filed, would_file, skipped, lines = [], [], [], []
        # A scan item nobody answered is moot once the session moved on (the owner replied in the session,
        # or the agent asked through `factory ask`): close it, or it would hold the thread as "waiting on you".
        answered = store.read("answers.json", {})
        for it in items:
            last = it.get("from") == "scan" and it["id"] not in answered and _last_message(con, it.get("thread"), id_col)
            if last and last[1] != filed_keys.get(it.get("thread")):
                if not dry_run:
                    from . import cli
                    if it["id"] in store.read("answers.json", {}):
                        continue  # answered meanwhile: its relay message must not be marked stale
                    try:
                        cli.resolve(argparse.Namespace(id=it["id"]))
                    except store.FactoryError:
                        continue  # resolved by someone else meanwhile
                lines.append(f"closed {it['id']}: the session moved on")
                open_threads.discard(it.get("thread"))
                open_ids.discard(it["id"])
        for tid in sorted(threads):
            th = threads[tid]
            title = f"Answer {(th.get('title') or tid).strip().replace(chr(10), ' ')[:50]}: it asked you in chat"
            reason, key, text = _skip_reason(con, tid, th, now, open_threads, open_ids, filed_keys, id_col)
            project = None
            if not reason:
                product = t3.thread_product(th, threads, roots, products)
                project = (_visible(t3.thread_project(th, threads, roots, products, projects))
                           or _run_lane(product, projects)
                           or _visible(config.get("chatscan.project")))
                if not project:
                    reason = "no visible project and chatscan.project unset"
            if reason:
                skipped.append({"thread": tid, "title": title, "reason": reason})
                if verbose or reason.startswith("no visible project"):
                    lines.append(f"skipped {title}: {reason}")
                continue
            row = {"thread": tid, "title": title, "project": project}
            if dry_run:
                would_file.append(row)
                lines.append(f"would file: {title}")
                continue
            item = store.add_ask_item(project=project, kind="action", title=title, short="Action for you",
                                      context=" ".join(text[-400:].split()), options=OPTIONS, reco="a",
                                      why="", sender="scan", thread=tid)
            with store.lock():
                state = store.read("chatscan.json", {})
                state[tid] = key
                store.write("chatscan.json", state)
            filed.append({"id": item["id"], **row})
            lines.append(f"filed {item['id']}: {title}")
        return {"filed": filed, "would_file": would_file, "skipped": skipped}, "\n".join(lines)
    finally:
        if own:
            con.close()
