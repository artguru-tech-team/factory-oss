"""Autopilot: opt-in scheduled nudges that keep a project's T3 session moving while you are away.

Plain Python, no LLM call of its own. A nudge is a warm message pushed through the relay into the
project's existing thread. Goals touching production, money or a merge are never nudged: the run
raises an action item in the inbox instead.
"""
import re
import unicodedata
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import relay, store, t3

EVERY_CHOICES = (1, 3, 6, 12, 24)
DEFAULT_EVERY = 3
DEFAULT_MAX_PER_DAY = 4
DEFAULT_DAILY_CAP = 12
HISTORY_MAX = 20
# A goal matching any of these is never nudged: it goes to the inbox instead. Matched after NFKC
# normalisation with camelCase and snake_case split into words ("PROD_API_KEY", "prodDeploy" and
# "payment_webhook" count), case-insensitive. A false match only costs an inbox item; a miss
# nudges an agent toward production, so the list leans wide, with three carve-outs:
# "product*" is not "prod", "migrate" counts only near database words (a design-system migration
# is ordinary work), and "pay" only as paying people out (not "pay attention").
SENSITIVE_RE = re.compile(
    r"\b(merg\w*|deploy\w*|releas\w*|prod(?!uct(?!ion))\w*|payouts?|payments?|payroll\w*|"
    r"pa(?:y|id|ying)\s+(?:the\s+)?(?:out|creators?|users?|clippers?)|wallets?|money|migrations?|"
    r"(?:db|database|schema|tables?|prisma|sql)\s+(?:\w+\s+){0,2}migrat\w*|"
    r"migrat\w*\s+(?:\w+\s+){0,2}(?:db|database|schema|tables?|prisma|sql))\b", re.IGNORECASE)


# T3 thread states (t3.agents()). "done" means the last turn finished and the session sits idle:
# that is the one state a nudge is for. "stalled" is a turn still open but silent, so not idle.
SKIP = {"running": "skipped working", "stalled": "skipped working", "unknown": "skipped working",
        "quota": "skipped working", "waiting": "skipped waiting on you",
        "failed": "skipped failed: check the session"}
NEEDS_YOU = "touches production/money/merge: needs you"


def touches_sensitive(goal):
    text = unicodedata.normalize("NFKC", goal or "")
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text).replace("_", " ")  # prodDeploy, PROD_API_KEY
    return bool(SENSITIVE_RE.search(text))


def nudge_text(goal, project, source="the dashboard"):
    return (f"Autopilot nudge from the factory dashboard (goal set from {source}): {goal}. "
            f"Keep going toward it. If something blocks you, ask with `factory ask`. "
            f"When the goal is met, run `factory autopilot done {project}`.")


NOT_DRIVABLE = "is a delegate, subagent or scheduled-task thread: pick the session that started it"
NOT_DRIVABLE_SKIP = "skipped: not a top-level session, set another with factory autopilot set <project> --thread"


def drivable(thread):
    """Only a top-level T3 session can be put on autopilot. Delegates, subagents and scheduled-task
    runs (ids "thread:project:...", e.g. the relay's delivery runs) are one-off workers."""
    return (bool(thread) and isinstance(thread, str) and t3.role_of(thread)[0] == "1"
            and not thread.startswith("thread:project:"))


def thread_taken(cfg, thread, pid):
    """The other project already driving this session, or None: two goals on one session would
    get two nudges in one run."""
    return next((p for p, e in cfg.get("projects", {}).items() if p != pid and e.get("thread") == thread), None)


def agent_rows(products, projects, now=None):
    """{thread id: {"state": ...}} for every open T3 thread. A T3 read failure propagates to run().

    agents() leaves out threads idle for more than a day; an open thread missing from it is read
    from its last run status (done / waiting / running / failed / unknown — never nudged)."""
    with closing(t3.connect()) as con:
        rows = {r["thread"]: r for r in t3.agents(products, projects, now, con=con)[0] if r.get("thread")}
        roots, threads = t3.load_threads(con)
        last = {tid: st for tid, st in con.execute(
            "select thread_id, status from orchestration_v2_projection_runs order by requested_at")}
        now = now or datetime.now(timezone.utc)
        hcs = t3.handles_for(threads)
        for tid, t in threads.items():
            if tid in rows or tid not in last:
                continue
            st = last[tid]
            state = ("waiting" if st == "waiting" else "running" if st in t3.ACTIVE
                     else "failed" if st in t3.FAILED else "done" if st in t3.DONE else "unknown")
            rows[tid] = {"thread": tid, "state": state, "since": store.iso(t["updated"] or now),
                         "hc": hcs.get(tid), "task": t["title"],
                         "product": t3.thread_product(t, threads, roots, products),
                         "project": t3.thread_project(t, threads, roots, products, projects)}
    return rows


def deliverable(thread, forced=False):
    """Checked again by the relay right before an autopilot nudge goes out (it may have waited in
    the outbox): True to send, False to drop it (paused, project off or gone, session not idle),
    None when T3 is unreadable (keep it queued). forced: a confirmed "Send anyway"."""
    try:
        cfg = store.autopilot()
    except store.FactoryError:
        return False
    if not drivable(thread) or cfg.get("paused") or not any(e.get("enabled") and e.get("thread") == thread
                                    for e in cfg.get("projects", {}).values()):
        return False
    try:
        rows = agent_rows(store.products(), store.projects())
    except Exception:
        return None
    state = (rows.get(thread) or {}).get("state")
    if state == "done" and thread not in open_inbox_threads():
        return True
    return forced and state in ("done", "waiting")


def open_inbox_threads():
    """{thread id: [unanswered inbox item ids]}: the questions a session is waiting on you for.
    An answered item waits on the agent (it applies the answer, then resolves it), not on you."""
    out, answered = {}, store.read("answers.json", {})
    for it in store.read("inbox.json", {"items": []}).get("items", []):
        if isinstance(it, dict) and it.get("thread") and it.get("id") not in answered:
            out.setdefault(it["thread"], []).append(str(it.get("id") or "?"))
    return out


def name_ids(ids, shown=3):
    """"d45, a43" or "d45, a43, a50 +2 more": short enough for a table cell."""
    more = len(ids) - shown
    return ", ".join(ids[:shown]) + (f" +{more} more" if more > 0 else "")


def waiting_result(ids):
    """The skip result, naming the inbox items to answer so the reader can act on it."""
    return f"skipped waiting on you: answer {name_ids(ids)}" if ids else "skipped waiting on you"


def _parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _due(entry, now):
    last = entry.get("last_nudge_at")
    if not last:
        return True
    try:
        return (now - _parse(last)) >= timedelta(hours=entry.get("every_hours", DEFAULT_EVERY))
    except ValueError:
        return True


def _nudged_today(entry, now):
    return (entry.get("nudged_on") or {}).get(now.date().isoformat(), 0)


def _record(entry, result, now):
    """Append one history entry, but never two of the same non-nudge result in a row ("nudged" is
    always appended). A nudge also bumps the daily counter `nudged_on` (today's and yesterday's
    keys only) and the last nudge time."""
    hist = entry.setdefault("history", [])
    if result != "nudged" and hist and hist[-1].get("result") == result:
        hist[-1].setdefault("since", hist[-1]["at"])  # first time this result was seen
        hist[-1]["at"] = store.iso(now)              # last time: shows how long it has been stuck
        return
    hist.append({"at": store.iso(now), "result": result})
    del hist[:-HISTORY_MAX]
    if result == "nudged":
        entry["last_nudge_at"] = hist[-1]["at"]
        nudged = entry.setdefault("nudged_on", {})
        today = now.date().isoformat()
        nudged[today] = nudged.get(today, 0) + 1
        keep = {today, (now.date() - timedelta(days=1)).isoformat()}
        for day in list(nudged):
            if day not in keep:
                del nudged[day]


def _finish(entry, result, dry, now):
    if not dry:
        _record(entry, result, now)
    return result


def decide(cfg, pid, now, agents, inbox_threads, dry, require_due, allow_waiting=False):
    """Decide one project entry and, unless dry, record the result in its history.

    Returns (result, action) where action is "nudge" | "ask" | None. The caller applies the side
    effect (relay enqueue or inbox ask) outside store.lock(), because those take their own lock.
    """
    entry = cfg["projects"][pid]
    if require_due and not _due(entry, now):
        return "not due", None
    thread = entry.get("thread")
    if thread and not drivable(thread):
        return _finish(entry, NOT_DRIVABLE_SKIP, dry, now), None
    if not thread or thread not in agents:
        return _finish(entry, "skipped thread gone", dry, now), None
    state = agents[thread]["state"]
    waiting = state == "waiting" or thread in inbox_threads
    if waiting and not allow_waiting:
        return _finish(entry, waiting_result(inbox_threads.get(thread)), dry, now), None
    if state in SKIP and state != "waiting":
        return _finish(entry, SKIP[state], dry, now), None
    if state not in ("done", "waiting"):
        return _finish(entry, "skipped working", dry, now), None
    if touches_sensitive(entry.get("goal", "")):
        return _finish(entry, NEEDS_YOU, dry, now), (None if dry else "ask")
    if (_nudged_today(entry, now) >= entry.get("max_per_day", DEFAULT_MAX_PER_DAY)
            or sum(_nudged_today(e, now) for e in cfg["projects"].values()) >= cfg.get("daily_cap", DEFAULT_DAILY_CAP)):
        return _finish(entry, "skipped cap", dry, now), None
    if _undelivered(thread):
        return _finish(entry, "skipped earlier nudge not delivered yet", dry, now), None
    return _finish(entry, "nudged", dry, now), (None if dry else "nudge")


def _undelivered(thread):
    """True when a nudge to this thread is still queued/sending/sent: never stack a second one."""
    return any(it.get("kind") == "autopilot" and it.get("thread") == thread
               and it.get("state") in ("queued", "sending", "sent")
               for it in relay.load_outbox())


def _ensure_ask(pid, goal, now, thread):
    """At most one 'Unblock autopilot: <project>' item: skip when one is open (any day) or was
    resolved or created today. The item targets the project's thread, so an answer relays to it."""
    title = f"Unblock autopilot: {pid}"
    return store.add_ask_item(pid, "action", title, "Action for you", goal,
                              [["done", "I handled it myself"], ["blocked", "Leave it blocked"]], None, "", "autopilot", thread,
                              at=store.iso(now), dedupe_title=title, dedupe_day=now.date().isoformat())


def every_error(v):
    """The one rule for an interval, shared by the CLI and the page."""
    if type(v) is not int or v not in EVERY_CHOICES:
        return f"every_hours must be one of {', '.join(map(str, EVERY_CHOICES))}"
    return None


def _source(entry):
    return "the CLI" if entry.get("goal_source") == "cli" else "the dashboard"


def _queue(action, entry, pid, nudges, asks, forced=False):
    if action == "nudge":
        nudges.append((entry["thread"], nudge_text(entry.get("goal", ""), pid, _source(entry)), forced))
    elif action == "ask":
        asks.append((pid, entry.get("goal", ""), entry.get("thread")))


def _apply(nudges, asks, now):
    """Side effects after store.lock() is released (relay and inbox take their own lock)."""
    for thread, text, forced in nudges:
        relay.enqueue_message(thread, text, kind="autopilot", label="forced" if forced else None)
    for pid, goal, thread in asks:
        _ensure_ask(pid, goal, now, thread)


def run(dry=False, only=None, now=None):
    """The `factory autopilot run` core. Returns (summary dict, one-line-per-project text)."""
    now = now or datetime.now(timezone.utc)
    products, projects = store.products(), store.projects()
    try:
        agents = agent_rows(products, projects, now)
        t3_error = None
    except Exception as e:  # a T3 read failure skips all with "T3 unreadable", never crashes
        agents, t3_error = {}, (str(e).splitlines()[0] if str(e) else type(e).__name__)
    inbox_threads = open_inbox_threads()
    nudges, asks, results = [], [], {}
    with store.lock():
        cfg = store.autopilot()
        if cfg.get("paused"):
            return {"paused": True, "results": {}}, "autopilot is paused: run does nothing"
        for pid in sorted(cfg.get("projects", {})):
            if only and pid != only:
                continue
            entry = cfg["projects"][pid]
            if not entry.get("enabled", True):
                continue
            if t3_error:
                results[pid] = "T3 unreadable"
                continue
            res, action = decide(cfg, pid, now, agents, inbox_threads, dry, require_due=True)
            if res == "not due":
                continue
            results[pid] = res
            _queue(action, entry, pid, nudges, asks)
        store.save_autopilot(cfg)
    _apply(nudges, asks, now)
    out = "\n".join(f"{pid}\t{res}" for pid, res in sorted(results.items())) or "no projects on autopilot"
    return {"paused": False, "results": results}, out


def nudge_project(pid, now=None, allow_waiting=False):
    """The POST `send` action: nudge one project now (same checks as run, minus "due").

    allow_waiting: you confirmed "Send anyway" on a session that waits on you."""
    now = now or datetime.now(timezone.utc)
    products, projects = store.products(), store.projects()
    try:
        agents = agent_rows(products, projects, now)
    except Exception:
        return {"result": "T3 unreadable", "delivered": False}
    inbox_threads = open_inbox_threads()
    nudges, asks = [], []
    with store.lock():
        cfg = store.autopilot()
        if cfg.get("paused"):
            return {"result": "paused", "delivered": False}
        entry = cfg.get("projects", {}).get(pid)
        if entry is None:
            raise store.FactoryError(f"project {pid!r} is not on autopilot (see: factory autopilot list)")
        if not entry.get("enabled", True):
            return {"result": "skipped: autopilot off for this project", "delivered": False}
        res, action = decide(cfg, pid, now, agents, inbox_threads, dry=False, require_due=False,
                             allow_waiting=allow_waiting)
        _queue(action, entry, pid, nudges, asks, forced=allow_waiting)
        store.save_autopilot(cfg)
    _apply(nudges, asks, now)
    relay.trigger_delivery_async()
    return {"result": res, "delivered": action == "nudge", "waitingOn": inbox_threads.get(entry.get("thread"), [])}
