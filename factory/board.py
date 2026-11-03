"""The factory board: one board across every product's repos, columns from GitHub state.

Columns come from an issue's labels and its linked PRs (read from GitHub), never from drag and
drop. Writes (promote / close / send back) go through `gh` and are validated against the
products' repos. The last good result is cached in board-cache.json, so a `gh` outage shows the
cached time ("Last good data HH:MM") instead of a blank board.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from . import sources, store
from .store import FactoryError

BOARD_REL = "board.json"
CACHE_REL = "board-cache.json"

# The labels that put an issue on the board (the search qualifier is their OR).
BOARD_LABELS = ("intake", "factory", "needs-decision", "building")

# source:* labels -> the source key shown on the card (anything else is "manual").
SOURCE_LABELS = {"source:sentry": "sentry", "source:signoz": "signoz", "source:bug": "bug", "source:support": "support"}

MERGE_WINDOW = timedelta(hours=24)   # "merged today"
STUCK_WINDOW = timedelta(hours=48)   # no update in triage/building -> stuck
SEARCH_PAGE = 100                    # per_page; the search API caps at 1000 results

# Every product repo needs these labels for the board to work (checked by `factory doctor`).
REQUIRED_LABELS = ("intake", "factory", "needs-decision", "building")

FACTORY_LABEL_DESC = "Approved for the factory lane"
FACTORY_LABEL_COLOR = "5E6AD2"


class BoardError(FactoryError):
    """A board write refused: carries the HTTP status serve should answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------- reading ----------


def _err(e):
    return str(e).splitlines()[0] if str(e) else type(e).__name__


def _repo_product(products):
    return {r: key for key, p in products.items() for r in p.get("repos", [])}


def _repo_from_url(repository_url):
    # https://api.github.com/repos/owner/repo -> owner/repo
    return "/".join((repository_url or "").rstrip("/").split("/")[-2:])


def _labels(item):
    out = set()
    for l in item.get("labels") or []:
        if isinstance(l, dict) and l.get("name"):
            out.add(l["name"])
        elif isinstance(l, str) and l:
            out.add(l)
    return out


def _search(q):
    """One gh api search/issues query, paginated. Raises FactoryError on any gh failure."""
    out = []
    for page in range(1, 11):  # the search API caps at 1000 results
        url = "search/issues?" + urlencode({"q": q, "per_page": str(SEARCH_PAGE), "page": str(page)})
        data = sources.gh_json("api", url)
        batch = data.get("items") or []
        out.extend(batch)
        if len(batch) < SEARCH_PAGE:
            break
    return out


def search_issues(products):
    """Every board-labelled issue, one search per repo (not per product), each in its own try:
    a failing repo (422, 404, renamed) is skipped and recorded. Returns (items, repo_errors, ok)
    where ok is the set of repos whose search succeeded."""
    closed_floor = (datetime.now(timezone.utc) - MERGE_WINDOW).date().isoformat()
    label_q = "label:" + ",".join(BOARD_LABELS)
    items, repo_errors, ok = [], {}, set()
    for repo in _repo_product(products):
        repo_q = f"repo:{repo} is:issue"
        try:
            items.extend(_search(f"{repo_q} is:open {label_q}"))
            items.extend(_search(f"{repo_q} is:closed {label_q} closed:>={closed_floor}"))
            ok.add(repo)
        except FactoryError as e:
            repo_errors[repo] = _err(e)
    return items, repo_errors, ok


PR_QUERY = (
    "query($o:String!,$m:String!){"
    "open: search(query:$o,type:ISSUE,first:100){"
    "nodes{... on PullRequest{number isDraft url closingIssuesReferences(first:20){nodes{number repository{nameWithOwner}}}}}"
    "pageInfo{hasNextPage endCursor}}"
    "merged: search(query:$m,type:ISSUE,first:100){"
    "nodes{... on PullRequest{number mergedAt url closingIssuesReferences(first:20){nodes{number repository{nameWithOwner}}}}}"
    "pageInfo{hasNextPage endCursor}}"
    "}"
)


def prs_for_repo(repo, now=None):
    """Open PRs (with the issues their body closes, keyed by repo+number) and merged PRs of the
    last 24 h, for one repo."""
    now = now or datetime.now(timezone.utc)
    merged_floor = (now - MERGE_WINDOW).date().isoformat()
    data = sources.gh_json(
        "api", "graphql",
        "-f", "query=" + PR_QUERY,
        "-f", f"o=repo:{repo} is:pr is:open",
        "-f", f"m=repo:{repo} is:pr is:merged merged:>={merged_floor}",
    )
    if data.get("errors"):
        raise FactoryError("gh graphql: " + "; ".join(e.get("message", "") for e in data["errors"]))
    d = data.get("data") or {}
    out = {"open": {}, "merged": {}}

    def closes(node):
        refs = []
        for c in (node.get("closingIssuesReferences") or {}).get("nodes") or []:
            if c and c.get("number"):
                refs.append((((c.get("repository") or {}).get("nameWithOwner")) or "", c["number"]))
        return refs

    for node in (d.get("open") or {}).get("nodes") or []:
        if node and node.get("number"):
            out["open"][node["number"]] = {"isDraft": bool(node.get("isDraft")),
                                           "url": node.get("url"), "closes": closes(node)}
    for node in (d.get("merged") or {}).get("nodes") or []:
        if node and node.get("number"):
            out["merged"][node["number"]] = {"mergedAt": node.get("mergedAt"),
                                             "url": node.get("url"), "closes": closes(node)}
    return out


# ---------- classification ----------


def source_for(labels):
    return next((v for k, v in SOURCE_LABELS.items() if k in labels), "manual")


def _linked_pr(repo, number, open_by_closed):
    linked = open_by_closed.get((repo, number), [])
    if not linked:
        return None
    for p in linked:  # prefer the non-draft PR: that is what made the card "review"
        if not p["isDraft"]:
            return {"number": p["number"], "repo": p["repo"], "url": p["url"]}
    p = linked[0]
    return {"number": p["number"], "repo": p["repo"], "url": p["url"]}


def _merged_pr(repo, number, merged_by_closed, now):
    cutoff = now - MERGE_WINDOW
    for p in merged_by_closed.get((repo, number), []):
        if _within(p["mergedAt"], cutoff):
            return {"number": p["number"], "repo": p["repo"], "url": p["url"]}
    return None


def _parse_iso(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _within(ts, cutoff):
    dt = _parse_iso(ts)
    return dt is not None and dt >= cutoff


def column_for(repo, number, labels, open_by_closed):
    """The column id for an OPEN issue (first match wins), or None when it is not on the board."""
    if "needs-decision" in labels:
        return "waiting"
    linked = open_by_closed.get((repo, number), [])
    if linked and any(not p["isDraft"] for p in linked):
        return "review"
    if linked or "building" in labels:
        return "building"
    if "factory" in labels:
        return "triage"
    if "intake" in labels:
        return "intake"
    return None


def _card(issue, col, product, pr, now):
    labels = issue["labels"]
    repo = issue["repo"]
    updated = _parse_iso(issue.get("updated_at"))
    stuck = "stuck" in labels or (
        col in ("triage", "building") and updated is not None
        and (now - updated) >= STUCK_WINDOW)
    return {
        "id": f"{repo}#{issue['number']}",
        "num": issue["number"],
        "col": col,
        "repo": repo,
        "repoShort": repo.split("/")[-1],
        "product": product,
        "source": source_for(labels),
        "title": issue["title"],
        "auto": "auto-promoted" in labels,
        "stuck": stuck,
        "pr": pr,
        "url": issue.get("html_url") or issue.get("url") or "",
        "body": (issue.get("body") or "")[:2000],
    }


def classify(products, issues, open_by_closed, merged_by_closed, now):
    """Cards from the raw search issues and the per-repo PRs (indexed by the repo+number each PR
    closes). Not shown: open issues with none of the board labels, and closed issues not closed by
    a merged PR in the last 24 h."""
    repo_product = _repo_product(products)
    cards = []
    for it in issues:
        repo = _repo_from_url(it.get("repository_url")) or _repo_from_url(it.get("html_url"))
        if repo not in repo_product:
            continue
        product = repo_product[repo]
        number = it.get("number")
        labels = _labels(it)
        issue = {"number": number, "title": it.get("title", ""), "labels": labels,
                 "state": (it.get("state") or "").lower(),
                 "updated_at": it.get("updated_at"), "repo": repo,
                 "html_url": it.get("html_url"), "body": it.get("body") or ""}
        if issue["state"] == "open":
            col = column_for(repo, number, labels, open_by_closed)
            if col is None:
                continue
            pr = _linked_pr(repo, number, open_by_closed) if col in ("review", "building") else None
            cards.append(_card(issue, col, product, pr, now))
        elif issue["state"] == "closed":
            merged = _merged_pr(repo, number, merged_by_closed, now)
            if merged:
                cards.append(_card(issue, "merged", product, merged, now))
    return cards


def _collect_pr_index(products, now, repo_errors):
    """Index the open and merged PRs of every product repo by the (repo, number) each PR closes,
    so a PR in one repo that closes an issue in another links to the issue, never to a same-number
    issue in the PR's own repo. A repo whose PRs cannot be read is skipped and recorded."""
    open_by_closed, merged_by_closed = {}, {}
    for repo in _repo_product(products):
        try:
            prs = prs_for_repo(repo, now)
        except FactoryError as e:
            repo_errors[repo] = _err(e)
            continue
        for num, p in prs["open"].items():
            for crepo, cnum in p["closes"]:
                open_by_closed.setdefault((crepo, cnum), []).append(
                    {"number": num, "repo": repo, "url": p["url"], "isDraft": p["isDraft"]})
        for num, p in prs["merged"].items():
            for crepo, cnum in p["closes"]:
                merged_by_closed.setdefault((crepo, cnum), []).append(
                    {"number": num, "repo": repo, "url": p["url"], "mergedAt": p["mergedAt"]})
    return open_by_closed, merged_by_closed


def _cached_result(error, repo_errors=None):
    """The last good cards with `error` set and `fetchedAt` = the cache time. The result is written
    back so GET /api/board and the next build see the same error state."""
    try:
        cached = store.read(CACHE_REL, {"cards": [], "fetchedAt": None})
    except Exception:
        cached = {"cards": [], "fetchedAt": None}
    result = {"cards": cached.get("cards", []), "fetchedAt": cached.get("fetchedAt"),
              "error": error, "repoErrors": repo_errors or cached.get("repoErrors", {})}
    with store.lock():
        store.write(CACHE_REL, result)
    return result


def collect(products, now=None):
    """The board: cards, fetchedAt, error, repoErrors. A repo whose search fails is recorded in
    repoErrors and skipped (the other repos' cards still show); `error` is set only when every repo
    failed, in which case the last good cards are returned with `fetchedAt` = the cache time."""
    now = now or datetime.now(timezone.utc)
    repos = list(_repo_product(products))
    try:
        issues, repo_errors, ok = search_issues(products)
    except Exception as e:  # gh unreachable: fall back to the cache
        return _cached_result(_err(e))
    if repos and not ok:  # every repo failed: fall back to the cache
        summary = "; ".join(f"{r}: {repo_errors[r]}" for r in repos if r in repo_errors)
        return _cached_result(summary or "every product repo failed", repo_errors)
    try:
        open_by_closed, merged_by_closed = _collect_pr_index(products, now, repo_errors)
        cards = classify(products, issues, open_by_closed, merged_by_closed, now)
    except Exception as e:  # unexpected source error: fall back to the cache
        return _cached_result(_err(e), repo_errors)
    if repo_errors:  # a repo whose issues or PRs could not be read keeps its last good cards
        try:
            cached = store.read(CACHE_REL, {"cards": []}).get("cards", [])
        except Exception:
            cached = []
        cards = ([c for c in cards if c["repo"] not in repo_errors]
                 + [c for c in cached if c.get("repo") in repo_errors])
    cards.sort(key=lambda c: (c["col"], -c["num"]))
    result = {"cards": cards, "fetchedAt": store.iso(now), "error": None,
              "repoErrors": repo_errors}
    with store.lock():
        store.write(CACHE_REL, result)
    return result


# ---------- board.json (auto-promote) ----------


def auto_promote():
    return bool(store.read(BOARD_REL, {}).get("auto_promote", True))


def set_auto_promote(enabled):
    data = store.read(BOARD_REL, {})
    data["auto_promote"] = bool(enabled)
    store.write(BOARD_REL, data)
    return data


# ---------- cache helpers (the board state serve reads between builds) ----------


def _cached_card(repo, number):
    data = store.read(CACHE_REL, {"cards": []})
    return next((c for c in data.get("cards", []) if c.get("id") == f"{repo}#{number}"), None)


def _update_cached_card(repo, number, **fields):
    with store.lock():
        data = store.read(CACHE_REL, {"cards": []})
        for c in data.get("cards", []):
            if c.get("id") == f"{repo}#{number}":
                c.update(fields)
                store.write(CACHE_REL, data)
                return c
    return None


def _remove_cached_card(repo, number):
    with store.lock():
        data = store.read(CACHE_REL, {"cards": []})
        kept = [c for c in data.get("cards", []) if c.get("id") != f"{repo}#{number}"]
        if len(kept) != len(data.get("cards", [])):
            data["cards"] = kept
            store.write(CACHE_REL, data)


# ---------- labels ----------


def repo_labels(repo):
    data = sources.gh_json("label", "list", "--repo", repo, "--json", "name")
    return {l.get("name") for l in (data or []) if isinstance(l, dict) and l.get("name")}


def missing_labels(repo):
    have = repo_labels(repo)
    return [l for l in REQUIRED_LABELS if l not in have]


def missing_labels_for_products():
    """For each product repo, which of the board's required labels are missing (a warning line,
    not a failure). A repo whose labels cannot be read is skipped."""
    out = []
    for repo in _repo_product(store.products()):
        try:
            missing = missing_labels(repo)
        except Exception:
            continue
        if missing:
            out.append(f"{repo} missing {', '.join(missing)}")
    return out


# ---------- writes ----------


def _check(products, repo, number):
    if not isinstance(repo, str) or repo not in _repo_product(products):
        raise BoardError(f"{repo!r} is not a product repo")
    if type(number) is not int or number < 1:
        raise BoardError("number must be a positive whole number")


def _issue_state(repo, number):
    """The issue's GitHub state and labels, read fresh (a write must check GitHub, not only the cache)."""
    data = sources.gh_json("issue", "view", str(number), "--repo", repo, "--json", "state,labels")
    return (data.get("state") or "").lower(), _labels({"labels": data.get("labels")})


def _ensure_label(repo, name, description, color):
    """Create the label, treating "already exists" as success (the repo may already have it)."""
    try:
        sources.run("gh", "label", "create", name, "--repo", repo,
                    "--description", description, "--color", color)
    except FactoryError as e:
        if "already" not in str(e).lower():
            raise


def promote(repo, number):
    state, labels = _issue_state(repo, number)
    if state != "open" or "intake" not in labels or "factory" in labels:
        raise BoardError("already promoted or closed on GitHub", 409)
    _ensure_label(repo, "factory", FACTORY_LABEL_DESC, FACTORY_LABEL_COLOR)
    edit = ["gh", "issue", "edit", str(number), "--repo", repo, "--add-label", "factory"]
    if "intake" in labels:
        edit += ["--remove-label", "intake"]
    sources.run(*edit)
    sources.run("gh", "issue", "comment", str(number), "--repo", repo,
                "--body", "Promoted from the factory board.")
    _update_cached_card(repo, number, col="triage")
    return {"repo": repo, "number": number, "col": "triage"}


def close(repo, number):
    sources.run("gh", "issue", "comment", str(number), "--repo", repo,
                "--body", "Closed from the factory board.")
    try:
        sources.run("gh", "issue", "close", str(number), "--repo", repo, "--reason", "not planned")
    except FactoryError as e:
        raise BoardError(f"close failed (the closing comment was posted): {_err(e)}")
    _remove_cached_card(repo, number)
    return {"repo": repo, "number": number, "closed": True}


def send_back(repo, number, text):
    if not isinstance(text, str) or not text.strip():
        raise BoardError("text is required")
    card = _cached_card(repo, number)
    if not card or card.get("col") not in ("review", "building"):
        raise BoardError("send back is only for review or building cards", 409)
    pr = (card or {}).get("pr")
    if not pr or not pr.get("number"):
        raise BoardError(f"no linked PR for {repo}#{number}", 409)
    pr_repo = pr.get("repo") or repo
    sources.run("gh", "pr", "comment", str(pr["number"]), "--repo", pr_repo, "--body", text.strip())
    return {"repo": repo, "number": number, "pr": pr["number"]}


def act(action, repo, number, text=None):
    """The shared write path for POST /api/board and `factory board promote|close`. Raises
    BoardError (a FactoryError with an HTTP status) on any refusal."""
    products = store.products()
    _check(products, repo, number)
    if action == "promote":
        return promote(repo, number)
    if action == "close":
        return close(repo, number)
    if action == "send_back":
        return send_back(repo, number, text)
    raise BoardError(f"unknown action {action!r}")


# ---------- timeline ----------


def _event_text(e, issue_repo):
    ev = e.get("event")
    if ev in ("labeled", "unlabeled"):
        name = (e.get("label") or {}).get("name")
        verb = "added" if ev == "labeled" else "removed"
        return f"Label {verb}: {name}" if name else None
    if ev == "cross-referenced":
        src = e.get("source") or {}
        issue = src.get("issue") or {}
        if src.get("type") == "issue" and issue.get("pull_request") is not None:
            pr = issue.get("pull_request") or {}
            pr_repo = _repo_from_url(issue.get("repository_url"))
            ref = (f"{pr_repo}#{issue.get('number')}" if pr_repo and pr_repo != issue_repo
                   else f"PR #{issue.get('number')}")
            return f"{ref} merged" if pr.get("merged_at") else f"{ref} opened"
        return None
    if ev == "closed":
        return "Issue closed"
    return None


def timeline(repo, number):
    """The card drawer's timeline: issue created + labels added + PR opened/merged, newest first,
    at most 10. Read lazily (GET /api/board/timeline), never at build time. A failed fetch raises
    a BoardError (502) with the error."""
    products = store.products()
    _check(products, repo, number)
    entries = []
    try:
        created = sources.gh_json("issue", "view", str(number), "--repo", repo, "--json", "createdAt")
        if created.get("createdAt"):
            entries.append({"at": created["createdAt"], "txt": "Issue created"})
    except FactoryError:
        pass
    try:
        events = sources.gh_json("api", f"repos/{repo}/issues/{number}/timeline?per_page=100")
    except FactoryError as e:
        raise BoardError(_err(e), 502)
    for e in events or []:
        txt = _event_text(e, repo)
        if txt:
            entries.append({"at": e.get("created_at"), "txt": txt})
    entries.sort(key=lambda x: x.get("at") or "", reverse=True)
    return entries[:10]
