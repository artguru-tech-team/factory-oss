"""Daily release manager: release config, state, and product registry."""
import copy
import fnmatch
import json
import math
import os
import posixpath
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config, relay, sources, store
from .store import FactoryError

_sleep = time.sleep
_monotonic = time.monotonic

GRACE_SAMPLES = 2

TUNIS = ZoneInfo("Africa/Tunis")

MIGRATION_GLOBS = ("**/migrations/**", "prisma/**", "supabase/migrations/**")
AUTH_GLOBS = ("**/auth/**", "**/middleware.*")
INFRA_GLOBS = (".github/workflows/**", "helm/**", "charts/**")
MONEY_GLOBS = ("**/payout*", "**/wallet*", "**/payment*", "**/checkout*", "**/fee[s_./-]*")

DEFAULT_RED = list(MIGRATION_GLOBS + AUTH_GLOBS + INFRA_GLOBS + MONEY_GLOBS)
_RED_KINDS = (
    ("migration", MIGRATION_GLOBS),
    ("money path", MONEY_GLOBS),
    ("auth", AUTH_GLOBS),
    ("infra", INFRA_GLOBS),
)


def _parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _try_parse_utc(s: str) -> datetime | None:
    try:
        return _parse_utc(s)
    except (ValueError, TypeError):
        return None


def _now_utc(now: datetime | None = None) -> datetime:
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)

LOCKFILES = {
    "package-lock.json",
    "bun.lock",
    "bun.lockb",
    "yarn.lock",
    "pnpm-lock.yaml",
    "requirements.txt",
    "poetry.lock",
    "go.sum",
}

ENV_VAR_RE = re.compile(r'(?:process\.env\.|getEnv\(["\']?)([A-Z0-9_]+)')


def _match_glob(path: str, pattern: str) -> bool:
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch("/" + path, pattern)


def _has_test_file(files: list[str]) -> bool:
    for f in files:
        f_lower = f.lower()
        if "test" in f_lower or "spec" in f_lower or "__tests__" in f_lower:
            return True
    return False


def score_pr(pr: dict, red_paths: list[str] | None = None) -> tuple[str, list[str]]:
    if red_paths is None:
        red_paths = []

    red_reasons: list[str] = []
    yellow_reasons: list[str] = []

    def _add(reasons: list[str], reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if pr.get("filesComplete") is False:
        _add(red_reasons, "file list incomplete")

    if "risk:critical" in (pr.get("labels") or []):
        _add(red_reasons, "risk-review critical")

    files = pr.get("files") or []
    for f in files:
        for kind, globs in _RED_KINDS:
            if any(_match_glob(f, pat) for pat in globs):
                _add(red_reasons, kind)

    for pattern in red_paths:
        if any(_match_glob(f, pattern) for f in files):
            _add(red_reasons, f"red path {pattern}")

    if (pr.get("changed") or 0) > 400:
        _add(yellow_reasons, "over 400 changed lines")

    if not _has_test_file(files):
        _add(yellow_reasons, "no test file changed")

    patch = pr.get("patch") or ""
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            for m in ENV_VAR_RE.finditer(line):
                _add(yellow_reasons, f"new env var {m.group(1)}")

    for f in files:
        if posixpath.basename(f) in LOCKFILES:
            _add(yellow_reasons, "dependency bump")

    if red_reasons:
        color = "red"
    elif yellow_reasons:
        color = "yellow"
    else:
        color = "green"

    return color, red_reasons + yellow_reasons


def release_score(scored: list[tuple[str, list[str]]]) -> str:
    red_count = 0
    yellow_count = 0
    for color, _ in scored:
        if color == "red":
            red_count += 1
        elif color == "yellow":
            yellow_count += 1

    if red_count > 0 or yellow_count >= 4:
        return "red"
    if yellow_count > 0:
        return "yellow"
    return "green"


RELEASE_REL = "release.json"

DEFAULT_RELEASE = {
    "products": {},
    "paused": {},
    "holds": {},
    "journal": [],
    "verdicts": {},
    "stale_asked": {},
    "shipped_prs": {},
    "queue_actions": {},
    "queue_log": [],
    "queue_asked": {},
}

ALLOWED_FLOWS = ("release-branch", "gitflow", "pr", "factory")
ALLOWED_MODES = ("shadow", "live")


def _argv_ok(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, str) for x in v)


def _is_valid_check(c):
    if not isinstance(c, dict):
        return False
    name = c.get("name")
    kind = c.get("kind")
    if not isinstance(name, str) or not name:
        return False
    if kind == "http":
        url = c.get("url")
        return isinstance(url, str) and (url.startswith("http://") or url.startswith("https://"))
    if kind in ("errors", "business"):
        return _argv_ok(c.get("cmd")) and _argv_ok(c.get("baseline_cmd"))
    return False


def _is_valid_product(prod, name: str = ""):
    if not (
        isinstance(prod, dict)
        and isinstance(prod.get("repo"), str)
        and prod.get("flow") in ALLOWED_FLOWS
        and isinstance(prod.get("base"), str)
        and isinstance(prod.get("head"), str)
        and (prod.get("thread") is None or isinstance(prod.get("thread"), str))
        and isinstance(prod.get("red_paths"), list)
        and all(isinstance(x, str) for x in prod.get("red_paths"))
        and isinstance(prod.get("project"), str)
        and prod.get("mode") in ALLOWED_MODES
    ):
        return False
    for k in ("display_name", "color", "short", "flow_label"):
        if prod.get(k) is not None and not isinstance(prod[k], str):
            return False
    cm = prod.get("comms")
    if cm is not None and not (isinstance(cm, dict) and all(
            isinstance(cm.get(k, []), list) and all(isinstance(x, str) and x.strip() for x in cm.get(k, []))
            for k in ("langs", "audiences", "internal_scopes"))):
        return False
    checks = prod.get("checks")
    if not isinstance(checks, list):
        return False
    for c in checks:
        if not _is_valid_check(c):
            c_name = c.get("name") if isinstance(c, dict) and c.get("name") else "<unnamed>"
            prod_name = name or prod.get("project") or "<unknown>"
            raise FactoryError(f"malformed check {c_name!r} in product {prod_name!r}")
    return True


def automation_url() -> str | None:
    """The dashboard's Develop queue link (config site.url), or None when it is not set."""
    base = config.get("site.url")
    return f"{base.rstrip('/')}/#/automation" if isinstance(base, str) and base.strip() else None


def close_comment() -> str:
    url = automation_url()
    return f"Closed from develop queue: {url}" if url else "Closed from develop queue"


def release():
    """release.json, refused unless it has the expected shape."""
    data = store.read(RELEASE_REL, copy.deepcopy(DEFAULT_RELEASE))
    ok = isinstance(data, dict)
    if ok:
        if "shipped_prs" not in data:
            data["shipped_prs"] = {}
        if "queue_actions" not in data:
            data["queue_actions"] = {}
        if "queue_log" not in data:
            data["queue_log"] = []
        if "queue_asked" not in data:
            data["queue_asked"] = {}
        products = data.get("products")
        paused = data.get("paused")
        holds = data.get("holds")
        journal = data.get("journal")
        verdicts = data.get("verdicts")
        stale_asked = data.get("stale_asked")
        shipped_prs = data.get("shipped_prs")
        queue_actions = data.get("queue_actions")
        queue_log = data.get("queue_log")
        queue_asked = data.get("queue_asked")
        ok = (
            isinstance(products, dict)
            and isinstance(paused, dict)
            and isinstance(holds, dict)
            and isinstance(journal, list)
            and isinstance(verdicts, dict)
            and isinstance(stale_asked, dict)
            and isinstance(shipped_prs, dict)
            and isinstance(queue_actions, dict)
            and isinstance(queue_log, list)
            and all(isinstance(x, dict) for x in queue_log)
            and isinstance(queue_asked, dict)
            and all(isinstance(k, str) and isinstance(v, list) for k, v in queue_asked.items())
        )
        if ok:
            for k, v in products.items():
                if not (isinstance(k, str) and _is_valid_product(v, k)):
                    ok = False
                    break
            for k, v in shipped_prs.items():
                if not (isinstance(k, str) and isinstance(v, list) and all(isinstance(x, int) for x in v)):
                    ok = False
                    break
            for k, v in queue_actions.items():
                if not (isinstance(k, str) and isinstance(v, dict) and "action" not in v):
                    ok = False
                    break
                for flag, val in v.items():
                    if flag == "applied":
                        if not (isinstance(val, dict) and all(isinstance(ak, str) and isinstance(av, str) for ak, av in val.items())):
                            ok = False
                            break
                    elif not isinstance(val, str):
                        ok = False
                        break
                if not ok:
                    break
    if not ok:
        raise FactoryError(f"{store.path(RELEASE_REL)} is not a release config: fix or restore it")
    return data


def save_release(data):
    store.write(RELEASE_REL, data)


_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _apply_meta(cur, a):
    """Display flags (name, colour, short id, flow label) and the comms block onto a product entry."""
    for key in ("display_name", "color", "short", "flow_label"):
        v = getattr(a, key, None)
        if v is not None:
            if key == "color" and not _HEX.match(v):
                raise FactoryError(f"--color must be a hex colour like #4670CF, got {v!r}")
            cur[key] = v
    given = {k: getattr(a, f"comms_{k}", None) for k in ("langs", "audiences", "internal_scopes")}
    if any(v is not None for v in given.values()):
        comms = cur.setdefault("comms", {"langs": [], "audiences": [], "internal_scopes": []})
        for k, v in given.items():
            if v is not None:
                if any(not x.strip() for x in v):
                    raise FactoryError(f"--comms-{k.replace('_', '-')} values must not be empty")
                comms[k] = list(v)


def product_add(a):
    with store.lock():
        data = release()
        if a.name in data["products"]:
            raise FactoryError(f"product {a.name!r} already exists")
        if a.flow not in ALLOWED_FLOWS:
            raise FactoryError(f"unknown flow {a.flow!r} (choose from {', '.join(ALLOWED_FLOWS)})")
        red_paths = list(a.red_path) if getattr(a, "red_path", None) else []
        entry = {
            "repo": a.repo,
            "flow": a.flow,
            "base": getattr(a, "base", None) or "develop",
            "head": getattr(a, "head", None) or "main",
            "thread": getattr(a, "thread", None),
            "red_paths": red_paths,
            "project": a.project,
            "mode": "shadow",
            "checks": [],
        }
        _apply_meta(entry, a)
        data["products"][a.name] = entry
        save_release(data)
    return entry, f"ok: product {a.name} added"


def product_set(a):
    with store.lock():
        data = release()
        if a.name not in data["products"]:
            raise FactoryError(f"unknown product {a.name!r}")
        cur = data["products"][a.name]
        if getattr(a, "thread", None) is not None:
            cur["thread"] = a.thread
        if getattr(a, "mode", None) is not None:
            if a.mode not in ALLOWED_MODES:
                raise FactoryError(f"unknown mode {a.mode!r} (choose from {', '.join(ALLOWED_MODES)})")
            cur["mode"] = a.mode
        if getattr(a, "red_path", None) is not None:
            cur["red_paths"] = list(a.red_path)
        _apply_meta(cur, a)
        save_release(data)
    return cur, f"ok: product {a.name} updated"


FACTORY_WAIT = "factory deploys all of main"


def unreleased_prs(product: dict) -> tuple[list[dict], str | None, list[str]]:
    repo = product["repo"]
    base = product.get("base", "develop")
    flow = product.get("flow")
    if flow == "factory":
        live_dir = os.path.expanduser("~/factory-live")
        head = sources.run("git", "-C", live_dir, "rev-parse", "HEAD").strip()
    else:
        head = product.get("head", "main")

    base_commit = sources.gh_json("api", f"repos/{repo}/commits/{base}") or {}
    base_sha = base_commit.get("sha")
    compare = sources.gh_json("api", f"repos/{repo}/compare/{head}...{base_sha}")
    commits = compare.get("commits") or []
    total_commits = compare.get("total_commits", 0)
    if total_commits > len(commits):
        # One compare response stops at 250 commits: read them all again, 100 per page.
        commits = []
        for page in range(1, total_commits // 100 + 2):
            more = sources.gh_json("api", f"repos/{repo}/compare/{head}...{base_sha}?per_page=100&page={page}")
            commits += (more or {}).get("commits") or []
        commits = list({c["sha"]: c for c in commits}.values())  # main moving between pages must not fake a full read
    if total_commits > len(commits):
        raise FactoryError(f"compare truncated: {len(commits)} of {total_commits} commits")

    unmapped_commits = []
    kept_pr_numbers = {}
    for c in commits:
        sha = c["sha"]
        pulls = sources.gh_json("api", f"repos/{repo}/commits/{sha}/pulls") or []
        commit_has_kept_pr = False
        for pr in pulls:
            if not pr.get("merged_at"):
                continue
            if pr.get("base", {}).get("ref") != base:
                continue
            kept_pr_numbers[pr["number"]] = None
            commit_has_kept_pr = True
        parents = c.get("parents") or []
        if len(parents) == 1 and not commit_has_kept_pr:
            unmapped_commits.append(sha)

    prs = []
    for num in kept_pr_numbers:
        pr_info = sources.gh_json("api", f"repos/{repo}/pulls/{num}")
        title = pr_info.get("title", "")
        merged_at = pr_info.get("merged_at", "")
        labels = [
            label["name"] if isinstance(label, dict) else str(label)
            for label in (pr_info.get("labels") or [])
        ]
        additions = pr_info.get("additions") or 0
        deletions = pr_info.get("deletions") or 0
        changed = additions + deletions
        changed_files = pr_info.get("changed_files") or 0

        files = []
        patches = []
        for page in range(1, 31):
            page_files = sources.gh_json(
                "api", f"repos/{repo}/pulls/{num}/files?per_page=100&page={page}"
            ) or []
            for item in page_files:
                if "filename" in item:
                    files.append(item["filename"])
                patch = item.get("patch")
                if patch:
                    patches.append(patch)
            if len(page_files) < 100:
                break

        files_complete = (len(files) == changed_files)
        patch_str = "\n".join(patches)

        prs.append({
            "number": num,
            "title": title,
            "mergedAt": merged_at,
            "labels": labels,
            "files": files,
            "changed": changed,
            "filesComplete": files_complete,
            "patch": patch_str,
        })

    prs.sort(key=lambda p: p["mergedAt"])
    return prs, base_sha, unmapped_commits


def decide(
    prs: list[dict],
    state: dict,
    product: str,
    ci_green: bool,
    now: datetime,
    unmapped: list[str] | int = 0,
    base_sha: str | None = None,
) -> dict:
    state = state or {}
    flow = (state.get("products", {}) or {}).get(product, {}).get("flow")
    now_utc = _now_utc(now)
    now_tunis = now_utc.astimezone(TUNIS)
    today_tunis = now_tunis.date()
    weekday_tunis = now_tunis.weekday()

    unmapped_list = unmapped if isinstance(unmapped, list) else []
    unmapped_count = len(unmapped_list) if isinstance(unmapped, list) else int(unmapped or 0)
    unmapped_reason = None
    if unmapped_count > 0:
        if unmapped_list:
            short_shas = [s[:7] for s in unmapped_list[:3]]
            sha_part = ", ".join(short_shas) + (", ..." if unmapped_count > 3 else "")
            unmapped_reason = f"{unmapped_count} commit(s) on develop without a PR ({sha_part}): not shipped"
        else:
            unmapped_reason = f"{unmapped_count} commit(s) on develop without a PR: not shipped"

    # Oldest first, except PRs the owner moved to the front (`factory release queue-action <repo>#<n> front`):
    # they come first, so they make the 10-PR cap and are the red that ships solo.
    repo = ((state.get("products", {}) or {}).get(product, {}).get("repo") or "").lower()
    fronted = {k.lower() for k, v in (state.get("queue_actions") or {}).items() if isinstance(v, dict) and "front" in v}
    prs_sorted = sorted(prs, key=lambda p: (f"{repo}#{p['number']}" not in fronted, p.get("mergedAt", "")))

    candidate_prs = []
    no_release_prs = []
    for p in prs_sorted:
        labels = p.get("labels") or []
        if "no-release" in labels:
            no_release_prs.append(p)
        else:
            candidate_prs.append(p)

    no_release_held = [{"number": p["number"], "reason": "no-release label"} for p in no_release_prs]

    def _score(ps: list[dict]) -> str:
        return release_score([(p.get("color", "green"), p.get("scoreReasons", [])) for p in ps])

    candidate_score = _score(candidate_prs)

    def _wait(reason: str) -> dict:
        return {
            "verdict": "wait",
            "ship": [],
            "held": list(no_release_held),
            "reasons": [reason],
            "score": candidate_score,
            "baseSha": base_sha,
        }

    # 1. wait if no PRs left after dropping no-release
    if not candidate_prs:
        return _wait("nothing to release")

    # 2. wait if paused[product] or paused["*"]
    paused = state.get("paused") or {}
    if paused.get(product) or paused.get("*"):
        return _wait("paused")

    # 3. wait if a hold exists for product (reason names the hold's issue)
    holds = state.get("holds") or {}
    if product in holds:
        issue = holds[product].get("issue", "")
        return _wait(f"on hold: {issue}")

    # 4. wait if a journal "rolled-back" for product is under 24h old
    journal = state.get("journal") or []
    for entry in journal:
        if entry.get("product") == product and entry.get("event") == "rolled-back":
            at_str = entry.get("at")
            if at_str:
                at_dt = _try_parse_utc(at_str)
                if at_dt is None:
                    continue
                delta = now_utc - at_dt
                if timedelta(0) <= delta < timedelta(hours=24):
                    return _wait("rolled back under 24h ago")

    # 5. wait if a journal "shipped" or "started" for product has today's Africa/Tunis date
    for entry in journal:
        if entry.get("product") == product and entry.get("event") in ("shipped", "started"):
            at_str = entry.get("at")
            if at_str:
                at_dt = _try_parse_utc(at_str)
                if at_dt is None:
                    continue
                if at_dt.astimezone(TUNIS).date() == today_tunis:
                    return _wait("already released today")

    # 6. wait if Saturday or Sunday (Africa/Tunis)
    if weekday_tunis in (5, 6):
        return _wait("weekend")

    # 7. wait if not ci_green
    if not ci_green:
        return _wait("CI is not green")

    # 8. take the oldest 10 (the rest held: "over the 10-PR cap")
    kept = candidate_prs[:10]
    over_cap = candidate_prs[10:]
    held_reasons: dict[int, str] = {}
    for p in no_release_prs:
        held_reasons[p["number"]] = "no-release label"
    for p in over_cap:
        held_reasons[p["number"]] = "over the 10-PR cap"

    # 9. Friday: keep only green PRs labelled bug/fix; none -> wait
    if weekday_tunis == 4:
        friday_kept = []
        for p in kept:
            labels = p.get("labels") or []
            is_bug_or_fix = any(l in ("bug", "fix") for l in labels)
            if p.get("color") == "green" and is_bug_or_fix:
                friday_kept.append(p)
            else:
                held_reasons[p["number"]] = "Friday: only green bug/fix PRs"
        if not friday_kept:
            return _wait("Friday: no green bug/fix PR")
        kept = friday_kept

    # Determine verdict & ship
    red_prs = [p for p in kept if p.get("color") == "red"]
    yellow_prs = [p for p in kept if p.get("color") == "yellow"]
    green_prs = [p for p in kept if p.get("color") == "green"]

    kept_score = _score(kept)

    if not red_prs and kept_score != "red":
        ship_prs = sorted(kept, key=lambda p: p.get("mergedAt", ""))  # cherry-pick in merge order
        count = len(ship_prs)
        if len(ship_prs) == len(prs) and unmapped_count == 0:
            verdict = "ship-all"
        else:
            verdict = "cherry-pick"
        if not yellow_prs:
            reasons = [f"{count} PR{'s' if count != 1 else ''}, all green"]
        else:
            reasons = [f"{count} PR{'s' if count != 1 else ''}, green/yellow"]

    elif not red_prs and kept_score == "red":
        # Four yellows case: cherry-pick 3 oldest yellows + every green
        verdict = "cherry-pick"
        shipped_yellows = yellow_prs[:3]
        for p in yellow_prs[3:]:
            held_reasons[p["number"]] = "4 yellows make the release red; held for tomorrow"
        ship_prs = sorted(shipped_yellows + green_prs, key=lambda p: p.get("mergedAt", ""))
        reasons = [f"cherry-pick 3 yellows and {len(green_prs)} green; 4 yellows make release red"]

    else:
        # Solo-red on Mon-Thu: ship oldest red
        verdict = "solo-red"
        oldest_red = red_prs[0]
        ship_prs = [oldest_red]
        for p in red_prs[1:]:
            held_reasons[p["number"]] = "second red: only the oldest red ships solo"
        for p in kept:
            if p.get("color") != "red":
                held_reasons[p["number"]] = "solo-red day: the red PR ships alone"
        cause = oldest_red.get("scoreReasons")[0] if oldest_red.get("scoreReasons") else "red"
        reasons = [f"solo red: #{oldest_red['number']} ({cause})"]

    if unmapped_reason:
        reasons.append(unmapped_reason)

    stall = None
    if flow == "factory" and verdict != "ship-all":
        # Factory deploys all of main: it cannot cherry-pick or leave a PR behind.
        all_reds = [p for p in candidate_prs if p.get("color") == "red"]
        friday_held = [
            p["number"] for p in candidate_prs
            if weekday_tunis == 4
            and (p.get("color") != "green" or not {"bug", "fix"} & set(p.get("labels") or []))
        ]
        wait_reason = None
        if no_release_prs:
            wait_reason = f"{FACTORY_WAIT}: #{no_release_prs[0]['number']} has the no-release label"
        elif friday_held:
            wait_reason = f"{FACTORY_WAIT}: Friday, and #{friday_held[0]} is not a green bug/fix PR"
        elif unmapped_reason:
            wait_reason = f"{FACTORY_WAIT}: {unmapped_reason}"
        elif all_reds and len(candidate_prs) > 1:
            stall = all_reds[0]["number"]
            wait_reason = (
                f"{FACTORY_WAIT}: red #{stall} cannot ship alone "
                f"({len(candidate_prs)} PRs on main); needs a manual release"
            )
        if wait_reason:
            verdict = "wait"
            for p in ship_prs:
                held_reasons.setdefault(p["number"], wait_reason)
            ship_prs = []
            reasons = [wait_reason]
        else:
            # Only the yellow or 10-PR limits held PRs back: ship all of main as one release.
            verdict = "solo-red" if all_reds else "ship-all"
            ship_prs = candidate_prs
            held_reasons = {}
            reasons = [f"factory deploys all of main: {len(ship_prs)} PRs"]
            if len(ship_prs) > 10:
                reasons.append("over the 10-PR cap")
            if not all_reds and _score(ship_prs) == "red":
                reasons.append("4 or more yellows make this release red")

    ship = [p["number"] for p in ship_prs]
    ship_set = set(ship)

    # Build held in mergedAt ascending order
    held = []
    for p in prs_sorted:
        num = p["number"]
        if num not in ship_set and num in held_reasons:
            held.append({"number": num, "reason": held_reasons[num]})

    if ship_prs:
        score = _score(ship_prs)
    else:
        score = candidate_score

    out = {
        "verdict": verdict,
        "ship": ship,
        "held": held,
        "reasons": reasons,
        "score": score,
        "baseSha": base_sha,
    }
    if stall is not None:
        out["stall"] = stall
    return out


VALID_EVENTS = (
    "started",
    "shipped",
    "rolled-back",
    "rollback-failed",
    "held",
    "queue-merged",
    "queue-retargeted",
    "queue-closed",
    "queue-agent-sent",
)


def _format_time(now: datetime | None = None) -> str:
    return store.iso(_now_utc(now))


def record(
    product: str,
    event: str,
    prs: list[int] | None = None,
    sha: str | None = None,
    note: str = "",
    now: datetime | None = None,
) -> dict:
    if event not in VALID_EVENTS:
        raise FactoryError(f"unknown event {event!r} (choose from {', '.join(VALID_EVENTS)})")
    with store.lock():
        data = release()
        if product not in data.get("products", {}):
            raise FactoryError(f"unknown product {product!r}")
        iso = _format_time(now)
        prs_list = [int(p) for p in (prs or [])]
        if event in ("shipped", "rolled-back"):
            last_started_idx = -1
            journal = data.get("journal", [])
            for idx, j_entry in enumerate(journal):
                if j_entry.get("product") == product and j_entry.get("event") == "started":
                    last_started_idx = idx
            held_prs = set()
            if last_started_idx != -1:
                for j_entry in journal[last_started_idx + 1:]:
                    if j_entry.get("product") == product and j_entry.get("event") == "held":
                        held_prs.update(j_entry.get("prs") or [])
            prs_list = [p for p in prs_list if p not in held_prs]
        entry = {
            "at": iso,
            "product": product,
            "event": event,
            "prs": prs_list,
            "sha": sha,
            "note": note or "",
        }
        if event == "rolled-back":
            data["holds"][product] = {"since": iso, "issue": note or ""}
        elif event == "rollback-failed":
            data["paused"][product] = True
        if event in ("shipped", "rolled-back"):
            prod_shipped = data.setdefault("shipped_prs", {}).setdefault(product, [])
            seen = set()
            deduped = []
            for p in prod_shipped + entry["prs"]:
                if p not in seen:
                    seen.add(p)
                    deduped.append(p)
            data["shipped_prs"][product] = deduped[-2000:]
        data["journal"].append(entry)
        data["journal"] = data["journal"][-500:]
        save_release(data)
    return entry


def pause(product: str = "*") -> dict:
    with store.lock():
        data = release()
        if product != "*" and product not in data.get("products", {}):
            raise FactoryError(f"unknown product {product!r}")
        data["paused"][product] = True
        save_release(data)
    return data["paused"]


def resume(product: str = "*") -> dict:
    with store.lock():
        data = release()
        if product != "*" and product not in data.get("products", {}):
            raise FactoryError(f"unknown product {product!r}")
        data["paused"].pop(product, None)
        save_release(data)
    return data["paused"]


UNHOLD_AFTER = timedelta(hours=24)  # a rollback hold can be lifted after this


def unhold(product: str, now: datetime | None = None) -> dict:
    with store.lock():
        data = release()
        holds = data.get("holds", {})
        if product not in holds:
            raise FactoryError(f"no hold on {product}")
        hold = holds[product]
        since_str = hold.get("since")
        if not since_str or not isinstance(since_str, str):
            raise FactoryError(f"corrupt hold on {product}: missing since")
        try:
            since_dt = _parse_utc(since_str)
        except (ValueError, TypeError) as e:
            raise FactoryError(f"corrupt hold on {product}: unparsable since {since_str!r}") from e
        now_utc = _now_utc(now)
        elapsed = now_utc - since_dt
        if elapsed < UNHOLD_AFTER:
            rem = UNHOLD_AFTER - elapsed
            rem_hours = rem.total_seconds() / 3600
            rem_str = f"{int(rem_hours)}" if rem_hours == int(rem_hours) else f"{rem_hours:.1f}"
            raise FactoryError(f"hold on {product} is under 24h old: {rem_str} hours remain")
        removed = data["holds"].pop(product)
        save_release(data)
    return removed


def _build_product_status(data: dict, product: str, today_tunis: str, global_paused: bool) -> tuple[dict, list[str]]:
    paused = bool(data.get("paused", {}).get(product) or global_paused)
    hold = data.get("holds", {}).get(product, None)
    verdict = data.get("verdicts", {}).get(today_tunis, {}).get(product, None)
    prod_data = data.get("products", {}).get(product)
    mode = prod_data.get("mode") if prod_data else None
    project = prod_data.get("project") if prod_data else None
    all_prod_journal = [e for e in data.get("journal", []) if e.get("product") == product]
    started_entries = [e for e in all_prod_journal if e.get("event") == "started"]
    last_started = started_entries[-1] if started_entries else None
    prod_journal = all_prod_journal[-5:]
    obj = {
        "paused": paused,
        "hold": hold,
        "verdict": verdict,
        "mode": mode,
        "project": project,
        "lastStarted": last_started,
        "journal": prod_journal,
    }
    lines = [
        f"paused: {'yes' if paused else 'no'}",
        f"hold: {hold['since']} ({hold['issue']})" if hold else "hold: none",
        f"today's verdict: {verdict.get('verdict', 'unknown')}" if verdict else "today's verdict: none",
        "journal:",
    ]
    if prod_journal:
        for e in prod_journal:
            lines.append(f"  {e.get('at')} {e.get('event')} sha={e.get('sha')} note={e.get('note')}")
    else:
        lines.append("  (no journal entries)")
    return obj, lines


def status(product: str | None = None, now: datetime | None = None) -> tuple[dict, str]:
    data = release()
    now_utc = _now_utc(now)
    today_tunis = now_utc.astimezone(TUNIS).strftime("%Y-%m-%d")

    global_paused = bool(data.get("paused", {}).get("*"))

    if product is not None:
        if product not in data.get("products", {}) and product not in data.get("paused", {}):
            raise FactoryError(f"unknown product {product!r}")
        obj, p_lines = _build_product_status(data, product, today_tunis, global_paused)
        lines = [f"product: {product}"] + p_lines
        return obj, "\n".join(lines)
    else:
        products = data.get("products", {})
        prod_statuses = {}
        lines = [
            f"global pause: {'yes' if global_paused else 'no'}",
        ]
        for p in sorted(products.keys()):
            p_obj, p_lines = _build_product_status(data, p, today_tunis, global_paused)
            prod_statuses[p] = p_obj
            lines.append(f"product: {p}")
            lines.extend(f"  {l}" for l in p_lines)
        obj = {
            "paused": global_paused,
            "products": prod_statuses,
        }
        return obj, "\n".join(lines)


def ci_green(product: dict, sha: str | None = None) -> bool:
    repo = product["repo"]
    target_sha = sha or product.get("base", "develop")
    status_res = sources.gh_json("api", f"repos/{repo}/commits/{target_sha}/status") or {}
    statuses_ok = (status_res.get("state") == "success" or status_res.get("total_count", 0) == 0)
    if not statuses_ok:
        return False
    runs = []
    for page in range(1, 11):
        runs_res = sources.gh_json("api", f"repos/{repo}/commits/{target_sha}/check-runs?per_page=100&page={page}") or {}
        page_runs = runs_res.get("check_runs", []) if isinstance(runs_res, dict) else (runs_res or [])
        runs.extend(page_runs)
        if len(page_runs) < 100:
            break
    if not runs:
        return True
    return all(r.get("conclusion") in ("success", "skipped", "neutral") for r in runs)


def _format_queue_lines(q: dict) -> list[str]:
    lines = []
    merges = q.get("merge", [])
    lines.append("Merges today:")
    if merges:
        for m in merges:
            kind_str = f" [{m['kind']}]" if m.get("kind") else ""
            lines.append(f"  - {m['repo_pr']} {m.get('title', '')}{kind_str}")
        lines.append("Reminder: run `factory release queue-recheck <repo_pr>` before each and `factory release queue-done <repo_pr> merged` after.")
    else:
        lines.append("  (none)")

    agents = (q.get("agents") or [])[:3]
    lines.append("Agent jobs:")
    if agents:
        for ag in agents:
            if ag.get("repo_pr"):
                title_str = f": {ag.get('title', '')}" if ag.get("title") else ""
                lines.append(f"  - {ag['repo_pr']} ({ag['job']}){title_str}")
            else:
                lines.append(f"  - {ag['job']}")
    else:
        lines.append("  (none)")

    auto_url = automation_url()
    pending = q.get("pending_actions") or []
    lines.append("Pending actions:")
    if pending:
        for pa in pending:
            act = pa.get("action")
            if act == "close":
                lines.append(f"  - {pa['repo_pr']}: close (comment: {close_comment()})")
            else:
                lines.append(f"  - {pa['repo_pr']}: {act}")
    else:
        lines.append("  (none)")

    asks = q.get("asks_new") if "asks_new" in q else (q.get("asks") or [])
    lines.append("Waiting on you:")
    if asks:
        for ask in asks:
            c = ask.get("classification") or {}
            why = c.get("why")
            why_str = f" ({why})" if why else ""
            repo = ask.get("repo")
            num = ask.get("number")
            prefix = f"{repo}#{num}" if repo else f"#{num}"
            lines.append(f"  - {prefix} {ask.get('title', '')}{why_str}")
        if auto_url:
            lines.append(f"  (decide in Develop queue: {auto_url})")
        else:
            lines.append("  (decide in Develop queue)")
    else:
        lines.append("  (none)")

    return lines


def _queue_today_if_any(name: str, now) -> dict | None:
    """Today's develop-queue work for a product, or None when there is none (or it cannot be read)."""
    data = release()
    prod_cfg = data.get("products", {}).get(name, {})
    if prod_cfg.get("flow") == "factory":
        return None
    now_utc = _now_utc(now)
    today_tunis = now_utc.astimezone(TUNIS).date()
    if today_tunis.weekday() >= 4:  # Fri (4), Sat (5), Sun (6)
        return None
    from . import queue as queue_mod  # queue.py imports this module, so import late
    try:
        q = queue_mod.today(name, now=now)
    except Exception:
        return None
    asks_to_check = q.get("asks_new") if "asks_new" in q else q.get("asks")
    return q if (q.get("merge") or q.get("agents") or q.get("pending_actions") or asks_to_check) else None


def claim_queue_relay(name: str, day: str) -> dict | None:
    """Mark today's queue message for `name` as sent, once. Returns a copy of the verdict, or None when
    there is no verdict for `day` or another run already claimed it."""
    with store.lock():
        cur_state = release()
        v_today = cur_state.get("verdicts", {}).get(day, {}).get(name)
        if not v_today or v_today.get("queue_relayed"):
            return None
        v_today["queue_relayed"] = day
        save_release(cur_state)
        return copy.deepcopy(v_today)


class QueueRelayError(FactoryError):
    """The queue message could not be sent; the claim was released and queueRelayError stored."""


def send_queue_relay(name: str, q_today: dict, day: str, now: datetime | None = None) -> bool:
    """Claim, send and book today's queue message for `name`. False when another run already claimed it.
    On a failed send the claim is released (queue_relayed False, queueRelayError) and the error re-raised."""
    if claim_queue_relay(name, day) is None:
        return False
    prod_cfg = (release().get("products") or {}).get(name) or {}
    try:
        relay.enqueue_message(prod_cfg["thread"], queue_message(name, q_today, now=now), kind="release")
    except Exception as e:
        try:
            with store.lock():
                cur_state = release()
                if name in cur_state.get("verdicts", {}).get(day, {}):
                    cur_state["verdicts"][day][name]["queue_relayed"] = False
                    cur_state["verdicts"][day][name]["queueRelayError"] = str(e)
                    save_release(cur_state)
        except Exception as e2:  # still report the failed send, so the caller's loop goes on
            print(f"could not release the queue relay claim for {name}: {e2}", file=sys.stderr)
        raise QueueRelayError(str(e)) from e
    try:
        _book_queue_relay(name, q_today, day, prod_cfg)
    except Exception as e:  # the message went out: keep the claim, never resend over a bookkeeping error
        print(f"queue relay bookkeeping failed for {name}: {e}", file=sys.stderr)
    return True


def _book_queue_relay(name: str, q_today: dict, day: str, prod_cfg: dict) -> None:
    """After a sent queue message: clear an old send error and remember which asks were sent."""
    with store.lock():
        cur_state = release()
        v = cur_state.get("verdicts", {}).get(day, {}).get(name)
        cleared = v is not None and v.pop("queueRelayError", None) is not None
        if not cleared and not q_today.get("asks_new"):
            return  # nothing to write
        if q_today.get("asks_new"):
            q_asked = cur_state.setdefault("queue_asked", {}).setdefault(name, [])
            repo = prod_cfg.get("repo", "")
            prod_snap = (cur_state.get("queue", {}).get("products") or {}).get(name)
            if prod_snap is not None and "prs" in prod_snap:
                current_nums = {p["number"] for p in prod_snap.get("prs", []) if isinstance(p, dict) and "number" in p}
                def _pr_num(k: str) -> int | None:
                    m = re.search(r"#(\d+)(?::|$)", k)
                    return int(m.group(1)) if m else None
                q_asked = [k for k in q_asked if _pr_num(k) in current_nums]
            for a in q_today.get("asks_new", []):
                st = (a.get("classification") or {}).get("status", "")
                k = f"{repo}#{a['number']}:{st}" if st else f"{repo}#{a['number']}"
                if k not in q_asked:
                    q_asked.append(k)
            cur_state["queue_asked"][name] = q_asked
        save_release(cur_state)


def queue_message(product: str, q: dict, now: datetime | None = None) -> str:
    now_utc = _now_utc(now)
    d_str = now_utc.astimezone(TUNIS).strftime("%Y-%m-%d")
    lines = [
        f"Release manager (factory release queue, {d_str}):",
        f"Product: {product}",
    ]
    lines.extend(_format_queue_lines(q))
    return "\n".join(lines)


def plan_message(product: str, verdict: dict, prs: list[dict], queue_today: dict | None = None) -> str:
    at_str = verdict.get("at")
    dt = _try_parse_utc(at_str) if at_str else None
    d_str = (dt or _now_utc()).astimezone(TUNIS).strftime("%Y-%m-%d")

    v_name = verdict.get("verdict", "")
    lines = [
        f"Release manager (factory release plan, {d_str}):",
        f"Product: {product}",
        f"Verdict: {v_name}",
    ]
    base_sha = verdict.get("baseSha")
    if base_sha:
        lines.append(f"Cut from: {base_sha}")
    reasons = verdict.get("reasons") or []
    if reasons:
        lines.append(f"Reasons: {'; '.join(reasons)}")

    pr_by_num = {p["number"]: p for p in prs}
    ship_nums = verdict.get("ship") or []
    lines.append("Ship:")
    if ship_nums:
        for num in ship_nums:
            p = pr_by_num.get(num, {})
            title = p.get("title", "")
            color = p.get("color", "green")
            score_reasons = p.get("scoreReasons") or []
            s_reasons = f" ({', '.join(score_reasons)})" if score_reasons else ""
            lines.append(f"  - #{num} {title} [{color}]{s_reasons}")
    else:
        lines.append("  (none)")

    held_items = verdict.get("held") or []
    lines.append("Held:")
    if held_items:
        for item in held_items:
            num = item.get("number")
            reason = item.get("reason", "")
            p = pr_by_num.get(num, {})
            title = p.get("title", "")
            t_str = f" {title}" if title else ""
            lines.append(f"  - #{num}{t_str}: {reason}")
    else:
        lines.append("  (none)")

    lines.append(f"Follow the release-manager skill for {product}.")

    if queue_today:
        lines.append("")
        lines.append("Merge these only after the watch window ends.")
        lines.extend(_format_queue_lines(queue_today))

    return "\n".join(lines)


def _working_days_between(d_start, d_end) -> int:
    if d_end <= d_start:
        return 0
    cur = d_start + timedelta(days=1)
    days = 0
    while cur <= d_end:
        if cur.weekday() < 5:
            days += 1
        cur += timedelta(days=1)
    return days


def _has_release_in_flight(journal: list[dict], today_date, verdicts: dict | None = None, product: str | None = None) -> bool:
    events_by_prod: dict[str, list[str]] = {}
    for entry in journal:
        at_str = entry.get("at")
        if not at_str:
            continue
        dt = _try_parse_utc(at_str)
        if dt is None:
            continue
        if dt.astimezone(TUNIS).date() == today_date:
            events_by_prod.setdefault(entry.get("product"), []).append(entry.get("event"))

    if product is not None:
        today_str = today_date.strftime("%Y-%m-%d") if hasattr(today_date, "strftime") else str(today_date)
        today_verdicts = verdicts.get(today_str, {}) if verdicts else {}
        v = today_verdicts.get(product, {})
        if not (v.get("relayed") is True and bool(v.get("ship"))):
            return False
        evts = events_by_prod.get(product, [])
        started_idx = -1
        terminal_idx = -1
        for idx, ev in enumerate(evts):
            if ev == "started":
                started_idx = idx
            elif ev in ("shipped", "rolled-back", "rollback-failed"):
                terminal_idx = idx
        if started_idx != -1:
            return terminal_idx < started_idx
        return terminal_idx == -1

    for prod, evts in events_by_prod.items():
        if prod == "factory":
            continue
        started_idx = -1
        terminal_idx = -1
        for idx, ev in enumerate(evts):
            if ev == "started":
                started_idx = idx
            elif ev in ("shipped", "rolled-back", "rollback-failed"):
                terminal_idx = idx
        if started_idx != -1 and started_idx > terminal_idx:
            return True

    if verdicts:
        today_str = today_date.strftime("%Y-%m-%d") if hasattr(today_date, "strftime") else str(today_date)
        today_verdicts = verdicts.get(today_str, {})
        for prod, v in today_verdicts.items():
            if prod == "factory":
                continue
            if v.get("relayed") is True and bool(v.get("ship")):
                evts = events_by_prod.get(prod, [])
                if not any(ev in ("shipped", "rolled-back", "rollback-failed") for ev in evts):
                    return True

    return False


def plan(now: datetime | None = None, only: str | None = None, dry: bool = False) -> dict:
    now_utc = _now_utc(now)
    today_tunis = now_utc.astimezone(TUNIS).strftime("%Y-%m-%d")
    today_date = now_utc.astimezone(TUNIS).date()
    iso_now = _format_time(now_utc)

    state = release()
    products_cfg = state.get("products", {})

    if only is not None:
        if only not in products_cfg:
            raise FactoryError(f"unknown product {only!r}")
        products_to_process = [only]
    else:
        products_to_process = [p for p in sorted(products_cfg.keys()) if p != "factory"]

    computed_verdicts: dict[str, dict] = {}
    computed_prs: dict[str, list[dict]] = {}
    to_relay: list[str] = []
    to_queue_relay: list[str] = []
    to_ask: list[dict] = []
    new_stale_asked: dict[str, str] = {}

    for name in products_to_process:
        prod_cfg = products_cfg[name]
        today_stored = state.get("verdicts", {}).get(today_tunis, {}).get(name)
        if today_stored and (today_stored.get("relayed") is True or today_stored.get("queue_relayed")):
            computed_verdicts[name] = today_stored
            continue

        if name == "factory" and _has_release_in_flight(state.get("journal", []), today_date, verdicts=state.get("verdicts", {})):
            v = {
                "verdict": "wait",
                "ship": [],
                "held": [],
                "reasons": ["other releases still in flight"],
                "score": "green",
            }
            prs = []
        else:
            try:
                prs, base_sha, unmapped_commits = unreleased_prs(prod_cfg)
                shipped_set = set(state.get("shipped_prs", {}).get(name, []))
                prs = [p for p in prs if p["number"] not in shipped_set]
                red_paths = prod_cfg.get("red_paths", [])
                for pr in prs:
                    color, score_reasons = score_pr(pr, red_paths)
                    pr["color"] = color
                    pr["scoreReasons"] = score_reasons
                ci = ci_green(prod_cfg, base_sha)
                v = decide(prs, state, name, ci, now, unmapped=unmapped_commits, base_sha=base_sha)
            except Exception as err:
                v = {
                    "verdict": "wait",
                    "ship": [],
                    "held": [],
                    "reasons": [f"could not read: {err}"],
                    "score": "green",
                }
                prs = []

        if v.get("ship"):
            try:
                failing = production_failing(name)
            except Exception as err:
                failing = [{"name": "checks", "detail": f"could not run: {err}"}]
            v = {**v, "gate_checked": True}
            if failing:
                names = [f["name"] for f in failing]
                v = {**v, "verdict": "wait", "ship": [], "gate": names,
                     "reasons": [f"production already failing {', '.join(names)}: "
                                 + "; ".join(f"{f['name']} {f['detail']}".strip() for f in failing)]}

        if not dry:
            try:
                from . import queue as queue_mod
                read_ok = not str((v.get("reasons") or [""])[0]).startswith(("could not read", "other releases still in flight"))
                queue_mod.queue(product=name, now=now,
                                dev_by_product={name: [{"number": p["number"], "title": p.get("title", ""),
                                                        "mergedAt": p.get("mergedAt")} for p in prs]} if read_ok else None)
            except Exception as err:
                v["queue_error"] = str(err)

        if today_stored and today_stored.get("queue_relayed"):
            v["queue_relayed"] = today_stored["queue_relayed"]  # keep the once-a-day queue message deduped
        computed_verdicts[name] = v
        computed_prs[name] = prs

        mode = prod_cfg.get("mode")
        thread = prod_cfg.get("thread")
        if not dry and mode == "live" and thread:
            if v.get("ship"):
                to_relay.append(name)
            else:
                if not (today_stored and today_stored.get("queue_relayed")) and _queue_today_if_any(name, now):
                    to_queue_relay.append(name)

        if not dry and mode == "live" and v.get("verdict") == "wait":
            wait_reason = "; ".join(v.get("reasons", []))
            stall_key = f"{name}:stall#{v['stall']}" if v.get("stall") else None
            if stall_key and stall_key not in state.get("stale_asked", {}):
                to_ask.append({
                    "project": prod_cfg.get("project", ""),
                    "kind": "urgent",
                    "title": f"Release {name} by hand: red PR #{v['stall']} cannot ship alone",
                    "short": "Factory release is stuck",
                    "context": wait_reason,
                    "options": [
                        ["manual", "I deploy main by hand after checking the red PR"],
                        ["revert", "Revert the red PR on main so the daily release resumes"],
                    ],
                    "reco": "manual",
                    "why": "Factory deploys all of main, so a red PR merged with others blocks every daily run",
                    "sender": "release",
                    "thread": prod_cfg.get("thread"),
                    "ask_key": stall_key,
                })
                new_stale_asked[stall_key] = today_tunis
            gate_key = f"{name}:gate:{today_tunis}" if v.get("gate") else None
            if gate_key and gate_key not in state.get("stale_asked", {}):
                to_ask.append({
                    "project": prod_cfg.get("project", ""),
                    "kind": "urgent",
                    "title": f"Check {name} production: {', '.join(v['gate'])} failing, release held"[:80],
                    "short": "Release held",
                    "context": wait_reason,
                    "options": [
                        ["wait", "Keep holding: it ships at the next run once the checks pass"],
                        ["fix", "I fix the check or its probe today (token, URL, query)"],
                    ],
                    "reco": "wait",
                    "why": "A watch that starts on a failing check would blame and roll back the release",
                    "sender": "release",
                    "thread": prod_cfg.get("thread"),
                    "ask_key": gate_key,
                })
                new_stale_asked[gate_key] = today_tunis
            for pr in prs:
                if "no-release" in (pr.get("labels") or []):
                    continue
                merged_at = pr.get("mergedAt")
                if not merged_at:
                    continue
                m_dt = _try_parse_utc(merged_at)
                if m_dt is None:
                    continue
                m_date = m_dt.astimezone(TUNIS).date()
                d = _working_days_between(m_date, today_date)
                if d > 3:
                    n = pr["number"]
                    ask_key = f"{name}#{n}"
                    if ask_key not in state.get("stale_asked", {}):
                        to_ask.append({
                            "project": prod_cfg.get("project", ""),
                            "kind": "applied",
                            "title": f"Ship or hold PR #{n} on {name}: waiting {d} days",
                            "short": "Applied by default",
                            "context": wait_reason,
                            "options": [
                                ["wait", "Keep waiting for the next release window"],
                                ["hold", "Hold it: add the no-release label"],
                            ],
                            "reco": "wait",
                            "why": "Release manager keeps waiting by default",
                            "sender": "release",
                            "thread": prod_cfg.get("thread"),
                            "ask_key": ask_key,
                        })
                        new_stale_asked[ask_key] = today_tunis

    if dry:
        return {
            name: (v if ("at" in v and "relayed" in v) else {**v, "at": iso_now, "relayed": False})
            for name, v in computed_verdicts.items()
        }

    marked_ask_keys: set[str] = set()
    with store.lock():
        cur_state = release()
        cur_state.setdefault("verdicts", {}).setdefault(today_tunis, {})
        cur_state.setdefault("stale_asked", {})
        for name, v in computed_verdicts.items():
            existing = cur_state["verdicts"][today_tunis].get(name, {})
            if existing.get("relayed") is True or existing.get("queue_relayed"):
                continue
            new_v = {
                **v,
                "at": iso_now,
                "relayed": False,
            }
            if "queue_relayed" in existing:
                new_v["queue_relayed"] = existing["queue_relayed"]
            cur_state["verdicts"][today_tunis][name] = new_v
        for k, val in new_stale_asked.items():
            if k not in cur_state["stale_asked"]:
                cur_state["stale_asked"][k] = val
                marked_ask_keys.add(k)
        save_release(cur_state)

    for item in to_ask:
        if item["ask_key"] in marked_ask_keys:
            store.add_ask_item(
                project=item["project"],
                kind=item["kind"],
                title=item["title"],
                short=item["short"],
                context=item["context"],
                options=item["options"],
                reco=item["reco"],
                why=item["why"],
                sender=item["sender"],
                thread=item["thread"],
            )

    actually_relayed: set[str] = set()
    for name in to_relay:
        q_today = _queue_today_if_any(name, now)

        with store.lock():
            cur_state = release()
            v_today = cur_state.get("verdicts", {}).get(today_tunis, {}).get(name)
            if not v_today or v_today.get("relayed") is True:
                continue
            already_queue_relayed = bool(v_today.get("queue_relayed"))
            v_today["relayed"] = True
            if q_today and not already_queue_relayed:
                v_today["queue_relayed"] = today_tunis
            save_release(cur_state)
            claimed_v = copy.deepcopy(v_today)

        prod_cfg = products_cfg[name]
        thread = prod_cfg["thread"]
        prs = computed_prs.get(name, [])
        msg = plan_message(name, claimed_v, prs, queue_today=None if already_queue_relayed else q_today)
        try:
            relay.enqueue_message(thread, msg, kind="release")
            actually_relayed.add(name)
            if q_today and not already_queue_relayed and q_today.get("asks_new"):
                with store.lock():
                    cur_state = release()
                    q_asked = cur_state.setdefault("queue_asked", {}).setdefault(name, [])
                    repo = prod_cfg.get("repo", "")
                    prod_snap = (cur_state.get("queue", {}).get("products") or {}).get(name)
                    if prod_snap is not None and "prs" in prod_snap:
                        current_nums = {p["number"] for p in prod_snap.get("prs", []) if isinstance(p, dict) and "number" in p}
                        def _pr_num(k: str) -> int | None:
                            m = re.search(r"#(\d+)(?::|$)", k)
                            return int(m.group(1)) if m else None
                        q_asked = [k for k in q_asked if _pr_num(k) in current_nums]
                    for a in q_today.get("asks_new", []):
                        st = (a.get("classification") or {}).get("status", "")
                        k = f"{repo}#{a['number']}:{st}" if st else f"{repo}#{a['number']}"
                        if k not in q_asked:
                            q_asked.append(k)
                    cur_state["queue_asked"][name] = q_asked
                    save_release(cur_state)
        except Exception as e:
            with store.lock():
                cur_state = release()
                if name in cur_state.get("verdicts", {}).get(today_tunis, {}):
                    cur_state["verdicts"][today_tunis][name]["relayed"] = False
                    if not already_queue_relayed:
                        cur_state["verdicts"][today_tunis][name]["queue_relayed"] = False
                    cur_state["verdicts"][today_tunis][name]["relayError"] = str(e)
                    save_release(cur_state)

    for name in to_queue_relay:
        q_today = _queue_today_if_any(name, now)
        if not q_today:
            continue

        try:
            if send_queue_relay(name, q_today, today_tunis, now=now):
                actually_relayed.add(name)
        except QueueRelayError:
            pass  # send_queue_relay released the claim and stored queueRelayError

    final_state = release()
    stored_today = final_state.get("verdicts", {}).get(today_tunis, {})
    res_verdicts: dict[str, dict] = {}
    for name, v in computed_verdicts.items():
        if name in stored_today:
            res_verdicts[name] = stored_today[name]
        else:
            is_rel = name in actually_relayed
            res_verdicts[name] = {**v, "at": iso_now, "relayed": is_rel}
    return res_verdicts


def _argv(s: str, name: str = "cmd") -> list[str]:
    if not isinstance(s, str) or not s.strip():
        raise FactoryError(f"--{name} is required")
    try:
        parts = shlex.split(s)
    except ValueError as e:
        raise FactoryError(f"bad --{name}: {e}") from e
    if not parts:
        raise FactoryError(f"--{name} is required")
    return parts


def check_add(
    product: str,
    name: str,
    kind: str,
    url: str | None = None,
    cmd: str | None = None,
    baseline_cmd: str | None = None,
) -> tuple[dict, str]:
    if not name or not str(name).strip():
        raise FactoryError("check name is required")
    name = str(name).strip()

    if kind not in ("http", "errors", "business"):
        raise FactoryError(f"unknown check kind {kind!r} (choose from http, errors, business)")

    if kind == "http":
        if not url:
            raise FactoryError("http check requires --url")
        if not (url.startswith("http://") or url.startswith("https://")):
            raise FactoryError(f"http check --url must start with http:// or https://: {url!r}")
        if cmd is not None or baseline_cmd is not None:
            raise FactoryError("http check does not take --cmd or --baseline-cmd")
        entry = {"name": name, "kind": "http", "url": url}
    else:
        if url is not None:
            raise FactoryError(f"{kind} check does not take --url")
        if not cmd or not baseline_cmd:
            raise FactoryError(f"{kind} check requires both --cmd and --baseline-cmd")

        cmd_list = _argv(cmd, "cmd")
        baseline_list = _argv(baseline_cmd, "baseline-cmd")

        entry = {
            "name": name,
            "kind": kind,
            "cmd": cmd_list,
            "baseline_cmd": baseline_list,
        }

    with store.lock():
        data = release()
        if product not in data.get("products", {}):
            raise FactoryError(f"unknown product {product!r}")
        prod = data["products"][product]
        checks = prod.setdefault("checks", [])
        if any(c.get("name") == name for c in checks):
            raise FactoryError(f"check {name!r} already exists for product {product!r}")
        checks.append(entry)
        save_release(data)

    return entry, f"ok: check {name} added to {product}"


SIGNAL_TIMEOUT = 60


def _run_signal_command(cmd: list[str], timeout: float | None = None) -> tuple[bool, int | None, str]:
    if timeout is None:
        timeout = SIGNAL_TIMEOUT
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, None, f"command timed out after {timeout}s"
    except Exception as e:
        return False, None, f"command failed: {e}"

    if res.returncode != 0:
        err_msg = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
        return False, None, f"command exited {res.returncode}: {err_msg}"

    out = res.stdout.strip()
    try:
        val = int(out)
        return True, val, ""
    except ValueError:
        return False, None, f"output not an integer: {out!r}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_http_opener = urllib.request.build_opener(_NoRedirectHandler)


def _result(name: str, kind: str, ok: bool, value, baseline, detail: str) -> dict:
    return {
        "name": name,
        "kind": kind,
        "ok": ok,
        "value": value,
        "baseline": baseline,
        "detail": detail,
    }


def _run_http_check(c: dict) -> dict:
    name = c.get("name", "")
    url = c.get("url", "")
    ok = False
    value = None
    detail = ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "factory-release-check/1.0"})
        with _http_opener.open(req, timeout=10) as resp:
            code = resp.status
            value = code
            ok = code == 200
            detail = "" if ok else f"status {code}"
    except urllib.error.HTTPError as e:
        value = e.code
        if 300 <= e.code < 400:
            loc = e.headers.get("Location", "") if hasattr(e, "headers") and e.headers else ""
            detail = f"redirect {e.code} to {loc}"
        else:
            detail = str(e)
    except urllib.error.URLError as e:
        detail = str(e.reason) if hasattr(e, "reason") else str(e)
    except TimeoutError:
        detail = "timed out"
    except Exception as e:
        detail = str(e)

    return _result(name, "http", ok, value, None, detail)


def _judge_errors(now_val: int, base_val: int) -> tuple[bool, str]:
    # 2x baseline plus 3 standard deviations of a Poisson count, never under 5: short or quiet
    # windows are mostly noise, and 2x alone rolled back ~5-11% of healthy releases at 6-20 errors/h
    limit = round(max(5.0, 2 * base_val + 3 * math.sqrt(max(base_val, 0))), 1)
    if now_val > limit:
        return False, f"{now_val} > {limit:g} (baseline {base_val})"
    return True, ""


def _judge_business(now_val: int, base_val: int) -> tuple[bool, str]:
    if base_val >= 10 and now_val < 0.5 * base_val:
        return False, f"{now_val} < 0.5 * baseline ({base_val})"
    return True, ""


def _run_signal_check(c: dict, judge) -> dict:
    name = c.get("name", "")
    kind = c.get("kind", "")
    cmd = c.get("cmd", [])
    baseline_cmd = c.get("baseline_cmd", [])

    cmd_ok, now_val, cmd_err = _run_signal_command(cmd)
    if not cmd_ok:
        return _result(name, kind, False, now_val, None, f"cmd error: {cmd_err}")

    base_ok, base_val, base_err = _run_signal_command(baseline_cmd)
    if not base_ok:
        return _result(name, kind, False, now_val, base_val, f"baseline_cmd error: {base_err}")

    ok, detail = judge(now_val, base_val)
    return _result(name, kind, ok, now_val, base_val, detail)


def production_failing(product: str) -> list[dict]:
    """Checks that already fail before a release: two samples, both bad. A broken probe (expired
    token, API down) counts too, because the watch would roll the release back on it."""
    first = [c for c in check(product)["checks"] if not c.get("ok")]
    if not first:
        return []
    again = {c["name"] for c in check(product)["checks"] if not c.get("ok")}
    return [{"name": c["name"], "detail": c.get("detail") or ""} for c in first if c["name"] in again]


def check(product: str) -> dict:
    data = release()
    if product not in data.get("products", {}):
        raise FactoryError(f"unknown product {product!r}")
    prod = data["products"][product]
    checks = prod.get("checks", [])

    results = []
    for c in checks:
        kind = c.get("kind")
        if kind == "http":
            res = _run_http_check(c)
        elif kind == "errors":
            res = _run_signal_check(c, _judge_errors)
        elif kind == "business":
            res = _run_signal_check(c, _judge_business)
        else:
            res = _result(
                c.get("name", ""),
                kind,
                False,
                None,
                None,
                f"unknown check kind {kind!r}",
            )
        results.append(res)

    all_ok = all(r["ok"] for r in results)
    return {
        "ok": all_ok,
        "checks": results,
    }


def watch(
    product: str,
    minutes: float | int,
    every: float | int = 300,
) -> tuple[dict, str]:
    data = release()
    if product not in data.get("products", {}):
        raise FactoryError(f"unknown product {product!r}")
    prod = data["products"][product]
    checks = prod.get("checks", [])
    if not checks:
        raise FactoryError(f"no checks configured for {product}")

    # error-count checks (`sweep count`) read this so they count only errors since the watch began
    from .sweep import WATCH_SINCE_ENV
    prev_since = os.environ.get(WATCH_SINCE_ENV)
    os.environ[WATCH_SINCE_ENV] = _now_utc().isoformat()
    try:
        return _watch_loop(product, checks, minutes, every)
    finally:
        if prev_since is None:
            os.environ.pop(WATCH_SINCE_ENV, None)
        else:
            os.environ[WATCH_SINCE_ENV] = prev_since


def _watch_loop(product, checks, minutes, every):
    start = _monotonic()
    deadline = start + (float(minutes) * 60.0)
    samples = 0
    http_streak = 0
    signal_streak = 0
    grace_samples = 0

    log_file = store.path(f"release-watch-{product}.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    while True:
        now = _monotonic()
        if samples > 0 and now >= deadline and http_streak == 0 and signal_streak == 0:
            obj = {"result": "pass", "samples": samples}
            return obj, f"watch pass ({samples} samples)"

        if now >= deadline:
            grace_samples += 1

        res = check(product)
        samples += 1

        chk_list = res.get("checks", [])
        http_checks = [c for c in chk_list if c.get("kind") == "http"]
        signal_checks = [c for c in chk_list if c.get("kind") in ("errors", "business")]

        http_streak = http_streak + 1 if any(not c.get("ok") for c in http_checks) else 0
        signal_streak = signal_streak + 1 if any(not c.get("ok") for c in signal_checks) else 0

        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "at": store.now_iso(),
                    "sample": samples,
                    "ok": res.get("ok", False),
                    "http_streak": http_streak,
                    "signal_streak": signal_streak,
                    "last": res,
                }) + "\n")
        except Exception:
            pass

        kind = "http" if http_streak >= 3 else "signal" if signal_streak >= 2 else None
        if kind:
            obj = {
                "result": "rollback",
                "kind": kind,
                "samples": samples,
                "last": res,
            }
            return obj, f"watch rollback: {kind} failure after {samples} samples"

        now = _monotonic()
        if now >= deadline:
            if http_streak == 0 and signal_streak == 0:
                obj = {"result": "pass", "samples": samples}
                return obj, f"watch pass ({samples} samples)"
            if grace_samples >= GRACE_SAMPLES:
                kind = "http" if http_streak > 0 else "signal"
                obj = {
                    "result": "rollback",
                    "kind": kind,
                    "reason": "still failing after the watch window",
                    "samples": samples,
                    "last": res,
                }
                return obj, f"watch rollback: {kind} failure after {samples} samples"
            _sleep(every)
        else:
            _sleep(min(every, max(0, deadline - now)))


