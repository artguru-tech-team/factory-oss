"""The supervisor: a deterministic program (no LLM) that compares what the factory says with what is true.

`factory supervise` runs a list of checks, each a pure function over already-loaded state and `now`. A check
returns findings {signature, severity, title, evidence, action}. A finding is stored by signature in
supervisor.json, acted on at most twice per signature per 24 h, escalated once after that, then left quiet
until it clears. See README "Supervisor"."""
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

from . import comms, config, pm, queue, relay, release, store
from .store import FactoryError

REL = "supervisor.json"
LOCK = "supervisor.lock"
SENDER = "super"  # the inbox "from" handle (an agent handle: 2 to 8 letters or digits)
BUDGET = 2  # actions per signature per window
MAX_ESCALATIONS = 3  # inbox items per run; the rest wait for the next run
DEGRADED_FAILS = 3  # consecutive failed actions on one finding that make `status` say degraded
WINDOW = timedelta(hours=24)
LATE_MIN, DEAD_MIN = 30, 60
STALE_UPDATE_DAYS = 3
STATUSES = ("open", "resolved", "muted", "false_alarm", "escalated")
DEFAULTS = {"disk_warn": 80, "disk_crit": 90, "mem_warn": 90}


def _parse(s):
    return release._try_parse_utc(s) if isinstance(s, str) else None


def _muted(rec, now):
    return rec["status"] == "muted" and (_parse(rec.get("muted_until")) or now) > now


def finding(signature, severity, title, evidence, action):
    return {"signature": signature, "severity": severity, "title": title, "evidence": evidence, "action": action}


# ---------- checks: pure functions over loaded state ----------

def check_release(ctx):
    """Release plan versus fact, per live product (ctx: release, queue_today, now)."""
    rel, now = ctx["release"], ctx["now"]
    date = now.astimezone(release.TUNIS).date().isoformat()
    out = []
    for product, cfg in sorted((rel.get("products") or {}).items()):
        if cfg.get("mode") != "live" or not cfg.get("thread"):
            continue
        v = (rel.get("verdicts") or {}).get(date, {}).get(product)
        if not v:
            continue  # the plan has not run today: nothing to compare yet
        reason = str((v.get("reasons") or [""])[0])
        if v.get("error") or reason.startswith("could not read"):
            out.append(finding(f"{product}:plan-read-failed:{date}", "crit", f"{product}: today's release plan could not be read",
                               v.get("error") or reason, {"kind": "escalate", "project": cfg.get("project") or None}))
        # no `continue`: a failed plan read is exactly when the queue relay never fires, so still check develop-fix
        q =(ctx.get("queue_today") or {}).get(product) or {}
        wants_fix = any(a.get("job") == "develop-fix" for a in q.get("agents") or [])
        # queue.today already drops develop-fix once queue_log has it for today
        if wants_fix and not v.get("queue_relayed") and not comms.relay_off(rel, product):
            out.append({**finding(f"{product}:develop-fix-not-dispatched:{date}", "warn",
                               f"{product}: the plan has a develop-fix job but the queue message never went out",
                               "queue today lists develop-fix; verdict queue_relayed is unset",
                               {"kind": "dispatch", "product": product, "date": date, "thread": cfg["thread"], "project": cfg.get("project") or None}),
                        "queue": q})  # not stored: perform sends it through release.send_queue_relay
    return out


def computed_health(p, updates, today, now):
    """(health, why) from facts: a phase past due with units left is offTrack, no update in 3 days is atRisk."""
    for ph in p.get("phases") or []:
        if ph.get("due") and ph["due"] < today.isoformat() and ph.get("merged", 0) < ph.get("total", 0):
            return "offTrack", f"phase {ph['name']} was due {ph['due']} with {ph.get('merged', 0)} of {ph['total']} units merged"
    last = max((t for t in (_parse(u.get("at")) for u in updates) if t), default=None) or _parse(p.get("createdAt"))
    if last and now - last > timedelta(days=STALE_UPDATE_DAYS):
        return "atRisk", f"no update since {last.date().isoformat()}"
    return "onTrack", "on schedule with a recent update"


def check_health(ctx):
    """Self-reported health versus facts, per active project (ctx: projects [(project, updates)], now)."""
    now = ctx["now"]
    out = []
    for p, updates in ctx.get("projects") or []:
        if pm.done(p):
            continue
        ups = sorted(updates, key=lambda u: str(u.get("at")))
        if not ups:
            continue  # nothing was self-reported, nothing to differ from
        reported = ups[-1].get("health")
        computed, why = computed_health(p, ups, now.astimezone(release.TUNIS).date(), now)
        if computed == reported:
            continue
        off = computed == "offTrack"
        out.append(finding(f"{p['id']}:health-mismatch", "crit" if off else "warn",
                           f"{p['id']}: reported {reported}, facts say {computed}",
                           f"computed {computed} ({why}); reported {reported} at {ups[-1].get('at')}",
                           {"kind": "escalate", "project": p["id"]} if off else {"kind": "record"}))
    return out


def check_vps(ctx):
    """Disk of / and memory (ctx: disk_pct, mem_pct, thresholds)."""
    th = {**DEFAULTS, **(ctx.get("thresholds") or {})}
    out = []
    d = ctx.get("disk_pct")
    if d is not None and d >= th["disk_warn"]:
        crit = d >= th["disk_crit"]
        out.append(finding("vps:disk", "crit" if crit else "warn", "VPS disk is filling up",
                           f"disk / over {th['disk_crit'] if crit else th['disk_warn']}% used", {"kind": "infra"}))
    m = ctx.get("mem_pct")
    if m is not None and m >= th["mem_warn"]:
        out.append(finding("vps:memory", "warn", "VPS memory is nearly full", f"memory over {th['mem_warn']}% used", {"kind": "infra"}))
    return out


def check_jobs(ctx):
    """Job heartbeats: a log in the state dir older than its max age in minutes (ctx: jobs, mtimes, now)."""
    now = ctx["now"]
    out = []
    for name, max_min in sorted((ctx.get("jobs") or {}).items()):
        mt = (ctx.get("mtimes") or {}).get(name)
        if mt is None:
            ev = f"{name} has no log yet"
        elif now - mt > timedelta(minutes=max_min):
            ev = f"{name} not written for over {max_min} minutes"
        else:
            continue
        out.append(finding(f"job:{name}:stale", "warn", f"job {name} looks stalled", ev, {"kind": "rerun", "lane": name}))
    return out


CHECKS = (("release", check_release), ("health", check_health), ("vps", check_vps), ("jobs", check_jobs))


# ---------- loading the facts the checks read ----------

def _mem_pct(meminfo="/proc/meminfo"):
    try:
        with open(meminfo) as f:
            kv = {k: int(v.split()[0]) for k, _, v in (ln.partition(":") for ln in f if ":" in ln)}
        return 100 * (1 - kv["MemAvailable"] / kv["MemTotal"])
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        return None


def _num(key, default):
    v = config.get(key)
    return v if type(v) in (int, float) else default


def load_ctx(now):
    rel = release.release()
    queue_today = {}
    for name, cfg in (rel.get("products") or {}).items():
        if cfg.get("mode") == "live":
            try:
                queue_today[name] = queue.today(name, now=now)
            except Exception:
                pass  # a product whose queue cannot be read has no develop-fix claim to compare
    jobs = config.get("supervisor.jobs")
    jobs = {k: v for k, v in jobs.items() if os.path.basename(k) == k and type(v) in (int, float)} if isinstance(jobs, dict) else {}
    mtimes = {}
    for name in jobs:
        try:
            mtimes[name] = datetime.fromtimestamp(os.stat(store.path(name)).st_mtime, timezone.utc)
        except OSError:
            mtimes[name] = None
    du = shutil.disk_usage("/")
    return {
        "release": rel, "queue_today": queue_today, "now": now, "jobs": jobs, "mtimes": mtimes,
        "projects": [(p, store.read_lines(f"updates/{p['id']}.jsonl")) for p in store.projects() if not pm.done(p)],
        "disk_pct": 100 * du.used / du.total, "mem_pct": _mem_pct(),
        "thresholds": {k: _num(f"supervisor.{k}", v) for k, v in DEFAULTS.items()},
    }


def snapshot(ctx):
    """The facts a run saw, for the dashboard: job ages, reported versus computed health, VPS usage."""
    now = ctx["now"]
    jobs = []
    for name, max_min in sorted((ctx.get("jobs") or {}).items()):
        mt = (ctx.get("mtimes") or {}).get(name)
        jobs.append({"name": name, "max_min": max_min, "last": store.iso(mt) if mt else None,
                     "stale": mt is None or now - mt > timedelta(minutes=max_min)})
    health = []
    today = now.astimezone(release.TUNIS).date()
    for p, updates in ctx.get("projects") or []:
        ups = sorted(updates, key=lambda u: str(u.get("at")))
        if pm.done(p) or not ups:
            continue
        computed, why = computed_health(p, ups, today, now)
        health.append({"project": p["id"], "reported": ups[-1].get("health"), "computed": computed, "why": why})
    pct = lambda v: None if v is None else round(v, 1)
    return {"at": store.iso(now), "jobs": jobs, "health": health,
            "vps": {"disk_pct": pct(ctx.get("disk_pct")), "mem_pct": pct(ctx.get("mem_pct")),
                    "thresholds": {**DEFAULTS, **(ctx.get("thresholds") or {})}}}


# ---------- state ----------

def state():
    data = store.read(REL, {"last_run": None, "findings": {}})
    ok = (isinstance(data, dict) and isinstance(data.get("findings"), dict)
          and all(isinstance(r, dict) and r.get("status") in STATUSES and isinstance(r.get("actions_at"), list)
                  for r in data["findings"].values()))
    if not ok:
        raise FactoryError(f"{store.path(REL)} is not a supervisor state file: fix or restore it")
    return data


def is_live():
    v = config.get("supervisor.live", False)
    return v is True or str(v).lower() in ("true", "1", "yes", "on")


def heartbeat(now=None):
    """('alive'|'degraded'|'late'|'dead', minutes since the last run or None). Degraded: running, but an
    action has failed DEGRADED_FAILS runs in a row, so a finding is not reaching anyone."""
    now = release._now_utc(now)
    try:
        st = state()
    except FactoryError:
        return "dead", None  # an unreadable state file is a dead watchdog, not a crash
    last = _parse(st.get("last_run"))
    if not last:
        return "dead", None
    mins = (now - last).total_seconds() / 60
    if mins >= DEAD_MIN:
        return "dead", int(mins)
    if any(r.get("fails", 0) >= DEGRADED_FAILS and r["status"] not in ("resolved", "muted", "false_alarm")
           for r in st["findings"].values()):
        return "degraded", int(mins)
    return ("alive" if mins < LATE_MIN else "late"), int(mins)


# ---------- acting ----------

def _escalate(project, title, context):
    project = project or config.get("supervisor.project")
    if not project:
        raise FactoryError("no project to file the inbox item under: set config supervisor.project")
    return store.add_ask_item(project, "urgent", title, "Supervisor", context,
                              [["seen", "Seen, I will handle it"], ["mute", "Mute it (factory supervise mute)"]],
                              "seen", "The supervisor found a gap between the plan and the facts.", SENDER, None)["id"]


def perform(f, action, budget_hit=False, now=None):
    """Do one action (live only). Returns (result, counts against the budget); raises on failure."""
    kind = action["kind"]
    if kind == "dispatch":
        if not release.send_queue_relay(action["product"], f["queue"], action["date"], now=now):
            return "already sent by the release run", False
        return f"dispatched to {action['thread']}", True
    if kind == "escalate":
        if budget_hit:
            ctx = f"{f['evidence']}. {BUDGET} actions in 24 h did not clear it; no more will be sent."
            return "inbox " + _escalate(action.get("project"), f"Supervisor gave up: {f['title']}", ctx), True
        return "inbox " + _escalate(action.get("project"), f["title"], f["evidence"]), True
    return "recorded", False  # rerun, infra, record: v1 records only


def _decide(rec, f, now):
    """(action, budget_hit) the next occurrence needs, or (None, False) when quiet."""
    rec["actions_at"] = [t for t in rec["actions_at"] if (_parse(t) or now) > now - WINDOW]
    act = f["action"]
    if rec["status"] == "escalated" or (act["kind"] == "escalate" and rec["actions_at"]):
        return None, False  # one inbox item per signature per 24 h, even when it flaps
    if act["kind"] not in ("record", "escalate") and len(rec["actions_at"]) >= BUDGET:
        return {"kind": "escalate", "project": act.get("project")}, True
    return act, False


def run(live=None, now=None, ctx=None):
    """One supervisor run. live None reads config supervisor.live (default off); True or False forces it."""
    now = release._now_utc(now)
    live = is_live() if live is None else live
    iso = store.iso(now)
    report = {"at": iso, "live": live, "findings": [], "resolved": []}
    ctx = ctx or load_ctx(now)
    escalations = 0
    with store.lock(LOCK):
        st = state()
        recs, found, failed = st["findings"], {}, set()
        for name, fn in CHECKS:
            try:
                for f in fn(ctx):
                    found[f["signature"]] = {**f, "check": name}
            except Exception as err:
                failed.add(name)
                sig = f"supervisor:check-failed:{name}"
                found[sig] = {**finding(sig, "warn", f"supervisor check {name} failed", f"{type(err).__name__}: {err}", {"kind": "record"}), "check": "supervisor"}
        for sig, f in sorted(found.items(), key=lambda kv: kv[1]["severity"] != "crit"):
            rec = recs.get(sig)
            if rec is None:
                rec = recs[sig] = {"signature": sig, "first_seen": iso, "attempts": 0, "status": "open", "actions_at": []}
            quiet = None
            if rec["status"] == "false_alarm" and rec.get("false_alarm_evidence") == f["evidence"]:
                quiet = "false alarm"
            elif _muted(rec, now):
                quiet = "muted"
            elif rec["status"] in ("false_alarm", "muted", "resolved"):
                rec["status"] = "open"
                rec.pop("fails", None)  # a reopened finding starts with a clean failure count
            rec.update(last_seen=iso, severity=f["severity"], title=f["title"], evidence=f["evidence"], check=f["check"], action=f["action"])
            row = {"signature": sig, "severity": f["severity"], "status": rec["status"], "action": f["action"]["kind"]}
            action, budget_hit = (None, False) if quiet else _decide(rec, f, now)
            if not quiet and action is not None and action["kind"] == "escalate" and escalations >= MAX_ESCALATIONS:
                quiet = "deferred: escalation cap for this run"
            if quiet or action is None:
                row["did"] = quiet or "quiet"
            elif not live:
                row["did"] = "would " + ("escalate (budget)" if budget_hit else action["kind"])
                rec["would"] = row["did"]
                escalations += action["kind"] == "escalate"
            else:
                try:
                    row["did"], counted = perform(f, action, budget_hit, now=now)
                    rec["last_result"] = row["did"]
                except Exception as err:
                    row["did"] = rec["last_result"] = f"error: {err}"
                    rec["fails"] = rec.get("fails", 0) + 1  # retried next run; `status` says degraded after a few
                else:
                    rec["fails"] = 0
                    escalations += action["kind"] == "escalate"
                    if counted:
                        rec["actions_at"].append(iso)
                        rec["attempts"] += 1
                    if action["kind"] == "escalate":
                        rec["status"], rec["escalated_at"] = "escalated", iso
                    rec.pop("would", None)
            rec["did"] = row["did"]  # what this run did or would do, deferrals included (the dashboard shows it)
            report["findings"].append(row)
        for sig, rec in recs.items():
            if (sig not in found and rec.get("check") not in failed
                    and rec["status"] in ("open", "escalated", "muted") and not _muted(rec, now)):
                rec["status"] = "resolved"
                rec.pop("would", None)
                rec.pop("did", None)
                rec.pop("fails", None)
                report["resolved"].append(sig)
        st["last_run"], st["live"] = iso, live
        try:
            st["snapshot"] = snapshot(ctx)
        except Exception as err:
            st.pop("snapshot", None)  # show nothing rather than facts from an older run
            print(f"supervisor: snapshot failed: {type(err).__name__}: {err}", file=sys.stderr)
        store.write(REL, st)
    return report


# ---------- owner actions ----------

def _set(signature, fn):
    with store.lock(LOCK):
        st = state()
        rec = st["findings"].get(signature)
        if rec is None:
            raise FactoryError(f"no finding {signature!r} (see: factory supervise list)")
        fn(rec)
        store.write(REL, st)
        return rec


def mute(signature, hours, now=None):
    until = store.iso(release._now_utc(now) + timedelta(hours=hours))
    return _set(signature, lambda r: (r.pop("did", None), r.update(status="muted", muted_until=until)))


def false_alarm(signature):
    return _set(signature, lambda r: (r.pop("did", None), r.update(status="false_alarm", false_alarm_evidence=r.get("evidence"))))
