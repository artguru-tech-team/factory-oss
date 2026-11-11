"""Develop queue: open PR reading, classification, train scheduling, and overrides."""
import re
import time
from datetime import datetime

from . import release, sources, store
from .store import FactoryError

_sleep = time.sleep

TUNIS = release.TUNIS
_parse_utc = release._parse_utc
_try_parse_utc = release._try_parse_utc
_now_utc = release._now_utc


OPEN_PRS_QUERY = """query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $cursor) {
      pageInfo {
        hasNextPage
        endCursor
      }
      nodes {
        number
        title
        baseRefName
        headRefName
        isDraft
        mergeable
        createdAt
        updatedAt
        author {
          login
        }
        labels(first: 50) {
          nodes {
            name
          }
        }
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup {
                state
                contexts(first: 100) {
                  nodes {
                    __typename
                    ... on CheckRun {
                      name
                      conclusion
                    }
                    ... on StatusContext {
                      context
                      state
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}"""

BAD_CONCLUSIONS = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
MAJOR_BUMP_RE = re.compile(r"from\s+v?(\d+)(?:\.\S*)?\s+to\s+v?(\d+)\b", re.IGNORECASE)
FEAT_RE = re.compile(r"^feat", re.IGNORECASE)
DOCS_RE = re.compile(r"^docs", re.IGNORECASE)
REPO_PR_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)$")
RELEASE_HEAD_RE = re.compile(r"^(develop$|release/|release-please--|revert|hotfix/|[^/]+/chore/release-)")
ALLOWED_ACTIONS = {"ok", "front", "hold", "unhold", "agent", "retarget", "close"}


def _split_repo(repo_str: str) -> tuple[str, str]:
    parts = repo_str.strip().split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise FactoryError(f"invalid repo {repo_str!r}")
    return parts[0], parts[1]


def is_bot_login(login: str | None) -> bool:
    if not login:
        return False
    login_lower = login.lower()
    return login_lower.endswith("[bot]") or login_lower == "dependabot"


def open_prs(product: dict) -> list[dict]:
    repo = product.get("repo", "")
    owner, name = _split_repo(repo)

    prs = []
    cursor = None
    has_next = True

    while has_next:
        args = [
            "api", "graphql",
            "-f", f"query={OPEN_PRS_QUERY}",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
        ]
        if cursor:
            args.extend(["-f", f"cursor={cursor}"])

        try:
            res = sources.gh_json(*args)
        except Exception as e:
            raise FactoryError(f"gh failure reading {repo}: {e}") from e

        if not isinstance(res, dict) or not isinstance(res.get("data"), dict):
            errors = res.get("errors") if isinstance(res, dict) else None
            err_msg = str(errors) if errors else "invalid response"
            raise FactoryError(f"gh failure reading {repo}: {err_msg}")

        repo_data = res["data"].get("repository")
        if not repo_data or "pullRequests" not in repo_data:
            raise FactoryError(f"gh failure reading {repo}: repository not found or inaccessible")

        pr_data = repo_data["pullRequests"]
        nodes = pr_data.get("nodes") or []
        for node in nodes:
            if not isinstance(node, dict) or node.get("isDraft"):
                continue

            # Parse author
            author_obj = node.get("author") or {}
            login = author_obj.get("login", "")
            is_bot = is_bot_login(login)

            # Parse labels
            labels_nodes = (node.get("labels") or {}).get("nodes") or []
            labels = [lbl.get("name") for lbl in labels_nodes if isinstance(lbl, dict) and lbl.get("name")]

            # Parse commit & statuses
            commits_nodes = (node.get("commits") or {}).get("nodes") or []
            last_commit = commits_nodes[0].get("commit") if commits_nodes else {}

            status_rollup = last_commit.get("statusCheckRollup") or {}
            contexts_nodes = (status_rollup.get("contexts") or {}).get("nodes") or []

            failing_contexts = 0
            has_pending = False
            review_state = None

            for ctx in contexts_nodes:
                typename = ctx.get("__typename")
                if typename == "CheckRun":
                    conc = (ctx.get("conclusion") or "").upper()
                    if conc in BAD_CONCLUSIONS:
                        failing_contexts += 1
                    elif conc not in ("SUCCESS", "SKIPPED", "NEUTRAL"):
                        has_pending = True
                elif typename == "StatusContext":
                    st = (ctx.get("state") or "").upper()
                    c_name = ctx.get("context", "")
                    if c_name == "factory/review":
                        review_state = st
                    if st in ("FAILURE", "ERROR"):
                        failing_contexts += 1
                    elif st in ("PENDING", "EXPECTED"):
                        has_pending = True

            rollup_state = (status_rollup.get("state") or "").upper()
            if rollup_state in ("FAILURE", "ERROR") and failing_contexts == 0:
                failing_contexts = 1
            elif rollup_state in ("PENDING", "EXPECTED"):
                has_pending = True
            mergeable_state = node.get("mergeable", "UNKNOWN")
            if mergeable_state == "UNKNOWN":
                has_pending = True  # GitHub is still computing it

            prs.append({
                "number": node.get("number"),
                "title": node.get("title", ""),
                "baseRefName": node.get("baseRefName", ""),
                "headRefName": node.get("headRefName", ""),
                "isDraft": node.get("isDraft", False),
                "mergeable": node.get("mergeable", "UNKNOWN"),
                "createdAt": node.get("createdAt", ""),
                "updatedAt": node.get("updatedAt", ""),
                "author": login,
                "is_bot": is_bot,
                "labels": labels,
                "failing_contexts": failing_contexts,
                "has_pending": has_pending,
                "review_state": review_state,
            })

        page_info = pr_data.get("pageInfo") or {}
        has_next = page_info.get("hasNextPage", False)
        cursor = page_info.get("endCursor")

    return prs


def dev_red(prs: list[dict]) -> bool:
    bot_dev_prs = [p for p in prs if p.get("is_bot") and p.get("baseRefName") == "develop"]
    if len(bot_dev_prs) < 5:
        return False
    failing_count = sum(1 for p in bot_dev_prs if p.get("failing_contexts", 0) > 0 or p.get("mergeable") == "CONFLICTING")
    return (failing_count / len(bot_dev_prs)) > 0.80


def classify(
    pr: dict,
    product_dict: dict,
    is_dev_red: bool,
    overrides: dict,
    now: datetime,
) -> dict:
    repo = product_dict.get("repo", "")
    key = f"{repo}#{pr['number']}"
    flags = overrides.get(key) if isinstance(overrides.get(key), dict) else {}

    # Handle explicit hold or close overrides
    if "close" in flags:
        return {
            "group": "held",
            "status": "closed",
            "why": "closed by dashboard action",
            "does": "close",
        }
    if "hold" in flags:
        return {
            "group": "held",
            "status": "hold",
            "why": "held by dashboard action",
            "does": "wait",
        }

    # Wanted base: if flow is "factory", base is product's "base" (defaults to main); else "develop"
    flow = product_dict.get("flow")
    if flow == "factory":
        wanted_base = product_dict.get("base", "main")
    else:
        wanted_base = "develop"

    base_ref = pr.get("baseRefName", "")
    # The release manager's own release, revert and hotfix PRs into main are the release
    # stage, not queue items
    if flow != "factory" and base_ref != wanted_base and RELEASE_HEAD_RE.match(pr.get("headRefName", "")):
        return {"group": "release", "status": "release", "why": "release PR in flight", "does": "release manager ships it"}
    is_bot = pr.get("is_bot", False)
    mergeable = pr.get("mergeable", "UNKNOWN")
    failing_contexts = pr.get("failing_contexts", 0)
    title = pr.get("title", "")
    updated_at_str = pr.get("updatedAt", "")
    review_state = pr.get("review_state")

    now_utc = _now_utc(now)
    updated_at = _parse_utc(updated_at_str) if updated_at_str else now_utc
    idle_days = (now_utc - updated_at).total_seconds() / 86400.0

    # 1. Wrong base: base is not develop (main for Factory)
    if base_ref != wanted_base:
        return {
            "group": "you",
            "status": "base",
            "why": f"base is {base_ref}, not {wanted_base}",
            "does": "retarget or close",
        }

    # 2. Bot, red or conflicting: dependabot/bot PR with failing checks or conflicts
    if is_bot and (failing_contexts > 0 or mergeable == "CONFLICTING"):
        why = "failing checks or conflicts on bot PR"
        if is_dev_red:
            why = "develop is red; waiting for develop fix"
            does = "wait for develop fix"
        else:
            does = "wait for Dependabot rebase"
        return {
            "group": "fix",
            "status": "red" if failing_contexts > 0 else "conf",
            "why": why,
            "does": does,
        }

    # 3. Bot, major bump: `from vX` to `vY`, X differs from Y
    if is_bot:
        m = MAJOR_BUMP_RE.search(title)
        if m and m.group(1) != m.group(2):
            return {
                "group": "you",
                "status": "major",
                "why": f"major bump from v{m.group(1)} to v{m.group(2)}",
                "does": "list for the owner",
            }
        if m and m.group(1) == m.group(2) and m.group(1) != "0":
            # 4. Bot, ready: green patch or minor bump on a 1.x+ major (0.x steps take the review loop)
            # CI green (failing_contexts == 0), no conflicts
            if pr.get("has_pending"):
                return {"group": "bots", "status": "pending", "why": "CI still running", "does": "waits for CI"}
            if failing_contexts == 0 and mergeable != "CONFLICTING":
                return {
                    "group": "bots",
                    "status": "ready",
                    "why": "green dependency bump",
                    "does": "Wednesday batch",
                }

    # 5. Stale: conflicts or red CI, and idle over 60 days
    if (mergeable == "CONFLICTING" or failing_contexts > 0) and idle_days > 60:
        return {
            "group": "you",
            "status": "stale",
            "why": f"broken and idle {int(idle_days)} days",
            "does": "revive or close",
        }

    # 6. Conflicts: mergeable is CONFLICTING
    if mergeable == "CONFLICTING":
        return {
            "group": "fix",
            "status": "conf",
            "why": "merge conflicts",
            "does": "rebase agent",
        }

    # 7. Red CI: failing checks
    if failing_contexts > 0:
        if is_dev_red:
            why = "failing checks (develop is red)"
            does = "wait for develop fix"
        else:
            why = f"{failing_contexts} failing check(s)"
            does = "fix agent"
        return {
            "group": "fix",
            "status": "red",
            "why": why,
            "does": does,
        }

    # 8. Stale docs: title starts with docs, idle over 60 days
    if DOCS_RE.match(title) and idle_days > 60:
        return {
            "group": "you",
            "status": "stale",
            "why": f"docs PR idle {int(idle_days)} days",
            "does": "merge as is or close",
        }

    # CI still running: not green yet, nothing to do but wait
    if pr.get("has_pending"):
        return {"group": "review", "status": "review", "why": "CI still running", "does": "waits for CI"}

    # 9. Needs review: no factory/review success on the head commit
    if review_state != "SUCCESS":
        return {
            "group": "review",
            "status": "review",
            "why": "needs factory/review success",
            "does": "review loop agent",
        }

    # 10. Feature, ready: title starts with feat, CI green, review clean
    if FEAT_RE.match(title):
        if "ok" in flags:
            return {
                "group": "ready",
                "status": "ready",
                "why": "feature approved by the owner",
                "does": "merges on train day",
            }
        return {
            "group": "ready",
            "status": "feat",
            "why": "feature needs owner's OK",
            "does": "merges on train day once OK",
        }

    # 11. Ready: CI green, review clean, no conflicts
    return {
        "group": "ready",
        "status": "ready",
        "why": "CI green and review clean",
        "does": "merges on train day",
    }


def assign_agents(classified_prs: list[dict], overrides: dict | None = None, repo: str = "", today: str = "",
                  slots: int = 3) -> None:
    for p in classified_prs:
        p.pop("agent_today", None)
        p.pop("agent_queued", None)
    # Newest updated first (sent-now first); the top 3 get agent_today, the rest are queued
    agent_prs = [
        p for p in classified_prs
        if p.get("classification", {}).get("does") in ("rebase agent", "fix agent", "review loop agent")
    ]
    overrides = overrides or {}
    def sent(p):
        at = (overrides.get(f"{repo}#{p['number']}") or {}).get("agent")
        return bool(at and today) and _parse_utc(at).astimezone(TUNIS).date().isoformat() == today
    agent_prs.sort(key=lambda p: p.get("updatedAt", ""), reverse=True)
    agent_prs.sort(key=lambda p: not sent(p))  # stable: sent-now first, then newest

    for idx, p in enumerate(agent_prs):
        p["agent_today"] = idx < slots
        if idx >= slots:
            p["agent_queued"] = True


def train(
    classified_prs: list[dict],
    overrides: dict,
    today_tunis_date,
    is_dev_red: bool,
    repo: str = "",
) -> dict:
    if is_dev_red:
        return {
            "blocked": True,
            "reason": "develop is red: 5+ bot PRs with >80% failing",
            "develop_agent": True,
            "days": [[], [], [], []],
            "left": 0,
        }

    # Filter out held PRs
    active_prs = [p for p in classified_prs if p.get("classification", {}).get("group") != "held"]

    # Map overrides for front
    def is_front(p):
        key = f"{repo}#{p['number']}"
        return "front" in (overrides.get(key) or {})

    # Sort active PRs into fixes, features, bot bumps and review PRs
    ready_fixes = []
    features = []
    bot_ready = []
    review_prs = []

    for p in active_prs:
        c = p.get("classification", {})
        group = c.get("group")
        status = c.get("status")
        title = p.get("title", "")

        if group == "bots" and status == "ready":
            bot_ready.append(p)
        elif group == "ready":
            if status == "feat" or FEAT_RE.match(title):
                features.append(p)
            else:
                ready_fixes.append(p)
        elif group == "review":
            review_prs.append(p)

    # Front-moved first, then oldest first
    def pr_sort_key(p):
        front_rank = 0 if is_front(p) else 1
        return (front_rank, p.get("createdAt", ""))

    ready_fixes.sort(key=pr_sort_key)
    features.sort(key=pr_sort_key)
    bot_ready.sort(key=pr_sort_key)
    review_prs.sort(key=pr_sort_key)

    days: list[list[dict]] = [[], [], [], []]  # Mon (0), Tue (1), Wed (2), Thu (3)
    wd = today_tunis_date.weekday() if today_tunis_date else 0
    first = wd if wd < 4 else 0  # Fri, Sat, Sun: next week's train
    open_days = range(first, 4)

    # 1. Ready fixes in batches of 3 oldest first:
    fix_idx = 0
    for day_idx in open_days:
        while fix_idx < len(ready_fixes) and len(days[day_idx]) < 3:
            p_dict = ready_fixes[fix_idx]
            days[day_idx].append({
                "number": p_dict["number"],
                "title": p_dict["title"],
                "kind": "fix",
            })
            fix_idx += 1

    # 2. Each feature alone on its own empty day
    feat_idx = 0
    for day_idx in open_days:
        if len(days[day_idx]) == 0 and feat_idx < len(features):
            p = features[feat_idx]
            entry = {
                "number": p["number"],
                "title": p["title"],
                "kind": "feature",
            }
            if p.get("classification", {}).get("status") == "feat":
                entry["needs_ok"] = True
            days[day_idx].append(entry)
            feat_idx += 1

    # 3. Up to 5 bot-ready PRs as one Wednesday batch
    wednesday_batch = bot_ready[:5]
    if wednesday_batch:
        if 2 in open_days:
            for b in wednesday_batch:
                days[2].append({
                    "number": b["number"],
                    "title": b["title"],
                    "kind": "bot",
                })

    # 4. Review PRs fill later days that have room (<3) and no feature, marked "cond": true
    rev_idx = 0
    for day_idx in (open_days[1:] if wd < 4 else open_days):
        has_feature = any(item.get("kind") == "feature" for item in days[day_idx])
        if not has_feature:
            while len(days[day_idx]) < 3 and rev_idx < len(review_prs):
                p = review_prs[rev_idx]
                days[day_idx].append({
                    "number": p["number"],
                    "title": p["title"],
                    "kind": "review",
                    "cond": True,
                })
                rev_idx += 1

    # left = train-eligible PRs that did not fit this week
    placed = {item["number"] for d in days for item in d}
    left = sum(1 for p in ready_fixes + features + bot_ready + review_prs if p["number"] not in placed)

    return {
        "days": days,
        "left": left,
    }


def reclassify(product: str, now: datetime | None = None, _data: dict | None = None) -> dict:
    now_utc = _now_utc(now)
    today_tunis = now_utc.astimezone(TUNIS).date()

    def _apply(data: dict) -> dict:
        products_cfg = data.get("products", {})
        if product not in products_cfg:
            raise FactoryError(f"unknown product {product!r}")
        prod_cfg = products_cfg[product]
        repo = prod_cfg.get("repo", "")

        snap = data.get("queue")
        if not snap or not isinstance(snap.get("products"), dict):
            return {}
        prod_snap = snap["products"].get(product)
        if not prod_snap:
            return {}

        stored_prs = prod_snap.get("prs", [])
        overrides = data.get("queue_actions", {})
        is_dev_red = dev_red(stored_prs)

        classified = []
        for p in stored_prs:
            c = classify(p, prod_cfg, is_dev_red, overrides, now_utc)
            p_copy = dict(p)
            p_copy["classification"] = c
            p_copy["repo"] = repo
            p_copy["base"] = p.get("baseRefName", p.get("base", ""))
            p_copy["head"] = p.get("headRefName", p.get("head", ""))
            classified.append(p_copy)

        assign_agents(classified, overrides, repo, today_tunis.isoformat(), slots=2 if is_dev_red else 3)
        # the train belongs to the week the snapshot was read in (the view labels it from `at`)
        snap_at = prod_snap.get("at") or snap.get("at")
        plan_day = _parse_utc(snap_at).astimezone(TUNIS).date() if snap_at else today_tunis
        train_res = train(classified, overrides, plan_day, is_dev_red, repo=repo)

        prod_snap["prs"] = classified
        prod_snap["train"] = train_res
        return prod_snap

    if _data is not None:
        return _apply(_data)

    with store.lock():
        data = release.release()
        res = _apply(data)
        release.save_release(data)
        return res


def _parse_repo_pr(repo_pr: str) -> tuple[str, int]:
    m = REPO_PR_RE.match(repo_pr.strip())
    if not m:
        raise FactoryError(f"invalid repo#n format: {repo_pr!r} (expected owner/repo#number)")
    return m.group(1), int(m.group(2))


def record_queue_action(repo_pr: str, action: str, now: datetime | None = None) -> dict:
    repo, num = _parse_repo_pr(repo_pr)

    if action not in ALLOWED_ACTIONS:
        raise FactoryError(f"unknown action {action!r} (choose from {', '.join(sorted(ALLOWED_ACTIONS))})")

    now_utc = _now_utc(now)
    iso_now = release._format_time(now_utc)

    with store.lock():
        data = release.release()
        # Validate that repo belongs to a configured product (case-insensitively)
        products = data.get("products", {})
        prod_entry = next(((name, p) for name, p in products.items() if p.get("repo", "").lower() == repo.lower()), None)
        if not prod_entry:
            raise FactoryError(f"repo {repo!r} does not belong to any configured product")
        prod_name, prod_cfg = prod_entry
        canonical_repo = prod_cfg.get("repo", repo)

        actions = data.setdefault("queue_actions", {})
        key = f"{canonical_repo}#{num}"

        flags = actions.get(key) if isinstance(actions.get(key), dict) else {}
        if action == "unhold":
            removed = any([flags.pop("hold", None) is not None, flags.pop("close", None) is not None])
        else:
            flags[action] = iso_now
            if "applied" in flags and isinstance(flags["applied"], dict):
                flags["applied"].pop(action, None)
                if not flags["applied"]:
                    flags.pop("applied", None)
            removed = False
        if flags:
            actions[key] = flags
        else:
            actions.pop(key, None)

        if data.get("queue", {}).get("products", {}).get(prod_name):
            reclassify(prod_name, now=now_utc, _data=data)

        release.save_release(data)
        return {"repo_pr": key, "action": action, "flags": flags, "removed": removed}


def queue(
    product: str | None = None,
    now: datetime | None = None,
    dev_by_product: dict | None = None,
) -> tuple[dict, str]:
    now_utc = _now_utc(now)
    iso_now = release._format_time(now_utc)
    today_tunis = now_utc.astimezone(TUNIS).date()

    data = release.release()
    products_cfg = data.get("products", {})

    if product is not None:
        if product not in products_cfg:
            raise FactoryError(f"unknown product {product!r}")
        target_products = [product]
    else:
        target_products = list(products_cfg)

    overrides = data.get("queue_actions", {})

    products_out = {}
    lines = []

    for name in target_products:
        prod_cfg = products_cfg[name]
        repo = prod_cfg.get("repo", "")
        error_msg = None
        prs_list = []
        train_res = {}

        try:
            raw_prs = open_prs(prod_cfg)
            is_dev_red = dev_red(raw_prs)

            classified = []
            for p in raw_prs:
                c = classify(p, prod_cfg, is_dev_red, overrides, now_utc)
                p_copy = dict(p)
                p_copy["classification"] = c
                p_copy["repo"] = repo
                p_copy["base"] = p.get("baseRefName", "")
                p_copy["head"] = p.get("headRefName", "")
                classified.append(p_copy)

            # a red develop gets one fix agent of its own, out of the day's 3
            assign_agents(classified, overrides, repo, today_tunis.isoformat(), slots=2 if is_dev_red else 3)
            train_res = train(classified, overrides, today_tunis, is_dev_red, repo=repo)
            prs_list = classified
        except Exception as e:
            error_msg = str(e)

        # Pipeline data
        open_count = sum(1 for p in prs_list if not p.get("is_bot") and p.get("classification", {}).get("group") != "release")
        dev_prs = []
        pipe_err = None

        if error_msg is not None:
            # Skip the pipe dev lookup entirely when open_prs failed for that product
            dev_prs = []
        elif dev_by_product is not None and name in dev_by_product:
            dev_prs = [
                {
                    "number": p["number"],
                    "title": p.get("title", ""),
                    "mergedAt": p.get("mergedAt", ""),
                }
                for p in dev_by_product[name]
            ]
        else:
            try:
                unreleased, _, _ = release.unreleased_prs(prod_cfg)
                shipped_set = set(data.get("shipped_prs", {}).get(name, []))
                unreleased = [p for p in unreleased if p["number"] not in shipped_set]
                dev_prs = [
                    {
                        "number": p["number"],
                        "title": p.get("title", ""),
                        "mergedAt": p.get("mergedAt", ""),
                    }
                    for p in unreleased
                ]
            except Exception as e:
                pipe_err = str(e)

        rel_prs = [
            {"number": p["number"], "title": p.get("title", "")}
            for p in prs_list
            if p.get("classification", {}).get("group") == "release"
        ]

        # prod: last shipped journal entry for product from release.release()["journal"] ({at, sha})
        prod_entry = None
        for j in reversed(data.get("journal", [])):
            if j.get("product") == name and j.get("event") == "shipped":
                prod_entry = {"at": j.get("at"), "sha": j.get("sha")}
                break

        # health: None = no check ran today, [] = the gate ran and passed, names = the gate held the release
        prod_verdict = (data.get("verdicts", {}).get(today_tunis.isoformat(), {}) or {}).get(name) or {}
        if prod_verdict.get("gate"):
            health = list(prod_verdict["gate"])
        elif prod_verdict.get("gate_checked"):
            health = []
        else:
            health = None

        pipe = {
            "open": open_count,
            "dev": dev_prs,
            "rel": rel_prs,
            "prod": prod_entry,
            "health": health,
        }
        if pipe_err:
            pipe["error"] = pipe_err

        products_out[name] = {
            "at": iso_now,
            "prs": prs_list,
            "train": train_res,
            "pipe": pipe,
            "error": error_msg,
        }

        # Format text output per product
        lines.append(f"product: {name} ({repo})")
        if error_msg:
            lines.append(f"  error: {error_msg}")
        else:
            # Train summary
            days_labels = ["Mon", "Tue", "Wed", "Thu"]
            if train_res.get("blocked"):
                lines.append(f"  train: BLOCKED ({train_res.get('reason')})")
            else:
                train_days = train_res.get("days", [[], [], [], []])
                for idx, day_prs in enumerate(train_days):
                    d_name = days_labels[idx]
                    if day_prs:
                        items_str = ", ".join(f"#{item['number']}" + ("(cond)" if item.get("cond") else "") + ("(needs-ok)" if item.get("needs_ok") else "") for item in day_prs)
                        lines.append(f"  {d_name}: {items_str}")
                    else:
                        lines.append(f"  {d_name}: (empty)")
                lines.append(f"  left: {train_res.get('left', 0)}")

            # Groups summary count
            counts: dict[str, int] = {}
            for p in prs_list:
                g = p.get("classification", {}).get("group", "unknown")
                counts[g] = counts.get(g, 0) + 1
            groups_order = ["ready", "you", "review", "fix", "bots", "held", "release"]
            counts_str = ", ".join(f"{g}: {counts.get(g, 0)}" for g in groups_order if counts.get(g, 0) > 0)
            lines.append(f"  counts: {counts_str or 'none'}")

    snapshot = {
        "at": iso_now,
        "products": products_out,
    }

    # Save to release.json["queue"]
    with store.lock():
        cur_data = release.release()
        if product is not None and isinstance(cur_data.get("queue"), dict):
            prev = dict(cur_data["queue"].get("products") or {})
            prev.update(products_out)
            snapshot = {"at": iso_now, "products": prev}
        cur_data["queue"] = snapshot
        # dashboard actions recorded while this run read GitHub are re-applied, not overwritten
        for pname in products_out:
            if pname in (cur_data.get("products") or {}):
                reclassify(pname, now=now_utc, _data=cur_data)
        release.save_release(cur_data)

    return snapshot, "\n".join(lines)


RECHECK_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: "refs/heads/develop") {
      target {
        ... on Commit {
          statusCheckRollup {
            state
          }
        }
      }
    }
    pullRequest(number: $number) {
      number
      title
      state
      baseRefName
      headRefOid
      isDraft
      mergeable
      author {
        login
      }
      labels(first: 50) {
        nodes {
          name
        }
      }
      commits(last: 1) {
        nodes {
          commit {
            oid
            author {
              user {
                login
              }
            }
            committer {
              email
              user {
                login
              }
            }
            signature {
              isValid
            }
            statusCheckRollup {
              state
              contexts(first: 100) {
                nodes {
                  __typename
                  ... on CheckRun {
                    name
                    conclusion
                  }
                  ... on StatusContext {
                    context
                    state
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}"""


def today(product: str, now: datetime | None = None) -> dict:
    data = release.release()
    products_cfg = data.get("products", {})
    if product not in products_cfg:
        raise FactoryError(f"unknown product {product!r}")

    prod_cfg = products_cfg[product]
    repo = prod_cfg.get("repo", "")
    flow = prod_cfg.get("flow", "")
    mode = prod_cfg.get("mode", "shadow")

    now_utc = _now_utc(now)
    today_tunis = now_utc.astimezone(TUNIS).date()
    wd = today_tunis.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun

    queue_data = data.get("queue") or {}
    prod_snap = (queue_data.get("products") or {}).get(product) or {}
    snapshot_at = prod_snap.get("at") or queue_data.get("at")

    snap_dt = _try_parse_utc(snapshot_at) if snapshot_at else None
    snap_tunis_date = snap_dt.astimezone(TUNIS).date() if snap_dt else None
    has_error = bool(prod_snap.get("error"))
    is_fresh = bool(snap_tunis_date and snap_tunis_date == today_tunis and not has_error)

    train_data = prod_snap.get("train") or {}
    prs = prod_snap.get("prs") or []
    blocked = bool(train_data.get("blocked", False))

    # Merge items for today's train cell (Tunis date; none on Fri-Sun)
    # Factory (flow "factory") never gets merge items (its own flow merges into main).
    # No merge and no agents unless fresh (Tunis date is today and no error)
    merge = []
    if is_fresh and flow != "factory" and wd < 4 and not blocked:
        days = train_data.get("days") or [[], [], [], []]
        if wd < len(days):
            for item in days[wd]:
                num = item.get("number")
                merge.append({
                    "repo_pr": f"{repo}#{num}",
                    "number": num,
                    "title": item.get("title", ""),
                    "kind": item.get("kind", ""),
                })

    # Agents: count today's queue-agent-sent events from queue_log
    # and dashboard agent relays today
    # at most 3 per product per day including develop-fix
    agents = []
    if is_fresh:
        sent_keys_today = set()
        sent_today = 0
        for entry in data.get("queue_log", []):
            if entry.get("product") == product and entry.get("what") in ("agent-sent", "queue-agent-sent"):
                entry_at = entry.get("at")
                if entry_at and _parse_utc(entry_at).astimezone(TUNIS).date() == today_tunis:
                    sent_today += 1
                    if entry.get("repo_pr"):
                        sent_keys_today.add(entry.get("repo_pr"))

        actions_data = data.get("queue_actions") or {}
        for key, flags in actions_data.items():
            if isinstance(flags, dict) and "agent_relayed" in flags:
                m = REPO_PR_RE.match(key)
                if m and m.group(1).lower() == repo.lower():
                    relayed_at = flags.get("agent_relayed")
                    if relayed_at and _try_parse_utc(relayed_at) and _try_parse_utc(relayed_at).astimezone(TUNIS).date() == today_tunis:
                        if key not in sent_keys_today:
                            sent_today += 1
                            sent_keys_today.add(key)

        remaining_slots = max(0, 3 - sent_today)
        has_dev_fix = bool(train_data.get("develop_agent"))
        dev_fix_already_sent = f"{product}:develop-fix" in sent_keys_today
        needs_dev_fix = has_dev_fix and not dev_fix_already_sent
        dev_fix_slot = 1 if (needs_dev_fix and remaining_slots > 0) else 0
        pr_slots = max(0, remaining_slots - dev_fix_slot)

        pr_agents = []
        for p in prs:
            if p.get("agent_today"):
                key = f"{repo}#{p['number']}"
                if key in sent_keys_today:
                    continue
                if len(pr_agents) >= pr_slots:
                    break
                c = p.get("classification") or {}
                does = c.get("does", "")
                if "rebase" in does:
                    job = "rebase"
                elif "fix" in does:
                    job = "fix"
                elif "review" in does:
                    job = "review"
                else:
                    job = does
                pr_agents.append({
                    "repo_pr": key,
                    "number": p["number"],
                    "title": p.get("title", ""),
                    "job": job,
                })
        agents.extend(pr_agents)
        if dev_fix_slot:
            agents.append({"job": "develop-fix"})

    # Pending actions: for queue_actions flags retarget/close not yet applied
    actions_data = data.get("queue_actions") or {}
    pending_actions = []
    for key, flags in actions_data.items():
        if isinstance(flags, dict):
            m = REPO_PR_RE.match(key)
            if m and m.group(1).lower() == repo.lower():
                applied = flags.get("applied") if isinstance(flags.get("applied"), dict) else {}
                for act in ("retarget", "close"):
                    if act in flags and act not in applied:
                        pending_actions.append({"repo_pr": key, "action": act})
    pending_actions.sort(key=lambda x: (x["repo_pr"], x["action"]))

    # Asks: PRs in group "you"
    asks = [p for p in prs if (p.get("classification") or {}).get("group") == "you"]
    asked_keys = set((data.get("queue_asked") or {}).get(product) or [])
    asks_new = []
    for a in asks:
        num = a.get("number")
        c = a.get("classification") or {}
        st = c.get("status")
        k_with_status = f"{repo}#{num}:{st}" if st else None
        k_plain = f"{repo}#{num}"
        is_asked = False
        if k_with_status and k_with_status in asked_keys:
            is_asked = True
        elif not k_with_status and (k_plain in asked_keys or str(num) in asked_keys or num in asked_keys):
            is_asked = True
        if not is_asked:
            asks_new.append(a)

    res = {
        "product": product,
        "merge": merge,
        "agents": agents,
        "pending_actions": pending_actions,
        "asks": asks,
        "asks_new": asks_new,
        "blocked": blocked,
        "mode": mode,
        "snapshot_at": snapshot_at,
    }
    if not is_fresh:
        res["stale"] = True
        res["snapshot_date"] = snap_tunis_date.isoformat() if snap_tunis_date else None
    return res


def recheck(repo_pr: str, now: datetime | None = None) -> tuple[bool, str, str]:
    repo, number = _parse_repo_pr(repo_pr)

    data = release.release()
    products = data.get("products", {})
    prod_name = None
    canonical_repo = None
    for name, p in products.items():
        if p.get("repo", "").lower() == repo.lower():
            prod_name = name
            canonical_repo = p.get("repo")
            break
    if not prod_name or not canonical_repo:
        return False, f"unknown repo {repo!r}", ""

    prod_cfg = products[prod_name]
    mode = prod_cfg.get("mode", "shadow")
    if mode != "live":
        return False, f"product {prod_name} is in shadow mode", ""

    flow = prod_cfg.get("flow", "")
    if flow == "factory":
        return False, "flow is factory", ""

    now_utc = _now_utc(now)
    today_tunis = now_utc.astimezone(TUNIS).date()
    if today_tunis.weekday() >= 4:
        return False, "no queue merges on Friday-Sunday", ""

    global_paused = bool(data.get("paused", {}).get("*"))
    if bool(data.get("paused", {}).get(prod_name) or global_paused):
        return False, f"product {prod_name} is paused", ""

    hold = data.get("holds", {}).get(prod_name)
    if hold:
        return False, f"product {prod_name} is on hold ({hold.get('issue', 'held')})", ""

    if release._has_release_in_flight(data.get("journal", []), today_tunis, verdicts=data.get("verdicts", {}), product=prod_name):
        return False, "release in flight", ""

    today_data = today(prod_name, now=now_utc)
    if not any(m.get("number") == number for m in today_data.get("merge", [])):
        return False, f"PR {canonical_repo}#{number} is not on today's merge list", ""

    owner, name = _split_repo(canonical_repo)

    res = None
    pr = None
    mergeable = None
    head_sha = ""
    repo_data = {}

    for attempt in range(4):
        if attempt > 0:
            _sleep(5)
        try:
            res = sources.gh_json(
                "api", "graphql",
                "-f", f"query={RECHECK_QUERY}",
                "-f", f"owner={owner}",
                "-f", f"name={name}",
                "-F", f"number={number}",
            )
        except Exception as e:
            raise FactoryError(f"gh failure reading {canonical_repo}#{number}: {e}") from e

        if not isinstance(res, dict) or not isinstance(res.get("data"), dict):
            errors = res.get("errors") if isinstance(res, dict) else None
            err_msg = str(errors) if errors else "invalid response"
            raise FactoryError(f"gh failure reading {canonical_repo}#{number}: {err_msg}")

        repo_data = res["data"].get("repository") if isinstance(res["data"], dict) else {}
        pr = repo_data.get("pullRequest") if isinstance(repo_data, dict) else None
        if not pr:
            return False, f"PR {canonical_repo}#{number} not found", ""

        mergeable = (pr.get("mergeable") or "UNKNOWN").upper()
        if mergeable != "UNKNOWN":
            break

    head_sha = pr.get("headRefOid") or ""
    commits_nodes = (pr.get("commits") or {}).get("nodes") or []
    last_commit = commits_nodes[0].get("commit") if commits_nodes else {}
    if not head_sha and last_commit.get("oid"):
        head_sha = last_commit.get("oid")
    if not head_sha:
        return False, "head commit unknown", ""
    if last_commit.get("oid") and last_commit.get("oid") != head_sha:
        # CI and review were read from a different commit than the head: never vouch for it
        return False, "head moved while reading checks, retry", head_sha

    dev_ref = repo_data.get("ref")
    if not dev_ref:
        return False, "develop CI not green: missing ref", head_sha
    dev_target = dev_ref.get("target") if isinstance(dev_ref, dict) else None
    dev_rollup = dev_target.get("statusCheckRollup") if isinstance(dev_target, dict) else None
    dev_state = dev_rollup.get("state") if isinstance(dev_rollup, dict) else None
    if not dev_state:
        return False, "develop CI not green: empty", head_sha
    dev_state_str = str(dev_state).upper()
    if dev_state_str != "SUCCESS":
        return False, f"develop CI not green: {dev_state_str}", head_sha

    # 1. PR open
    state = (pr.get("state") or "").upper()
    if state != "OPEN":
        return False, f"PR is {state.lower()}, not open", head_sha

    # 2. Base is develop
    base_ref = pr.get("baseRefName", "")
    if base_ref != "develop":
        return False, f"base is {base_ref}, not develop", head_sha

    # 3. Not draft
    if pr.get("isDraft"):
        return False, "PR is draft", head_sha

    # 4. Mergeable MERGEABLE
    if mergeable != "MERGEABLE":
        return False, f"mergeable is {mergeable}", head_sha

    # 5. Check author bot
    author_obj = pr.get("author") or {}
    author_login = author_obj.get("login", "")
    is_bot = is_bot_login(author_login)

    # 6. CI rollup state
    status_rollup = last_commit.get("statusCheckRollup") or {}
    rollup_state = (status_rollup.get("state") or "").upper()
    if rollup_state in ("FAILURE", "ERROR"):
        return False, f"checks failing (rollup: {rollup_state})", head_sha
    if rollup_state in ("PENDING", "EXPECTED"):
        return False, f"checks pending (rollup: {rollup_state})", head_sha
    if rollup_state != "SUCCESS":
        return False, f"checks {rollup_state.lower()} (rollup: {rollup_state})", head_sha

    title = pr.get("title", "")
    m = MAJOR_BUMP_RE.search(title)
    if is_bot and m and m.group(1) != m.group(2):
        return False, f"major bump from v{m.group(1)} to v{m.group(2)}", head_sha

    commit_author_obj = last_commit.get("author") or {}
    commit_author_user = commit_author_obj.get("user") or {}
    commit_author_login = commit_author_user.get("login")

    commit_committer_obj = last_commit.get("committer") or {}
    commit_committer_user = commit_committer_obj.get("user") or {}
    commit_committer_login = commit_committer_user.get("login")

    # 0.x versions: a minor step (0.3 -> 0.4) can break, so only 1.x+ same-major bumps count
    is_single_bump = bool(m and m.group(1) == m.group(2) and m.group(1) != "0")
    # GitHub signs Dependabot's commits: committer is "GitHub <noreply@github.com>" with no user.
    # Anyone else's push changes the author, so the author check carries the weight.
    committer_is_github = (not commit_committer_login
                           and (commit_committer_obj.get("email") or "").lower() == "noreply@github.com"
                           and bool((last_commit.get("signature") or {}).get("isValid")))
    commit_bot_ok = bool(commit_author_login and is_bot_login(commit_author_login) and
                         ((commit_committer_login and is_bot_login(commit_committer_login)) or committer_is_github))
    bot_path = bool(is_bot and is_single_bump and commit_bot_ok)

    if not bot_path:
        # factory/review status SUCCESS on the head
        contexts_nodes = (status_rollup.get("contexts") or {}).get("nodes") or []
        review_state = None
        for ctx in contexts_nodes:
            if ctx.get("__typename") == "StatusContext" and ctx.get("context") == "factory/review":
                review_state = (ctx.get("state") or "").upper()
                break
        if review_state != "SUCCESS":
            return False, f"factory/review is {review_state or 'missing'}, needs SUCCESS", head_sha

    # No no-release label
    labels_nodes = (pr.get("labels") or {}).get("nodes") or []
    labels = [lbl.get("name") for lbl in labels_nodes if isinstance(lbl, dict) and lbl.get("name")]
    if "no-release" in labels:
        return False, "has no-release label", head_sha

    # Not held/closed in queue_actions (lookup by canonical key)
    actions = data.get("queue_actions", {})
    canonical_key = f"{canonical_repo}#{number}"
    flags = actions.get(canonical_key) if isinstance(actions.get(canonical_key), dict) else {}
    if "hold" in flags:
        return False, "held in queue_actions", head_sha
    if "close" in flags:
        return False, "closed in queue_actions", head_sha

    # For a feature (title ^feat) an ok flag
    if not is_bot and FEAT_RE.match(title) and "ok" not in flags:
        return False, "feature needs ok flag", head_sha

    if bot_path:
        return True, "ok (bot: review status to be posted by the release manager)", head_sha

    return True, "ok", head_sha


def queue_done(
    repo_pr: str,
    what: str,
    note: str = "",
    now: datetime | None = None,
    develop_fix: bool = False,
) -> dict:
    valid_whats = ("merged", "retargeted", "closed", "agent-sent")
    if what not in valid_whats:
        raise FactoryError(f"unknown what {what!r} (choose from {', '.join(valid_whats)})")

    data = release.release()
    products = data.get("products", {})

    is_dev_fix = develop_fix or repo_pr.endswith(":develop-fix") or (repo_pr in products and what == "agent-sent")

    if is_dev_fix:
        prod_name = repo_pr[:-12] if repo_pr.endswith(":develop-fix") else repo_pr
        if prod_name not in products:
            raise FactoryError(f"unknown product {prod_name!r}")
        if what != "agent-sent":
            raise FactoryError("only agent-sent is allowed for develop-fix")
        canonical_key = f"{prod_name}:develop-fix"
        prs_list = []
    else:
        repo, number = _parse_repo_pr(repo_pr)

        prod_name = None
        canonical_repo = None
        for name, p_cfg in products.items():
            if p_cfg.get("repo", "").lower() == repo.lower():
                prod_name = name
                canonical_repo = p_cfg.get("repo")
                break
        if not prod_name or not canonical_repo:
            raise FactoryError(f"repo {repo!r} does not belong to any configured product")

        canonical_key = f"{canonical_repo}#{number}"
        prs_list = [number]

        if what == "merged":
            try:
                pr_info = sources.gh_json("api", f"repos/{canonical_repo}/pulls/{number}")
            except Exception as e:
                raise FactoryError(f"gh failure checking {canonical_key}: {e}") from e
            if not (isinstance(pr_info, dict) and (pr_info.get("merged") is True or pr_info.get("merged_at"))):
                raise FactoryError(f"PR {canonical_key} is not merged on GitHub")

    now_utc = _now_utc(now)
    iso_now = release._format_time(now_utc)
    tunis_date = now_utc.astimezone(TUNIS).date().isoformat()

    with store.lock():
        cur_data = release.release()
        queue_log = cur_data.setdefault("queue_log", [])

        # Dedupe on (what, canonical_key, tunis_date)
        for item in queue_log:
            item_date = _parse_utc(item["at"]).astimezone(TUNIS).date().isoformat() if item.get("at") else None
            if (
                item.get("what") == what
                and item.get("repo_pr") == canonical_key
                and item_date == tunis_date
            ):
                return item

        entry = {
            "at": iso_now,
            "product": prod_name,
            "repo_pr": canonical_key,
            "prs": prs_list,
            "what": what,
            "event": f"queue-{what}",
            "note": note or "",
        }
        queue_log.append(entry)
        if len(queue_log) > 1000:
            queue_log = queue_log[-1000:]
        cur_data["queue_log"] = queue_log

        if what in ("retargeted", "closed"):
            flag = "retarget" if what == "retargeted" else "close"
            actions = cur_data.setdefault("queue_actions", {})
            pr_flags = actions.setdefault(canonical_key, {})
            applied = pr_flags.setdefault("applied", {})
            applied[flag] = iso_now
            entry["applied"] = {flag: iso_now}

        release.save_release(cur_data)
        return entry
