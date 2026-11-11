"""factory: the agents' only write path to the dashboard. See README.md."""
import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone

from . import assign, autopilot, comms, config, office, relay, release, roadmap, store, t3
from .comms import CommsValidationError
from .store import FactoryError, check_id, lock, now_iso

KINDS = ("build", "loop", "run")
HEALTH = {"on-track": "onTrack", "at-risk": "atRisk", "off-track": "offTrack"}
LEVER_KINDS = ("urgent", "applied", "action")
STATES = ("running", "waiting", "failed", "quota", "done")
HC = re.compile(r"^[A-Za-z0-9]{2,8}$")


def day(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise FactoryError(f"bad date {s!r}: use YYYY-MM-DD")


def count(s):
    if not re.fullmatch(r"[0-9]+", str(s)):
        raise FactoryError(f"bad count {s!r}: use a whole number >= 0")
    return int(s)


def number(s):
    try:
        v = float(s)
    except ValueError:
        v = math.nan
    if not math.isfinite(v):
        raise FactoryError(f"bad number {s!r}")
    return v


def when(s):
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise FactoryError(f"bad time {s!r}: use ISO 8601, e.g. 2026-10-28T09:00:00Z")
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def text(s, what):
    if not (s or "").strip():
        raise FactoryError(f"{what} is empty")
    return s


def slug(s):
    """'Pay link' -> 'pay-link'; the prototype id is the slug of product + feature."""
    out = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return out or "x"


def project_of_kind(pid, kind):
    p = store.find_project(pid)
    if p["kind"] != kind:
        raise FactoryError(f"project {pid!r} is a {p['kind']} project, not {kind}")
    return p


# ---------- products ----------

def product_set(a):
    check_id(a.key, "product key")
    with lock():
        ps = store.products()
        cur = ps.get(a.key, {"name": a.key, "workspaces": [], "repos": [], "run_labels": ["bug", "small-feature"]})
        if a.name:
            cur["name"] = a.name
        for field, val in (("workspaces", a.workspace), ("repos", a.repo), ("run_labels", a.run_label)):
            if val:
                cur[field] = val
        ps[a.key] = cur
        store.write("products.json", ps)
    return {a.key: cur}, f"ok: product {a.key}"


def product_list(a):
    ps = store.products()
    return ps, "\n".join(f"{k}\t{v['name']}\t{', '.join(v.get('repos', []))}" for k, v in ps.items()) or "no products"


# ---------- projects ----------

def project_add(a):
    check_id(a.id, "project id")
    if a.product not in store.products():
        raise FactoryError(f"no product {a.product!r} (add it: factory product set {a.product} --name ...)")
    with lock():
        if any(p["id"] == a.id for p in store.projects()):
            raise FactoryError(f"project {a.id!r} already exists")
        p = {"id": a.id, "product": a.product, "kind": a.kind, "name": a.name, "feat": a.feat or "",
             "prio": a.prio, "next": [], "prs": [], "match": [], "createdAt": now_iso()}
        if a.kind == "build":
            p.update(phases=[], scopes=[], added=0, history={})
        elif a.kind == "loop":
            p.update(metric="", unit="", baseline=None, target=None, exps=[], running=None, lastActive=None)
        store.save_project(p)
    return p, f"ok: project {a.id} ({a.kind}, {a.product})"


def pr_keys(p, refs):
    """PR references as owner/repo#N keys, a bare number in the product's first repo; duplicates dropped."""
    from .sources import pr_key
    repo = (store.products()[p["product"]].get("repos") or [None])[0]
    return list(dict.fromkeys(pr_key(x, repo) for x in refs if str(x).strip()))


def phase_links(p, refs):
    """`--pr REF[:UNITS]` values as [key, units] pairs (units default 1); '' alone unlinks."""
    out = {}
    for r in refs:
        r = str(r).strip()
        if not r:
            continue
        ref, _, units = r.partition(":")
        out[pr_keys(p, [ref])[0]] = count(units) if units else 1
    return [[k, u] for k, u in out.items()]


def project_set(a):
    with lock():
        p = store.find_project(a.id)
        for field in ("name", "feat", "prio", "source"):
            if getattr(a, field) is not None:
                p[field] = getattr(a, field)
        if a.next is not None:
            p["next"] = a.next
        if a.pr is not None:
            p["prs"] = pr_keys(p, a.pr)
        if a.add_pr:
            p["prs"] = list(dict.fromkeys(p["prs"] + pr_keys(p, a.add_pr)))
        if a.match is not None:
            p["match"] = a.match
        store.save_project(p)
    return p, f"ok: project {a.id}"


def project_list(a):
    ps = store.projects()
    return ps, "\n".join(f"{p['id']}\t{p['product']}\t{p['kind']}\t{p['name']}" for p in ps) or "no projects"


# ---------- build plans ----------

def phase_of(p, name):
    for ph in p["phases"]:
        if ph["name"] == name:
            return ph
    raise FactoryError(f"no phase {name!r} in {p['id']} (phases: {', '.join(x['name'] for x in p['phases']) or 'none'})")


def phase_name(s):
    """A phase name `plan scope` can name: it strips names and splits on commas."""
    s = text(s, "phase name").strip()
    if "," in s:
        raise FactoryError(f"phase name {s!r} has a comma, which `plan scope` reads as a separator")
    return s


snapshot = store.snapshot


def plan_phase(a):
    a.name = phase_name(a.name)
    start, due = day(a.start), day(a.due)
    if due < start:
        raise FactoryError("--due is before --start")
    with lock():
        p = project_of_kind(a.project, "build")
        ph = next((x for x in p["phases"] if x["name"] == a.name), None)
        if ph is None:
            ph = {"name": a.name, "merged": 0, "inPR": 0, "blocked": 0}
            p["phases"].append(ph)
        total = count(a.total)
        if a.keep_merged and not (a.pr and not ph.get("prs")):
            raise FactoryError("--keep-merged goes with the first --pr of a phase that has no links yet")
        if a.pr is not None:
            links = phase_links(p, a.pr)
            # --keep-merged: the units merged by hand before the first link stay merged, the PRs add to them
            base = (ph["merged"] if a.keep_merged else ph.get("handMerged", 0)) if links else 0
            if new := [k for k, _ in links if k not in {x for x, _ in ph.get("prs") or []}] if base else []:
                # a merged PR's units are already in the kept hand count: linking it would count them twice.
                # ponytail: gh read under the state lock (up to its 120 s timeout); move it before the lock if that bites
                from . import sources
                unread = []
                rows = sources.prs_for(new, unread)
                if unread or (done := [k for k in rows if rows[k][2] == "Merged"]):
                    raise FactoryError("; ".join(unread) or f"{', '.join(done)} already merged: its units would count twice, once in the "
                                       f"{base} kept merged by hand. Link it without --keep-merged, giving it its units "
                                       f"(--pr '' first if the phase already keeps them)")
            carried, counted = sum(u for _, u in links) + base, ph["merged"] + ph["inPR"]
            if links and carried < counted:
                raise FactoryError(f"these PRs carry {carried - base} units but {a.name} already counts {counted} merged or in PR: "
                                   f"the build would drop them. Give each PR its units (--pr N:UNITS), and link the "
                                   f"earlier PRs too" + ("" if ph.get("prs") else f", or keep the {ph['merged']} merged by hand with --keep-merged"))
            ph["prs"] = links
            if base:
                ph["handMerged"] = base
            else:
                ph.pop("handMerged", None)
        if ph.get("prs"):
            linked = sum(u for _, u in ph["prs"])
            if linked + ph.get("handMerged", 0) + ph["blocked"] > total:
                raise FactoryError(f"--total {total} is less than the units its PRs carry ({linked}) plus kept merged "
                                   f"({ph.get('handMerged', 0)}) plus blocked ({ph['blocked']})")
        elif ph["merged"] + ph["inPR"] + ph["blocked"] > total:
            raise FactoryError(f"--total {total} is less than the units already counted in {a.name} "
                               f"({ph['merged']} merged, {ph['inPR']} in PR, {ph['blocked']} blocked)")
        ph.update(total=total, start=start, due=due)
        p["phases"].sort(key=lambda x: (x["start"], x["due"]))
        snapshot(p)
        store.save_project(p)
    return ph, f"ok: {a.project} phase {a.name}"


def plan_rename(a):
    with lock():
        p = project_of_kind(a.project, "build")
        ph = phase_of(p, a.old)
        a.new = phase_name(a.new)
        if a.old == a.new:
            raise FactoryError(f"new phase name is the same as old: {a.new!r}")
        if any(x["name"] == a.new for x in p["phases"]):
            raise FactoryError(f"phase {a.new!r} already exists in {p['id']}")
        ph["name"] = a.new
        for s in p["scopes"]:
            s[1] = [a.new if name == a.old else name for name in s[1]]
        store.save_project(p)
    return ph, f"ok: {a.project} renamed phase {a.old} to {a.new}"


def plan_remove(a):
    with lock():
        p = project_of_kind(a.project, "build")
        ph = phase_of(p, a.phase)
        named_in = [s[0] for s in p["scopes"] if a.phase in s[1]]
        if not a.force:
            if ph["merged"]:
                raise FactoryError(f"phase {a.phase!r} has {ph['merged']} merged units: removing it lowers today's "
                                   f"merged count on the project graph (use --force)")
            if named_in:
                raise FactoryError(f"phase {a.phase!r} is named in scopes: {', '.join(map(repr, named_in))} (use --force)")
        p["phases"].remove(ph)
        for s in p["scopes"]:
            if s[0] in named_in:
                s[1] = [x for x in s[1] if x != a.phase]
        # a scope left holding 0 units is one `plan scope` refuses: drop it
        p["scopes"] = [s for s in p["scopes"] if s[0] not in named_in or sum(phase_of(p, n)["total"] for n in s[1])]
        snapshot(p)
        store.save_project(p)
    return ph, f"ok: {a.project} removed phase {a.phase}"


def plan_units(a):
    with lock():
        p = project_of_kind(a.project, "build")
        ph = phase_of(p, a.phase)
        if ph.get("prs") and (a.merged is not None or a.in_pr is not None):
            raise FactoryError(f"counts of {a.phase} come from its PRs ({', '.join(k for k, _ in ph['prs'])}): the build sets them; "
                               f"only --blocked is set by hand (unlink with factory plan phase ... --pr '')")
        new = {k: count(v) if v is not None else ph[k] for k, v in (("merged", a.merged), ("inPR", a.in_pr), ("blocked", a.blocked))}
        if ph.get("prs") and sum(u for _, u in ph["prs"]) + ph.get("handMerged", 0) + new["blocked"] > ph["total"]:
            raise FactoryError(f"blocked {new['blocked']} + the units its PRs carry ({sum(u for _, u in ph['prs'])})"
                               f" + kept merged ({ph.get('handMerged', 0)}) is more than the phase total {ph['total']}")
        if not ph.get("prs") and sum(new.values()) > ph["total"]:
            raise FactoryError(f"merged + in PR + blocked = {sum(new.values())} is more than the phase total {ph['total']}")
        ph.update(new)
        snapshot(p)
        store.save_project(p)
    return ph, f"ok: {a.project} {a.phase}: {ph['merged']} merged, {ph['inPR']} in PR, {ph['blocked']} blocked of {ph['total']}"


def plan_scope(a):
    names = [n.strip() for n in a.phases.split(",") if n.strip()]
    if not names:
        raise FactoryError("give at least one phase name")
    with lock():
        p = project_of_kind(a.project, "build")
        if not sum(phase_of(p, n)["total"] for n in names):
            raise FactoryError(f"scope {a.scope!r} holds 0 units: give its phases a --total first")
        p["scopes"] = [s for s in p["scopes"] if s[0] != a.scope] + [[a.scope, names]]
        store.save_project(p)
    return p["scopes"], f"ok: scope {a.scope!r} = {', '.join(names)}"


def plan_added(a):
    with lock():
        p = project_of_kind(a.project, "build")
        p["added"] = count(a.n)
        store.save_project(p)
    return {"added": p["added"]}, f"ok: {a.project} scope added since start = {p['added']}"


# ---------- loops ----------

def loop_meta(a):
    with lock():
        p = project_of_kind(a.project, "loop")
        for field in ("metric", "unit"):
            if getattr(a, field) is not None:
                p[field] = getattr(a, field)
        if a.target is not None:
            p["target"] = number(a.target)
        if a.baseline is not None:
            p["baseline"] = number(a.baseline)
            row = ["Baseline", p["baseline"], 1]
            p["exps"] = [row] + p["exps"][1:] if p["exps"] else [row]
        if p["baseline"] is not None and p["target"] is not None and p["target"] <= p["baseline"]:
            raise FactoryError("the target must be above the baseline: the page only draws metrics where higher "
                               "is better (for a lower-is-better metric, record its inverse or the saving)")
        store.save_project(p)
    return p, f"ok: {a.project} metric {p['metric']!r}, baseline {p['baseline']}, target {p['target']}"


def loop_start(a):
    with lock():
        p = project_of_kind(a.project, "loop")
        p["running"], p["lastActive"] = a.change, now_iso()
        store.save_project(p)
    return p, f"ok: {a.project} running {a.change!r}"


def loop_record(a):
    with lock():
        p = project_of_kind(a.project, "loop")
        if not p["exps"]:
            raise FactoryError(f"set the baseline first: factory loop meta {a.project} --baseline X")
        p["exps"].append([a.change, number(a.value), 1 if a.kept else 0, now_iso()])
        if p["running"] == a.change:
            p["running"] = None
        p["lastActive"] = now_iso()
        store.save_project(p)
    return p["exps"][-1], f"ok: {a.project} #{len(p['exps']) - 1} {a.change!r} {'kept' if a.kept else 'discarded'}"


# ---------- updates ----------

def update(a):
    with lock():
        p = store.find_project(a.project)
        row = {"at": now_iso(), "agent": text(a.agent, "--agent"), "health": HEALTH[a.health], "text": text(a.text, "the update text")}
        store.append_line(f"updates/{p['id']}.jsonl", row)
        if p["kind"] == "loop":
            p["lastActive"] = row["at"]
            store.save_project(p)
    return row, f"ok: update on {a.project}"


# ---------- inbox ----------

def parse_option(s):
    k, sep, label = s.partition("=")
    if not sep or not re.match(r"^[a-z0-9]{1,12}$", k) or not label.strip():
        raise FactoryError(f"bad --option {s!r}: use a=\"label\"")
    return [k, label.strip()]


def ask(a):
    p = store.find_project(a.project)
    if hidden := store.why_hidden(p):
        raise FactoryError(f"project {a.project!r} is not on the page yet ({hidden}): "
                           f"{config.owner()} would never see the question")
    text(a.title, "--title")
    if a.sender and not HC.match(a.sender):
        raise FactoryError(f"bad --from {a.sender!r}: an agent handle, 2 to 8 letters or digits")
    if a.sender == "scan":
        raise FactoryError("--from scan is reserved for `factory chatscan`")
    opts = [parse_option(o) for o in a.option or []]
    if a.kind == "action" and not opts:
        opts = [["done", "Done"], ["blocked", "Blocked"]]
    if len(opts) < 2:
        raise FactoryError("give at least two --option values")
    if len({k for k, _ in opts}) != len(opts):
        raise FactoryError("two options share a key")
    if a.reco and a.reco not in {k for k, _ in opts}:
        raise FactoryError(f"--reco {a.reco!r} is not an option key")
    if a.kind == "applied" and not a.reco:
        raise FactoryError("an applied item needs --reco (the option the agent applied)")
    thread, warning = a.thread, None
    try:
        thread = t3.whoami(store.products(), store.projects(), a.thread).get("thread")
    except Exception as e:
        if a.thread:
            raise
        warning = f"cannot record T3 thread: {e}"
    item = store.add_ask_item(a.project, a.kind, a.title,
                              a.short or {"urgent": "Decision", "applied": "Applied by default", "action": "Action for you"}[a.kind],
                              a.context or "", opts, a.reco, a.why or "", a.sender, thread)
    out_text = item["id"]
    if warning:
        out_text += f"\nwarning: {warning}"
    return item, out_text


def add_item(item):
    """Insert an item with a given id (imports); returns False when the id exists."""
    with lock():
        box = store.inbox()
        if any(x["id"] == item["id"] for x in box["items"] + box["resolved"]):
            return False
        box["items"].append(item)
        store.write("inbox.json", box)
    return True


def answered(since=None, ids=None, sender=None):
    box = store.read("inbox.json", {"items": []})
    ans = store.read("answers.json", {})
    out = []
    for it in box["items"]:
        if ids and it["id"] not in ids:
            continue
        if sender and it.get("from") != sender:
            continue
        a = ans.get(it["id"])
        if a and (not since or when(a["at"]) > since):
            label = next((t for k, t in it["options"] if k == a["choice"]), a["choice"])
            out.append({"id": it["id"], "project": it["project"], "title": it["title"], "choice": a["choice"],
                        "choiceLabel": label, "note": a.get("note", ""), "at": a["at"], "from": it.get("from")})
    return sorted(out, key=lambda x: x["at"])


def answers(a):
    since = when(a.since) if a.since else None
    if a.wait is not None and not (math.isfinite(a.wait) and 0 <= a.wait <= 86400):
        raise FactoryError("--wait must be between 0 and 86400 seconds")
    if a.id:
        box = store.read("inbox.json", {"items": []})
        open_ids = {x["id"] for x in box["items"]}
        resolved = {x["id"] for x in box.get("resolved", [])}
        for i in a.id:
            if i in resolved:
                raise FactoryError(f"item {i!r} is already resolved")
            if i not in open_ids:
                raise FactoryError(f"no open inbox item {i!r}")
    deadline = time.time() + (a.wait or 0)
    while True:
        out = answered(since, ids=a.id, sender=a.sender)
        if out or time.time() >= deadline:
            break
        time.sleep(min(2, max(0.1, deadline - time.time())))
    text = "\n".join(f"{x['id']}\t{x['choice']}\t{x['choiceLabel']}\t{x['note']}" for x in out) or "no answers"
    return out, text


def resolve(a):
    warning = None
    with lock():
        box = store.inbox()
        item = next((x for x in box["items"] if x["id"] == a.id), None)
        if not item:
            raise FactoryError(f"no open inbox item {a.id!r}")
        box["items"].remove(item)
        item["resolvedAt"] = now_iso()
        item["answer"] = store.read("answers.json", {}).get(a.id)
        box["resolved"].append(item)
        store.write("inbox.json", box)
        try:
            relay_items = store.read("relay.json", [])
            changed = False
            for r in relay_items:
                r_item_id = relay._extract_item_id(r)
                if r_item_id == a.id and r.get("state") in ("queued", "sending"):
                    r["state"] = "stale"
                    r["staleAt"] = now_iso()
                    changed = True
            if changed:
                store.write("relay.json", relay_items)
        except Exception as e:
            warning = f"cannot update relay outbox: {e}"
            sys.stderr.write(f"warning: {warning}\n")
    out_text = f"ok: resolved {a.id}"
    if warning:
        out_text += f"\nwarning: {warning}"
    return item, out_text


# ---------- agents outside T3 ----------

def agent_set(a):
    if not HC.match(a.hc):
        raise FactoryError(f"bad agent handle {a.hc!r}: 2 to 8 letters or digits")
    with lock():
        extra = store.read("agents-extra.json", {})
        cur = extra.get(a.hc, {"hc": a.hc, "since": now_iso()})
        if a.project:
            cur["project"], cur["product"] = a.project, store.find_project(a.project)["product"]
        elif a.product:
            if a.product not in store.products():
                raise FactoryError(f"no product {a.product!r}")
            cur["project"], cur["product"] = None, a.product
        if "product" not in cur:
            raise FactoryError("a new agent needs --project or --product")
        for field in ("who", "provider", "model", "task", "via"):
            if getattr(a, field) is not None:
                cur[field] = getattr(a, field)
        if a.state and a.state != cur.get("state"):
            cur["state"], cur["since"] = a.state, now_iso()
        cur.setdefault("state", "running")
        cur["updatedAt"] = now_iso()
        extra[a.hc] = cur
        store.write("agents-extra.json", extra)
    return cur, f"ok: agent {a.hc} {cur['state']}"


def agent_assign(a):
    """Link a T3 thread to a project (thread-projects.json): beats the title match for it and its delegates."""
    if not a.clear:
        store.find_project(a.project)  # --clear must work after the project was removed
    with lock():
        if a.clear and store.thread_links().get(a.thread) != a.project:
            raise FactoryError(f"thread {a.thread!r} is not linked to {a.project!r}")
        store.set_thread_link(a.thread, None if a.clear else a.project)
        if not a.clear:
            assign.clear_pending(a.project)  # answers the page's "starting" row
    return ({"thread": a.thread, "project": None if a.clear else a.project},
            f"ok: thread {a.thread} " + ("unlinked" if a.clear else f"assigned to {a.project}"))


def office_install(a):
    from . import office
    if getattr(a, "public", False) is True:
        res = office.install_public(a.file)
        return res, f"ok: installed public page {res['path']} ({res['size']} bytes)"
    res = office.install(a.file)
    return res, f"ok: installed {res['path']} ({res['size']} bytes); run factory build to enable the Office button"


def office_status(a):
    from . import office
    res = office.status()
    if res["installed"]:
        return res, f"installed: {res['size']} bytes ({office.size_mb(res['size'])} MB), modified {res['mtime']}"
    return res, "not installed"


def office_serve_public(a):
    from . import public_serve
    public_serve.serve(host=a.host, port=a.port)
    return None, ""


# ---------- machines ----------

MACHINE_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")


def _machine_push_age(ts_str, now=None):
    if not ts_str:
        return "never"
    try:
        dt = t3.ts(ts_str)
        if not dt:
            return "never"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        sec = max(0, int((now - dt).total_seconds()))
        if sec < 60:
            return f"{sec}s"
        if sec < 3600:
            return f"{sec // 60}m"
        if sec < 86400:
            return f"{sec // 3600}h"
        return f"{sec // 86400}d"
    except Exception:
        return "never"


def machine_add(a):
    if not MACHINE_ID_RE.fullmatch(a.id):
        raise FactoryError(f"bad machine id {a.id!r}: use a-z, 0-9 and '-', 1 to 32 characters")
    from . import office
    import secrets as secrets_py
    import hashlib

    loc_id = office.machine_info()["id"]
    if a.id in ("vps", loc_id):
        raise FactoryError(f"cannot add local machine {a.id!r}")

    machines = office.machines_doc(strict=True)
    is_rotate = getattr(a, "rotate", False)
    if a.id in machines and not is_rotate:
        raise FactoryError(f"machine {a.id!r} already exists (use --rotate to generate a new token)")
    if a.id not in machines and not getattr(a, "label", None):
        raise FactoryError("--label is required")

    label = a.label or machines.get(a.id, {}).get("label") or a.id
    token = secrets_py.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    with lock():
        machines = office.machines_doc(strict=True)
        if a.id in machines and not is_rotate:
            raise FactoryError(f"machine {a.id!r} already exists (use --rotate to generate a new token)")
        cur = machines.get(a.id, {})
        cur["label"] = label
        cur["tokenHash"] = token_hash
        cur.setdefault("createdAt", store.now_iso())
        cur.setdefault("lastPushAt", None)
        cur.setdefault("agents", [])
        machines[a.id] = cur
        store.write("machines.json", machines)

    return {"id": a.id, "label": label, "token": token}, token


def machine_list(a):
    machines = office.machines_doc(strict=True)
    if not machines:
        return {}, "no machines"
    lines = []
    sanitized = {}
    for mid, m in sorted(machines.items()):
        age = _machine_push_age(m.get("lastPushAt"))
        cnt = len(m["agents"]) if isinstance(m.get("agents"), list) else 0
        lines.append(f"{mid}\t{m.get('label', '')}\t{age}\t{cnt}")
        sanitized[mid] = {k: v for k, v in m.items() if k != "tokenHash"}
    return sanitized, "\n".join(lines)


def machine_remove(a):
    if not MACHINE_ID_RE.fullmatch(a.id):
        raise FactoryError(f"bad machine id {a.id!r}: use a-z, 0-9 and '-', 1 to 32 characters")
    with lock():
        machines = office.machines_doc(strict=True)
        if a.id not in machines:
            raise FactoryError(f"no machine {a.id!r} (see: factory machine list)")
        del machines[a.id]
        store.write("machines.json", machines)
    return {"removed": a.id}, f"ok: removed {a.id}"


# ---------- collector ----------

def collect_push(a):
    from . import secrets as secrets_mod
    import urllib.parse

    push_token = secrets_mod.get_secret("machine-push-token")

    def _opt_secret(name):
        try:
            return secrets_mod.get_secret(name)
        except Exception:
            return None

    cf_id = _opt_secret("access-client-id")
    cf_secret = _opt_secret("access-client-secret")

    products = store.products()
    projects = store.projects()
    rows, _ = t3.agents(products, projects)
    if len(rows) > 300:
        rows = rows[:300]

    base = a.url.rstrip("/")
    if base.endswith("/agents"):
        target_url = base
    else:
        target_url = f"{base}/api/machines/{a.machine}/agents"

    key = getattr(a, "key", None) or os.environ.get("FACTORY_KEY")
    if key:
        sep = "&" if "?" in target_url else "?"
        target_url += f"{sep}k={key}"

    parsed = urllib.parse.urlsplit(target_url)
    safe_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

    payload = {"agents": rows, "sentAt": store.now_iso()}
    headers = {"X-Factory-Machine-Token": push_token}
    if cf_id and cf_secret:
        headers.update({"CF-Access-Client-Id": cf_id, "CF-Access-Client-Secret": cf_secret})
    from .sweep import _request
    try:
        status, _, raw = _request("POST", target_url, headers=headers, body=payload, timeout=20)
    except FactoryError as e:
        # _request's message names the full URL (with ?k=): keep only its tail, e.g. "HTTP 403"
        raise FactoryError(f"push to {safe_url} failed ({str(e).rsplit('-> ', 1)[-1].replace(target_url, safe_url)})")
    if status != 200:
        raise FactoryError(f"push to {safe_url} failed (HTTP {status})")
    data = json.loads(raw.decode("utf-8")) if raw else {}
    return data, f"ok: pushed {len(rows)} agent(s) to {safe_url}"



# ---------- import, build, doctor, serve ----------

def import_ds_notes(a):
    from .sources import ds_notes_items
    added = [it["id"] for it in ds_notes_items(a.project, a.notes, a.answered) if add_item(it)]
    return {"added": added}, f"ok: imported {len(added)} item(s)" + (f": {', '.join(added)}" if added else "")


def build(a):
    from .build import build as run
    out, data = run()
    head = f"ok: wrote {out}" if data["written"] else "skipped: a newer build already wrote the page"
    return {"path": out, "written": data["written"], "builtAt": data["builtAt"], "projects": len(data["projects"]),
            "agents": len(data["agents"]), "warnings": data["warnings"]}, head + "".join(f"\nwarning: {w}" for w in data["warnings"])


def doctor(a):
    from .sources import doctor_checks
    from . import digest as digest_mod
    checks = doctor_checks()
    checks.extend(digest_mod.doctor_checks())
    text = "\n".join(f"{'ok  ' if ok else 'FAIL'} {name}: {msg}" for name, ok, msg in checks)
    res = [{"check": n, "ok": ok, "detail": m} for n, ok, m in checks]
    if not all(ok for _, ok, _ in checks):
        if a.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            sys.exit(1)
        raise FactoryError(text.replace("\n", " | "))
    return res, text


def serve(a):
    from .serve import run
    run(a.port)
    return None, ""


# ---------- config ----------

_MISSING = object()


def config_get(a):
    cur = config.get(a.key, _MISSING)
    if cur is _MISSING:
        raise FactoryError(f"config key {a.key!r} not set")
    text = cur if isinstance(cur, str) else json.dumps(cur, ensure_ascii=False, indent=2)
    return cur, text


def config_set(a):
    val = config.set_value(a.key, a.value, as_json=a.as_json)
    text = f"ok: {a.key}={val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)}"
    return {"key": a.key, "value": val}, text


def config_list(a):
    cfg = config.load()
    flat = config.flatten(cfg)
    text = "\n".join(f"{k}={v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)}" for k, v in flat) or "no config"
    return cfg, text


# ---------- key ----------

def key_rotate(a):
    store.rotate_key()
    return {"rotated": True}, "ok: key rotated"


# ---------- relay ----------

def relay_set(a):
    from . import relay
    masked = relay.set_webhook(a.webhook)
    return {"webhook": masked}, f"ok: webhook set ({masked})"


def relay_clear(a):
    from . import relay
    relay.clear_webhook()
    return {"ok": True}, "ok: webhook cleared"


def relay_list(a):
    from . import relay
    relay.confirm_sent()
    items = relay.load_outbox()
    text = "\n".join(f"{x['id']}\t{x['thread']}\t{x['state']}\t{x['tries']}" for x in items) or "no relay items"
    return items, text


def relay_retry(a):
    res = relay.send_queued()
    if not res["webhook"]:
        return res, "not configured"
    return res, f"ok: {res['sent']} sent, {res['failed']} failed, {res['queued']} queued"


# ---------- whoami ----------

def whoami(a):
    r = t3.whoami(store.products(), store.projects(), a.thread)
    return r, "\t".join([r["handle"], r["thread"], r["title"]] + ([r["project"]] if r["project"] else []))


# ---------- prototypes ----------

def proto_add(a):
    text(a.product, "--product")
    text(a.feature, "--feature")
    if a.product not in store.products():
        raise FactoryError(f"no product {a.product!r} (add it: factory product set {a.product} --name ...)")
    if not re.search(r"[a-z0-9]", a.feature.lower()):
        raise FactoryError("--feature needs at least one latin letter or digit (it names the prototype id)")
    path = os.path.expanduser(a.file)
    if not os.path.isfile(path):
        raise FactoryError(f"no such file {path!r}")
    if not path.lower().endswith(".html"):
        raise FactoryError(f"{path!r} is not a .html file")
    pid = slug(f"{a.product} {a.feature}")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        raise FactoryError(f"{path!r} is not UTF-8 text: save it as UTF-8 and add it again")
    with lock():
        protos = store.protos()
        proto = store.find_proto(protos, pid)
        if proto and (proto.get("product"), proto.get("feature")) != (a.product, a.feature):
            raise FactoryError(f"id {pid!r} is already {proto.get('product')} / {proto.get('feature')!r}: "
                               f"reuse that exact --product and --feature for a new version, or rename the feature")
        if proto is None:
            proto = {"id": pid, "product": a.product, "feature": a.feature,
                     "project": a.project or None, "versions": []}
            protos.append(proto)
        n = max((v.get("v", 0) for v in proto["versions"]), default=0) + 1
        for v in proto["versions"]:
            if v.get("status") in ("waiting", "sent-back"):
                v["status"] = "superseded"
        version = {"v": n, "status": "waiting", "date": now_iso(), "agent": a.agent or None,
                   "thread": a.thread or None, "file": f"protos/{pid}/v{n}.html",
                   "note": a.note or "", "look_at": a.look_at or [],
                   "decisions": None, "sent_back_reason": None, "pr": None}
        proto["versions"].insert(0, version)
        store.write_text(os.path.join("protos", pid, f"v{n}.html"), content)
        store.save_protos(protos)
    return {"id": pid, **version}, f"ok: proto {pid} v{n} ({a.feature})"


def proto_list(a):
    protos = store.protos()
    text = "\n".join(f"{p['id']}\t{p.get('product', '')}\t{p.get('feature', '')}\t"
                     f"v{(p.get('versions') or [{}])[0].get('v', '?')}\t"
                     f"{(p.get('versions') or [{}])[0].get('status', '?')}" for p in protos) or "no protos"
    return protos, text


def proto_show(a):
    proto = store.find_proto(store.protos(), a.id)
    if proto is None:
        raise FactoryError(f"no prototype {a.id!r} (see: factory proto list)")
    lines = [f"{proto['id']}\t{proto.get('product', '')}\t{proto.get('feature', '')}\t"
             f"{proto.get('project') or ''}"]
    for v in proto.get("versions", []):
        lines.append(f"  v{v.get('v', '?')} {v.get('status', '?')} {v.get('date', '')}")
    return proto, "\n".join(lines)


def proto_built(a):
    with lock():
        protos = store.protos()
        proto = store.find_proto(protos, a.id)
        if proto is None:
            raise FactoryError(f"no prototype {a.id!r} (see: factory proto list)")
        versions = proto.get("versions", [])
        # the newest version that got past review; an older validated one under a built one is history
        target = next((v for v in versions if v.get("status") in ("validated", "built")), versions[0])
        if target.get("status") != "validated":
            raise FactoryError(f"{a.id} v{target.get('v')} is {target.get('status')}, not validated")
        target["status"] = "built"
        target["pr"] = count(a.pr)
        store.save_protos(protos)
    return target, f"ok: {a.id} v{target['v']} built (PR #{a.pr})"



# ---------- roadmap ----------

def roadmap_list(a):
    v = roadmap.view(a.product)
    items = v["items"]

    lanes_data = {l: [] for l in roadmap.LANES}
    for it in items:
        lanes_data.setdefault(it.get("lane", "later"), []).append(it)

    lines = []
    lane_titles = {
        "now": "Now (1–2 months, being built)",
        "next": "Next (3–6 months, to decide)",
        "later": "Later (6+ months)",
        "icebox": "Icebox",
        "done": "Done (in prod)",
    }
    for lane_key in roadmap.LANES:
        its = lanes_data.get(lane_key, [])
        lines.append(f"{lane_titles[lane_key]} ({len(its)})")
        if not its:
            lines.append("  (empty)")
        for it in its:
            rank_str = f"P{it['rank']}  " if lane_key == "now" else ""
            flag_str = f" [{it['flag']}]" if it.get("flag") else ""
            done_str = f" ({it['done']})" if lane_key == "done" and it.get("done") else ""
            lines.append(f"  {rank_str}{it['id']}\t{' '.join(it['title'].split())}{flag_str}{done_str}")
            if it.get("note"):  # "what done means": the owner writes it for agents
                # every line indented, so a note can never pass for a lane header or a ranked item
                lines += ["        " + ln.replace("\t", " ") for ln in it["note"].splitlines()]
        lines.append("")

    stale = [f"{prod}: {' '.join(str(src.get('error') or src.get('warning')).split())} (last sync {src.get('syncedAt') or 'never'})"
             for prod, src in sorted(v["sources"].items())  # agents must know when they read a stale copy
             if a.product in (None, "all", prod) and (src.get("error") or src.get("warning"))]
    lines = stale + lines
    return {"items": items, "sources": v["sources"]}, "\n".join(lines).strip()


def roadmap_move(a):
    item = roadmap.move(a.id, a.lane, before=a.before)
    rank_info = f" as P{item['rank']}" if item.get("lane") == "now" else ""
    return item, f"ok: moved {item['id']} to {item['lane']}{rank_info}"


def roadmap_swap(a):
    res = roadmap.swap(a.id_in, a.id_out)
    return res, f"ok: swapped {a.id_in} in Now as P{res['in']['rank']}, {a.id_out} to Next"


def roadmap_add(a):
    item = roadmap.add(a.product, a.lane, a.title)
    return item, f"ok: added {item['id']} to {item['lane']}"


def roadmap_set(a):
    item = roadmap.update(a.id, title=a.title, due=a.due, note=a.note, project=a.project)
    return item, f"ok: updated {item['id']}"


def roadmap_sync(a):
    res = roadmap.sync(a.product)
    summary = ", ".join(f"{p}: {n} items" for p, n in res) if res else "nothing to sync"
    return {"synced": res}, f"ok: synced {summary}"

# ---------- autopilot ----------

def _ap_entry(cfg, pid):
    entry = cfg.get("projects", {}).get(pid)
    if entry is None:
        raise FactoryError(f"project {pid!r} is not on autopilot (see: factory autopilot list)")
    return entry


def _ap_every(v):
    if v is None:
        return autopilot.DEFAULT_EVERY
    err = autopilot.every_error(v)
    if err:
        raise FactoryError(err)
    return v


def _ap_max_per_day(v):
    n = count(v)
    if n < 1:
        raise FactoryError(f"bad --max-per-day {v!r}: a whole number >= 1")
    return n


def _ap_check_thread(thread, force, products, projects):
    if not thread:
        raise FactoryError("--thread is required")
    if force:
        return
    if not autopilot.drivable(thread):
        raise FactoryError(f"thread {thread!r} {autopilot.NOT_DRIVABLE}")
    if thread not in autopilot.agent_rows(products, projects):
        raise FactoryError(f"thread {thread!r} is not a live T3 thread (use --force to add it anyway)")


def _ap_check_free(cfg, thread, pid):
    if (other := autopilot.thread_taken(cfg, thread, pid)):
        raise FactoryError(f"thread {thread!r} already runs autopilot for {other!r}: one session, one goal")


def autopilot_add(a):
    store.find_project(a.project)  # the same "no project" error and fix as other commands
    goal = text(a.goal, "--goal")
    every = _ap_every(a.every)
    max_per_day = autopilot.DEFAULT_MAX_PER_DAY if a.max_per_day is None else _ap_max_per_day(a.max_per_day)
    enabled = not a.off
    _ap_check_thread(a.thread, a.force, store.products(), store.projects())
    with lock():
        cfg = store.autopilot()
        if a.project in cfg.get("projects", {}):
            raise FactoryError(f"project {a.project!r} is already on autopilot (use factory autopilot set ...)")
        _ap_check_free(cfg, a.thread, a.project)
        cfg["projects"][a.project] = {"thread": a.thread, "goal": goal, "every_hours": every,
                                      "enabled": enabled, "max_per_day": max_per_day,
                                      "goal_source": "cli", "last_nudge_at": None, "history": []}
        store.save_autopilot(cfg)
    return cfg["projects"][a.project], f"ok: autopilot {a.project} (every {every}h, {'on' if enabled else 'off'})"


def autopilot_set(a):
    with lock():
        cfg = store.autopilot()
        entry = _ap_entry(cfg, a.project)
        if a.goal is not None:
            entry["goal"] = text(a.goal, "--goal")
            entry["goal_source"] = "cli"
        if a.every is not None:
            entry["every_hours"] = _ap_every(a.every)
        if a.max_per_day is not None:
            entry["max_per_day"] = _ap_max_per_day(a.max_per_day)
        if a.thread is not None:
            _ap_check_thread(a.thread, a.force, store.products(), store.projects())
            _ap_check_free(cfg, a.thread, a.project)
            entry["thread"] = a.thread
        if a.on:
            entry["enabled"] = True
        elif a.off:
            entry["enabled"] = False
        store.save_autopilot(cfg)
    return entry, f"ok: autopilot {a.project}"


def autopilot_remove(a):
    with lock():
        cfg = store.autopilot()
        _ap_entry(cfg, a.project)
        del cfg["projects"][a.project]
        store.save_autopilot(cfg)
    return {"removed": a.project}, f"ok: removed {a.project} from autopilot"


def autopilot_list(a):
    cfg = store.autopilot()
    out = "\n".join(f"{pid}\t{p.get('thread', '')}\t{'on' if p.get('enabled', True) else 'off'}\t"
                    f"{p.get('every_hours', autopilot.DEFAULT_EVERY)}h\t{p.get('goal', '')}"
                    for pid, p in sorted(cfg.get("projects", {}).items())) or "no projects on autopilot"
    return cfg, out


def autopilot_pause(a):
    with lock():
        cfg = store.autopilot()
        cfg["paused"] = True
        cfg["pausedAt"] = now_iso()
        store.save_autopilot(cfg)
    return cfg, "ok: autopilot paused"


def autopilot_resume(a):
    with lock():
        cfg = store.autopilot()
        cfg["paused"] = False
        cfg["pausedAt"] = None
        store.save_autopilot(cfg)
    return cfg, "ok: autopilot resumed"


def autopilot_done(a):
    with lock():
        cfg = store.autopilot()
        entry = _ap_entry(cfg, a.project)
        entry["enabled"] = False
        autopilot._record(entry, "done", datetime.now(timezone.utc))
        store.save_autopilot(cfg)
    return entry, f"ok: autopilot done {a.project}"


def autopilot_run(a):
    summary, out = autopilot.run(dry=a.dry_run, only=a.project)
    if not a.dry_run and not summary.get("paused"):
        relay.send_queued()
    return {"paused": summary["paused"], "results": summary["results"]}, out


# ---------- board ----------

def board_list(a):
    from . import board
    data = board.collect(store.products())
    cards = [c for c in data["cards"] if not a.product or c.get("product") == a.product]
    text = "\n".join(f"{c['col']}\t{c['repo']}#{c['num']}\t{c['title']}" for c in cards) or "no factory issues"
    return {"cards": cards, "fetchedAt": data["fetchedAt"], "error": data["error"]}, text


def board_promote(a):
    from . import board
    return board.act("promote", a.repo, count(a.number)), f"ok: promoted {a.repo}#{a.number}"


def board_close(a):
    from . import board
    return board.act("close", a.repo, count(a.number)), f"ok: closed {a.repo}#{a.number}"


# ---------- secrets ----------

def secret_set(a):
    from . import secrets
    secrets.set_secret(a.name)
    return {"name": a.name}, f"ok: secret {a.name} set"


def secret_list(a):
    from . import secrets
    names = secrets.list_secrets()
    return {"secrets": names}, "\n".join(names) or "no secrets"


# ---------- sweep ----------

def sweep_map(a):
    from . import board, sweep as sweep_mod
    valid = set(board._repo_product(store.products()))
    if a.repo not in valid:
        raise FactoryError(f"{a.repo!r} is not a product repo (add it: factory product set ... --repo ...)")
    with lock():
        cfg = sweep_mod.sweep()
        if a.source == "sentry":
            cfg.setdefault("sentry", {}).setdefault("projects", {})[a.name] = a.repo
        else:
            cfg.setdefault("signoz", {}).setdefault("services", {})[a.name] = a.repo
        sweep_mod.save_sweep(cfg)
    return cfg, f"ok: sweep maps {a.source} {a.name} -> {a.repo}"


def sweep_config_set(a):
    from . import sweep as sweep_mod
    allowed = ("sentry.base_url", "sentry.org", "signoz.base_url", "signoz.default_repo",
               "min_events", "max_per_repo", "include_performance", "ignore")
    if a.key not in allowed:
        raise FactoryError(f"unknown sweep config key {a.key!r} (one of {', '.join(allowed)})")
    if a.key in ("min_events", "max_per_repo"):
        val = count(a.value)
        if val < 1:
            raise FactoryError(f"bad {a.key} {a.value!r}: a whole number >= 1")
    elif a.key == "include_performance":
        low = a.value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            val = True
        elif low in ("false", "0", "no", "off"):
            val = False
        else:
            raise FactoryError(f"bad include_performance {a.value!r}: use true or false")
    elif a.key in ("sentry.base_url", "signoz.base_url"):
        if not (a.value.startswith("http://") or a.value.startswith("https://")):
            raise FactoryError(f"bad {a.key} {a.value!r}: must start with http:// or https://")
        val = a.value.rstrip("/")
    elif a.key == "ignore":  # comma-separated substrings; "" clears the list
        val = [x.strip() for x in a.value.split(",") if x.strip()]
    elif a.key == "signoz.default_repo" and a.value.strip().lower() in ("null", "none", ""):
        val = None
    else:
        val = a.value
    with lock():
        cfg = sweep_mod.sweep()
        cur = cfg
        for part in a.key.split(".")[:-1]:
            cur = cur.setdefault(part, {})
        cur[a.key.split(".")[-1]] = val
        sweep_mod.save_sweep(cfg)
    return {"key": a.key, "value": val}, f"ok: sweep {a.key}={val if val is not None else 'null'}"


def sweep_run(a):
    from . import sweep as sweep_mod
    summary, out = sweep_mod.run(source=a.source, dry=a.dry_run)
    return summary, out


def sweep_count(a):
    from . import sweep as sweep_mod
    since = os.environ.get(sweep_mod.WATCH_SINCE_ENV)
    n = sweep_mod.count(a.source, a.target, baseline=a.baseline, minutes=a.minutes,
                        since=datetime.fromisoformat(since) if since else None)
    return n, str(n)


def release_product_add(a):
    return release.product_add(a)


def release_product_set(a):
    return release.product_set(a)


def release_record(a):
    entry = release.record(
        product=a.product,
        event=a.event,
        prs=a.pr or [],
        sha=a.sha,
        note=a.note or "",
    )
    return entry, f"ok: {a.product} {a.event}"


def release_pause(a):
    product = a.product or "*"
    res = release.pause(product)
    return res, f"ok: paused {product}"


def release_resume(a):
    product = a.product or "*"
    res = release.resume(product)
    return res, f"ok: resumed {product}"


def release_unhold(a):
    res = release.unhold(a.product)
    return res, f"ok: unheld {a.product}"


def release_status(a):
    return release.status(a.product)


def release_plan(a):
    only = a.only
    dry = bool(a.dry_run)
    verdicts = release.plan(only=only, dry=dry)
    lines = []
    for p in sorted(verdicts.keys()):
        v = verdicts[p]
        v_name = v.get("verdict", "")
        reasons_str = "; ".join(v.get("reasons", []))
        lines.append(f"{p}: {v_name} ({reasons_str})")
    return verdicts, "\n".join(lines)


def release_check(a):
    res = release.check(a.product)
    if not res.get("ok"):
        a.exit_code = 3
    checks = res.get("checks", [])
    if not checks:
        text = "no checks configured"
    else:
        lines = []
        for c in checks:
            st = "ok" if c["ok"] else "FAIL"
            line = f"{st} {c['name']} ({c['kind']}): value={c['value']} baseline={c['baseline']}"
            if c.get("detail"):
                line += f" {c['detail']}"
            lines.append(line)
        text = "\n".join(lines)
    return res, text


def release_check_add(a):
    return release.check_add(
        product=a.product,
        name=a.name,
        kind=a.kind,
        url=a.url,
        cmd=a.cmd,
        baseline_cmd=a.baseline_cmd,
    )


def release_watch(a):
    res, text = release.watch(a.product, minutes=a.minutes, every=a.every)
    if res.get("result") == "rollback":
        a.exit_code = 3
    return res, text


def release_queue(a):
    from . import queue as queue_mod
    res, text = queue_mod.queue(product=a.product)
    return res, text


def release_queue_today(a):
    from . import queue as queue_mod
    res = queue_mod.today(a.product)
    lines = [f"product: {res['product']} (mode: {res['mode']})"]
    if res.get("blocked"):
        lines.append("train: BLOCKED")
    merges = res.get("merge", [])
    if merges:
        m_str = ", ".join(f"#{m['number']}" for m in merges)
        lines.append(f"merges today: {m_str}")
    else:
        lines.append("merges today: (none)")
    agents = res.get("agents", [])
    if agents:
        a_str = ", ".join(f"{ag['repo_pr'] or ag['job']} ({ag['job']})" if 'repo_pr' in ag else ag['job'] for ag in agents)
        lines.append(f"agents: {a_str}")
    else:
        lines.append("agents: (none)")
    pending = res.get("pending_actions", [])
    if pending:
        p_str = ", ".join(f"{p['repo_pr']}->{p['action']}" for p in pending)
        lines.append(f"pending actions: {p_str}")
    else:
        lines.append("pending actions: (none)")
    asks = res.get("asks", [])
    lines.append(f"asks: {len(asks)}")
    return res, "\n".join(lines)


def release_queue_recheck(a):
    from . import queue as queue_mod
    ok, reason, head_sha = queue_mod.recheck(a.repo_pr)
    res = {"ok": ok, "reason": reason, "repo_pr": a.repo_pr, "head_sha": head_sha}
    if not ok:
        a.exit_code = 3
        text = f"{reason} head {head_sha}" if head_sha else reason
    else:
        text = f"ok: {a.repo_pr} head {head_sha}" if head_sha else f"ok: {a.repo_pr}"
    return res, text


def release_queue_done(a):
    from . import queue as queue_mod
    develop_fix = getattr(a, "develop_fix", False)
    res = queue_mod.queue_done(a.repo_pr, a.what, note=a.note, develop_fix=develop_fix)
    return res, f"ok: {res['repo_pr']} {a.what}"


def release_queue_action(a):
    from . import queue as queue_mod
    res = queue_mod.record_queue_action(a.repo_pr, a.action)
    if a.action == "unhold" and not res["removed"]:
        return res, f"ok: {a.repo_pr} was not held"
    return res, f"ok: {a.repo_pr} -> {a.action}"


def chatscan(a):
    from . import chatscan as chatscan_mod
    summary, out = chatscan_mod.run(dry_run=a.dry_run, verbose=a.verbose)
    return summary, out


# ---------- comms ----------

def comms_changelog(a):
    entry, md = comms.changelog(
        a.product,
        at=a.at,
        titles=getattr(a, "titles", False),
        refresh=getattr(a, "refresh", False),
    )
    return entry, md


def comms_copy(a):
    if not os.path.exists(a.file):
        raise CommsValidationError(f"file not found: {a.file}")
    try:
        with open(a.file, encoding="utf-8") as f:
            copies = json.load(f)
    except Exception as e:
        raise CommsValidationError(f"invalid JSON in {a.file}: {e}")
    res = comms.set_copy(a.product_at, copies)
    return res, f"copy saved for {a.product_at}"


def comms_due(a):
    if a.all:
        jobs = []
        for prod in comms.comms_products():
            jobs.extend(comms.due(prod))
    elif a.product:
        jobs = comms.due(a.product)
    else:
        raise CommsValidationError("specify a product or --all")
    if not jobs:
        return jobs, "no comms jobs due"
    lines = [f"{j['id']}\t{j.get('product')}\t{j.get('kind')}\tPRs: {len(j.get('prs', []))}" for j in jobs]
    return jobs, "\n".join(lines)


def comms_draft(a):
    if not os.path.exists(a.file):
        raise CommsValidationError(f"file not found: {a.file}")
    try:
        with open(a.file, encoding="utf-8") as f:
            draft = json.load(f)
    except Exception as e:
        raise CommsValidationError(f"invalid JSON in {a.file}: {e}")
    job = comms.save_draft(a.job_id, draft)
    return job, f"draft saved for {a.job_id}: status {job['status']}"


def comms_decide(a):
    job = comms.decide(a.job_id, a.choice, ask=a.ask)
    return job, f"decided {a.job_id}: {job['status']}"


def comms_check(a):
    res = comms.check(a.job_id)
    if not res["ok"]:
        a.exit_code = 3
        return res, f"not ok: {res['reason']}"
    return res, f"ok: {a.job_id} can be sent"


def comms_sent(a):
    job = comms.sent(a.job_id, a.count)
    return job, f"marked {a.job_id} sent to {a.count} recipients"


def comms_failed(a):
    job = comms.failed(a.job_id, a.reason)
    return job, f"marked {a.job_id} failed: {a.reason}"


def comms_video_failed(a):
    job = comms.video_failed(a.job_id)
    return job, f"recorded video failure for {a.job_id}"


def comms_list(a):
    jobs = comms.list_jobs(product=getattr(a, "product", None))
    if not jobs:
        return jobs, "no comms jobs"
    lines = [f"{j['id']}\t{j.get('product')}\t{j.get('kind')}\t{j.get('status')}\t{j.get('updated', '')}" for j in jobs]
    return jobs, "\n".join(lines)


def comms_relay(a):
    res = comms.relay()
    lines = []
    has_error = False
    for prod in sorted(res.keys()):
        info = res[prod]
        if info.get("error"):
            has_error = True
            lines.append(f"{prod}: error ({info['error']})")
        elif info.get("relayed"):
            lines.append(f"{prod}: relayed {len(info['relayed'])} job(s) ({', '.join(info['relayed'])})")
        else:
            reason = info.get("skipped") or "none"
            lines.append(f"{prod}: skipped ({reason})")
    if has_error:
        a.exit_code = 1
    text = "\n".join(lines) if lines else "no comms products configured"
    return res, text


# ---------- digest ----------

def digest_config_set(a):
    from . import digest as digest_mod
    allowed = ("enabled", "chatwoot.base_url", "chatwoot.account_id", "llm.base_url", "llm.model", "llm.usd_per_mtok", "project",
               "question", "days", "max_conversations", "max_chars_per_conversation", "daily_cap_usd")
    if a.key not in allowed:
        raise FactoryError(f"unknown digest config key {a.key!r} (one of {', '.join(allowed)})")
    if a.key == "enabled":
        low = a.value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            val = True
        elif low in ("false", "0", "no", "off"):
            val = False
        else:
            raise FactoryError(f"bad enabled {a.value!r}: use true or false")
    elif a.key in ("chatwoot.base_url", "llm.base_url"):
        if not a.value.startswith(("http://", "https://")):
            raise FactoryError(f"bad {a.key} {a.value!r}: must start with http:// or https://")
        val = a.value.rstrip("/")
    elif a.key in ("chatwoot.account_id", "days", "max_conversations", "max_chars_per_conversation"):
        val = count(a.value)
        if val < 1:
            raise FactoryError(f"bad {a.key} {a.value!r}: a whole number >= 1")
    elif a.key in ("daily_cap_usd", "llm.usd_per_mtok"):
        val = number(a.value)
        if val <= 0:
            raise FactoryError(f"bad {a.key} {a.value!r}: a positive number")
    else:
        val = a.value
    with lock():
        cfg = digest_mod.digest()
        cur = cfg
        for part in a.key.split(".")[:-1]:
            cur = cur.setdefault(part, {})
        cur[a.key.split(".")[-1]] = val
        digest_mod.save_digest(cfg)
    return {"key": a.key, "value": val}, f"ok: digest {a.key}={val}"


def digest_inboxes(a):
    from . import digest as digest_mod
    ids = [count(v) for v in a.ids]
    with lock():
        cfg = digest_mod.digest()
        cfg["chatwoot"]["inboxes"] = ids
        digest_mod.save_digest(cfg)
    return {"inboxes": ids}, f"ok: digest inboxes {ids}"


def digest_run(a):
    from . import digest as digest_mod
    return digest_mod.run(dry=a.dry_run)


# ---------- fixlane ----------

def fixlane_run(a):
    from . import fixlane as fixlane_mod
    return fixlane_mod.run(dry=getattr(a, "dry_run", False))


def fixlane_collect(a):
    from . import fixlane as fixlane_mod
    return fixlane_mod.collect()


def fixlane_status(a):
    from . import fixlane as fixlane_mod
    return fixlane_mod.status()


def fixlane_retry(a):
    from . import fixlane as fixlane_mod
    return fixlane_mod.retry(a.target)


# ---------- argparse ----------

class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise FactoryError(f"{self.prog}: {message}")


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print JSON")
    top = Parser(prog="factory", parents=[common], description="Write to the factory dashboard.")
    sub = top.add_subparsers(dest="cmd", required=True, parser_class=Parser)

    def cmd(parent, name, fn, help):
        p = parent.add_parser(name, parents=[common], help=help)
        p.set_defaults(fn=fn)
        return p

    def group(name, help):
        return sub.add_parser(name, help=help).add_subparsers(dest="sub", required=True, parser_class=Parser)

    g = group("product", "products and their sources")
    p = cmd(g, "set", product_set, "add or change a product")
    p.add_argument("key")
    p.add_argument("--name")
    p.add_argument("--workspace", action="append", help="T3 workspace path (repeat)")
    p.add_argument("--repo", action="append", help="owner/repo for PRs and the run lane (repeat)")
    p.add_argument("--run-label", action="append", help="issue label counted in the run lane (repeat)")
    cmd(g, "list", product_list, "list products")

    g = group("project", "projects")
    p = cmd(g, "add", project_add, "add a project")
    p.add_argument("id")
    p.add_argument("--product", required=True)
    p.add_argument("--kind", required=True, choices=KINDS)
    p.add_argument("--name", required=True)
    p.add_argument("--feat")
    p.add_argument("--prio", type=int)
    p = cmd(g, "set", project_set, "change a project")
    p.add_argument("id")
    p.add_argument("--name")
    p.add_argument("--feat")
    p.add_argument("--prio", type=int)
    p.add_argument("--next", action="append", help="next step (repeat; replaces the list)")
    p.add_argument("--pr", action="append", help="PR number or owner/repo#N (repeat; replaces the list)")
    p.add_argument("--add-pr", action="append", help="PR number or owner/repo#N added to the list (repeat; keeps the others)")
    p.add_argument("--match", action="append", help="T3 thread title text that belongs to this project (repeat)")
    p.add_argument("--source", choices=["ds-kit", "manual"], help="where the build reads the plan")
    cmd(g, "list", project_list, "list projects")

    g = group("plan", "build plans")
    p = cmd(g, "phase", plan_phase, "add or change a phase")
    p.add_argument("project")
    p.add_argument("name")
    p.add_argument("--total", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--due", required=True)
    p.add_argument("--pr", action="append", help="PR that delivers units of this phase, as REF[:UNITS] (units default 1; repeat; "
                   "replaces the phase's list). The build sets merged and in PR from GitHub; units no PR "
                   "carries stay to do; --pr '' unlinks")
    p.add_argument("--keep-merged", action="store_true", help="with the first --pr: the units already merged by hand "
                   "stay merged and the PRs add to them")
    p = cmd(g, "rename", plan_rename, "rename a phase")
    p.add_argument("project")
    p.add_argument("old")
    p.add_argument("new")
    p = cmd(g, "remove", plan_remove, "remove a phase")
    p.add_argument("project")
    p.add_argument("phase")
    p.add_argument("--force", action="store_true", help="also when it has merged units or a scope names it: remove it from those scopes, delete any left with 0 units")
    p = cmd(g, "units", plan_units, "set a phase's unit counts")
    p.add_argument("project")
    p.add_argument("phase")
    p.add_argument("--merged")
    p.add_argument("--in-pr")
    p.add_argument("--blocked")
    p = cmd(g, "scope", plan_scope, "name a set of phases")
    p.add_argument("project")
    p.add_argument("scope")
    p.add_argument("phases", help="comma-separated phase names")
    p = cmd(g, "added", plan_added, "units added since the start")
    p.add_argument("project")
    p.add_argument("n")

    g = group("loop", "metric loops")
    p = cmd(g, "record", loop_record, "record an experiment result")
    p.add_argument("project")
    p.add_argument("change")
    p.add_argument("--value", required=True)
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--kept", action="store_true")
    m.add_argument("--discarded", action="store_true")
    p = cmd(g, "start", loop_start, "mark an experiment as running")
    p.add_argument("project")
    p.add_argument("change")
    p = cmd(g, "meta", loop_meta, "metric, unit, baseline, target")
    p.add_argument("project")
    p.add_argument("--metric")
    p.add_argument("--unit")
    p.add_argument("--baseline")
    p.add_argument("--target")

    p = cmd(sub, "update", update, "post a project update")
    p.add_argument("project")
    p.add_argument("--health", required=True, choices=list(HEALTH))
    p.add_argument("--agent", required=True)
    p.add_argument("text")

    p = cmd(sub, "ask", ask, "put a question in the owner's inbox; prints its id")
    p.add_argument("--project", required=True)
    p.add_argument("--kind", required=True, choices=LEVER_KINDS)
    p.add_argument("--title", required=True)
    p.add_argument("--short", help="one-line subtitle")
    p.add_argument("--context")
    p.add_argument("--option", action="append", help='a="label" (repeat)')
    p.add_argument("--reco")
    p.add_argument("--why")
    p.add_argument("--from", dest="sender")
    p.add_argument("--thread", help="override T3 thread id")
    p = cmd(sub, "answers", answers, "print answered, unresolved items")
    p.add_argument("--since")
    p.add_argument("--wait", type=float, help="seconds to wait for an answer")
    p.add_argument("--id", action="append", help="only this item id (repeat)")
    p.add_argument("--from", dest="sender", help="only items from this agent handle")
    p = cmd(sub, "resolve", resolve, "move an answered or applied item to the decision log")
    p.add_argument("id")

    g = group("relay", "outbox for answers pushed to T3 threads")
    p = cmd(g, "set", relay_set, "set the T3 webhook URL")
    p.add_argument("--webhook", required=True, help="webhook URL")
    cmd(g, "clear", relay_clear, "clear the webhook URL")
    cmd(g, "list", relay_list, "list outbox items")
    cmd(g, "retry", relay_retry, "send queued outbox items now")

    g = group("config", "configuration keys")
    p = cmd(g, "get", config_get, "get a config value by dotted key")
    p.add_argument("key", help="dotted key, e.g. access.team")
    p = cmd(g, "set", config_set, "set a config value")
    p.add_argument("key", help="dotted key, e.g. access.team")
    p.add_argument("value", help="stored as a string unless --as-json")
    p.add_argument("--as-json", action="store_true", help="parse the value as JSON (lists, numbers)")
    cmd(g, "list", config_list, "list all config settings")

    g = group("key", "access keys")
    cmd(g, "rotate", key_rotate, "rotate the dashboard ?k= key")

    g = group("agent", "agents not visible in T3")
    p = cmd(g, "set", agent_set, "add or change an agent")
    p.add_argument("hc")
    for f in ("who", "provider", "model", "project", "product", "task", "via"):
        p.add_argument("--" + f)
    p.add_argument("--state", choices=STATES)
    p = cmd(g, "assign", agent_assign, "link a T3 thread (and its delegates) to a project; beats the title match")
    p.add_argument("project", help="project id")
    p.add_argument("--thread", required=True, help="T3 thread id")
    p.add_argument("--clear", action="store_true", help="remove the link instead")

    g = group("import", "one-off imports")
    p = cmd(g, "ds-notes", import_ds_notes, "seed the inbox from the DS kit notes (config kit.notes)")
    p.add_argument("--project", default="ds")
    p.add_argument("--notes", help="notes JSON (default: config kit.notes)")
    p.add_argument("--answered", help="answered decisions JSON (default: config kit.answered)")

    g = group("proto", "prototypes agents produce, validated or sent back from the dashboard")
    p = cmd(g, "add", proto_add, "add a prototype version (same id = new version)")
    p.add_argument("--product", required=True, help="product key")
    p.add_argument("--feature", required=True, help="what the prototype is")
    p.add_argument("--file", required=True, help="path to the .html file to copy")
    p.add_argument("--project", help="FEAT-N it belongs to")
    p.add_argument("--agent", help="agent handle")
    p.add_argument("--thread", help="T3 thread id to message on validate/send back")
    p.add_argument("--note", help="what the agent wants the reviewer to check")
    p.add_argument("--look-at", action="append", help="one thing to look at (repeat)")
    cmd(g, "list", proto_list, "list prototypes")
    p = cmd(g, "show", proto_show, "show one prototype and its versions")
    p.add_argument("id")
    p = cmd(g, "built", proto_built, "mark the latest validated version built")
    p.add_argument("id")
    p.add_argument("--pr", required=True, help="PR number")

    g = group("roadmap", "product roadmap (Now / Next / Later / Done)")
    p = cmd(g, "list", roadmap_list, "list roadmap items")
    p.add_argument("product", nargs="?", help="product key (default: all)")
    p = cmd(g, "move", roadmap_move, "move an item to a lane or position")
    p.add_argument("id", help="item id")
    p.add_argument("lane", choices=roadmap.LANES, help="target lane")
    p.add_argument("--before", help="insert before this item id")
    p = cmd(g, "swap", roadmap_swap, "swap an item into Now and another to Next")
    p.add_argument("id_in", help="item to move into Now")
    p.add_argument("id_out", help="item to move out of Now into Next")
    p = cmd(g, "add", roadmap_add, "add a new roadmap item")
    p.add_argument("product", help="product key")
    p.add_argument("lane", choices=roadmap.LANES, help="initial lane")
    p.add_argument("title", help="item title")
    p = cmd(g, "set", roadmap_set, "update item details")
    p.add_argument("id", help="item id")
    p.add_argument("--title", help="new title")
    p.add_argument("--due", help="target launch date (YYYY-MM-DD or \x27none\x27)")
    p.add_argument("--note", help="note / context")
    p.add_argument("--project", help="linked factory project id (or \x27none\x27)")
    p = cmd(g, "sync", roadmap_sync, "sync roadmap from Notion database")
    p.add_argument("product", nargs="?", help="product key (default: all with Notion configured)")

    g = group("autopilot", "opt-in scheduled nudges that keep a project's session moving")
    p = cmd(g, "add", autopilot_add, "add a project to autopilot")
    p.add_argument("project")
    p.add_argument("--thread", required=True, help="T3 thread id to nudge")
    p.add_argument("--goal", required=True, help="done criteria (one line)")
    p.add_argument("--every", type=int, help="hours between nudges (1, 3, 6, 12, 24; default 3)")
    p.add_argument("--max-per-day", type=int, help="at most this many nudges per day (default 4)")
    p.add_argument("--off", action="store_true", help="add it disabled")
    p.add_argument("--force", action="store_true", help="add even when the thread is not a live T3 thread")
    p = cmd(g, "set", autopilot_set, "change a project's autopilot")
    p.add_argument("project")
    p.add_argument("--goal")
    p.add_argument("--every", type=int)
    p.add_argument("--max-per-day", type=int)
    p.add_argument("--thread")
    p.add_argument("--force", action="store_true")
    on_off = p.add_mutually_exclusive_group()
    on_off.add_argument("--on", action="store_true", help="enable nudges")
    on_off.add_argument("--off", action="store_true", help="disable nudges")
    p = cmd(g, "remove", autopilot_remove, "remove a project from autopilot")
    p.add_argument("project")
    cmd(g, "list", autopilot_list, "list projects on autopilot")
    cmd(g, "pause", autopilot_pause, "pause all autopilot nudges")
    cmd(g, "resume", autopilot_resume, "resume autopilot nudges")
    p = cmd(g, "done", autopilot_done, "the goal is met: disable it and record done")
    p.add_argument("project")
    p = cmd(g, "run", autopilot_run, "nudge due projects now")
    p.add_argument("--dry-run", action="store_true", help="decide without nudging")
    p.add_argument("--project", help="only this project")

    g = group("board", "the factory board across every product's repos")
    p = cmd(g, "list", board_list, "list the board's issues")
    p.add_argument("--product", help="only this product")
    p = cmd(g, "promote", board_promote, "promote an intake issue to triage (adds the factory label)")
    p.add_argument("repo")
    p.add_argument("number")
    p = cmd(g, "close", board_close, "close an issue as not planned")
    p.add_argument("repo")
    p.add_argument("number")

    g = group("secret", "secrets read from stdin, printed by name only")
    p = cmd(g, "set", secret_set, "set a secret value (read from stdin)")
    p.add_argument("name", help="e.g. sentry-token or signoz-key")
    cmd(g, "list", secret_list, "list secret names (never values)")

    g = group("sweep", "file production errors as intake issues")
    p = cmd(g, "map", sweep_map, "map an error source to a product repo")
    p.add_argument("source", choices=["sentry", "signoz"], help="sentry: a project slug; signoz: a service name")
    p.add_argument("name", help="Sentry project slug, or SigNoz service name")
    p.add_argument("repo", help="owner/repo")
    gc = g.add_parser("config", parents=[common], help="sweep configuration").add_subparsers(
        dest="sub2", required=True, parser_class=Parser)
    p = cmd(gc, "set", sweep_config_set, "set a sweep config value")
    p.add_argument("key", help="sentry.base_url / sentry.org / signoz.base_url / signoz.default_repo / min_events / max_per_repo / include_performance / ignore (comma-separated substrings)")
    p.add_argument("value")
    p = cmd(g, "run", sweep_run, "read errors and file intake issues")
    p.add_argument("--source", choices=["sentry", "signoz", "all"], default="all", help="which source to read")
    p.add_argument("--dry-run", action="store_true", help="decide without filing or recording")
    p = cmd(g, "count", sweep_count, "print errors in the last hour (a release errors check)")
    p.add_argument("source", choices=["sentry", "signoz"])
    p.add_argument("target", help="Sentry project slug, or SigNoz service name")
    p.add_argument("--minutes", type=int, default=60, help="window length (default 60); inside `release watch` the window starts no earlier than the watch")
    p.add_argument("--baseline", action="store_true", help="mean errors per window over the 24 h before the window")

    g = group("release", "daily release manager")
    gp = g.add_parser("product", parents=[common], help="product release configuration").add_subparsers(
        dest="sub2", required=True, parser_class=Parser)
    p = cmd(gp, "add", release_product_add, "register a product for daily releases")
    p.add_argument("name", help="product name")
    p.add_argument("--repo", required=True, help="owner/repo")
    p.add_argument("--flow", required=True, choices=release.ALLOWED_FLOWS, help="release flow")
    p.add_argument("--project", required=True, help="factory project id")
    p.add_argument("--base", default="develop", help="source branch (default develop)")
    p.add_argument("--head", default="main", help="prod branch (default main)")
    p.add_argument("--thread", help="release T3 thread id")
    p.add_argument("--red-path", action="extend", nargs="+", help="glob for red risk score (repeat)")
    p.add_argument("--display-name", help="name shown on the dashboard")
    p.add_argument("--color", help="hex colour, e.g. #4670CF")
    p.add_argument("--short", help="short id for queue rows")
    p.add_argument("--flow-label", help="flow text on the queue, e.g. 'develop → main · 07:30'")
    p.add_argument("--comms-langs", action="extend", nargs="+", help="comms languages, first is the default (makes it a comms product)")
    p.add_argument("--comms-audiences", action="extend", nargs="+", help="comms audiences")
    p.add_argument("--comms-internal-scopes", action="extend", nargs="+", help="commit scopes that never reach users")

    p = cmd(gp, "set", release_product_set, "update a product release configuration")
    p.add_argument("name", help="product name")
    p.add_argument("--thread", help="release T3 thread id")
    p.add_argument("--mode", choices=release.ALLOWED_MODES, help="release mode (shadow or live)")
    p.add_argument("--red-path", action="extend", nargs="+", help="glob for red risk score (repeat)")
    p.add_argument("--display-name", help="name shown on the dashboard")
    p.add_argument("--color", help="hex colour, e.g. #4670CF")
    p.add_argument("--short", help="short id for queue rows")
    p.add_argument("--flow-label", help="flow text on the queue, e.g. 'develop → main · 07:30'")
    p.add_argument("--comms-langs", action="extend", nargs="+", help="comms languages, first is the default (makes it a comms product)")
    p.add_argument("--comms-audiences", action="extend", nargs="+", help="comms audiences")
    p.add_argument("--comms-internal-scopes", action="extend", nargs="+", help="commit scopes that never reach users")

    p = cmd(g, "record", release_record, "record a release lifecycle event")
    p.add_argument("product", help="product name")
    p.add_argument("event", choices=release.VALID_EVENTS, help="event name")
    p.add_argument("--pr", type=int, action="extend", nargs="+", help="PR number(s)")
    p.add_argument("--sha", help="commit SHA")
    p.add_argument("--note", default="", help="note or issue description")

    p = cmd(g, "pause", release_pause, "pause releases for a product or globally")
    p.add_argument("product", nargs="?", default="*", help="product name (default: * for all)")

    p = cmd(g, "resume", release_resume, "resume releases for a product or globally")
    p.add_argument("product", nargs="?", default="*", help="product name (default: * for all)")

    p = cmd(g, "unhold", release_unhold, "lift a hold on a product after 24h")
    p.add_argument("product", help="product name")

    p = cmd(g, "status", release_status, "show release status")
    p.add_argument("product", nargs="?", default=None, help="product name")

    p = cmd(g, "plan", release_plan, "run the daily release plan")
    p.add_argument("--only", help="product name (default: all except factory)")
    p.add_argument("--dry-run", action="store_true", help="decide without writing verdicts or relaying")

    p = cmd(g, "check", release_check, "run health and signal checks for a product (exit 3 on failure)")
    p.add_argument("product", help="product name")

    p = cmd(g, "check-add", release_check_add, "add a health or signal check to a product")
    p.add_argument("product", help="product name")
    p.add_argument("--name", required=True, help="check name")
    p.add_argument("--kind", required=True, choices=["http", "errors", "business"], help="check kind")
    p.add_argument("--url", help="HTTP URL (for kind=http)")
    p.add_argument("--cmd", help="command to measure current value (for errors/business)")
    p.add_argument("--baseline-cmd", help="command to measure baseline value (for errors/business)")

    p = cmd(g, "watch", release_watch, "watch health and signal checks after release (exit 3 = roll back)")
    p.add_argument("product", help="product name")
    p.add_argument("--minutes", type=float, required=True, help="watch window in minutes")
    p.add_argument("--every", type=float, default=300, help="seconds between check samples (default 300)")

    p = cmd(g, "queue", release_queue, "show develop queue and train for products")
    p.add_argument("--product", help="filter by product name")

    p = cmd(g, "queue-today", release_queue_today, "show today's develop queue for a product")
    p.add_argument("product", help="product name")

    p = cmd(g, "queue-recheck", release_queue_recheck, "re-check a PR before merge (exit 3 on failure)")
    p.add_argument("repo_pr", help="owner/repo#number (e.g. acme/app#42)")

    p = cmd(g, "queue-done", release_queue_done, "record a completed queue action")
    p.add_argument("repo_pr", help="owner/repo#number (e.g. acme/app#42) or product name with --develop-fix")
    p.add_argument("what", choices=["merged", "retargeted", "closed", "agent-sent"], help="action completed")
    p.add_argument("--develop-fix", action="store_true", help="record develop-fix agent for product (repo_pr is product name)")
    p.add_argument("--note", default="", help="optional note")

    p = cmd(g, "queue-action", release_queue_action, "record an override action on a PR")
    p.add_argument("repo_pr", help="repo#number (e.g. acme/factory#50)")
    p.add_argument("action", choices=["ok", "front", "hold", "unhold", "agent", "retarget", "close"], help="action to take")

    g = group("comms", "release comms manager")
    p = cmd(g, "changelog", comms_changelog, "generate changelog for product release")
    p.add_argument("product", help="product name")
    p.add_argument("--at", help="shipped event ISO timestamp")
    p.add_argument("--titles", action="store_true", help="show PR titles instead of plain copy")
    p.add_argument("--refresh", action="store_true", help="re-fetch from GitHub and re-classify while keeping stored copy")

    p = cmd(g, "copy", comms_copy, "set plain-words copy for changelog PRs")
    p.add_argument("product_at", help="product@at (e.g. app@2026-11-10T08:00:00Z)")
    p.add_argument("--file", required=True, help="path to copies JSON file")

    p = cmd(g, "due", comms_due, "list comms jobs due")
    p.add_argument("product", nargs="?", default=None, help="product name")
    p.add_argument("--all", action="store_true", help="all products")

    p = cmd(g, "draft", comms_draft, "validate and save draft for job")
    p.add_argument("job_id", help="job id (e.g. f-app-42)")
    p.add_argument("--file", required=True, help="path to draft JSON file")

    p = cmd(g, "decide", comms_decide, "decide on an approval: send, edit, or skip")
    p.add_argument("job_id", help="job id")
    p.add_argument("choice", choices=["send", "edit", "skip"], help="send | edit | skip")
    p.add_argument("--ask", help="the inbox item id the owner answered (required for send and edit)")

    p = cmd(g, "check", comms_check, "gate right before a send (exit 3 when it must not go out)")
    p.add_argument("job_id", help="job id")

    p = cmd(g, "sent", comms_sent, "record job sent to recipient count")
    p.add_argument("job_id", help="job id")
    p.add_argument("--count", type=int, required=True, help="recipient count")

    p = cmd(g, "failed", comms_failed, "record job send failure")
    p.add_argument("job_id", help="job id")
    p.add_argument("--reason", required=True, help="failure reason")

    p = cmd(g, "video-failed", comms_video_failed, "record brag video failure for fallback")
    p.add_argument("job_id", help="job id")

    p = cmd(g, "list", comms_list, "list comms jobs")
    p.add_argument("--product", help="filter by product")

    cmd(g, "relay", comms_relay, "relay due comms jobs to product release threads")


    g = group("digest", "support digest: top customer asks from Chatwoot")
    gc = g.add_parser("config", parents=[common], help="digest configuration").add_subparsers(
        dest="sub2", required=True, parser_class=Parser)
    p = cmd(gc, "set", digest_config_set, "set a digest config value")
    p.add_argument("key", help="enabled / chatwoot.base_url / chatwoot.account_id / llm.base_url / llm.model / llm.usd_per_mtok / project / "
                   "question / days / max_conversations / max_chars_per_conversation / daily_cap_usd")
    p.add_argument("value")
    p = cmd(g, "inboxes", digest_inboxes, "set the Chatwoot inbox ids to read")
    p.add_argument("ids", nargs="+", help="inbox id (one or more)")
    p = cmd(g, "run", digest_run, "run the support digest job")
    p.add_argument("--dry-run", action="store_true", help="read Chatwoot, print prompt stats, make no model call")

    g = group("fixlane", "the daily fix lane")
    p = cmd(g, "run", fixlane_run, "pick ready issues and start fix tasks")
    p.add_argument("--dry-run", action="store_true", help="decide without writing tasks or modifying issues")
    cmd(g, "collect", fixlane_collect, "collect results of finished fix tasks")
    cmd(g, "status", fixlane_status, "list fix tasks with their state")
    p = cmd(g, "retry", fixlane_retry, "mark an issue's latest fix task retired so it can be picked again")
    p.add_argument("target", help="owner/repo#number")

    g = group("office", "the office page")
    p = cmd(g, "install", office_install, "install a built office HTML file")
    p.add_argument("file", help="path to office HTML file")
    p.add_argument("--public", action="store_true", help="install into site-public/index.html")
    cmd(g, "status", office_status, "office installation status")
    p = cmd(g, "serve-public", office_serve_public, "serve the public office page")
    p.add_argument("--host", default="127.0.0.1", help="host to bind (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=3046, help="port to bind (default 3046)")

    g = group("machine", "extra machines pushing agent telemetry")
    p = cmd(g, "add", machine_add, "register a machine for remote agent telemetry")
    p.add_argument("id")
    p.add_argument("--label")
    p.add_argument("--rotate", action="store_true", help="rotate push token for existing machine")
    cmd(g, "list", machine_list, "list registered machines")
    p = cmd(g, "remove", machine_remove, "remove a registered machine")
    p.add_argument("id")

    g = group("collect", "collector commands")
    p = cmd(g, "push", collect_push, "push local agent rows to remote factory")
    p.add_argument("--url", required=True, help="factory URL, e.g. https://<host>")
    p.add_argument("--machine", required=True, help="this machine's registered id")
    p.add_argument("--key", default=os.environ.get("FACTORY_KEY"), help="key for local testing (?k=)")

    p = cmd(sub, "whoami", whoami, "print caller's agent handle, thread id, title and project")
    p.add_argument("--thread", help="override T3 thread id")

    p = cmd(sub, "chatscan", chatscan, "scan T3 sessions for chat-only questions and file inbox items")
    p.add_argument("--dry-run", action="store_true", help="decide without filing")
    p.add_argument("--verbose", "-v", action="store_true", help="print skipped threads and reasons")

    cmd(sub, "build", build, "render ~/.factory/site/factory.html")
    cmd(sub, "doctor", doctor, "check every source")
    p = cmd(sub, "serve", serve, "serve the page on 127.0.0.1")
    p.add_argument("--port", type=int, default=3045)
    return top


def main(argv=None):
    try:
        a = parser().parse_args(argv)
        a.json = getattr(a, "json", False)
        obj, text = a.fn(a)
    except FactoryError as e:
        print(f"factory: error: {e}", file=sys.stderr)
        return getattr(e, "exit_code", 1)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # the contract is one line on stderr, never a traceback
        print(f"factory: error: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}", file=sys.stderr)
        return 1
    if a.fn not in (serve, office_serve_public):
        print(json.dumps(obj, ensure_ascii=False, indent=2) if a.json else text)
    return getattr(a, "exit_code", 0)


if __name__ == "__main__":
    sys.exit(main())
