"""Product manager: a daily sweep of what slipped, and the commands that clear it.

Plain Python, no LLM: it reads the project files, updates, prototypes, autopilot config, roadmap,
support digest and inbox that factory already keeps; only "merged, not in prod" reads GitHub (the
release manager's read). A project is `closed` once its work is done; closed projects leave the
page's lists. Snoozes, hidden signals and rejected moves live in pm.json.
"""
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import autopilot, comms, config, digest, relay, release, roadmap, store
from .store import FactoryError

PM_REL = "pm.json"
QUIET_DEFAULT = 7
GROUPS = ("finished", "proto", "quiet", "late")
FINISHED_SNOOZE, SNOOZE = 7, 3  # days: "keep open" on a finished project, and the other snoozes
MAX_DAYS = 365
MEMO_SENDER = "pm"
SIGNAL_DAYS, IGNORE_DAYS, INBOX_OLD_HOURS = 7, 14, 24
STOP = {"this", "that", "with", "from", "have", "were", "they", "their", "there", "what", "when", "where", "which", "will",
        "would", "could", "should", "about", "into", "than", "then", "them", "these", "those", "your", "just", "still",
        "also", "some", "more", "been", "being", "does", "done", "make", "need", "only", "very", "tickets", "ticket"}
RISK = ("money", "payment", "payout", "fraud", "kyc", "security")
MEMO_HOUR = 8  # the README's cron line runs `pm memo` on weekdays at 08:00, server time
RUNS_MAX = 30
UNRELEASED_TTL, UNRELEASED_ERROR_TTL = 600, 60  # seconds
_unreleased_cache = {}
LAUNCH_DAYS, LAUNCH_KEEP, WATCH_DAYS = 35, 40, 31  # a launch is shown 35 days, its state kept 40, "under watch" 30
CHECKS = (("d1", 1), ("d7", 7), ("d30", 30))
VERDICTS = ("keep", "iterate", "rollback")
ROLLBACK_HINT = "roll back with the release manager"  # the CLI has no rollback command: the release manager does it
COMMS_GRACE = timedelta(days=1)  # a comms job not sent a day after its ship (or deferred) date is late
_launch_cache = {}
STAGES = ("Signal", "Roadmap", "Prototype", "Validated", "Build", "Review", "Develop", "Prod", "Announced", "Day 7", "Day 30")
STUCK_DAYS = {"Roadmap": 14, "Prototype": 7, "Validated": 7, "Build": 10, "Review": 3, "Develop": 5, "Prod": 3}  # Prod: days to announce
STUCK_DO = {"Roadmap": "Prototype {t} or move it out of Now (10 min)",  # step 2 of the Unstick inbox item
            "Prototype": "Open its prototype in Factory > Prototypes and validate it or send it back (5 min)",
            "Validated": "Plan the build of {t} with factory plan phase (5 min)",
            "Build": "Post an update or a plan for {p} (2 min)",
            "Review": "Open the PR and merge or close it (5 min)",
            "Develop": "Ship it with the release manager (5 min)",
            "Prod": "Send or skip its comms job in Factory > Release (2 min)"}
STUCK_NEXT = {"Roadmap": "no prototype yet", "Prototype": "not validated yet", "Validated": "build not started",
              "Build": "no update", "Review": "PR not merged", "Develop": "not in prod yet", "Prod": "not announced"}
NUDGE_DAYS = 3  # an unstuck row shows "nudged <date>" and no Unstick button this long
UNSTICK_SENDER = "pm-unstick"


STATE_MAPS = ("signals", "rejected", "launches", "stages", "nudged", "roadmaps")


def state():
    """pm.json, refused unless it is {snoozed: {project: ISO}, lastSweep: {...}[, signals: {id: {by, until}},
    rejected: {product:row: {at, score}}, launches: {id: {checks, verdict, kit_queued}}, stages: {feature: {stage, at}},
    nudged: {feature: {at, stage, item}}, roadmaps: {product: {at, by, sources, rows}}, runs: [...]]}."""
    data = store.read(PM_REL, {"snoozed": {}, "lastSweep": {}})
    if not (isinstance(data, dict) and isinstance(data.get("snoozed"), dict) and isinstance(data.get("lastSweep"), dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in data["snoozed"].items())
            and all(isinstance(data.setdefault(k, {}), dict) for k in STATE_MAPS)
            and all(isinstance(v, dict) for k in STATE_MAPS for v in data[k].values())
            and isinstance(data.setdefault("runs", []), list)):
        raise FactoryError(f"{store.path(PM_REL)} is not a product manager state file: fix or restore it")
    return data


def quiet_days():
    n = config.get("pm.quiet_days", QUIET_DEFAULT)
    if isinstance(n, str) and n.isdigit():
        n = int(n)  # `factory config set` stores strings unless --json
    if type(n) is not int or not 1 <= n <= MAX_DAYS:
        raise FactoryError(f"config pm.quiet_days is {n!r}: set a whole number of days, 1 to {MAX_DAYS}")
    return n


def _ts(s):
    """An ISO timestamp or date as an aware datetime, or None."""
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _day(d):
    return d.strftime("%d %b").lstrip("0")


def done(p):
    """A build project whose every phase has merged all its units (and that has units at all)."""
    phases = p.get("phases") or []
    return p.get("kind") == "build" and sum(ph["total"] for ph in phases) > 0 and all(ph["merged"] >= ph["total"] for ph in phases)


def last_touch(p):
    """Latest of: a project update, a plan snapshot, a loop result, creation. None when nothing is dated."""
    stamps = [_ts(u.get("at")) for u in store.read_lines(f"updates/{p['id']}.jsonl")]
    stamps += [_ts(d) for d in (p.get("history") or {})]
    stamps += [_ts(p.get("lastActive")), _ts(p.get("createdAt"))] + [_ts(e[3]) for e in p.get("exps") or [] if len(e) > 3]
    return max((s for s in stamps if s), default=None)


def _act(action, label, **kw):
    return {"action": action, "label": label, **kw}


def sweep(now=None):
    """The findings, in group order. Closed and snoozed projects are left out (a snooze hides all of a project's rows)."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    st, qd = state(), quiet_days()
    snoozed = {pid for pid, until in st["snoozed"].items() if (_ts(until) or now) > now}
    try:
        on_ap = set(store.autopilot()["projects"])
    except FactoryError:
        on_ap = set()
    projects = store.projects()
    by_id = {p["id"]: p for p in projects}
    protos = store.protos()
    nudge_act = lambda pid: _act("nudge", "Nudge agent", disabled=None if pid in on_ap else "not on autopilot: no agent session to nudge")
    out = {g: [] for g in GROUPS}

    for p in projects:
        if p.get("closedAt"):
            continue
        pid, name, finished = p["id"], p["name"], done(p)
        if finished:
            n = sum(ph["total"] for ph in p["phases"])
            out["finished"].append({"project": pid, "title": name, "evidence": f"{n}/{n} units merged",
                                    "actions": [_act("close", "Close"), _act("snooze", "Keep open", days=FINISHED_SNOOZE)]})
        if not finished and (t := last_touch(p)) and (age := (now - t).days) > qd:
            out["quiet"].append({"project": pid, "title": name, "evidence": f"no update, plan change or result since {_day(t)} ({age} days)",
                                 "actions": [nudge_act(pid), _act("snooze", f"Snooze {SNOOZE}d", days=SNOOZE)]})
        late = sorted((ph for ph in p.get("phases") or [] if ph["merged"] < ph["total"] and ph["due"] < today.isoformat()),
                      key=lambda ph: ph["due"])
        if late:
            ph, more = late[0], len(late) - 1
            out["late"].append({"project": pid, "title": name,
                                "evidence": f"phase {ph['name']}: {ph['merged']}/{ph['total']} units, due {_day(_ts(ph['due']))}"
                                            + (f" (+{more} more late)" if more else ""),
                                "actions": [nudge_act(pid), _act("snooze", f"Snooze {SNOOZE}d", days=SNOOZE)]})

    for pr in protos:
        v, p = pr["versions"][0], by_id.get(pr.get("project"))
        if v.get("status") == "validated" and p and done(p) and not p.get("closedAt"):
            out["proto"].append({"project": p["id"], "key": pr["id"], "title": f"{pr.get('feature') or pr['id']} prototype v{v['v']}",
                                 "evidence": f"validated {str(v.get('date') or '')[:10]}, project done, prototype not marked built: check the screen shipped",
                                 "actions": [_act("open-proto", "Open prototype", proto=pr["id"], v=v["v"]),
                                             _act("proto-built", "Mark built", proto=pr["id"])]})

    findings = [dict(f, group=g, id=f"{g}:{f.get('key', f['project'])}") for g in GROUPS for f in out[g] if f["project"] not in snoozed]
    counts = {g: sum(f["group"] == g for f in findings) for g in GROUPS}
    return {"at": store.iso(now), "findings": findings, "counts": counts, "total": len(findings), "quietDays": qd,
            "checked": {"projects": len(projects), "prototypes": len(protos)}}


def unreleased():
    """Merged PRs not in prod, per configured release product. A product whose read fails carries its error."""
    try:
        rel = release.release()
    except FactoryError as e:
        return [{"product": "release", "name": "Release config", "error": str(e), "prs": []}]
    out, names = [], store.products()
    for key, cfg in sorted(rel["products"].items()):
        row = {"product": key, "name": (names.get(key) or {}).get("name", key), "repo": cfg.get("repo"), "prs": []}
        # ponytail: in-process cache, one GitHub read per product per 10 min (a failure: 60 s); a stored release plan would remove it
        prs, err = _cached((store.home(), key, cfg.get("repo")),
                           lambda: [{"number": p["number"], "title": p["title"], "mergedAt": p.get("mergedAt")}
                                   for p in release.unreleased_prs(cfg)[0]],
                           _unreleased_cache)
        shipped = set(rel["shipped_prs"].get(key, []))
        row["prs"] = [p for p in prs or [] if p["number"] not in shipped]
        if err:
            row["error"] = err
        out.append(row)
    return out


def record(result):
    """Remember the last sweep (and drop snoozes that ended)."""
    with store.lock():
        st = state()
        st["lastSweep"] = last = {k: result[k] for k in ("at", "total", "counts")}
        st["runs"] = st["runs"][-(RUNS_MAX - 1):] + [dict(last, checked=result["checked"])]
        at = _ts(result["at"])
        st["snoozed"] = {k: v for k, v in st["snoozed"].items() if (_ts(v) or at) > at}
        st["signals"] = {k: v for k, v in st["signals"].items() if (_ts(v.get("until")) or at) > at}
        try:
            now_rows = {f"{i.get('product')}:{i.get('id')}" for i in roadmap.load()["items"]
                        if isinstance(i, dict) and i.get("lane") == "now" and i.get("product") and i.get("id")}
            st["rejected"] = {k: v for k, v in st["rejected"].items() if k in now_rows}
        except FactoryError:
            pass  # an unreadable roadmap keeps the rejections until it is fixed
        life = (result.get("errors") or {}).get("life") or []
        # a failed shared source must not restart any clock; a failed product read or feature only freezes its own rows
        if "lifecycle" in result and not [e for e in life if not e.startswith(("unreleased ", "lifecycle "))]:
            frozen = {e[len("lifecycle "):].partition(": ")[0] for e in life if e.startswith("lifecycle ")}
            frozen |= {r["id"] for r in result["lifecycle"] if r["unread"]}
            st["stages"] = {**{k: v for k, v in st["stages"].items() if k in frozen},
                            **{r["id"]: old if (old := st["stages"].get(r["id"])) and old.get("stage") == r["stageName"]
                               else {"stage": r["stageName"], "at": result["at"]}
                               for r in result["lifecycle"] if r["stageName"] and r["id"] not in frozen}}
        st["nudged"] = {k: v for k, v in st["nudged"].items() if at - (_ts(v.get("at")) or at) < timedelta(days=NUDGE_DAYS)}
        store.write(PM_REL, st)


def close(pid, note="", now=None):
    with store.lock():
        p = store.find_project(pid)
        if p.get("closedAt"):
            raise FactoryError(f"project {pid!r} is already closed (reopen it: factory project reopen {pid})")
        p["closedAt"] = store.iso(now or datetime.now(timezone.utc))
        if note := note.strip():
            p["closeNote"] = note
        store.save_project(p)
    return p


def reopen(pid):
    with store.lock():
        p = store.find_project(pid)
        if not p.get("closedAt"):
            raise FactoryError(f"project {pid!r} is not closed")
        p.pop("closedAt"), p.pop("closeNote", None)
        store.save_project(p)
    return p


def snooze(pid, days, now=None):
    """Leave pid out of the sweep for `days` days; 0 ends a snooze. Returns the end (ISO) or None."""
    if type(days) is not int or not 0 <= days <= MAX_DAYS:
        raise FactoryError(f"bad days {days!r}: use a whole number, 0 to {MAX_DAYS} (0 ends a snooze)")
    store.find_project(pid)
    now = now or datetime.now(timezone.utc)
    with store.lock():
        st = state()
        if days:
            st["snoozed"][pid] = until = store.iso(now + timedelta(days=days))
        else:
            st["snoozed"].pop(pid, None)
            until = None
        store.write(PM_REL, st)
    return until


def nudge(pid):
    """Send the project's agent session the autopilot nudge (the same path as the Autopilot page's Send)."""
    store.find_project(pid)
    return autopilot.nudge_project(pid)


def mark_built(pid, proto_id):
    """Mark one validated prototype of project pid as built (`factory proto built`): only the row clicked."""
    store.find_project(pid)
    ids = [pr["id"] for pr in store.protos() if pr.get("project") == pid and pr["versions"][0].get("status") == "validated"]
    if proto_id not in ids:
        raise FactoryError(f"prototype {proto_id!r} is not a validated prototype of project {pid!r}")
    # pr 0 = built with no PR to name: the page shows a PR badge only for a non-zero number
    return store.mark_proto_built(proto_id, 0)


def _n(n, one, many):
    return f"{n} {one if n == 1 else many}"


# ---------- signals, Now priority and the morning memo ----------

def _clip(s, n):
    """s cut to at most n characters at a word boundary, with "..." when cut."""
    return s if len(s) <= n else s[:n - 3].rsplit(" ", 1)[0].rstrip(" ,:;") + "..."


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower()) if len(w) > 3 and w not in STOP}


def maps_to(text, rows):
    """The roadmap rows whose title shares at least 2 significant words with text."""
    w = _words(text)
    return [{"id": r["id"], "title": r["title"], "product": r["product"]} for r in rows if len(w & _words(r["title"])) >= 2]


def _ago(now, at):
    h = int((now - at).total_seconds() // 3600)
    return f"{h}h" if h < 48 else f"{h // 24} days"


def _guarded(errs, name, fn, default):
    """fn(), or default when it fails: one bad file costs its own section of the page, never the whole page."""
    try:
        return fn()
    except Exception as e:  # corrupt JSON shapes raise KeyError/TypeError/AttributeError as well as FactoryError
        errs.append(f"{name}: {e}" if str(e) else f"{name}: {type(e).__name__}")
        return default


def _inbox(key="items"):
    """inbox.json's open items (or resolved ones), refused unless it is a list of items with ids."""
    v = store.inbox().get(key, [])
    if not isinstance(v, list) or not all(isinstance(it, dict) and isinstance(it.get("id"), str) for it in v):
        raise FactoryError(f"{key} is not a list of inbox items")
    return v


def signals(now=None):
    """What people asked for in the last 7 days, newest first, each mapped to roadmap rows.
    Support: the digest's posted results. You: inbox answers with a note, and your messages to agents.
    Inbox: open items waiting on the owner for more than 24 h. supportError is set when the latest digest run failed;
    errors names every other source that could not be read (its signals are left out)."""
    now = now or datetime.now(timezone.utc)
    since, out, err, errs = now - timedelta(days=SIGNAL_DAYS), [], None, []
    try:
        hist = [h for h in digest.digest()["history"] if isinstance(h, dict)]
    except FactoryError as e:
        hist, err = [], str(e)
    posted = [h for h in hist if h.get("posted") and isinstance(h.get("result"), str) and _ts(h.get("at"))]
    if hist and hist[-1].get("error"):
        err = str(hist[-1]["error"])
    last = max((_ts(h["at"]) for h in posted), default=None)
    support_error = {"error": err, "from": store.iso(last) if last else None} if err else None
    for h in posted:
        if (at := _ts(h["at"])) >= since:
            m = re.search(r"\s*\((\d+) tickets?\)\s*$", h["result"])
            n = int(m.group(1)) if m else 0
            out.append({"id": f"support:{h['at']}", "src": "Support", "title": h["result"][:m.start()] if m else h["result"],
                        "tickets": n, "at": h["at"], "evidence": (f"{_n(n, 'ticket', 'tickets')} · " if m else "") + f"support digest {_day(at)}"})
    box = _guarded(errs, "inbox.json", lambda: {k: _inbox(k) for k in ("items", "resolved")}, {"items": [], "resolved": []})

    def answers():
        titles = {it["id"]: str(it.get("title") or "") for it in box["items"] + box["resolved"]}
        return [{"id": f"answer:{iid}", "src": "You", "title": a["note"].strip(), "tickets": 0, "at": a["at"],
                 "evidence": f"your note on {iid} ({titles.get(iid, '')[:60]}), {_day(at)}"}
                for iid, a in store.read("answers.json", {}).items()
                if isinstance(a, dict) and isinstance(a.get("note"), str) and a["note"].strip() and (at := _ts(a.get("at"))) and at >= since]

    def messages():
        return [{"id": f"message:{m['id']}", "src": "You", "title": m["raw"].strip(), "tickets": 0, "at": m["at"],
                 "evidence": f"your message to an agent, {_day(at)}"}
                for m in relay.load_outbox() if isinstance(m, dict) and m.get("kind") == "message" and isinstance(m.get("raw"), str)
                and m["raw"].strip() and (at := _ts(m.get("at"))) and at >= since]

    old_items = [{"id": f"inbox:{it['id']}", "src": "Inbox", "title": str(it.get("title") or ""), "tickets": 0, "at": it["at"],
                  "evidence": f"open {_ago(now, at)}, waiting on you"}
                 for it in box["items"] if it.get("from") not in (MEMO_SENDER, UNSTICK_SENDER, "support-digest")
                 and (at := _ts(it.get("at"))) and now - at > timedelta(hours=INBOX_OLD_HOURS)]
    out += _guarded(errs, "answers.json", answers, []) + _guarded(errs, "relay.json", messages, []) + old_items
    rows = _guarded(errs, "roadmap.json", lambda: [i for i in roadmap.items() if i.get("lane") != "done"], [])
    hidden = {k: v for k, v in _guarded(errs, "pm.json", lambda: state()["signals"], {}).items() if (_ts(v.get("until")) or now) > now}
    for s in out:
        s["maps"] = maps_to(s["title"], rows)
        if s["id"] in hidden:
            s["hidden"] = hidden[s["id"]].get("by")
    out.sort(key=lambda s: _ts(s["at"]), reverse=True)
    return {"signals": out, "supportError": support_error, "errors": errs}


def _base(asks, risk):
    """The part of a row's score that is not its place: what a rejection holds on."""
    return asks * 3 + (3 if risk else 0)


def priority(sigs):
    """The proposed order of each product's Now lane. Score = support tickets mapped to the row x 3 + the owner's
    place (P1 = 7 down to P7 = 1) + 3 for money risk. A rejected row keeps its place while its tickets and risk
    are unchanged."""
    rejected, counted, names = state()["rejected"], [s for s in sigs if s.get("hidden") != "ignore"], store.products()
    data, out = roadmap.load(), []
    for prod in sorted({i["product"] for i in data["items"] if i.get("lane") == "now"}):
        rows = []
        for k, it in enumerate((i for i in roadmap.items(prod, data) if i.get("lane") == "now"), 1):
            asks = sum(s["tickets"] for s in counted if any(m["id"] == it["id"] for m in s["maps"]))
            risk = any(w in f"{it['title']} {it.get('note') or ''}".lower() for w in RISK)
            score = _base(asks, risk) + max(0, 8 - k)
            rej = rejected.get(f"{prod}:{it['id']}")
            rows.append({"id": it["id"], "title": it["title"], "was": k, "asks": asks, "risk": risk, "score": score,
                         "rejected": bool(rej and rej.get("base") == _base(asks, risk))})
        order, free = list(rows), [r for r in rows if not r["rejected"]]
        for k, r in zip([k for k, r in enumerate(rows) if not r["rejected"]], sorted(free, key=lambda r: -r["score"])):
            order[k] = r  # sorted() is stable: a tie keeps today's order
        for k, r in enumerate(order, 1):
            d = r["was"] - k
            r.update(rank=k, move=f"+{d}" if d > 0 else str(d) if d else "=",
                     why=", ".join([f"{_n(r['asks'], 'ticket', 'tickets')} this week" if r["asks"] else "no tickets this week"]
                                   + (["money risk"] if r["risk"] else []) + [f"your P{r['was']}"]))
        out.append({"product": prod, "name": (names.get(prod) or {}).get("name", prod), "rows": order,
                    "moves": sum(r["move"] != "=" for r in order)})
    return out


SWEEP_TITLES = {
    "finished": lambda n: f"Close {_n(n, 'finished project', 'finished projects')}",
    "proto": lambda n: f"Check {_n(n, 'validated prototype', 'validated prototypes')} not marked built",
    "quiet": lambda n: f"Nudge or snooze {_n(n, 'quiet project', 'quiet projects')}",
    "late": lambda n: f"Chase {_n(n, 'project behind its date', 'projects behind their date')}",
    "unreleased": lambda n: f"Ship {_n(n, 'merged PR', 'merged PRs')} not in prod yet"}


def decisions(sw, sig, prio, cards=()):
    """The morning memo, at most 3 decisions in this order: a launch with a failing check (prod health first), a
    proposed Now reorder, the largest sweep group, the strongest signal no roadmap row covers. The Today tab and the
    inbox item both show this list."""
    out = _launch_decisions(cards)
    if p := next((p for p in prio if p["moves"]), None):
        top = max(p["rows"], key=lambda r: (r["was"] - r["rank"], -r["rank"]))
        out.append({"kind": "reorder", "tab": "prio", "product": p["product"],
                    "title": f"Reorder Now for {p['name']}: move {_clip(top['title'], 30)} to #{top['rank']}",
                    "text": f"{top['why'][0].upper()}{top['why'][1:]}. {_n(p['moves'], 'row moves', 'rows move')}."})
    sizes = {**{g: sw["counts"][g] for g in GROUPS}, "unreleased": sum(len(u["prs"]) for u in sw["unreleased"])}
    if sizes[g := max(sizes, key=sizes.get)]:  # a tie goes to the first group
        names = [f["title"] for f in sw["findings"] if f["group"] == g] or \
                [f"{u['name']} #{pr['number']} {pr['title']}" for u in sw["unreleased"] for pr in u["prs"]]
        out.append({"kind": "sweep", "tab": "sweep", "group": g, "title": SWEEP_TITLES[g](sizes[g]),
                    "text": ", ".join(names[:4]) + (f" (+{len(names) - 4} more)" if len(names) > 4 else "")})
    # only what customers and the owner asked for: an old inbox item or a message to an agent is not a product ask
    if loose := [s for s in sig["signals"] if s["id"].startswith(("support:", "answer:")) and not s.get("hidden") and not s["maps"]]:
        s = max(loose, key=lambda s: (s["tickets"], _ts(s["at"])))
        out.append({"kind": "signal", "tab": "sig", "signal": s["id"], "title": f"No roadmap row covers: {_clip(s['title'], 50)}",
                    "text": f"{s['src']}, {s['evidence']}"})
    return out[:3]


def next_run(now):
    """When the cron line files the next memo: the next weekday at MEMO_HOUR, server time."""
    t = now.astimezone().replace(hour=MEMO_HOUR, minute=0, second=0, microsecond=0)
    while t <= now or t.weekday() > 4:
        t += timedelta(days=1)
    return t


def overview(now=None):
    """Everything the page shows: the sweep, signals, Now priority, the memo's decisions, the tiles and the
    pm runs since yesterday."""
    now = now or datetime.now(timezone.utc)
    errs = {"sweep": [], "sig": [], "prio": [], "life": [], "after": [], "rm": [], "brief": []}  # by page tab: a source that could not be read
    sw = _guarded(errs["sweep"], "sweep", lambda: sweep(now), None) or {
        "at": store.iso(now), "findings": [], "counts": dict.fromkeys(GROUPS, 0), "total": 0, "quietDays": QUIET_DEFAULT,
        "checked": {"projects": 0, "prototypes": 0}}
    sw["unreleased"] = _guarded(errs["sweep"], "unreleased", unreleased, [])  # not in sweep(): the page build counts the sweep and must not read GitHub
    sig = signals(now)
    errs["sig"] = sig.pop("errors")
    prio = _guarded(errs["prio"], "roadmap", lambda: priority(sig["signals"]), [])
    cards = _guarded(errs["after"], "launches", lambda: launches(sig["signals"], now), [])
    if any(e.startswith("unreleased") for e in errs["sweep"]):
        errs["life"].append("unreleased: merged, not in prod could not be read")
    life = _guarded(errs["life"], "lifecycle", lambda: lifecycle(sig["signals"], sw["unreleased"], now, errs["life"]), [])
    since = datetime.combine(now.date() - timedelta(days=1), datetime.min.time(), timezone.utc)
    stats = {"followed": _guarded(errs["brief"], "projects", lambda: sum(not p.get("closedAt") for p in store.projects()), 0),
             "stuck": sum(r["stuck"] for r in life),
             "launched": sum(c["day"] < WATCH_DAYS and not c["verdict"] for c in cards),
             "signals": sum(not s.get("hidden") for s in sig["signals"]),
             "inbox": _guarded(errs["brief"], "inbox.json", lambda: len(_inbox()), 0)}
    runs = _guarded(errs["brief"], "pm.json", lambda: [
        r for r in state()["runs"] if isinstance(r, dict) and isinstance(r.get("counts"), dict) and isinstance(r.get("checked"), dict)
        and (_ts(r.get("at")) or since - timedelta(1)) >= since][::-1], [])
    rms = _guarded(errs["rm"], "roadmap", roadmaps, [])  # reads roadmap.json, like the priority tab
    return {**sw, **sig, "priority": prio, "launches": cards, "lifecycle": life, "roadmaps": rms, "lifeStages": STAGES, "decisions": decisions(sw, sig, prio, cards), "stats": stats, "runs": runs,
            "nextRun": store.iso(next_run(now)), "errors": errs}


def _prio_of(product):
    p = next((p for p in priority(signals()["signals"]) if p["product"] == product), None)
    if not p:
        raise FactoryError(f"product {product!r} has no Now rows")
    return p


def _row(p, row):
    r = next((r for r in p["rows"] if r["id"] == row), None)
    if not r:
        raise FactoryError(f"{row!r} is not in {p['product']}'s Now lane")
    return r


def _lane(product):
    """The product's Now row ids, in the lane's current order."""
    return [i["id"] for i in roadmap.items(product) if i.get("lane") == "now"]


def _stale(product):
    return FactoryError(f"{product}'s Now lane changed: reload")


def restore(product, ids, expect):
    """Undo: put the product's Now rows back in the order ids, only while the lane is still in the order expect
    (the order the approve left). Uses `factory roadmap move` (each row to the end of Now)."""
    if not isinstance(ids, list) or not isinstance(expect, list) or _lane(product) != expect or sorted(ids) != sorted(expect):
        raise _stale(product)
    _apply_order(ids)
    return {"product": product, "order": ids}


def _apply_order(ids):
    for i in ids:
        roadmap.move(i, "now")


def approve(product, row=None, shown=None, rank=None, proposed=None):
    """Apply one row's proposed move (row None: the whole proposed order). shown is the lane order the page showed,
    rank the row's shown proposed place, proposed the shown proposed order: when given, anything that moved since
    is refused. Returns {before, after}: the lane order before and right after, for Undo."""
    p = _prio_of(product)
    before = [r["id"] for r in sorted(p["rows"], key=lambda r: r["was"])]
    if shown is not None and shown != before:
        raise _stale(product)
    if row is None:
        if proposed is not None and proposed != [r["id"] for r in p["rows"]]:
            raise _stale(product)
        if not p["moves"]:
            raise FactoryError(f"nothing to move in {product}'s Now lane")
        after = [r["id"] for r in p["rows"]]
        _apply_order(after)
    else:
        r = _row(p, row)
        if rank is not None and rank != r["rank"]:
            raise _stale(product)
        if r["move"] == "=":
            raise FactoryError(f"{row} already sits where the proposal puts it")
        rest = [i for i in before if i != row]
        roadmap.move(row, "now", before=rest[r["rank"] - 1] if r["rank"] <= len(rest) else None)
        after = rest[:r["rank"] - 1] + [row] + rest[r["rank"] - 1:]
    # after is the order this call applied, not a re-read: Undo must never capture another writer's change
    return {"product": product, "row": row, "before": before, "after": after}


def reject(product, row, undo=False):
    """Keep a row where it is while its tickets and risk do not change (undo: let the proposal move it again)."""
    key = f"{product}:{row}"
    r = None if undo else _row(_prio_of(product), row)
    if r and r["move"] == "=":
        raise FactoryError(f"{row} has no proposed move to reject")
    with store.lock():
        st = state()
        if undo and st["rejected"].pop(key, None) is None:
            raise FactoryError(f"{row} is not rejected")
        if r:
            st["rejected"][key] = {"at": store.now_iso(), "base": _base(r["asks"], r["risk"])}
        store.write(PM_REL, st)
    return {"product": product, "row": row, "rejected": not undo}


def hide_signal(sid, by, now=None):
    """by "ignore": hidden 14 days and left out of the priority score. by "ok": noted, hidden while it is in
    the 7-day window. by None: show it again (Undo)."""
    now = now or datetime.now(timezone.utc)
    if by is not None and sid not in {s["id"] for s in signals(now)["signals"]}:
        raise FactoryError(f"no signal {sid!r} in the last {SIGNAL_DAYS} days")
    with store.lock():
        st = state()
        if by is None:
            if st["signals"].pop(sid, None) is None:
                raise FactoryError(f"signal {sid!r} is not hidden")
        else:
            st["signals"][sid] = {"by": by, "until": store.iso(now + timedelta(days=IGNORE_DAYS if by == "ignore" else SIGNAL_DAYS))}
        store.write(PM_REL, st)
    return {"signal": sid, "hidden": by}


# ---------- after launch ----------

def _cached(key, fn, cache=None):
    """fn() kept 10 min (a failure: 60 s) in cache (default: the launch cache). Returns (value, error)."""
    cache = _launch_cache if cache is None else cache
    hit = cache.get(key)
    if not (hit and time.time() - hit[0] < (UNRELEASED_ERROR_TTL if hit[2] else UNRELEASED_TTL)):
        try:
            hit = (time.time(), fn(), None)
        except Exception as e:  # gh down, a probe that cannot start: the chip says so, the tab stays
            hit = (time.time(), None, str(e) or type(e).__name__)
        cache[key] = hit
    return hit[1], hit[2]


def _chip(name, st, detail):
    return {"name": name, "state": st, "detail": detail}


def _touches(j, prs):
    return bool({comms._pr_num(p) for p in j.get("prs") or []} & set(prs))


def _launch_jobs(data, product, prs):
    """The comms jobs announcing any of a launch's PRs."""
    return [j for j in data["jobs"].values() if isinstance(j, dict) and j.get("product") == product and _touches(j, prs)]


def _due_why(off):
    return f"comms relay off: {off}" if off else "due: the release comms run drafts it"


def _no_job_email(data, product, prs, at, cl, now, off):
    """Why the launch PRs no stored job covers have no email yet (comms.due and comms.window decide which job, if
    any, will announce them)."""
    since = data["since"].get(product)
    if (_ts(at) or now) < (_ts(since) if since else comms._week_start(now.astimezone(release.TUNIS))):
        return "shipped before comms started"
    if not cl:
        return "waits for the changelog"
    if not since:  # with the relay off no run sets since: say so, not "next comms run"
        return _due_why(off) if off else "comms start on the next comms run"
    due = [j for j in comms.due(product, now, write=False) if _touches(j, prs)]
    if any(j["kind"] == "feature" for j in due):
        return _due_why(off)
    digest = next((j["until"] for j in due if j["kind"] == "digest"), None)
    if not digest:
        w = comms.window(product, now)
        digest = w["until"] if set(prs) & {p["n"] for p in w["pending"]} else None
    if digest:  # a digest is drafted only after the relay hands it to an agent
        return _due_why(off) if off else f"in the {_day(_ts(digest))} digest"
    return "no email due for these PRs"


def _kit(product, prs, at, now=None):
    """The launch kit, from comms.json: {items: [chip], missing: [names of the ✕ items]}, or {note} when there is no
    kit to make. at is the ship time as the journal wrote it (the changelog's key). Only the changelog can be missing:
    Draft the missing stores it. Email, video and caption are drafted by the release comms run: ✓, or ! with why not yet."""
    now = now or datetime.now(timezone.utc)
    if comms.comms_cfg(product) is None:
        return {"note": "no comms configured", "items": [], "missing": []}
    data = comms.comms()
    cl = data["changelogs"].get(f"{product}@{at}")
    if isinstance(cl, dict) and cl.get("items") and all(it.get("group") == "internal" for it in cl["items"]):
        return {"note": "internal release, no kit", "items": [], "missing": []}
    off = comms.relay_off(release.release(), product)
    due_why = _due_why(off)
    jobs = _launch_jobs(data, product, prs)
    drafts = [j["draft"] for j in jobs if isinstance(j.get("draft"), dict)]
    why = []  # per PR: what still keeps the email from going out
    for j in jobs:
        if j.get("status") not in comms.DONE:
            # a stored job with no draft and no status (a video failure) is still due
            why.append((j.get("status") or "pending") if isinstance(j.get("draft"), dict) or j.get("status") else due_why)
    internal = {it.get("n") for it in (cl or {}).get("items", []) if it.get("group") == "internal"}
    left = [n for n in prs if n not in internal and not any(_touches(j, [n]) for j in jobs)]
    if left:
        why.append(_no_job_email(data, product, left, at, cl, now, off))
    why = list(dict.fromkeys(why)) or ([] if jobs else ["no email due for these PRs"])
    if why:
        email = _chip("Email", "w", "; ".join(why))
    else:
        email = _chip("Email", "y", "skipped" if jobs and all(j["status"] == "skipped" for j in jobs) else "sent")
    gap = "redraft pending" if any(j.get("status") in ("needs-edit", "failed") for j in jobs) else \
        "drafted without it" if any(isinstance(j.get("draft"), dict) and j.get("status") not in comms.DONE for j in jobs) else \
        "sent without it" if email["state"] == "y" else email["detail"]
    video = _chip("Brag video", "y", "") if any((d.get("video") or {}).get("url") for d in drafts) else \
        _chip("Brag video", "w", "static card") if any(j.get("video_failed_at") for j in jobs) else _chip("Brag video", "w", gap)
    caption = _chip("Caption", "y", "") if any(d.get("caption") for d in drafts) else _chip("Caption", "w", gap)
    return {"items": [_chip("Changelog", "y" if cl else "n", ""), email, video, caption], "missing": [] if cl else ["Changelog"]}


def _comms_chip(product, prs, at, now):
    if comms.comms_cfg(product) is None:
        return _chip("Comms sent", "w", "no comms configured")
    data = comms.comms()
    jobs = _launch_jobs(data, product, prs)
    if not jobs:
        return _chip("Comms sent", "w", "no comms job yet")
    deferred = data["deferred"].get(product) or {}
    late = []
    for j in jobs:
        if j.get("status") in comms.DONE:
            continue
        start = (_ts(j.get("until")) or at) if j.get("kind") == "digest" else at  # a digest goes out on Friday, not on ship day
        due = max([start] + [d for p in j.get("prs") or [] if (d := _ts(deferred.get(str(comms._pr_num(p)))))]) + COMMS_GRACE
        if now > due:
            late.append(f"{j['id']} {j.get('status') or 'pending'} since {_day(due - COMMS_GRACE)}")
    if late:
        return _chip("Comms sent", "n", "; ".join(late))
    if all(j.get("status") in comms.DONE for j in jobs):
        return _chip("Comms sent", "y", ", ".join(f"{j['id']} {j['status']}" for j in jobs))
    return _chip("Comms sent", "w", "comms job not due yet")


def _errors_chip(product, cfg):
    checks = [c for c in cfg.get("checks") or [] if c.get("kind") == "errors"]
    if not checks:
        return _chip("Errors since launch", "w", "no error check configured")
    res, err = _cached((store.home(), "errors", product, repr(checks)),
                       lambda: [release._run_signal_check(c, release._judge_errors) for c in checks])
    if err:
        return _chip("Errors since launch", "w", f"could not run the error check: {err}")
    broken = [r for r in res if str(r.get("detail") or "").startswith(("cmd error", "baseline_cmd error"))]
    bad = [r for r in res if not r.get("ok") and r not in broken]
    if broken and not bad:  # a broken probe is not an error spike, but it never hides a real one
        return _chip("Errors since launch", "w", "; ".join(f"{r['name']}: {r['detail']}" for r in broken))
    return _chip("Errors since launch", "n" if bad else "y",
                 "; ".join(f"{r['name']}: {r['detail'] or r['value']}" for r in bad)
                 or "; ".join(f"{r['name']}: {r['value']} (baseline {r['baseline']})" for r in res))


def _tickets_chip(titles, at, sigs):
    if not titles:
        return _chip("Support tickets", "w", "no PR titles to match tickets against")
    rows = [{"id": n, "title": t, "product": ""} for n, t in titles.items()]
    hits = [s for s in sigs if s["src"] == "Support" and s.get("hidden") != "ignore"
            and (_ts(s["at"]) or at) >= at and maps_to(s["title"], rows)]
    if not hits:
        return _chip("Support tickets", "y", "0 mention it")
    n = sum(s["tickets"] or 1 for s in hits)  # a result with no "(N tickets)" is still one
    return _chip("Support tickets", "w", f"{_n(n, 'ticket mentions', 'tickets mention')} it, newest: {hits[0]['title']}")


def _guard_chip(name, fn):
    try:
        return fn()
    except Exception as e:  # one unreadable source costs its chip, never the card
        return _chip(name, "w", f"could not read: {e}" if str(e) else f"could not read: {type(e).__name__}")


def _shipped(rel, now):
    """(launch id, journal entry, PR numbers, ship time) for each release shipped in the last 35 days and not
    rolled back since. The id is product:sha, or product:first PR when the entry has no sha."""
    journal = rel["journal"]
    for i, e in enumerate(journal):
        key, prs = e.get("product"), [n for n in e.get("prs") or [] if type(n) is int]
        at = _ts(e.get("at"))
        if e.get("event") != "shipped" or key not in rel["products"] or not at or not (now - timedelta(days=LAUNCH_DAYS) <= at <= now):
            continue
        if not (e.get("sha") or prs):
            continue  # nothing to name it by
        if any(x.get("event") == "rolled-back" and x.get("product") == key
               and (set(x.get("prs") or []) & set(prs) or (e.get("sha") and x.get("sha") == e.get("sha"))) for x in journal[i + 1:]):
            continue
        yield f"{key}:{e.get('sha') or prs[0]}", e, prs, at


def launches(sigs=(), now=None):
    """Shipped releases of the last 35 days, newest first, not rolled back since: each with live chips (comms, errors,
    support tickets), the recorded day-1/7/30 snapshots, the next check and the owner's verdict."""
    now = now or datetime.now(timezone.utc)
    rel, names, st, out = release.release(), store.products(), state()["launches"], []
    for lid, e, prs, at in _shipped(rel, now):
        key = e["product"]
        cfg = rel["products"][key]
        facts, terr = _cached((store.home(), "titles", cfg.get("repo"), tuple(prs)),
                              lambda: {n: f["title"] for n, f in comms.pr_facts(cfg["repo"], prs).items()})
        titles = facts or {}
        ship, today = at.date(), now.date()
        rec = st.get(lid) or {}
        nxt = next(((k, n) for k, n in CHECKS if ship + timedelta(days=n) > today), None)
        out.append({"id": lid, "product": key, "name": (names.get(key) or {}).get("name", key), "repo": cfg.get("repo"),
                    "at": e["at"], "sha": e.get("sha"), "day": (today - ship).days,
                    "prs": [{"number": n, "title": titles.get(n, "")} for n in prs], **({"titlesError": terr} if terr else {}),
                    "chips": [_guard_chip("Comms sent", lambda: _comms_chip(key, prs, at, now)),
                              _guard_chip("Errors since launch", lambda: _errors_chip(key, cfg)),
                              _tickets_chip(titles, at, sigs)],
                    "kit": _guarded_kit(key, prs, e["at"], rec, now),
                    "checks": rec.get("checks") or {}, "verdict": rec.get("verdict"),
                    **({"rollback": ROLLBACK_HINT} if (rec.get("verdict") or {}).get("by") == "rollback" else {}),
                    "next": f"Day-{nxt[1]} check {_day(ship + timedelta(days=nxt[1]))}" if nxt else "Day-30 check done",
                    "due": [k for k, n in CHECKS if ship + timedelta(days=n) <= today]})
    out.sort(key=lambda x: _ts(x["at"]), reverse=True)
    return out


def _guarded_kit(product, prs, at, rec, now):
    try:
        k = _kit(product, prs, at, now)
    except Exception as e:  # an unreadable comms.json costs the kit line, never the card
        k = {"error": f"could not read: {str(e) or type(e).__name__}", "items": [], "missing": []}
    return {**k, "queued": rec.get("kit_queued")}


class KitError(FactoryError):
    """Reading the kit or storing the changelog failed: the server's fault, not the request's."""


def kit(lid, undo=False, now=None):
    """Draft the missing: store the launch's missing changelog and note kit_queued on the launch. Nothing else is
    drafted here and no agent starts: the release comms run drafts the email, video and caption.
    undo: remove the note; the stored changelog stays."""
    now = now or datetime.now(timezone.utc)
    if undo:
        with store.lock():
            st = state()
            if (st["launches"].get(lid) or {}).pop("kit_queued", None) is None:
                raise FactoryError(f"launch {lid!r} has no queued kit")
            store.write(PM_REL, st)
        return {"launch": lid, "queued": None}
    hit = next(((e, prs) for i, e, prs, _ in _shipped(release.release(), now) if i == lid), None)
    if not hit:
        raise FactoryError(f"no launch {lid!r} in the last {LAUNCH_DAYS} days")
    e, prs = hit
    product, at = e["product"], e["at"]
    if (q := (state()["launches"].get(lid) or {}).get("kit_queued")):
        raise FactoryError(f"{lid}: kit already queued {q['at'][:10]}")
    try:
        k = _kit(product, prs, at, now)
    except Exception as x:
        raise KitError(f"could not read the kit of {lid}: {str(x) or type(x).__name__}") from x
    if k.get("note"):
        raise FactoryError(f"{lid}: {k['note']}")
    if not k["missing"]:
        raise FactoryError(f"{lid}: its changelog is already stored")
    try:
        comms.changelog(product, at=at)
    except Exception as x:
        raise KitError(f"could not store the changelog of {lid}: {str(x) or type(x).__name__}") from x
    with store.lock():
        st = state()
        rec = st["launches"].setdefault(lid, {"at": at})
        rec["kit_queued"] = q = {"at": store.iso(now), "items": k["missing"]}
        store.write(PM_REL, st)
    return {"launch": lid, "queued": q}


def record_checks(cards, now=None):
    """The memo's day checks: for each launch, snapshot its chips as the latest due check (d1, d7 or d30) once;
    drop the state of launches shipped more than 40 days ago. Returns the ids snapshotted."""
    now = now or datetime.now(timezone.utc)
    done = []
    with store.lock():
        st = state()
        for c in cards:
            if not c["due"]:
                continue
            rec = st["launches"].setdefault(c["id"], {"at": c["at"]})
            checks = rec.setdefault("checks", {})
            if c["due"][-1] not in checks:
                checks[c["due"][-1]] = {"at": store.iso(now), "chips": c["chips"]}
                done.append(c["id"])
        kept = {k: v for k, v in st["launches"].items() if now - (_ts(v.get("at")) or now) <= timedelta(days=LAUNCH_KEEP)}
        if done or len(kept) < len(st["launches"]):
            st["launches"] = kept
            store.write(PM_REL, st)
    return done


def verdict(lid, by, now=None):
    """The owner's verdict on a launch (keep, iterate, rollback), or None to remove it (Undo). Nothing is rolled back:
    a rollback verdict names how to do it."""
    now = now or datetime.now(timezone.utc)
    at = None
    if by is not None:
        if by not in VERDICTS:
            raise FactoryError(f"bad verdict {by!r}: use one of {', '.join(VERDICTS)}")
        at = next((e["at"] for i, e, _, _ in _shipped(release.release(), now) if i == lid), None)
        if not at:
            raise FactoryError(f"no launch {lid!r} in the last {LAUNCH_DAYS} days")
    with store.lock():
        st = state()
        rec = st["launches"].get(lid) or {}
        if by is None:
            if rec.pop("verdict", None) is None:
                raise FactoryError(f"launch {lid!r} has no verdict")
        else:
            st["launches"][lid] = rec = {"at": at, **rec, "verdict": {"by": by, "at": store.iso(now)}}
        store.write(PM_REL, st)
    return {"launch": lid, "verdict": rec.get("verdict"), **({"command": ROLLBACK_HINT} if by == "rollback" else {})}


def _launch_decisions(cards):
    """A launch whose live chips have a ✕ and no verdict yet: prod health, first on Today."""
    out = []
    for c in cards:
        bad = [x for x in c["chips"] if x["state"] == "n"]
        if bad and not c["verdict"]:
            what = ", ".join({"Errors since launch": "errors up since launch", "Comms sent": "comms not sent"}.get(x["name"], x["name"])
                             for x in bad)
            out.append({"kind": "launch", "tab": "after", "launch": c["id"], "title": f"{c['name']} release {_day(_ts(c['at']))}: {what}",
                        "text": "; ".join(x["detail"] for x in bad)[:200]})
    return out


# ---------- lifecycle ----------

def _features(projects, rows):
    """Every open project, then each Now row no project carries yet: (id, tag, title, product, project, row).
    Row ids repeat across products: a project's feat matches a row of its own product, a row's feature id is product:id."""
    by_id = {(r.get("product"), r["id"]): r for r in rows if r.get("id")}
    feats = {(p["product"], p["feat"]) for p in projects if p.get("feat")}
    out = [(p["id"], p.get("feat") or p["id"], p["name"], p["product"], p, by_id.get((p["product"], p.get("feat")))) for p in projects]
    return out + [(f"{r['product']}:{r['id']}", r["id"], str(r.get("title") or r["id"]), r["product"], None, r) for r in rows
                  if r.get("lane") == "now" and r.get("id") and r.get("product") and not r.get("project")
                  and (r["product"], r["id"]) not in feats]


def _latest(stamps, now):
    """The newest of stamps; None (undated) counts as now. None when stamps is empty."""
    return max(stamps, key=lambda d: d or now, default=None)


def _reached(title, product, p, row, src, now, unread):
    """{stage: when it got there (None: unknown)} for every stage the feature reached. unread gets each PR whose
    product's merged list could not be read (the feature's stage past Build is unknown)."""
    at, rel = {}, src["rel"]
    if row and (t := [_ts(s["at"]) for s in src["sigs"] if any((m["product"], m["id"]) == (row["product"], row["id"]) for m in s["maps"])]):
        at["Signal"] = _latest(t, now)
    if row or (p and p.get("feat")):
        at["Roadmap"] = _ts(p.get("createdAt")) if p else None
    # a prototype is linked by its project; one with no project matches by product and 2 significant title words
    mine = [x for x in src["protos"] if (p and x.get("project") == p["id"]) or
            (not x.get("project") and x.get("product") == product and len(_words(x.get("feature") or "") & _words(title)) >= 2)]
    if mine:
        at["Prototype"] = _latest([_ts(x["versions"][0].get("date")) for x in mine], now)
        if ok := [_ts(v.get("date")) for x in mine for v in x["versions"] if v.get("status") in ("validated", "built")]:
            at["Validated"] = _latest(ok, now)
    if not p:
        return at
    refs = p.get("prs") or []
    if refs or sum(ph.get("total", 0) for ph in p.get("phases") or []):
        at["Build"] = last_touch(p)
    prs = set()  # (release product, PR number); a PR whose repo no release product names stays unknown
    for ref in refs:
        repo, _, n = str(ref).rpartition("#")
        if (k := src["repos"].get(repo)) is not None:
            prs.add((k, int(n)))
    state_of = {(k, n): "shipped" if n in src["shipped"].get(k, ()) else "merged" if n in src["merged"].get(k, ())
                else "open" if k in src["merged"] else None for k, n in prs}  # None: that product's merged list could not be read
    unread += [kn for kn, v in state_of.items() if v is None]
    if "open" in state_of.values():
        at["Review"] = last_touch(p)
    if not (prs and len(prs) == len(refs) and all(v in ("merged", "shipped") for v in state_of.values())):
        return at
    # the newest merge of its PRs still waiting for prod (None when GitHub gave none: the first pm run that saw it here dates it)
    at["Develop"] = max((m for k, n in prs if state_of[(k, n)] == "merged" and (m := _ts(src["merged"][k][n]))), default=None)
    if any(v != "shipped" for v in state_of.values()):
        return at
    at["Prod"] = _latest([_ts(e.get("at")) for e in rel["journal"] if isinstance(e, dict) and e.get("event") == "shipped"
                          and {(e.get("product"), n) for n in e.get("prs") or []} & prs], now)
    if sent := [_ts(j.get("sent_at") or j.get("updated")) for j in src["jobs"] if j.get("status") in comms.DONE
                and {(j.get("product"), comms._pr_num(x)) for x in j.get("prs") or []} & prs]:
        at["Announced"] = _latest(sent, now)
    for lid, e, nums, _ in src["launched"]:
        if {(e["product"], n) for n in nums} & prs:
            for k, name in (("d7", "Day 7"), ("d30", "Day 30")):
                if c := ((src["launches"].get(lid) or {}).get("checks") or {}).get(k):
                    at[name] = _ts(c.get("at"))
    return at


def lifecycle(sigs=(), unrel=(), now=None, errs=None):
    """One row per followed feature (open projects, Now rows with no project): the furthest stage it reached, its
    days there and whether that is past the stage's limit. Stuck rows first, then by stage, furthest last.
    Each source that cannot be read, and each feature that cannot be placed, adds to errs; the rest still show."""
    now, errs = now or datetime.now(timezone.utc), [] if errs is None else errs
    projects = [p for p in _guarded(errs, "projects", store.projects, []) if not p.get("closedAt")]
    rows = _guarded(errs, "roadmap.json", roadmap.items, [])
    rel = _guarded(errs, "release.json", release.release, None) or {"products": {}, "shipped_prs": {}, "journal": []}
    st = _guarded(errs, "pm.json", state, None) or {"stages": {}, "nudged": {}, "launches": {}}
    src = {"sigs": sigs, "rel": rel, "launches": st["launches"], "protos": _guarded(errs, "protos.json", store.protos, []),
           "jobs": _guarded(errs, "comms.json", lambda: [j for j in comms.comms()["jobs"].values() if isinstance(j, dict)], []),
           "repos": {cfg.get("repo"): k for k, cfg in rel["products"].items() if isinstance(cfg, dict)},
           "shipped": {k: set(v) for k, v in rel["shipped_prs"].items()},
           "merged": {u["product"]: {x["number"]: x.get("mergedAt") for x in u["prs"]} for u in unrel if not u.get("error")},
           "launched": list(_shipped(rel, now))}
    errs += [f"unreleased {u['product']}: {u['error']}" for u in unrel if u.get("error")]  # its PRs' stages are unknown
    out = []
    for fid, tag, title, product, p, row in _features(projects, rows):
        try:
            at = _reached(title, product, p, row, src, now, unread := [])
            ups = sorted(store.read_lines(f"updates/{fid}.jsonl"), key=lambda u: str(u.get("at"))) if p else []
        except Exception as e:  # one malformed project or row costs its own line
            errs.append(f"lifecycle {fid}: {e or type(e).__name__}")
            continue
        k = max((STAGES.index(s) for s in at), default=-1)
        name = STAGES[k] if k >= 0 else None
        seen = st["stages"].get(fid) or {}
        when = at.get(name) or (_ts(seen.get("at")) if seen.get("stage") == name else None)
        days = max(0, (now - when).days) if name and when else None
        stuck = name in STUCK_DAYS and days is not None and days > STUCK_DAYS[name]  # Signal, Announced, Day 7/30 never stick
        if "Prod" in at and (at["Prod"] is None or now - at["Prod"] > timedelta(days=LAUNCH_DAYS)):
            stuck = False  # shipped past the launch window, or so long ago the journal lost it: finished, the sweep closes it
        note = f"{name} for {days} days (limit {STUCK_DAYS[name]}): {STUCK_NEXT[name]}" if stuck else \
            str(ups[-1].get("text") or "") if ups else "In Now, no project yet" if not p else "No update yet"
        n = _ts((st["nudged"].get(fid) or {}).get("at"))
        out.append({"id": fid, "tag": tag, "title": title, "product": product, "project": p["id"] if p else None,
                    "stage": k, "stageName": name, "reached": [s in at for s in STAGES], "days": days, "stuck": stuck, "unread": bool(unread),
                    "limit": STUCK_DAYS.get(name), "note": note,
                    "nudged": store.iso(n) if n and now - n < timedelta(days=NUDGE_DAYS) else None})
    out.sort(key=lambda r: (not r["stuck"], r["stage"], -(r["days"] or 0)))
    return out


def unstick(fid, undo=False, now=None):
    """File one inbox item naming a stuck feature, its stage and days, and note "nudged" in pm.json (no agent starts).
    undo: remove the note; the inbox item stays."""
    now = now or datetime.now(timezone.utc)
    if undo:
        with store.lock():
            st = state()
            if st["nudged"].pop(fid, None) is None:
                raise FactoryError(f"{fid!r} was not nudged")
            store.write(PM_REL, st)
        return {"feature": fid, "nudged": None}
    errs = []
    r = next((r for r in lifecycle(signals(now)["signals"], unreleased(), now, errs) if r["id"] == fid), None)
    # another product's failed merged-not-in-prod read leaves this feature alone; any other failed source might not
    if [e for e in errs if not e.startswith(("unreleased ", "lifecycle "))] or (r and r["unread"]) \
            or [e for e in errs if e.startswith(f"lifecycle {fid}: ")]:
        raise FactoryError(f"could not read {'; '.join(errs)}: the stage and days may be wrong, fix that first")
    if not r:
        raise FactoryError(f"no followed feature {fid!r} (see: factory pm lifecycle)")
    if not r["stuck"]:
        raise FactoryError(f"{fid} is not stuck")
    opened = [p for p in store.projects() if not p.get("closedAt")]
    # the feature's own project first; the inbox item needs a project the page can draw
    anchor = next((p["id"] for p in sorted(opened, key=lambda p: p["id"] != r["project"]) if not store.why_hidden(p)), None)
    if anchor is None:
        raise FactoryError("no open project the page can draw to file the inbox item on")
    with store.lock():  # the check and a pending note in one lock: two clicks file one item (add_ask_item takes the lock)
        st = state()
        if (n := _ts((st["nudged"].get(fid) or {}).get("at"))) and now - n < timedelta(days=NUDGE_DAYS):
            raise FactoryError(f"{fid} was nudged {store.iso(n)[:10]}: wait {NUDGE_DAYS} days or undo it")
        st["nudged"][fid] = rec = {"at": store.iso(now), "stage": r["stageName"], "item": None}
        store.write(PM_REL, st)
    step = STUCK_DO[r["stageName"]].format(t=r["title"], p=r["project"] or r["title"])
    try:
        item = store.add_ask_item(anchor, "action", _clip(f"Unstick {r['title']}: {r['stageName']} {r['days']} days", 80),
                                  "Unstick a feature", f"1. Open Factory > Product manager > Lifecycle (1 min)\n2. {step}",
                                  [["done", "Done"], ["blocked", "Blocked"]], None, r["note"], UNSTICK_SENDER, None)
    except Exception:
        with store.lock():
            st = state()
            st["nudged"].pop(fid, None)
            store.write(PM_REL, st)
        raise
    with store.lock():
        st = state()
        st["nudged"][fid] = rec = dict(rec, item=item["id"])
        store.write(PM_REL, st)
    return {"feature": fid, "nudged": rec, "item": item["id"]}


# ---------- Roadmaps: an agent proposes a first roadmap, the owner approves or drops each row ----------

RM_LANES = ("now", "next", "later")
RM_ROWS = (3, 12)
RM_TITLE_MAX = 80
RM_FIELDS = ("lane", "title", "goal", "evidence")
RM_LOCK = ".pm-roadmaps.lock"  # one proposal or row change at a time: two clicks never add a row twice
_RANK_PREFIX = re.compile(r"^P\d+\s*[-—–]\s*")  # a Notion Now title's "P1 — ", as roadmap.py writes it


class RoadmapWriteError(FactoryError):
    """roadmap.py refused or failed to add a row (Now full, Notion down): the server's answer, not a bad request."""
    def __init__(self, message, row=None):
        super().__init__(message)
        self.row = row


class StaleProposal(FactoryError):
    """The page or the CLI acted on an older proposal than the one stored: a 409."""


def _rm_rows(rows):
    """The proposal's rows, checked: 3 to 12, each an object of non-empty strings, a known lane, a one-line title."""
    if not isinstance(rows, list) or not RM_ROWS[0] <= len(rows) <= RM_ROWS[1]:
        got = f"{len(rows)} rows" if isinstance(rows, list) else "no list"
        raise FactoryError(f"a proposal has {RM_ROWS[0]} to {RM_ROWS[1]} rows, got {got}")
    for n, r in enumerate(rows, 1):
        if not isinstance(r, dict):
            raise FactoryError(f"row {n} must be an object with {', '.join(RM_FIELDS)}")
        for k in RM_FIELDS:
            if not isinstance(r.get(k), str) or not r[k].strip():
                raise FactoryError(f"row {n}: {k} must be a non-empty string")
        t = r["title"].strip()
        if len(t) > RM_TITLE_MAX:
            raise FactoryError(f"row {n}: title is {len(t)} characters, at most {RM_TITLE_MAX}")
        if any(ord(c) < 32 for c in t):
            raise FactoryError(f"row {n}: title must be one line")
        if r["lane"] not in RM_LANES:
            raise FactoryError(f"row {n}: lane {r['lane']!r} is not one of {', '.join(RM_LANES)}")
    seen = [_RANK_PREFIX.sub("", r["title"].strip()).casefold() for r in rows]
    if dup := next((t for k, t in enumerate(seen) if t in seen[:k]), None):
        raise FactoryError(f"two rows have the title {dup!r}: titles must differ")
    return rows


def _rm_writable(product, rows, data):
    """Refuse a proposal the roadmap could not take: a source in error, a Notion source never synced (it may hold
    rows the factory has not pulled), or more Now rows than the room left in Now."""
    if err := roadmap.source_error(product, data):
        raise FactoryError(f"{product}'s roadmap source is in error ({err}): fix it before proposing")
    if roadmap.get_notion_config(product) and not (data.get("sources", {}).get(product) or {}).get("syncedAt"):
        raise FactoryError(f"{product}'s Notion roadmap has never synced: run factory roadmap sync {product} first")
    room = roadmap.cap() - sum(i.get("lane") == "now" for i in roadmap.items(product, data))
    if (n := sum(r["lane"] == "now" for r in rows)) > room:
        raise FactoryError(f"{n} Now rows, but {product}'s Now lane has room for {room}")


def propose_roadmap(product, rows, sources="", now=None):
    """Store an agent's proposed first roadmap for a product with none. It replaces an older proposal only while
    none of that one's rows is approved. Drafts nothing and starts no agent."""
    now = now or datetime.now(timezone.utc)
    if product not in store.products():
        raise FactoryError(f"unknown product {product!r}")
    rows = _rm_rows(rows)
    if not isinstance(sources, str):
        raise FactoryError("sources must be a string")
    with store.lock(RM_LOCK):
        data = roadmap.load()
        if roadmap.items(product, data):
            raise FactoryError(f"{product} already has a roadmap: change it in the Roadmap view")
        _rm_writable(product, rows, data)
        with store.lock():
            st = state()
            if any(r.get("status") == "approved" for r in _rm_prop_rows(st, product)):
                raise FactoryError(f"{product}'s proposal has approved rows: it cannot be replaced")
            st["roadmaps"][product] = rec = {
                "at": now.isoformat(timespec="microseconds").replace("+00:00", "Z"),  # the stale-click guard: unique per proposal
                "sources": sources.strip(),
                "rows": [{"id": f"{product}-r{n}", **{k: r[k].strip() for k in RM_FIELDS}, "status": "proposed"}
                         for n, r in enumerate(rows, 1)]}
            store.write(PM_REL, st)
    return {"product": product, **rec}


def _rm_prop_rows(st, product):
    """A stored proposal's rows, refused (for the tab's error) when pm.json was hand-edited into another shape."""
    rows = st["roadmaps"].get(product, {}).get("rows", [])
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise FactoryError(f"pm.json roadmaps.{product}.rows is not a list of rows: fix or restore it")
    return rows


def roadmaps():
    """One card per product the dashboard knows: its status (none, draft, live), sources and proposed rows."""
    st, cards = state(), []
    for key, p in store.products().items():
        prop, n, rows = st["roadmaps"].get(key) or {}, len(roadmap.items(key)), _rm_prop_rows(st, key)
        status = "draft" if any(r.get("status") == "proposed" for r in rows) else "live" if n else "none"
        sources = (prop.get("sources") or "not given") if prop else f"{n} items in the Roadmap view" if n else "none yet"
        cards.append({"product": key, "name": p.get("name", key), "status": status, "items": n, "rows": rows,
                      "at": prop.get("at"), "sources": sources,
                      "command": f"factory pm propose-roadmap {key} --file rows.json"})
    return cards


def _rm_find(st, rid):
    for product in st["roadmaps"]:
        for r in _rm_prop_rows(st, product):
            if r.get("id") == rid:
                return product, r
    raise FactoryError(f"no proposed roadmap row {rid!r} (see: factory pm roadmaps)")


def _rm_check(st, product, at):
    """The proposal acted on must be the stored one: row ids repeat across proposals."""
    if product not in store.products():
        raise FactoryError(f"unknown product {product!r}")
    if at != (st["roadmaps"].get(product) or {}).get("at"):
        raise StaleProposal(f"{product}'s proposal changed, reload")


def _rm_set(rid, **fields):
    """Set (a None value: remove) fields on one proposed row in pm.json; returns the row."""
    with store.lock():
        st = state()
        r = _rm_find(st, rid)[1]
        for k, v in fields.items():
            if v is None:
                r.pop(k, None)
            else:
                r[k] = v
        store.write(PM_REL, st)
        return dict(r)


def _rm_place(product, rows, r, item):
    """roadmap add puts an item on top of its lane: move it so the lane keeps the proposal's order (the next approved
    proposal row of that lane comes after it, else it goes right after the last earlier one)."""
    lane = [i["id"] for i in roadmap.items(product) if i.get("lane") == r["lane"] and i["id"] != item]
    pos = {x["item"]: k for k, x in enumerate(rows) if x.get("status") == "approved" and x.get("lane") == r["lane"] and x.get("item")}
    me = next(k for k, x in enumerate(rows) if x["id"] == r["id"])
    later = next((i for i in lane if pos.get(i, -1) > me), None)
    if later:
        return roadmap.move(item, r["lane"], before=later)
    earlier = [k for k, i in enumerate(lane) if i in pos]
    if earlier:  # all approved rows come before this one: right after the last of them
        k = earlier[-1] + 1
        roadmap.move(item, r["lane"], before=lane[k] if k < len(lane) else None)


def _rm_approve(product, rows, r, now):
    """roadmap add (unless an earlier try already added the title), placed in the proposal's order, then the goal
    and evidence as the item's note. A roadmap failure marks nothing; a failure after the add names the item."""
    warn = []
    taken = {x.get("item") for x in rows if x["id"] != r["id"]}  # never mark a row with another row's item
    have = next((i for i in roadmap.items(product)
                 if i["id"] not in taken and i.get("lane") == r["lane"]  # an earlier try of this row, never an item elsewhere
                 and _RANK_PREFIX.sub("", i.get("title", "")) == r["title"]), None)
    if have:
        item = have
        warn.append(f"already in the roadmap as {item['id']}: marked, not added again")
    else:
        try:
            item = roadmap.add(product, r["lane"], r["title"])
        except Exception as e:
            raise RoadmapWriteError(f"{r['id']}: {str(e) or type(e).__name__}", r["id"]) from e
        warn += [item["warning"]] if item.get("warning") else []
        for what, fn in (("not placed in the proposal's order", lambda: _rm_place(product, rows, r, item["id"])),
                         ("the goal and evidence note was not saved",
                          lambda: roadmap.update(item["id"], note=f"Goal: {r['goal']} · Evidence: {r['evidence']}"))):
            try:
                fn()
            except Exception as e:  # the item is in the roadmap now: still mark the row
                warn.append(f"added, but {what}: {str(e) or type(e).__name__}")
    try:
        row = _rm_set(r["id"], status="approved", item=item["id"], at=store.iso(now))
    except Exception as e:
        raise RoadmapWriteError(f"{r['id']}: added as {item['id']}, but the row was not marked "
                                f"({str(e) or type(e).__name__}); approving it again marks it", r["id"]) from e
    r.update(row)  # approve all: later rows place themselves after this one
    return dict(row, warning="; ".join(warn)) if warn else row


def roadmap_row(rid, action, at, undo=False, now=None):
    """approve: add the row to the roadmap (no undo: it is in the Roadmap view). drop: mark it dropped (undo: proposed).
    at is the proposal's at the caller saw: another proposal since is refused (StaleProposal)."""
    now = now or datetime.now(timezone.utc)
    if action not in ("approve", "drop"):
        raise FactoryError("action must be approve or drop")
    if undo and action == "approve":
        raise FactoryError("an approved row has no undo: it is in the Roadmap view, change it there")
    with store.lock(RM_LOCK):
        st = state()
        product, r = _rm_find(st, rid)
        _rm_check(st, product, at)
        want = "dropped" if undo else "proposed"
        if r.get("status") != want:
            raise FactoryError(f"{rid} is {r.get('status')}, not {want}")
        if action == "approve":
            return {"product": product, "row": _rm_approve(product, _rm_prop_rows(st, product), r, now)}
        row = _rm_set(rid, status="proposed", at=None) if undo else _rm_set(rid, status="dropped", at=store.iso(now))
        return {"product": product, "row": row}


def approve_roadmap(product, at, now=None):
    """Approve all: every proposed row of a product, in order, stopping at the first roadmap error."""
    now = now or datetime.now(timezone.utc)
    done = []
    with store.lock(RM_LOCK):
        st = state()
        _rm_check(st, product, at)
        rows = _rm_prop_rows(st, product)
        todo = [r for r in rows if r.get("status") == "proposed"]
        if not todo:
            raise FactoryError(f"{product} has no proposed roadmap rows")
        for r in todo:
            try:
                done.append(_rm_approve(product, rows, r, now))
            except RoadmapWriteError as e:
                raise RoadmapWriteError(f"{len(done)} of {len(todo)} added, then {e}", e.row) from e
    return {"product": product, "approved": len(done), "rows": done}


def proposal_at(product=None, row=None):
    """The stored proposal's at, for the CLI (which acts on what `pm roadmaps` shows now)."""
    st = state()
    if row is not None:
        product = _rm_find(st, row)[0]
    return (st["roadmaps"].get(product) or {}).get("at")


def _open_memo():
    """True while an unanswered memo item is open. An answered pm item (a memo or an unstick) is resolved here:
    no agent session owns it, so nothing else would ever move it out of the open list."""
    with store.lock():
        box, answers = store.inbox(), store.read("answers.json", {})
        mine = [it for it in box["items"] if it.get("from") in (MEMO_SENDER, UNSTICK_SENDER)]
        for it in mine:
            if it["id"] in answers:
                box["items"].remove(it)
                box["resolved"].append(dict(it, resolvedAt=store.iso(datetime.now(timezone.utc)), answer=answers[it["id"]]))
        if any(it["id"] in answers for it in mine):
            store.write("inbox.json", box)
        return any(it["id"] not in answers and it.get("from") == MEMO_SENDER for it in mine)


def memo(now=None):
    """File one inbox action item listing the Today tab's decisions. None (nothing filed) on a quiet day,
    or while an earlier memo item is still open (an open Unstick item does not hold it back). A source that could not be read is never a quiet day: the item
    names it, and so does stderr (the cron log)."""
    r = overview(now)
    failed = sorted({e.split(":", 1)[0].removesuffix(".json") for errs in r["errors"].values() for e in errs})
    if failed:
        sys.stderr.write(f"pm memo: could not read {', '.join(failed)}; the memo may be incomplete"
                         + ("; inbox.json cannot take the memo item, nothing filed\n" if "inbox" in failed else "\n"))
    if not r["errors"]["after"]:
        r["checked"]["launches"] = len(record_checks(r["launches"], now))
    if not r["errors"]["sweep"]:  # a failed sweep must not be stored as a run that swept 0 projects
        record(r)
    dec = r["decisions"]
    # _open_memo also resolves answered pm items, so it runs on a quiet day too
    if "inbox" in failed or _open_memo() or not (dec or failed):
        return None
    warn = f"Could not read: {', '.join(failed)}. The memo may be incomplete." if failed else None
    steps = ["Open Factory > Product manager > Today (1 min)"] + [f"{d['title']}: {d['text']}"[:200] for d in dec] + ([warn] if warn else [])
    title = _clip(dec[0]["title"] if len(dec) == 1 else f"{len(dec)} decisions: {dec[0]['title']}", 80) if dec else \
        _clip(f"Fix the product manager's sources: {', '.join(failed)}", 80)
    cands = [f["project"] for f in r["findings"]] + _guarded([], "projects", lambda: [p["id"] for p in store.projects() if not p.get("closedAt")], [])
    anchor = next((pid for pid in cands if not store.why_hidden(store.find_project(pid))), cands[0] if cands else None)
    if anchor is None:  # an inbox item needs a project to sit on
        return None
    item = store.add_ask_item(anchor, "action", title, "Product manager memo",
                              "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)), [["done", "Done"], ["blocked", "Blocked"]],
                              None, f"Checked {r['checked']['projects']} projects, {r['checked']['prototypes']} prototypes "
                                    f"and {_n(len(r['signals']), 'signal', 'signals')}.",
                              MEMO_SENDER, None)
    if not r["errors"]["sweep"]:  # mark the run this memo recorded, never an older one
        with store.lock():
            st = state()
            if st["runs"]:
                st["runs"][-1]["filed"] = item["id"]
                store.write(PM_REL, st)
    return item
