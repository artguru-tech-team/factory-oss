"""External sources read by the build: GitHub (gh), the DS migration kit, ~/ds-pages notes.

Everything here is read-only (git show, gh list/view; no fetch). The DS kit logic (catalogue parsing, unit to PR matching, unit
status) is the same as ~/ds-pages/build_progress.py; it is copied, not imported, so the build
never writes bytecode into ~/ds-pages.
"""
import json
import os
import re
import subprocess
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

from . import config, store
from .store import FactoryError

# ---------- commands ----------


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120).stdout
    except FileNotFoundError:
        raise FactoryError(f"{cmd[0]} is not installed")
    except subprocess.CalledProcessError as e:
        lines = (e.stderr or e.stdout or "").strip().splitlines()
        raise FactoryError(f"{' '.join(cmd[:3])} failed: {lines[-1] if lines else 'exit ' + str(e.returncode)}")
    except subprocess.TimeoutExpired:
        raise FactoryError(f"{' '.join(cmd[:3])} timed out")


def gh_json(*args):
    return json.loads(run("gh", *args))


def read_json_file(p):
    try:
        with open(os.path.expanduser(p), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def iso_day(s):
    return s[:10] if s else None


# ---------- pull requests ----------

BAD = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
PR_FIELDS = "number,title,state,isDraft,statusCheckRollup,reviewDecision"


def ci_of(checks):
    if not checks:
        return "none"
    if any((c.get("conclusion") or c.get("state") or "") in BAD for c in checks):
        return "fail"
    if any(c.get("status") not in (None, "COMPLETED") or c.get("state") in ("PENDING", "EXPECTED") for c in checks):
        return "run"
    return "pass"


def pr_row(p):
    if p["state"] == "MERGED":
        status = "Merged"
    elif p["state"] == "CLOSED":
        status = "Closed"
    elif p.get("isDraft"):
        status = "Draft"
    else:
        status = {"APPROVED": "Approved", "CHANGES_REQUESTED": "Changes requested",
                  "REVIEW_REQUIRED": "Review required"}.get(p.get("reviewDecision") or "", "Open")
    return [p["title"], ci_of(p.get("statusCheckRollup")), status]


PR_REF = re.compile(r"^(?:([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)#)?([0-9]+)$")


def pr_key(ref, default_repo):
    """'1252' or 'owner/repo#1252' -> 'owner/repo#1252'."""
    m = PR_REF.match(str(ref))
    if not m or not (m.group(1) or default_repo):
        raise FactoryError(f"bad PR reference {ref!r}: use a number (the product's first repo) or owner/repo#N")
    return f"{m.group(1) or default_repo}#{int(m.group(2))}"


def prs_for(keys, warnings):
    """Title, CI state and status of each PR key, one gh call per repo plus one per PR not open.
    A repo or PR gh cannot read is left out (the page shows "Not found on GitHub") and warned about."""
    out, by_repo = {}, {}
    for k in keys:
        repo, n = k.split("#")
        by_repo.setdefault(repo, set()).add(int(n))
    for repo, nums in by_repo.items():
        try:
            open_prs = gh_json("pr", "list", "--repo", repo, "--state", "open", "--limit", "200", "--json", PR_FIELDS)
        except FactoryError as e:
            warnings.append(f"PRs of {repo} not shown: {e}")
            continue
        for p in open_prs:
            if p["number"] in nums:
                out[f"{repo}#{p['number']}"] = pr_row(p)
        for n in nums:
            if f"{repo}#{n}" not in out:
                try:
                    out[f"{repo}#{n}"] = pr_row(gh_json("pr", "view", str(n), "--repo", repo, "--json", PR_FIELDS))
                except FactoryError as e:
                    warnings.append(f"PR {repo}#{n} not shown: {e}")
    return out


# ---------- phases counted from their PRs ----------

def phase_counts(ph, rows):
    """(merged, inPR, closed) units of a phase with linked PRs, read from prs_for rows; None when it
    has no PRs or a PR could not be read (its stored counts then stand). Each linked PR carries the
    units given to it (`--pr 222:9`, default 1); units no PR carries stay to do."""
    links = ph.get("prs") or []
    if not links or any(k not in rows for k, _ in links):
        return None
    out = {"Merged": 0, "Closed": 0, "open": 0}
    for k, units in links:
        st = rows[k][2]
        out[st if st in ("Merged", "Closed") else "open"] += units
    return out["Merged"], out["open"], out["Closed"]


def phase_prs_keys(projects):
    """Every PR key a manual build project names, on the project or on a phase."""
    return sorted({k for p in projects if p["kind"] == "build" and p.get("source") != "ds-kit"
                   for k in p.get("prs", []) + [k for ph in p.get("phases", []) for k, _ in ph.get("prs", [])]})


def stale_phases(projects, rows, today):
    """Warnings for phases whose counts look out of date: units in PR while no PR of theirs (or of the
    project, for a phase with none) is open, or more than 2 days past due and not all merged."""
    out = []
    for p in projects:
        if p["kind"] != "build" or p.get("source") == "ds-kit":
            continue
        for ph in p.get("phases", []):
            got = phase_counts(ph, rows)
            merged, in_pr = (got[0], got[1]) if got else (ph["merged"], ph["inPR"])
            keys = [k for k, _ in ph.get("prs") or []] or p.get("prs", [])
            # a PR gh could not read says nothing about the phase: the read failure is reported on its own
            if in_pr and all(k in rows for k in keys) and not any(rows[k][2] not in ("Merged", "Closed") for k in keys):
                out.append(f"{p['id']}: phase {ph['name']} counts {in_pr} in PR but none of its PRs is open "
                           f"(factory plan units {p['id']} \"{ph['name']}\" ..., or link them: factory plan phase ... --pr N:UNITS)")
            late = (today - date.fromisoformat(ph["due"])).days
            if late > 2 and merged + ph["blocked"] < ph["total"]:
                out.append(f"{p['id']}: phase {ph['name']} is {late} days past due at {merged} of {ph['total']} merged")
    return out


# ---------- run lane ----------

RUN_DAYS = 14
ISSUE_LIMIT = 1000


def run_lane(product, today=None):
    """Intake vs closed over 14 days and the open items, from issues with the run labels."""
    today = today or date.today()
    start = today - timedelta(days=RUN_DAYS - 1)
    issues, repos = {}, product.get("repos", [])
    for i, repo in enumerate(repos):
        for label in product.get("run_labels", []):
            got = gh_json("issue", "list", "--repo", repo, "--state", "all", "--label", label, "--limit", str(ISSUE_LIMIT),
                          "--json", "number,title,createdAt,closedAt,state")
            if len(got) >= ISSUE_LIMIT:
                raise FactoryError(f"{repo} has {ISSUE_LIMIT}+ issues labelled {label}: raise ISSUE_LIMIT, the counts would be wrong")
            for it in got:
                name = ("" if i == 0 else repo.split("/")[1]) + f"#{it['number']}"
                issues[name] = it
    open_at_start = sum(1 for it in issues.values() if iso_day(it["createdAt"]) < start.isoformat()
                        and (not it["closedAt"] or iso_day(it["closedAt"]) >= start.isoformat()))
    daily = []
    for d in range(RUN_DAYS):
        day = (start + timedelta(days=d)).isoformat()
        came = sum(1 for it in issues.values() if start.isoformat() <= iso_day(it["createdAt"]) <= day)
        closed = sum(1 for it in issues.values() if it["closedAt"] and start.isoformat() <= iso_day(it["closedAt"]) <= day)
        daily.append([open_at_start + came, closed])
    items = sorted(([name, it["title"], iso_day(it["createdAt"]), "Open"] for name, it in issues.items()
                    if it["state"] == "OPEN"), key=lambda x: x[2])
    return daily, items


# ---------- DS migration kit (logic from ~/ds-pages/build_progress.py) ----------

KIT_UNITS = "docs/ds-migration/UNITS.md"
# FAST-TRACK.md: "Week n still starts on 2026-09-28 + 7(n-1) days", except Phase 0 (v2 W1-W4), which
# it compresses into 2026-10-12 -> 2026-10-23 ("Where a Phase 0 ... date in another kit file differs, this file wins").
WEEK1 = date(2026, 9, 28)
COMPRESSED = (4, date(2026, 10, 12), date(2026, 10, 23))
PHASES = {"−1": "Prerequisites", "0": "Tools, tests, copy", "1": "Foundations", "2": "Primitives and shell",
          "FLIP": "Flip", "3": "Screens after the flip", "4": "Cleanup"}
PRE_FLIP = {"−1", "0", "1", "2", "FLIP"}  # progress.tpl.html: "Avant le flip (phases −1 à 2, FLIP)"


def kit_repo():
    v = config.get("kit.repo")
    return os.path.expanduser(v) if v else None


def kit_ref():
    return config.get("kit.ref", "main")


def kit_gh():
    return config.get("kit.gh")


def kit_notes():
    v = config.get("kit.notes")
    return os.path.expanduser(v) if v else None


def kit_bases():
    v = config.get("kit.bases") or []
    if not isinstance(v, list) or not all(isinstance(b, str) for b in v):
        raise FactoryError("kit.bases must be a JSON list of branch names (factory config set kit.bases --as-json '[...]')")
    return v


def kit_search_base():
    return config.get("kit.search_base", "develop")


def kit_search():
    return config.get("kit.search", "ds- in:head")


def show(path, ref=None):
    repo = kit_repo()
    if not repo:
        return ""
    ref = ref or kit_ref()
    try:
        return run("git", "-C", repo, "show", f"{ref}:{path}")
    except FactoryError:
        return ""


def plain(cell):
    cell = re.sub(r"<br>\s*", " — ", cell)
    cell = re.sub(r"[*`]", "", cell)
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cell).strip()


def catalogue(text):
    units, on = [], False
    for line in text.splitlines():
        if line.startswith("## Catalogue"):
            on = True
            continue
        if on and line.startswith("## "):
            break
        if not on or not line.startswith("| ") or line.startswith("| ID") or line.startswith("|---"):
            continue
        c = [x.strip() for x in line.strip().strip("|").split(" | ")]
        if len(c) < 11:
            continue
        units.append({"id": plain(c[0]), "title": plain(c[2].split("<br>")[0]), "phase": plain(c[3]),
                      "week": plain(c[4]), "kitStatus": plain(c[10]).lower()})
    return units


def kit_prs():
    repo = kit_repo()
    gh = kit_gh()
    if not repo or not gh:
        return []
    out = []
    bases = kit_bases()
    search_base = kit_search_base()
    search_query = kit_search()
    for base in bases:
        search_arg = search_query if base == search_base else ""
        out += gh_json("pr", "list", "--repo", gh, "--base", base, "--state", "all", "--limit", "300",
                       "--search", search_arg,
                       "--json", "number,state,headRefName,mergedAt")
    return out


def unit_match(unit_id, branch):
    return re.search(rf"/ds-{re.escape(unit_id.lower())}(-|$)", branch.lower()) is not None


def unit_status(u, mine):
    if any(p["state"] == "MERGED" for p in mine):
        return "merged"
    if any(p["state"] == "OPEN" for p in mine):
        return "in-pr"
    # "merged (PR #1081, ...)" and "done (answered ...)" count too; build_progress.py compares the whole
    # cell and reads them as to do, the one place this copy differs
    k = u["kitStatus"].split(" (")[0].strip()
    if k in ("released", "done", "merged", "in prod", "decided", "answered"):
        return "merged"
    if k.startswith("in review") or k.startswith("claimed"):
        return "in-pr"
    if "block" in k:
        return "blocked"
    return "todo"


def week_dates(week):
    m = re.search(r"\bW(\d+)", week or "")
    if not m:
        return None
    if int(m.group(1)) <= COMPRESSED[0]:
        return COMPRESSED[1], COMPRESSED[2]
    start = WEEK1 + timedelta(days=7 * (int(m.group(1)) - 1))
    return start, start + timedelta(days=4)


def scope_history():
    """[(day, unit count)] from every commit that touched UNITS.md."""
    repo = kit_repo()
    if not repo:
        return []
    ref = kit_ref()
    out = []
    log = run("git", "-C", repo, "log", "--reverse", "--format=%H %cI", ref, "--", KIT_UNITS)
    for line in log.splitlines():
        sha, when = line.split()
        out.append((when[:10], len(catalogue(show(KIT_UNITS, sha)))))
    return out


def ds_kit(today=None):
    """Phases, scopes, project-graph series, scope added and open PRs of the DS migration."""
    # no git fetch here: the ~/ds-pages cron fetches this ref every 10 minutes, and the build stays read-only
    repo = kit_repo()
    if not repo:
        return {}
    today = today or date.today()
    ref = kit_ref()
    units = catalogue(show(KIT_UNITS))
    if not units:
        raise FactoryError(f"no unit catalogue at {ref}:{KIT_UNITS}")
    prs = kit_prs()
    notes_path = kit_notes()
    notes = read_json_file(notes_path) if notes_path else {}
    gh = kit_gh() or ""
    merged_on, open_prs = {}, set()
    for u in units:
        mine = [p for p in prs if unit_match(u["id"], p["headRefName"])]
        over = notes.get("status", {})
        u["status"] = over[u["id"]] if u["id"] in over else unit_status(u, mine)
        dates = sorted(iso_day(p["mergedAt"]) for p in mine if p["state"] == "MERGED" and p["mergedAt"])
        merged_on[u["id"]] = dates[0] if dates else None
        if u["status"] == "in-pr" and gh:
            open_prs |= {f"{gh}#{p['number']}" for p in mine if p["state"] == "OPEN"}

    order = list(dict.fromkeys(u["phase"] for u in units))
    all_weeks = [w for w in (week_dates(u["week"]) for u in units) if w]
    phases = []
    for ph in order:
        mine = [u for u in units if u["phase"] == ph]
        weeks = [w for w in (week_dates(u["week"]) for u in mine) if w] or all_weeks
        n = lambda s: sum(1 for u in mine if u["status"] == s)  # noqa: E731
        phases.append([PHASES.get(ph, "Phase " + ph), n("merged"), len(mine), min(w[0] for w in weeks).isoformat(),
                       max(w[1] for w in weeks).isoformat(), n("in-pr"), n("blocked")])
    start = min(date.fromisoformat(p[3]) for p in phases)
    if today < start:
        raise FactoryError(f"the DS kit starts on {start}, after today")

    hist = scope_history()
    series = []
    for i in range((today - start).days + 1):
        d = (start + timedelta(days=i)).isoformat()
        scope = next((c for day, c in reversed(hist) if day <= d), hist[0][1] if hist else len(units))
        # merged units with no merged PR (human actions, kit status) have no date: counted from the start
        done = sum(1 for u in units if u["status"] == "merged" and (merged_on[u["id"]] or "") <= d)
        series.append([scope, done])
    series[-1][0] = len(units)
    return {"phases": phases,
            "scopes": [["Whole programme", list(range(len(phases)))],
                       ["Before the flip", [i for i, ph in enumerate(order) if ph in PRE_FLIP]]],
            "added": len(units) - (hist[0][1] if hist else len(units)),
            "series": series, "prs": sorted(open_prs), "next": notes.get("next", []),
            "undatedMerged": sum(1 for u in units if u["status"] == "merged" and not merged_on[u["id"]])}


def ds_notes_items(project, notes_path=None, answered_path=None):
    """Inbox items for the open decisions and actions."""
    notes_path = notes_path or kit_notes()
    answered_path = answered_path or config.get("kit.answered")
    if answered_path is not None and not isinstance(answered_path, str):
        raise FactoryError("kit.answered must be a path")
    if not notes_path:
        raise FactoryError("no notes file: pass --notes or factory config set kit.notes <path>")
    store.find_project(project)
    notes_path = os.path.expanduser(notes_path)
    notes = read_json_file(notes_path)
    done = read_json_file(os.path.expanduser(answered_path)) if answered_path else {}
    if not notes:
        raise FactoryError(f"no notes at {notes_path}")
    at = store.iso(datetime.fromtimestamp(os.path.getmtime(notes_path), timezone.utc))
    out = []
    for d in notes.get("decisions", []):
        if d["id"] in done:
            continue
        store.check_id("ds-" + d["id"], "decision id")
        if not all(re.fullmatch(r"[a-z0-9]{1,12}", o[0]) for o in d["options"]):
            raise FactoryError(f"decision {d['id']}: option keys must be a-z or 0-9")
        why = d.get("why", "")
        applied = d.get("reco") in {o[0] for o in d["options"]} and ("par défaut" in why.lower() or "by default" in why.lower())
        out.append({"id": "ds-" + d["id"], "project": project, "kind": "applied" if applied else "urgent",
                    "title": d["title"], "short": "Applied by default" if applied else "Decision", "from": None,
                    "thread": None,
                    "at": at, "context": d.get("context", "") + (f" See {d['link']}." if d.get("link") else ""),
                    "options": [list(o) for o in d["options"]], "reco": d.get("reco"), "why": why})
    for x in notes.get("actions", []):
        if x["id"] in done:
            continue
        store.check_id("ds-" + x["id"], "action id")
        out.append({"id": "ds-" + x["id"], "project": project, "kind": "action", "title": x["title"],
                    "short": "Action for you", "from": None, "thread": None, "at": at,
                    "context": x.get("context", "") + (f" See {x['link']}." if x.get("link") else ""),
                    "options": [["done", "Done"], ["blocked", "Blocked"]], "reco": None, "why": ""})
    return out


# ---------- doctor ----------


def doctor_checks():
    from . import t3, tokens
    checks = []

    def check(name, fn):
        try:
            checks.append((name, True, fn() or "ok"))
        except FactoryError as e:
            checks.append((name, False, str(e)))

    def key_mode():
        store.key()
        return store.check_mode_600(".key")

    def products():
        ps = store.products()
        if not ps:
            raise FactoryError("no products: factory product set acme --name Acme --workspace ... --repo ...")
        return ", ".join(ps)

    def t3_db():
        with closing(t3.connect()) as con:
            unknown = t3.unknown_statuses(t3.agents(store.products(), store.projects(), con=con)[0])
        if unknown:
            raise FactoryError(f"unknown T3 run status(es) {sorted(unknown)}: factory/t3.py KNOWN_RUN must learn them")
        return t3.db_path() + " (schema and run statuses ok)"

    def usage():
        _, at = tokens.load()
        return f"{tokens.cache_path()} (scanned {at.strftime('%Y-%m-%d %H:%M')} UTC)"

    def gh():
        run("gh", "auth", "status")
        return "logged in"

    def relay_check():
        from . import relay
        return relay.doctor_status()

    def phases():
        ps = [p for p in store.projects() if p["kind"] == "build" and p.get("source") != "ds-kit"]
        unread = []  # a PR gh cannot read is reported as such, not as a stale phase
        rows = prs_for(phase_prs_keys(ps), unread)
        found = unread + stale_phases(ps, rows, date.today())
        if found:
            raise FactoryError("; ".join(found))
        return f"{sum(len(p['phases']) for p in ps)} phases current"

    check("state", key_mode)
    check("relay", relay_check)
    check("products", products)
    check("t3 database", t3_db)
    check("token scan", usage)
    check("gh", gh)
    check("phases", phases)
    for p in store.products().values():
        for repo in p.get("repos", []):
            check(f"repo {repo}", lambda repo=repo: run("gh", "repo", "view", repo, "--json", "name") and "reachable")

    def access():
        team, aud = config.get("access.team"), config.get("access.aud")
        if team is None and aud is None:
            return "not configured: tunnelled requests are refused"
        if not isinstance(team, str) or not team.startswith("https://") or not isinstance(aud, str) or not aud:
            raise FactoryError("access.team must be https://<team>.cloudflareaccess.com and access.aud set")
        return team

    def board_labels():
        from . import board
        missing = board.missing_labels_for_products()
        return "; ".join(missing) if missing else "all labels present"

    check("access", access)
    check("board labels", board_labels)
    if any(p.get("source") == "ds-kit" for p in store.projects()):
        def kit():
            repo = kit_repo()
            if not repo:
                raise FactoryError("a ds-kit project exists but kit.repo is unset (factory config set kit.repo <path>)")
            ref = kit_ref()
            return run("git", "-C", repo, "rev-parse", "--short", ref).strip() + f" ({ref})"

        def notes():
            kn = kit_notes()
            if not kn or not os.access(kn, os.R_OK):
                raise FactoryError(f"cannot read {kn}")
            return kn
        check("ds kit", kit)
        check("ds notes", notes)

    from . import sweep
    checks.extend(sweep.doctor_checks())
    return checks
