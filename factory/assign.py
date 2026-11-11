"""Assign an agent to a project from the page. The page never launches threads: for a new agent it
messages the dispatcher T3 session (config assign.dispatcher), which launches the thread and links
it with `factory agent assign`; for an existing session it writes the link and messages that session."""
from datetime import timedelta

from . import config, relay, store, t3

PROVIDERS = ("claude", "antigravity", "opencode")
PENDING_REL = "assign-pending.json"
STALE_AFTER = timedelta(minutes=30)   # a "starting" row turns red after this
DROP_AFTER = timedelta(hours=24)      # and is dropped from the page after this
FIX = "factory config set assign.dispatcher <thread>"


def dispatcher():
    """The dispatcher thread id when it is set, a top-level thread, and open in T3 (an idle one counts),
    else None (also when T3 cannot be read)."""
    tid = config.get("assign.dispatcher")
    if not isinstance(tid, str) or t3.role_of(tid)[0] == "3":
        return None
    return tid if relay.is_thread_live(tid) else None


def dispatch_text(project, provider, brief):
    return (f"Assign an agent to project {project['id']} ({project['name']}).\n"
            f"Provider: {provider}\nBrief:\n{brief}\n\n"
            "Launch a T3 worktree thread for this project with that provider (pick an account with quota, "
            "per the workhorse policy), titled with the project, give it the brief, then run "
            f"`factory agent assign {project['id']} --thread <new thread id>`.")


def _at(entry):
    try:
        return t3.ts(entry.get("at"))
    except (AttributeError, TypeError, ValueError):
        return None


def read_pending():
    """{project: {provider, at, msgId}} asks the dispatcher has not answered with `factory agent assign`."""
    pending = store.read(PENDING_REL, {})
    return pending if isinstance(pending, dict) else {}


def add_pending(pid, provider, msg_id, now):
    """Record an ask; drops entries older than DROP_AFTER (or unreadable). The caller holds store.lock()."""
    pending = {k: v for k, v in read_pending().items() if (at := _at(v)) and now - at <= DROP_AFTER}
    pending[pid] = {"provider": provider, "at": store.iso(now), "msgId": msg_id}
    store.write(PENDING_REL, pending)


def clear_pending(pid):
    """The dispatcher linked a thread to pid: its "starting" row is answered. The caller holds store.lock()."""
    pending = read_pending()
    if pending.pop(pid, None) is not None:
        store.write(PENDING_REL, pending)


def live_pending(pending, pid, now):
    """'N min ago' when pid has an ask younger than STALE_AFTER (a second one would launch a second agent), else None."""
    at = _at(pending.get(pid) or {})
    return f"{int((now - at).total_seconds() // 60)} min ago" if at and now - at <= STALE_AFTER else None


def empty():
    """The `assign` block when nothing could be read: dispatcher offline, no sessions, nothing pending."""
    return {"dispatcher": "offline", "fix": FIX, "providers": list(PROVIDERS), "free": [], "pending": {}}


def data(rows, now):
    """The page's `assign` block: dispatcher state, providers, free sessions, and the pending
    (starting / not started) asks `factory agent assign` has not answered."""
    disp = dispatcher()
    free = [{"thread": r["thread"], "hc": r["hc"], "title": r.get("task") or "", "state": r["state"]}
            for r in rows if r.get("canReceive") and not r.get("project") and r["thread"] != config.get("assign.dispatcher")]
    pending = {}
    for pid, e in read_pending().items():
        at = _at(e)
        if not at or now - at > DROP_AFTER:
            continue
        pending[pid] = {"provider": e.get("provider"), "at": e["at"], "stale": now - at > STALE_AFTER}
    return {**empty(), "dispatcher": "online" if disp else "offline", "free": free, "pending": pending}
