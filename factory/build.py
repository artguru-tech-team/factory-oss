"""Render ~/.factory/site/factory.html from the factory state, T3, the token scan and GitHub.

Every number on the page is computed here or in the page from these sources. A failing source
stops the build, so the last good page stays and shows its age; the exceptions are PRs gh cannot
read and projects not ready to draw, which are left out with a warning (DATA.warnings, stdout).
"""
import json
import os
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

from . import autopilot, board, relay, skills, sources, store, t3, tokens
from .store import FactoryError, iso, now_iso

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "template", "factory.html")
MARKER = "/*DATA*/null"
PROV_COLORS = {"Claude": "var(--mist)", "Antigravity": "var(--iris)", "OpenCode": "var(--orange)",
               "LiteLLM": "var(--teal)", "Codex": "var(--lav)"}
EXTRA_DONE_WINDOW = timedelta(hours=24)
FUTURE_SLACK = timedelta(minutes=5)


def manual_series(p, today):
    """Daily [scope, merged] from the snapshots `factory plan` records, carried forward, from the
    earlier of the first phase start and today (the page plots it from the same day)."""
    start = min(date.fromisoformat(min(ph["start"] for ph in p["phases"])), today)
    hist = sorted(p.get("history", {}).items())
    # before the first snapshot nothing was recorded as merged; the scope is the first one recorded
    series, cur = [], [hist[0][1][0], 0] if hist else [0, 0]
    for i in range((today - start).days + 1):
        d = (start + timedelta(days=i)).isoformat()
        for day, v in hist:
            if day <= d:
                cur = v
        series.append(list(cur))
    return series


def derived(ph, rows):
    """A phase's merged and in-PR counts from its PRs; None when they cannot be derived. Blocked stays
    as set by hand (plan phase/units keep linked units + blocked within the total)."""
    got = sources.phase_counts(ph, rows)
    return {"merged": got[0], "inPR": got[1]} if got else None


def sync_phases(raw, rows, warnings):
    """Phases with linked PRs take their counts from GitHub; changed counts are saved (with a history
    snapshot, so the graph follows) under the state lock, re-read so a concurrent write is not lost.
    ponytail: two builds racing can save the older gh read last; the next build corrects it."""
    stale = set()
    for p in raw:
        for ph in p.get("phases", []):
            got = sources.phase_counts(ph, rows)
            if got and got[2]:
                warnings.append(f"{p['id']}: phase {ph['name']} has a PR closed without merging ({got[2]} units count as neither)")
            new = derived(ph, rows)
            if new and any(ph[k] != v for k, v in new.items()):
                stale.add(p["id"])
    if not stale:
        return raw
    with store.lock():
        for pid in stale:
            fresh = store.find_project(pid)
            for ph in fresh["phases"]:
                if new := derived(ph, rows):
                    ph.update(new)
            store.snapshot(fresh)
            store.save_project(fresh)
    return [store.find_project(p["id"]) if p["id"] in stale else p for p in raw]


def project_data(p, product, today, warnings):
    repo = (product.get("repos") or [None])[0]
    ups = sorted(store.read_lines(f"updates/{p['id']}.jsonl"), key=lambda u: u["at"], reverse=True)[:10]
    out = {k: p.get(k) for k in ("id", "product", "kind", "name", "feat", "prio")}
    out.update(next=p.get("next") or [], prs=[sources.pr_key(n, repo) for n in p.get("prs", [])],
               updates=[[u["at"], u["agent"], u["health"], u["text"]] for u in ups])
    if p["kind"] == "build" and p.get("source") == "ds-kit":
        kit = sources.ds_kit(today)
        if not kit:
            warnings.append(f"{p['id']}: source ds-kit but kit.repo is unset, shown empty (factory config set kit.repo <path>)")
        kit = kit or dict(phases=[], scopes=[["Whole programme", []]], added=0, series=[],
                                            undatedMerged=0, prs=[], next=[])
        out.update(phases=kit["phases"], scopes=kit["scopes"], added=kit["added"], series=kit["series"],
                   undatedMerged=kit["undatedMerged"])
        out["prs"] = out["prs"] or kit["prs"]
        out["next"] = out["next"] or kit["next"]
    elif hidden := store.why_hidden(p):
        warnings.append(f"{p['id']}: {hidden}, not shown")
        return None
    elif p["kind"] == "build":
        names = [ph["name"] for ph in p["phases"]]
        out.update(phases=[[ph["name"], ph["merged"], ph["total"], ph["start"], ph["due"], ph["inPR"], ph["blocked"]]
                           for ph in p["phases"]],
                   # the page reads scopes[0] as the whole project, as in the prototype ("Whole programme")
                   scopes=[["Whole project", list(range(len(names)))]]
                   + [[n, [names.index(x) for x in idx]] for n, idx in p.get("scopes", [])],
                   added=p.get("added", 0), series=manual_series(p, today), undatedMerged=0)
    elif p["kind"] == "loop":
        out.update({k: p.get(k) for k in ("metric", "unit", "baseline", "target", "running", "lastActive", "createdAt")})
        out["exps"] = [e[:3] for e in p["exps"]]
    else:
        out["daily"], out["items"] = sources.run_lane(product, today)
    return out


def log_rows(box, projects, ids):
    rows = []
    for it in box.get("resolved", []):
        if it["project"] not in ids:
            continue
        a = it.get("answer")
        choice = a["choice"] if a else it.get("reco")
        label = next((t for k, t in it["options"] if k == choice), choice or "resolved")
        rows.append({"at": (a or {}).get("at") or it["resolvedAt"], "project": it["project"], "text": it["title"],
                     "choice": label, "by": "you" if a else "default"})
    for it in box.get("items", []):
        if it["kind"] == "applied" and it["project"] in ids:
            label = next((t for k, t in it["options"] if k == it["reco"]), it["reco"])
            rows.append({"at": it["at"], "project": it["project"], "text": it["title"], "choice": label, "by": "default"})
    for p in projects:
        if p["id"] not in ids:
            continue
        for e in p.get("exps", [])[1:]:
            if len(e) > 3:
                rows.append({"at": e[3], "project": p["id"], "text": e[0],
                             "choice": f"{'Kept' if e[2] else 'Discarded'} ({e[1]})", "by": "loop"})
    return rows


def agents_data(products, projects, now, warnings):
    with closing(t3.connect()) as con:
        rows, sessions = t3.agents(products, projects, now, con)
        thread_map, hcs = t3.thread_attribution_map(con, products, projects)
    unknown = t3.unknown_statuses(rows)
    for st, n in unknown.items():
        warnings.append(f"T3 run status {st!r} is unknown ({n} run(s)): those agents show as 'Unknown T3 status'; "
                        "teach factory/t3.py what it means")
    ids = {p["id"] for p in projects}
    taken = {r["hc"] for r in rows}
    for hc, a in store.read("agents-extra.json", {}).items():
        if a.get("product") not in products or (a.get("project") and a["project"] not in ids):
            continue
        if hc in taken:
            warnings.append(f"agent {hc}: the handle is also a T3 thread's, not shown (use another handle)")
            continue
        if a.get("state") == "done" and now - t3.ts(a["updatedAt"]) > EXTRA_DONE_WINDOW:
            continue
        rows.append({"hc": hc, "who": a.get("who") or hc, "provider": a.get("provider") or "unknown",
                     "model": a.get("model") or "", "via": a.get("via") or "factory CLI", "product": a["product"],
                     "project": a.get("project"), "state": a["state"], "since": a["since"], "tokens": None,
                     "task": a.get("task") or ""})
    for r in rows:
        tid = r.get("thread")
        is_subagent = bool(tid and t3.role_of(tid)[0] == "3")
        r["canReceive"] = bool(tid and not is_subagent)
        if is_subagent:
            parent = r.get("parent")
            r["disabledHint"] = f"subagent: message its parent {parent}" if parent else "subagent: message its parent"
        elif not tid:
            r["disabledHint"] = "not in T3: no way to deliver"

    token_warnings = []
    token_history, agent_tokens, meta = tokens.token_history_and_agents(
        rows, sessions, now, warnings, thread_map=thread_map, hcs=hcs, token_warnings=token_warnings)
    return rows, meta, token_history, agent_tokens, token_warnings


def skills_data(now, warnings):
    try:
        skills_list, skill_warns, _ = skills.collect(now=now)
        warnings.extend(skill_warns)
        return skills_list
    except Exception as e:
        warnings.append(f"skills data collection error: {e}")
        return []


def protos_data(warnings):
    """The prototype list, or ([], error) when protos.json is corrupt: the page shows its error banner."""
    try:
        protos = store.protos()
    except FactoryError as e:
        warnings.append(f"protos.json: {e}")
        return [], str(e)
    for p in protos:
        for v in p["versions"]:
            v["token"] = store.proto_token(f"protos/{p['id']}/v{v.get('v')}.html")
    return protos, None


AP_STATE_LABEL = {"running": "Working", "waiting": "Waiting on you", "stalled": "Working",
                  "done": "Idle", "failed": "Failed: check the session", "quota": "Quota blocked",
                  "unknown": "Unknown", "gone": "Thread gone"}


def _idle_label(since, now):
    """'Idle 2h 40m' for a done session, from its last activity time."""
    if not since:
        return "Idle"
    try:
        delta = now - t3.ts(since)
    except (ValueError, TypeError):
        return "Idle"
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "Idle"
    if mins < 60:
        return f"Idle {mins}m"
    hours, rem = divmod(mins, 60)
    if hours < 24:
        return f"Idle {hours}h {rem:02d}m" if rem else f"Idle {hours}h"
    return f"Idle {hours // 24}d"


def _next_run(now):
    """The next :00/:30 cron boundary as HH:MM (the autopilot cron runs every 30 minutes)."""
    nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=((now.minute // 30) + 1) * 30)
    return nxt.strftime("%H:%M")


def autopilot_data(products, projects, now, warnings):
    """The autopilot config plus one row per project (thread state, next due, last result).

    Thread states come from autopilot.agent_rows (the same source `autopilot run` reads) plus the
    open inbox threads; a T3 read failure leaves every row "unknown" and sets t3Error for the page."""
    try:
        cfg = store.autopilot()
    except FactoryError as e:
        warnings.append(f"autopilot.json: {e}")
        return {"paused": False, "pausedAt": None, "daily_cap": autopilot.DEFAULT_DAILY_CAP,
                "projects": [], "error": str(e), "t3Error": None}
    try:
        by_thread = autopilot.agent_rows(products, projects, now)
        t3_error = None
    except Exception as e:
        by_thread, t3_error = {}, (str(e).splitlines()[0] if str(e) else type(e).__name__)
    inbox_threads = autopilot.open_inbox_threads()
    rows = []
    for pid in sorted(cfg.get("projects", {})):
        entry = cfg["projects"][pid]
        goal = entry.get("goal", "")
        every = entry.get("every_hours", autopilot.DEFAULT_EVERY)
        thread = entry.get("thread")
        enabled = bool(entry.get("enabled", True))
        hist = entry.get("history", [])
        last_result = hist[-1].get("result") if hist else None
        last_nudge = entry.get("last_nudge_at")
        try:
            next_due = store.iso(t3.ts(last_nudge) + timedelta(hours=every)) if last_nudge else None
        except (ValueError, TypeError):
            next_due = None
        if t3_error:
            row, state = None, "unknown"
        else:
            row = by_thread.get(thread)
            state = "waiting" if (row and thread in inbox_threads) else (row["state"] if row else "gone")
        waiting_on = [] if t3_error else inbox_threads.get(thread, [])
        state_label = _idle_label(row.get("since"), now) if state == "done" else AP_STATE_LABEL.get(state, "Unknown")
        if state == "waiting" and waiting_on:
            state_label = f"Waiting on you: {autopilot.name_ids(waiting_on)}"
        if not enabled and last_result == "done":
            nudge = "skip"
        elif not enabled:
            nudge = "off"
        elif autopilot.touches_sensitive(goal):
            nudge = "inbox"
        elif state == "gone":
            nudge = "gone"
        else:
            nudge = "warm"
        rows.append({
            "project": pid, "thread": thread, "goal": goal, "every_hours": every, "enabled": enabled,
            "max_per_day": entry.get("max_per_day", autopilot.DEFAULT_MAX_PER_DAY),
            "last_nudge_at": last_nudge, "next_due": next_due,
            "state": state, "stateLabel": state_label,
            "idle_since": row.get("since") if row else None,
            "tokens": row.get("tokens") if row else None,
            "last_result": last_result,
            "last_result_at": hist[-1].get("at") if hist else None,
            "nudge": nudge, "history": hist[-autopilot.HISTORY_MAX:], "waitingOn": waiting_on,
        })
    nudged = sum(1 for r in rows if r["last_result"] == "nudged")
    skipped = sum(1 for r in rows if r["last_result"] and str(r["last_result"]).startswith("skipped"))
    return {"paused": bool(cfg.get("paused")), "pausedAt": cfg.get("pausedAt"),
            "daily_cap": cfg.get("daily_cap", autopilot.DEFAULT_DAILY_CAP), "projects": rows,
            "error": None, "t3Error": t3_error,
            "next_run": _next_run(now), "last_nudged": nudged, "last_skipped": skipped,
            "sessions": autopilot_sessions(by_thread)}


def autopilot_sessions(by_thread):
    """Every open top-level session the Add drawer can offer, newest first. Not only agents():
    it drops sessions idle for more than a day, and an idle session is what autopilot nudges.
    Left out: failed or unknown (every run would skip them). Sessions outside every product are
    kept (product None): the drawer offers them in their own group."""
    rows = [{k: r.get(k) for k in ("thread", "hc", "task", "product", "project", "state", "since")}
            for tid, r in by_thread.items()
            if autopilot.drivable(tid) and r.get("state") not in ("failed", "unknown")]
    return sorted(rows, key=lambda r: r["since"] or "", reverse=True)


def board_data(products, now, warnings):
    """The board cards plus the auto-promote toggle. A gh failure is a warning, never a crash."""
    try:
        data = board.collect(products, now)
    except Exception as e:  # collect should never raise; this is a last line of defence
        warnings.append(f"board: {e}")
        return {"cards": [], "fetchedAt": None, "error": str(e).splitlines()[0], "auto_promote": board.auto_promote()}
    data["auto_promote"] = board.auto_promote()
    if data.get("error"):
        warnings.append(f"board: {data['error']}")
    for repo, err in (data.get("repoErrors") or {}).items():
        warnings.append(f"board: {repo}: {err}")
    return data


def collect(now=None):
    now = now or datetime.now(timezone.utc)
    today = now.date()
    warnings = []
    products = store.products()
    if not products:
        raise FactoryError("no products yet: factory product set <key> --name ... --workspace ... --repo ...")
    raw = [p for p in store.projects() if p["product"] in products]
    asked = sources.phase_prs_keys(raw)
    rows = sources.prs_for(asked, warnings)
    raw = sync_phases(raw, rows, warnings)
    warnings.extend(sources.stale_phases(raw, rows, today))
    projects = [x for x in (project_data(p, products[p["product"]], today, warnings) for p in raw) if x]
    ids = {p["id"] for p in projects}
    agents, tok, token_history, agent_tokens, token_warnings = agents_data(products, raw, now, warnings)
    for a in agents:
        if a["project"] not in ids:
            a["project"] = None
    box = store.read("inbox.json", {"items": [], "resolved": []})
    levers = []
    t3_hcs = {}
    try:
        t3_hcs = t3.thread_handles()
    except Exception:
        pass
    for a in agents:  # if T3 could not be read again, the agent rows still name live threads
        if a.get("thread") and a.get("hc"):
            t3_hcs.setdefault(a["thread"], a["hc"])

    for it in box["items"]:
        if it["project"] not in ids:
            warnings.append(f"inbox {it['id']}: project {it['project']!r} is not on the page")
            continue
        lv = {k: it.get(k) for k in ("id", "project", "kind", "title", "short", "from", "at", "context", "options", "reco", "why", "thread")}
        lv["applied"], lv["isAction"] = it["kind"] == "applied", it["kind"] == "action"
        tid = it.get("thread")
        lv["target"] = (t3_hcs.get(tid) if tid else None) or "no session"
        levers.append(lv)
    open_ids = {lv["id"] for lv in levers}
    answers = {k: v for k, v in store.read("answers.json", {}).items() if k in open_ids}
    pr_keys = sorted({k for p in projects for k in p["prs"]})
    protos, proto_error = protos_data(warnings)
    autopilot_rows = autopilot_data(products, raw, now, warnings)
    board_rows = board_data(products, now, warnings)
    return {
        "builtAt": iso(now),
        "products": {k: {"name": v.get("name", k)} for k, v in products.items()},
        "projects": projects, "agents": agents, "tokens": tok,
        "tokenHistory": token_history, "agentTokens": agent_tokens,
        "tokenWarnings": token_warnings,
        "tokenSourcesLoaded": token_history.get("sourcesLoaded", True),
        "eurUsdRate": tokens.EUR_USD_RATE,
        "skills": skills_data(now, warnings),
        "protos": protos, "protoError": proto_error,
        "autopilot": autopilot_rows,
        "board": board_rows,
        "levers": levers, "answers": answers,
        "prs": {**rows, **sources.prs_for([k for k in pr_keys if k not in asked], warnings)}, "log": log_rows(box, raw, ids), "provColors": PROV_COLORS,
        "warnings": list(dict.fromkeys(warnings)), "relayConfigured": bool(relay.get_webhook()),
    }


def render(data):
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    if tpl.count(MARKER) != 1:
        raise FactoryError(f"template {TEMPLATE} must contain {MARKER} exactly once")
    # "<" only occurs inside JSON strings; escaping all of them stops "</script>" and "<!--<script" breakouts
    blob = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    # cron and agents build concurrently: write under the state lock, and never let a build that
    # started earlier replace the page a later one already wrote. The stamp goes first, so a crash
    # between the two writes can only make the next older build skip, never a newer one.
    built = t3.ts(data["builtAt"])
    with store.lock():
        raw = None
        try:
            with open(store.path("site", "built-at"), encoding="utf-8") as f:
                raw = f.read().strip()
                stamp = t3.ts(raw)
                if stamp and stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
        except FileNotFoundError:
            stamp = None
        except ValueError:
            data.setdefault("warnings", []).append(
                f"{store.path('site', 'built-at')} said {raw!r}, unparsable: ignored")
            stamp = None
        if stamp and stamp > datetime.now(timezone.utc) + FUTURE_SLACK:
            data.setdefault("warnings", []).append(
                f"{store.path('site', 'built-at')} said {iso(stamp)}, in the future (clock change?): ignored")
            stamp = None
        if stamp and stamp > built:
            data.setdefault("warnings", []).append(f"a build from {iso(stamp)} already wrote the page; this one was skipped")
            data["written"] = False
            return store.path("site", "factory.html")
        store.write_text(os.path.join("site", "built-at"), data["builtAt"] + "\n")
        store.write_text(os.path.join("site", "factory.html"), tpl.replace(MARKER, blob))
    data["written"] = True
    return store.path("site", "factory.html")


def build(now=None):
    data = collect(now)
    return render(data), data
