"""Release comms: changelog generation, due jobs, truth validation, approval routing, and send recording."""
import copy
import re
from datetime import date, datetime, timedelta, timezone

from . import queue, release, sources, store
from .store import FactoryError

COMMS_REL = "comms.json"

DEFAULT_COMMS = {
    "changelogs": {},
    "jobs": {},
    "since": {},
    "deferred": {},
    "relayed": {},
}

def comms_products() -> dict[str, dict]:
    """Comms products, in release-config order: those with a `comms` block (langs, audiences, internal_scopes)."""
    out = {}
    for name, p in release.release().get("products", {}).items():
        c = p.get("comms")
        if isinstance(c, dict) and c.get("langs"):  # no language = nothing to send
            out[name] = {"langs": list(c.get("langs") or []), "audiences": list(c.get("audiences") or []),
                         "internal_scopes": set(c.get("internal_scopes") or [])}
    return out


def comms_cfg(product: str) -> dict | None:
    return comms_products().get(product)


def product_name(product: str) -> str:
    p = release.release().get("products", {}).get(product) or {}
    return p.get("display_name") or product.title()


INTERNAL_SCOPES = {"admin", "cli", "agent", "agents", "ops", "infra", "deps", "ci", "build", "test", "tests",
                   "db", "sentry", "monitoring"}

ROADMAP_FEAT_RE = re.compile(r"Roadmap:\s*FEAT-\d+", re.IGNORECASE)
INTERNAL_PREFIXES = {"chore", "ci", "build", "docs", "test", "refactor"}


class CommsValidationError(FactoryError):
    """Validation error in release comms (exits with code 2)."""
    exit_code = 2


def comms() -> dict:
    """Read comms.json (call under store.lock() when the result is written back)."""
    data = store.read(COMMS_REL, copy.deepcopy(DEFAULT_COMMS))
    if not isinstance(data, dict):
        data = copy.deepcopy(DEFAULT_COMMS)
    for k in DEFAULT_COMMS:
        if not isinstance(data.get(k), dict):
            data[k] = {}
    return data


def save_comms(data: dict) -> None:
    """Save comms.json under store.lock()."""
    store.write(COMMS_REL, data)


def pr_facts(repo: str, numbers: list[int]) -> dict[int, dict]:
    """Fetch facts for PR numbers in ONE gh api graphql call using aliases."""
    if not numbers:
        return {}

    owner, name = queue._split_repo(repo)
    unique_numbers = list(dict.fromkeys(numbers))

    fragments = []
    for n in unique_numbers:
        alias = f"pr_{n}"
        fragments.append(
            f"""  {alias}: pullRequest(number: {n}) {{
    number
    title
    body
    url
    author {{
      login
      __typename
    }}
    labels(first: 50) {{
      nodes {{
        name
      }}
    }}
  }}"""
        )

    query = "query($owner: String!, $name: String!) {\n  repository(owner: $owner, name: $name) {\n" + "\n".join(fragments) + "\n  }\n}"

    args = [
        "api", "graphql",
        "-f", f"query={query}",
        "-f", f"owner={owner}",
        "-f", f"name={name}",
    ]

    try:
        res = sources.gh_json(*args)
    except Exception as e:
        raise FactoryError(f"gh failure reading {repo}: {e}") from e

    if not isinstance(res, dict) or not isinstance(res.get("data"), dict):
        errors = res.get("errors") if isinstance(res, dict) else None
        err_msg = str(errors) if errors else "invalid response"
        raise FactoryError(f"gh failure reading {repo}: {err_msg}")

    repo_data = res.get("data", {}).get("repository") or {}
    out: dict[int, dict] = {}
    for n in unique_numbers:
        pr_data = repo_data.get(f"pr_{n}")
        if not pr_data:
            continue
        author_info = pr_data.get("author") or {}
        author_login = author_info.get("login") or ""
        author_type = author_info.get("__typename") or ""
        is_bot = (author_type == "Bot") or queue.is_bot_login(author_login)
        labels_val = pr_data.get("labels")
        if isinstance(labels_val, dict):
            nodes = labels_val.get("nodes") or []
        elif isinstance(labels_val, list):
            nodes = labels_val
        else:
            nodes = []
        labels = []
        for node in nodes:
            if isinstance(node, dict):
                lbl_name = node.get("name")
                if lbl_name:
                    labels.append(lbl_name)
            elif isinstance(node, str):
                labels.append(node)
        out[n] = {
            "title": pr_data.get("title") or "",
            "body": pr_data.get("body") or "",
            "labels": labels,
            "url": pr_data.get("url") or "",
            "author": author_login,
            "is_bot": is_bot,
        }
    return out


def classify(product: str, fact: dict) -> dict:
    """Classify a PR fact: user-facing or internal, big or small, changelog group, and why."""
    title = (fact.get("title") or "").strip()
    labels = [str(l).lower() for l in (fact.get("labels") or [])]
    m = re.match(r"^([a-zA-Z0-9_-]+)(?:\(([^)]*)\))?!?\s*:", title)
    prefix = m.group(1).lower() if m else None
    scopes = {x.strip().lower() for x in re.split(r"[,/]", m.group(2) or "")} if m else set()
    cfg = comms_cfg(product)

    reason = None
    if cfg is None:
        reason = "internal product"
    elif fact.get("unreadable"):
        reason = "PR could not be read"
    elif title.startswith("["):
        reason = "agent or tooling title"
    elif prefix in INTERNAL_PREFIXES:
        reason = f"{prefix} change"
    elif scopes & (INTERNAL_SCOPES | cfg["internal_scopes"]):
        reason = f"internal scope {sorted(scopes & (INTERNAL_SCOPES | cfg['internal_scopes']))[0]}"
    elif re.search(r"\badmin\b", title, re.IGNORECASE):
        reason = "admin tooling"
    elif "no-comms" in labels:
        reason = "label no-comms"
    elif "dependencies" in labels:
        reason = "dependency bump"
    elif fact.get("is_bot") or queue.is_bot_login(fact.get("author")):
        reason = "bot author"

    if reason:
        return {"user": False, "big": False, "group": "internal", "reason": reason}
    group = {"feat": "new", "fix": "fixed"}.get(prefix, "improved")
    big = "big" in labels or bool(ROADMAP_FEAT_RE.search(fact.get("body") or ""))
    return {"user": True, "big": big, "group": group, "reason": "user-facing"}


def _ordinal_label_suffix(order_index: int) -> str:
    """Return suffix for multiple releases on the same Tunis date: 0 -> '', 1 -> ' (2nd)', etc."""
    if order_index == 0:
        return ""
    n = order_index + 1
    if 11 <= (n % 100) <= 13:
        sfx = "th"
    elif n % 10 == 1:
        sfx = "st"
    elif n % 10 == 2:
        sfx = "nd"
    elif n % 10 == 3:
        sfx = "rd"
    else:
        sfx = "th"
    return f" ({n}{sfx})"


def changelog_markdown(entry_data: dict, titles: bool = False) -> str:
    """Format changelog markdown: uses first-language copy when present unless titles=True."""
    product = entry_data.get("product", "")
    prod_title = product_name(product)
    label = entry_data.get("label", "")
    items = entry_data.get("items", [])
    cfg = comms_cfg(product) or {}
    first_lang = (cfg.get("langs") or ["en"])[0]

    md_lines = [f"## {prod_title} {label}"]

    group_order = [
        ("new", "### New"),
        ("fixed", "### Fixed"),
        ("improved", "### Improved"),
        ("internal", "### Internal (not sent to users)"),
    ]

    for g_key, g_header in group_order:
        g_items = [it for it in items if it.get("group") == g_key]
        if g_items:
            md_lines.append("")
            md_lines.append(g_header)
            for it in g_items:
                copy_text = None
                if not titles and isinstance(it.get("copy"), dict):
                    copy_text = it["copy"].get(first_lang)
                text = copy_text or it.get("title") or f"PR #{it['n']}"
                md_lines.append(f"- {text} (#{it['n']})")

    return "\n".join(md_lines)


def changelog(product: str, at: str | None = None, titles: bool = False, refresh: bool = False) -> tuple[dict, str]:
    """Generate or retrieve stored changelog for latest (or specified at) shipped release."""
    rel_data = release.release()
    journal = rel_data.get("journal", [])
    shipped_events = [e for e in journal if e.get("product") == product and e.get("event") == "shipped"]
    if not shipped_events:
        raise FactoryError(f"no shipped release found for {product}")

    if at:
        matches = [e for e in shipped_events if e.get("at") == at]
        if not matches:
            raise FactoryError(f"no shipped release found for {product} at {at}")
        entry = matches[0]
    else:
        entry = shipped_events[-1]

    entry_at = entry["at"]
    entry_dt = release._parse_utc(entry_at).astimezone(release.TUNIS)
    entry_tunis_date = entry_dt.date()
    date_str = entry_tunis_date.isoformat()

    same_day_events = []
    for e in shipped_events:
        e_dt = release._parse_utc(e["at"]).astimezone(release.TUNIS)
        if e_dt.date() == entry_tunis_date:
            same_day_events.append((e_dt, e))
    same_day_events.sort(key=lambda x: x[0])

    idx = [e[1].get("at") for e in same_day_events].index(entry_at)
    label = f"{date_str}{_ordinal_label_suffix(idx)}"

    with store.lock():
        c_data = comms()
        stored = c_data.get("changelogs", {}).get(f"{product}@{entry_at}")

    if stored and not refresh:
        entry_data = stored
    else:
        existing_copies = {}
        if stored:
            for it in stored.get("items", []):
                if it.get("n") is not None and it.get("copy"):
                    existing_copies[it["n"]] = it["copy"]

        repo = rel_data.get("products", {}).get(product, {}).get("repo", "")
        prs = entry.get("prs") or []
        facts = pr_facts(repo, prs) if (repo and prs) else {}

        items = []
        for n in prs:
            # An unreadable PR is internal: comms fail closed, never "PR #n" to users.
            fact = facts.get(n) or {
                "title": f"PR #{n}",
                "url": f"https://github.com/{repo}/pull/{n}" if repo else "",
                "unreadable": True,
            }
            c = classify(product, fact)
            item_data = {
                "n": n,
                "title": fact.get("title") or f"PR #{n}",
                "url": fact.get("url") or "",
                "group": c["group"],
                "big": c["big"],
                "reason": c["reason"],
                "body": fact.get("body") or "",
            }
            if n in existing_copies:
                item_data["copy"] = existing_copies[n]
            items.append(item_data)

        entry_data = {
            "product": product,
            "at": entry_at,
            "sha": entry.get("sha", ""),
            "label": label,
            "items": items,
        }

        with store.lock():
            c_data = comms()
            latest_stored = c_data.get("changelogs", {}).get(f"{product}@{entry_at}")
            if latest_stored:
                for it in latest_stored.get("items", []):
                    if it.get("n") is not None and it.get("copy"):
                        for target_it in items:
                            if target_it.get("n") == it["n"]:
                                target_it.setdefault("copy", {}).update(it["copy"])
            c_data.setdefault("changelogs", {})[f"{product}@{entry_at}"] = entry_data
            save_comms(c_data)

    markdown = changelog_markdown(entry_data, titles=titles)
    return entry_data, markdown


def set_copy(changelog_key: str, copies: dict) -> dict:
    """Set plain-words copy for user-facing PRs in a changelog.

    copies = {"<pr>": {"<lang>": "plain-words line"}}; stored on the changelog item as copy.
    Refuse PR numbers not in that changelog, internal items, unknown languages, empty strings.
    """
    if not isinstance(copies, dict):
        raise CommsValidationError("copies must be a JSON object")

    with store.lock():
        c_data = comms()
        entry_data = c_data.get("changelogs", {}).get(changelog_key)
        if not entry_data:
            raise CommsValidationError(f"unknown changelog {changelog_key!r}")

        product = entry_data.get("product")
        cfg = comms_cfg(product)
        if not cfg:
            raise CommsValidationError(f"unknown product for changelog {changelog_key!r}")
        allowed_langs = cfg.get("langs", [])

        items_by_n = {it["n"]: it for it in entry_data.get("items", []) if "n" in it}

        for raw_pr, lang_map in copies.items():
            try:
                pr_num = int(raw_pr)
            except (ValueError, TypeError):
                raise CommsValidationError(f"invalid PR number {raw_pr!r}")

            if pr_num not in items_by_n:
                raise CommsValidationError(f"PR #{pr_num} is not in changelog {changelog_key}")

            item = items_by_n[pr_num]
            if item.get("group") == "internal":
                raise CommsValidationError(f"PR #{pr_num} is internal: cannot set copy")

            if not isinstance(lang_map, dict):
                raise CommsValidationError(f"copy for PR #{pr_num} must be an object of {{lang: text}}")

            clean_copy = {}
            for lang, line in lang_map.items():
                if lang not in allowed_langs:
                    raise CommsValidationError(f"unknown language {lang!r} for {product} (use {', '.join(allowed_langs)})")
                if not isinstance(line, str) or not line.strip():
                    raise CommsValidationError(f"empty copy for PR #{pr_num} in {lang}")
                clean_copy[lang] = line.strip()

            item.setdefault("copy", {}).update(clean_copy)

        save_comms(c_data)

    return entry_data


def _week_start(now_tunis: datetime) -> datetime:
    """Monday 00:00 Tunis of the week holding now_tunis."""
    monday = now_tunis.date() - timedelta(days=now_tunis.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=release.TUNIS)


def _rolled_back(journal: list[dict], product: str) -> dict[int, datetime]:
    """PR number -> time of its latest rollback. A PR rolled back after it shipped is not in production."""
    out: dict[int, datetime] = {}
    for e in journal:
        if e.get("product") == product and e.get("event") == "rolled-back":
            for n in e.get("prs") or []:
                out[n] = release._parse_utc(e["at"])
    return out


def _pr_obj(item: dict) -> dict:
    return {"n": item["n"], "number": item["n"], "title": item["title"],
            "url": item.get("url", ""), "body": item.get("body", "")}


def _pr_num(p) -> int | None:
    v = p.get("n") if isinstance(p, dict) else p
    return v if type(v) is int else None


def _sync(product: str, now_utc: datetime, write: bool = True) -> tuple[dict, dict[int, tuple[datetime, dict]], list[dict]]:
    """Store missing changelogs since the product's comms start; return (comms data, live items, journal).

    live: PR number -> (shipped at, changelog item) for user-facing PRs in production, newest release wins.
    `since` is the Monday that opened the week comms first ran, so switching comms on announces nothing old.
    """
    journal = release.release().get("journal", [])
    with store.lock():
        c_data = comms()
        if product not in c_data.get("since", {}):
            if write:
                c_data.setdefault("since", {})[product] = store.iso(_week_start(now_utc.astimezone(release.TUNIS)))
                save_comms(c_data)
            else:
                return c_data, {}, journal
    since_dt = release._parse_utc(c_data["since"][product])
    if write:
        for e in journal:
            if (e.get("product") == product and e.get("event") == "shipped"
                    and release._parse_utc(e["at"]) >= since_dt
                    and f"{product}@{e['at']}" not in c_data["changelogs"]):
                changelog(product, at=e["at"])
        with store.lock():
            c_data = comms()
    rolled = _rolled_back(journal, product)
    live: dict[int, tuple[datetime, dict]] = {}
    for cl in c_data["changelogs"].values():
        if cl.get("product") != product or not cl.get("at"):
            continue
        cl_dt = release._parse_utc(cl["at"])
        if cl_dt < since_dt or cl_dt > now_utc:
            continue
        for item in cl.get("items", []):
            n = item["n"]
            if item.get("group") == "internal" or (n in rolled and rolled[n] >= cl_dt):
                continue
            if n not in live or live[n][0] < cl_dt:
                live[n] = (cl_dt, item)
    return c_data, live, journal


def _refresh(job: dict, live: dict) -> dict | None:
    """A stored job with PRs no longer in production dropped; None when nothing is left to announce."""
    job = copy.deepcopy(job)
    job["prs"] = [p for p in job.get("prs", []) if _pr_num(p) in live]
    job["recap"] = [p for p in job.get("recap", []) if _pr_num(p) in live]
    return job if job["prs"] or job["recap"] else None


DONE = ("sent", "skipped")  # terminal statuses


def due(product: str, now: datetime | None = None, write: bool = True) -> list[dict]:
    """Comms jobs due now for product: feature jobs, unfinished digests, and this Friday's digest."""
    if product not in release.release().get("products", {}):
        raise FactoryError(f"unknown product {product!r}")
    if comms_cfg(product) is None:
        return []
    now_utc = release._now_utc(now)
    now_tunis = now_utc.astimezone(release.TUNIS)
    today = now_tunis.date()
    c_data, live, _ = _sync(product, now_utc, write=write)
    if product not in c_data.get("since", {}):
        return []
    jobs_store = {k: j for k, j in c_data["jobs"].items() if j.get("product") == product and j.get("kind")}
    deferred = c_data["deferred"].get(product, {})
    first_send = not any(j.get("status") == "sent" for j in jobs_store.values())
    cfg = comms_cfg(product)
    base = {"product": product, "audiences": cfg["audiences"], "langs": cfg["langs"], "first_send": first_send}

    jobs: list[dict] = []

    # 1. Feature jobs: one per big user-facing PR, from the day it ships until sent or skipped.
    for n, (_, item) in sorted(live.items()):
        if not item.get("big"):
            continue
        job_id = f"f-{product}-{n}"
        existing = jobs_store.get(job_id)
        if existing and existing.get("status") in DONE:
            continue
        if deferred.get(str(n)) and date.fromisoformat(deferred[str(n)]) > today:
            continue  # a scheduled launch: its email is drafted on or after the date
        job = _refresh(existing, live) if existing else {
            **base, "id": job_id, "kind": "feature", "prs": [_pr_obj(item)], "recap": []}
        if job:
            jobs.append(job)

    # 2. Digests already cut but not sent or skipped stay due (a failed or late-approved digest is not lost).
    for job_id, existing in sorted(jobs_store.items()):
        if existing.get("kind") == "digest" and existing.get("status") not in DONE:
            job = _refresh(existing, live)
            if job:
                jobs.append(job)

    # 3. This Friday's digest, from 10:00 Tunis. Window [cutoff, until): until is this Friday 10:00,
    # cutoff is the latest earlier digest's until (first digest: comms start).
    digest_id = f"d-{product}-{today.isoformat()}"
    if now_tunis.weekday() == 4 and now_tunis.hour >= 10 and digest_id not in jobs_store:
        until_dt = datetime(today.year, today.month, today.day, 10, tzinfo=release.TUNIS)
        untils = [release._parse_utc(j["until"]) for j in jobs_store.values()
                  if j.get("kind") == "digest" and j.get("until")]
        cutoff_dt = max(untils) if untils else release._parse_utc(c_data["since"][product])
        prs, recap = [], []
        held = {_pr_num(p) for j in jobs_store.values()
                if j.get("kind") == "digest" and j.get("status") not in DONE for p in j.get("prs", [])}
        for n, (shipped_dt, item) in sorted(live.items()):
            defer = deferred.get(str(n))
            if item.get("big"):
                continue  # announced by its own email; recapped below once sent
            if defer:
                if date.fromisoformat(defer) <= today and n not in held:
                    prs.append(_pr_obj(item))  # a scheduled change, announced on or after its date
            elif cutoff_dt <= shipped_dt < until_dt:
                prs.append(_pr_obj(item))
        recapped = {_pr_num(p) for j in jobs_store.values() if j.get("kind") == "digest" for p in j.get("recap", [])}
        for j in jobs_store.values():
            # A big feature whose own email went out: a one-line recap in the next digest, once.
            if j.get("kind") == "feature" and j.get("status") == "sent":
                recap.extend(p for p in j.get("prs", []) if _pr_num(p) in live and _pr_num(p) not in recapped)
        if prs or recap:
            jobs.append({**base, "id": digest_id, "kind": "digest", "prs": prs, "recap": recap,
                         "cutoff": store.iso(cutoff_dt), "until": store.iso(until_dt)})

    # 4. Brag video fallback: 2 h after the first render failure, send with a static card (stored).
    flagged = []
    for job in jobs:
        vf = job.get("video_failed_at")
        if vf and not job.get("static_card") and now_utc - release._parse_utc(vf) >= timedelta(hours=2):
            job["static_card"] = True
            flagged.append(job["id"])
    if flagged and write:
        with store.lock():
            c_data = comms()
            for job_id in flagged:
                if job_id in c_data["jobs"]:
                    c_data["jobs"][job_id]["static_card"] = True
            save_comms(c_data)
    return jobs


def window(product: str, now: datetime | None = None) -> dict:
    """Read-only summary of the current digest window and pending comms for product."""
    if product not in release.release().get("products", {}):
        raise FactoryError(f"unknown product {product!r}")
    if comms_cfg(product) is None:
        return {
            "not_started": True,
            "cutoff": None,
            "until": None,
            "pending": [],
            "big_pending": [],
            "unsynced": [],
        }

    now_utc = release._now_utc(now)
    now_tunis = now_utc.astimezone(release.TUNIS)
    today = now_tunis.date()
    c_data, live, journal = _sync(product, now_utc, write=False)

    jobs_store = {k: j for k, j in c_data.get("jobs", {}).items()
                  if j.get("product") == product and j.get("kind")}
    deferred = c_data.get("deferred", {}).get(product, {})

    # until: the next Friday 10:00 Tunis at or after now.
    # On Friday before 10:00 that is today; from Friday 10:00 on it is next Friday,
    # unless today's digest job is not stored yet, in which case it stays today's Friday 10:00 so it matches due().
    w = today.weekday()  # Mon=0, Fri=4, Sat=5, Sun=6
    today_fri_10 = datetime(today.year, today.month, today.day, 10, 0, tzinfo=release.TUNIS)
    today_digest_id = f"d-{product}-{today.isoformat()}"
    today_digest_stored = (today_digest_id in jobs_store
                           or any(j.get("kind") == "digest" and j.get("until")
                                  and release._parse_utc(j["until"]).astimezone(release.TUNIS).date() == today
                                  for j in jobs_store.values()))

    if w < 4:
        days_ahead = 4 - w
        fri_date = today + timedelta(days=days_ahead)
        until_dt = datetime(fri_date.year, fri_date.month, fri_date.day, 10, 0, tzinfo=release.TUNIS)
    elif w == 4:
        if now_tunis < today_fri_10:
            until_dt = today_fri_10
        else:
            next_fri = today + timedelta(days=7)
            next_fri_10 = datetime(next_fri.year, next_fri.month, next_fri.day, 10, 0, tzinfo=release.TUNIS)
            # Today's window stays open only while due() would still cut today's digest (checked below).
            until_dt = next_fri_10 if today_digest_stored else today_fri_10
    else:  # w > 4 (Saturday=5, Sunday=6)
        days_ahead = 7 - (w - 4)
        fri_date = today + timedelta(days=days_ahead)
        until_dt = datetime(fri_date.year, fri_date.month, fri_date.day, 10, 0, tzinfo=release.TUNIS)

    if product not in c_data.get("since", {}):
        return {
            "not_started": True,
            "cutoff": None,
            "until": store.iso(until_dt),
            "pending": [],
            "big_pending": [],
            "unsynced": [],
        }

    # cutoff: the latest stored digest's until, or else since.
    untils = [release._parse_utc(j["until"]) for j in jobs_store.values()
              if j.get("kind") == "digest" and j.get("until")]
    cutoff_dt = max(untils) if untils else release._parse_utc(c_data["since"][product])

    # pending: user-facing, non-big, in-production PRs from stored changelogs whose release falls in [cutoff, until),
    # plus deferred PRs whose date is on or before until. Each is {n, title}.
    held = {_pr_num(p) for j in jobs_store.values()
            if j.get("kind") == "digest" and j.get("status") not in DONE for p in j.get("prs", [])}

    def pending_for(until_dt):
        until_utc = until_dt.astimezone(timezone.utc)
        until_date = until_dt.astimezone(release.TUNIS).date()
        out = []
        for n, (shipped_dt, item) in sorted(live.items()):
            if item.get("big"):
                continue
            defer = deferred.get(str(n))
            if defer:
                if date.fromisoformat(defer) <= until_date and n not in held:
                    out.append({"n": n, "title": item["title"]})
            elif cutoff_dt <= shipped_dt < until_utc:
                out.append({"n": n, "title": item["title"]})
        return out

    pending = pending_for(until_dt)
    if w == 4 and now_tunis >= today_fri_10 and not today_digest_stored and not pending:
        # due() cuts no digest today unless a sent big feature still needs its recap line.
        recapped = {_pr_num(p) for j in jobs_store.values() if j.get("kind") == "digest" for p in j.get("recap", [])}
        recap_due = any(j.get("kind") == "feature" and j.get("status") == "sent"
                        and any(_pr_num(p) in live and _pr_num(p) not in recapped for p in j.get("prs", []))
                        for j in jobs_store.values())
        if not recap_due:
            until_dt = next_fri_10
            pending = pending_for(until_dt)
    until_date = until_dt.astimezone(release.TUNIS).date()

    # big_pending: big user-facing PRs in production whose feature job is not sent or skipped.
    big_pending = []
    for n, (_, item) in sorted(live.items()):
        if not item.get("big"):
            continue
        job_id = f"f-{product}-{n}"
        existing = jobs_store.get(job_id)
        if existing and existing.get("status") in DONE:
            continue
        defer = deferred.get(str(n))
        if defer and date.fromisoformat(defer) > until_date:
            continue  # a launch scheduled after this window: due() skips it until its date
        big_pending.append({"n": n, "title": item["title"]})

    # unsynced: journal shipped events for the product since since that have no stored changelog yet. Each is {at, prs}.
    since_dt = release._parse_utc(c_data["since"][product])
    unsynced = []
    for e in journal:
        if (e.get("product") == product and e.get("event") == "shipped"
                and release._parse_utc(e["at"]) >= since_dt
                and f"{product}@{e['at']}" not in c_data.get("changelogs", {})):
            unsynced.append({"at": e["at"], "prs": e.get("prs", [])})

    return {
        "not_started": False,
        "cutoff": store.iso(cutoff_dt),
        "until": store.iso(until_dt),
        "pending": pending,
        "big_pending": big_pending,
        "unsynced": unsynced,
    }


def _load_job(job_id: str, now: datetime | None) -> dict:
    """The due job with that id (stored state refreshed against production). Raises when it is not due."""
    # every product whose name prefixes the id: "f-web-admin-3" could be "web" or "web-admin"
    products = sorted((p for p in comms_products() if job_id.startswith(f"f-{p}-") or job_id.startswith(f"d-{p}-")),
                      key=len, reverse=True)  # longest first: one sync in the common case
    if products:
        for p in products:
            for j in due(p, now=now):
                if j["id"] == job_id:
                    return j
        with store.lock():
            stored = comms()["jobs"].get(job_id)
        if stored and stored.get("status") in DONE:
            raise CommsValidationError(f"job {job_id} is {stored['status']}: it cannot change again")
    raise CommsValidationError(f"job {job_id} is not due or does not exist")


def _check_claim(where: str, claim, langs: list[str], allowed: set[int], live: dict) -> list[int]:
    """One user-facing claim: text in the product's languages, backed by PRs of this job in production."""
    if not isinstance(claim, dict):
        raise CommsValidationError(f"{where} must be an object with text and prs")
    text = claim.get("text")
    if not isinstance(text, dict) or not isinstance(text.get(langs[0]), str) or not text[langs[0]].strip():
        raise CommsValidationError(f"{where} is missing its {langs[0]!r} text")
    for lang, val in text.items():
        if lang not in langs:
            raise CommsValidationError(f"{where} has unknown language {lang!r} (use {', '.join(langs)})")
        if not isinstance(val, str) or not val.strip():
            raise CommsValidationError(f"{where} has an empty {lang!r} text")
    prs = claim.get("prs")
    if not isinstance(prs, list) or not prs:
        raise CommsValidationError(f"{where} has no PR: every claim must be backed by a PR")
    for n in prs:
        if type(n) is not int:
            raise CommsValidationError(f"{where} cites {n!r}: PR numbers are integers")
        if n not in allowed:
            raise CommsValidationError(f"{where} cites PR #{n}, which is not in this job")
        if n not in live:
            raise CommsValidationError(f"{where} cites PR #{n}, which is internal or not in production")
    return prs


def _close_ask(ask_id: str, note: str) -> None:
    """Move an open inbox item to resolved (a newer draft replaces the one the owner was asked about)."""
    with store.lock():
        box = store.inbox()
        item = next((x for x in box["items"] if x["id"] == ask_id), None)
        if item:
            box["items"].remove(item)
            item["resolvedAt"] = store.now_iso()
            item["answer"] = {"note": note}
            box["resolved"].append(item)
            store.write("inbox.json", box)


def save_draft(job_id: str, draft, now: datetime | None = None) -> dict:
    """Validate the agent's draft against the truth rules and store it; ask the owner when approval is needed.

    draft = {"subject": claim, "sentences": [claim + "audience"], "caption": claim?,
             "video": {"url": str, "frames": [claim]}?, "defer": {"<pr>": "YYYY-MM-DD"}?}
    claim = {"text": {lang: str}, "prs": [int]}
    """
    if not isinstance(draft, dict):
        raise CommsValidationError("the draft must be a JSON object")
    now_utc = release._now_utc(now)
    today = now_utc.astimezone(release.TUNIS).date()
    job = _load_job(job_id, now)
    product = job["product"]
    cfg = comms_cfg(product)
    langs = cfg["langs"]
    _, live, _ = _sync(product, now_utc)
    allowed = {_pr_num(p) for p in job.get("prs", []) + job.get("recap", [])} - {None}

    # Scheduled changes: deferred PRs leave this send and join the first digest on or after their date.
    defer = draft.get("defer") or {}
    if not isinstance(defer, dict):
        raise CommsValidationError("defer must map PR numbers to YYYY-MM-DD dates")
    deferred: dict[str, str] = {}
    for key, when in defer.items():
        if not str(key).isdigit() or int(key) not in allowed:
            raise CommsValidationError(f"defer names PR {key!r}, which is not in this job")
        try:
            d = date.fromisoformat(when) if isinstance(when, str) else None
        except ValueError:
            d = None
        if d is None:
            raise CommsValidationError(f"defer date for PR #{key} must be YYYY-MM-DD")
        if d <= today:
            raise CommsValidationError(f"PR #{key} is scheduled for {when}, which is not in the future: announce it now")
        deferred[str(int(key))] = when
    if job["kind"] == "feature" and deferred:
        # A scheduled big feature: no email now; the job comes back on its date for a fresh draft.
        with store.lock():
            c_data = comms()
            if (c_data["jobs"].get(job_id) or {}).get("status") in DONE:
                raise CommsValidationError(f"job {job_id} is already {c_data['jobs'][job_id]['status']}")
            old_ask = job.get("ask_id")
            job.update({"status": "deferred", "ask_id": None, "updated": store.iso(now_utc)})
            c_data["jobs"][job_id] = job
            c_data["deferred"].setdefault(product, {}).update(deferred)
            save_comms(c_data)
        if old_ask:
            _close_ask(old_ask, "deferred to its launch date")
        return job

    cited: set[int] = set()
    cited.update(_check_claim("subject", draft.get("subject"), langs, allowed, live))
    sentences = draft.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        raise CommsValidationError("the draft needs at least one sentence")
    audiences = set()
    for i, s in enumerate(sentences, 1):
        cited.update(_check_claim(f"sentence {i}", s, langs, allowed, live))
        if s.get("audience") not in cfg["audiences"]:
            raise CommsValidationError(f"sentence {i} audience {s.get('audience')!r} is not one of {cfg['audiences']}")
        audiences.add(s["audience"])
    if draft.get("caption") is not None:
        cited.update(_check_claim("caption", draft["caption"], langs, allowed, live))
    video = draft.get("video")
    if video is not None:
        if not isinstance(video, dict) or not isinstance(video.get("url"), str) or not video["url"]:
            raise CommsValidationError("video must be an object with a url")
        if not isinstance(video.get("frames"), list) or not video["frames"]:
            raise CommsValidationError("video needs its frames: every on-screen line cites PRs")
        for i, f in enumerate(video["frames"], 1):
            cited.update(_check_claim(f"video frame {i}", f, langs, allowed, live))
    early = sorted(cited & {int(k) for k in deferred})
    if early:
        raise CommsValidationError(f"PR #{early[0]} is deferred: it cannot be announced before its date")

    status = "needs-approval" if job["kind"] == "feature" or job.get("first_send") else "ready"
    old_ask = job.get("ask_id")
    ask_id = None
    if status == "needs-approval":
        if old_ask:
            _close_ask(old_ask, "superseded by a newer draft")
        name = product_name(product)
        subject = draft["subject"]["text"][langs[0]].strip()
        body = " ".join(f"{s['text'][langs[0]].strip()} ({', '.join(f'#{n}' for n in s['prs'])})" for s in sentences)
        media = "brag video" if video else "static card"
        ask_id = store.add_ask_item(
            project="fac-release-mgr", kind="applied",
            title=f"Approve the {name} email: {subject}"[:80],
            short=f"{job['kind']} email to {', '.join(sorted(audiences))}",
            context=(f"Subject: {subject}\n{body[:400]}\n"
                     f"{', '.join(sorted(audiences))} · {len(cited)} PRs · {media}"),
            options=[["a", "Send now"], ["b", "Edit first"], ["c", "Skip this one"]],
            reco="a",
            why=(f"Job {job_id}. A big feature or a product's first send waits for the owner. "
                 f"Apply the answer with `factory comms decide {job_id} send|edit|skip --ask <this item's id>`."),
            sender="comms",
            thread=(release.release().get("products", {}).get(product) or {}).get("thread"),
            at=store.iso(now_utc),
        )["id"]

    with store.lock():
        c_data = comms()
        current = c_data["jobs"].get(job_id) or {}
        if current.get("status") in DONE:
            raise CommsValidationError(f"job {job_id} became {current['status']} while drafting")
        job.update({"draft": draft, "status": status, "ask_id": ask_id, "updated": store.iso(now_utc)})
        c_data["jobs"][job_id] = job
        prod_deferred = c_data["deferred"].setdefault(product, {})
        prod_deferred.update(deferred)
        save_comms(c_data)
    return job


def _cited(job: dict) -> set[int]:
    d = job.get("draft") or {}
    claims = [d.get("subject"), d.get("caption"), *(d.get("sentences") or []),
              *((d.get("video") or {}).get("frames") or [])]
    return {n for c in claims if isinstance(c, dict) for n in c.get("prs") or [] if type(n) is int}


def decide(job_id: str, choice: str, ask: str | None = None, now: datetime | None = None) -> dict:
    """Apply the owner's answer to the inbox item `ask`: send -> ready, edit -> needs-edit, skip -> skipped.

    The answer must be about the draft waiting now: an answer to an older, superseded ask is refused,
    so a draft the owner never saw cannot be approved. skip also stops a ready or failed job for good.
    """
    allowed_from = {"send": ("needs-approval",), "edit": ("needs-approval",),
                    "skip": ("needs-approval", "needs-edit", "ready", "failed", "deferred")}
    statuses = {"send": "ready", "edit": "needs-edit", "skip": "skipped"}
    if choice not in statuses:
        raise FactoryError(f"invalid choice {choice!r} (choose from send, edit, skip)")
    with store.lock():
        job = comms()["jobs"].get(job_id)
    if not job:
        raise FactoryError(f"unknown job {job_id!r}")
    if job.get("status") not in allowed_from[choice]:
        raise FactoryError(f"job {job_id} is {job.get('status')!r}: cannot {choice} it")
    if choice != "skip" and job.get("ask_id") != ask:
        raise FactoryError(f"answer {ask!r} is not about the current draft of {job_id} (waiting on {job.get('ask_id')!r})")
    if choice == "send":
        _, live, _ = _sync(job["product"], release._now_utc(now))
        gone = sorted(_cited(job) - set(live))
        if gone:
            raise FactoryError(f"PR #{gone[0]} is no longer in production: re-draft {job_id}")
    with store.lock():
        c_data = comms()
        cur = c_data["jobs"][job_id]
        if cur.get("status") != job.get("status") or cur.get("ask_id") != job.get("ask_id"):
            raise FactoryError(f"job {job_id} changed while deciding: read it again")
        cur["status"] = statuses[choice]
        cur["updated"] = store.iso(release._now_utc(now))
        save_comms(c_data)
        return cur


def check(job_id: str, now: datetime | None = None) -> dict:
    """The gate right before a send: the job is ready, every cited PR is still in production,
    and a digest repeats no PR a sent digest already announced. Returns {"ok", "reason"}."""
    with store.lock():
        c_data = comms()
    job = c_data["jobs"].get(job_id)
    if not job:
        raise FactoryError(f"unknown job {job_id!r}")
    if job.get("status") != "ready":
        return {"ok": False, "reason": f"job {job_id} is {job.get('status')!r}, want 'ready'"}
    _, live, _ = _sync(job["product"], release._now_utc(now))
    gone = sorted(_cited(job) - set(live))
    if gone:
        return {"ok": False, "reason": f"PR #{gone[0]} is no longer in production: re-draft {job_id}"}
    if job.get("kind") == "digest":
        def own(j):
            return _cited(j) - {_pr_num(p) for p in j.get("recap", [])}
        announced = {n for j in c_data["jobs"].values()
                     if j.get("product") == job["product"] and j.get("kind") == "digest"
                     and j.get("status") == "sent" for n in own(j)}
        again = sorted(own(job) & announced)
        if again:
            return {"ok": False, "reason": f"PR #{again[0]} was already announced in a sent digest: re-draft {job_id}"}
    return {"ok": True, "reason": ""}


def sent(job_id: str, count: int, now: datetime | None = None) -> dict:
    """Record a send that happened (run `check` before sending). count is read from the product at send time."""
    if type(count) is not int or count <= 0:
        raise CommsValidationError(f"bad count {count!r}: must be an integer > 0")
    now_utc = release._now_utc(now)
    with store.lock():
        c_data = comms()
        job = c_data["jobs"].get(job_id)
        if not job:
            raise FactoryError(f"unknown job {job_id!r}")
        if job.get("status") != "ready":
            raise FactoryError(f"job {job_id} status is {job.get('status')!r}, want 'ready'")
        job.update({"status": "sent", "sent_at": store.iso(now_utc), "count": count, "updated": store.iso(now_utc)})
        prod_deferred = c_data["deferred"].get(job["product"], {})
        for n in _cited(job):
            prod_deferred.pop(str(n), None)  # a scheduled change announced: no longer deferred
        save_comms(c_data)
        return job


def failed(job_id: str, reason: str) -> dict:
    """Record a failed send of a ready job; it stays due and can be re-drafted."""
    if not isinstance(reason, str) or not reason.strip():
        raise FactoryError("failure reason is empty")
    with store.lock():
        c_data = comms()
        job = c_data["jobs"].get(job_id)
        if not job:
            raise FactoryError(f"unknown job {job_id!r}")
        if job.get("status") != "ready":
            raise FactoryError(f"job {job_id} is {job.get('status')!r}: only a ready job can fail to send")
        job.update({"status": "failed", "reason": reason.strip(), "updated": store.iso(release._now_utc())})
        save_comms(c_data)
        return job


def video_failed(job_id: str, now: datetime | None = None) -> dict:
    """Record the first brag video render failure and alert the owner; 2 h later the job sends with a static card."""
    now_utc = release._now_utc(now)
    job = _load_job(job_id, now)
    with store.lock():
        c_data = comms()
        stored = c_data["jobs"].setdefault(job_id, job)
        first = not stored.get("video_failed_at")
        if first:
            stored["video_failed_at"] = store.iso(now_utc)
            stored["updated"] = store.iso(now_utc)
            save_comms(c_data)
    if first:
        at = (now_utc + timedelta(hours=2)).astimezone(release.TUNIS).strftime("%H:%M")
        store.add_ask_item(
            project="fac-release-mgr", kind="urgent",
            title=f"Brag video failed for {job_id}: static card at {at}"[:80],
            short="The email waits 2 h for the video, then sends with a static card",
            context=f"Job {job_id}. The render failed at {now_utc.astimezone(release.TUNIS):%H:%M} Tunis.",
            options=[["a", "OK, use the static card"], ["b", "Hold this email"]],
            reco="a",
            why=f"To hold it, run `factory comms decide {job_id} skip`, or retry the render before then.",
            sender="comms",
            thread=(release.release().get("products", {}).get(job["product"]) or {}).get("thread"),
            at=store.iso(now_utc),
        )
    return stored


def list_jobs(product: str | None = None) -> list[dict]:
    """List jobs, optionally filtered by product."""
    with store.lock():
        jobs = list(comms()["jobs"].values())
    if product:
        jobs = [j for j in jobs if j.get("product") == product]
    jobs.sort(key=lambda j: j.get("updated") or j.get("id"), reverse=True)
    return jobs


def relay(now: datetime | None = None) -> dict:
    """Relay due comms jobs to release threads for live, configured products (deduped per Tunis day)."""
    from . import relay as relay_mod

    now_utc = release._now_utc(now)
    today_tunis = now_utc.astimezone(release.TUNIS).date().isoformat()

    rel_data = release.release()
    configured = rel_data.get("products", {})
    global_paused = bool((rel_data.get("paused") or {}).get("*"))

    out: dict[str, dict] = {}
    for product in comms_products():
        prod_cfg = configured[product]
        if prod_cfg.get("mode") != "live":
            out[product] = {"relayed": [], "skipped": "shadow mode", "error": None}
            continue
        thread = prod_cfg.get("thread")
        if not thread:
            out[product] = {"relayed": [], "skipped": "no thread", "error": None}
            continue
        if bool((rel_data.get("paused") or {}).get(product) or global_paused):
            out[product] = {"relayed": [], "skipped": "paused", "error": None}
            continue

        try:
            due_jobs = due(product, now=now)
        except Exception as err:
            out[product] = {"relayed": [], "skipped": None, "error": str(err)}
            continue

        # A job waiting on the owner is not re-sent: their answer's relay wakes the agent, and a
        # re-draft would supersede the ask they may be reading.
        due_jobs = [j for j in due_jobs if j.get("status") != "needs-approval"]
        if not due_jobs:
            out[product] = {"relayed": [], "skipped": "no jobs due", "error": None}
            continue

        with store.lock():
            c_data = comms()
            relayed_entry = (c_data.get("relayed") or {}).get(product) or {}
            date_entry = relayed_entry.get("date")
            if date_entry == today_tunis:
                prev_job_ids = list(relayed_entry.get("jobs", []))
                already_relayed = set(prev_job_ids)
            else:
                prev_job_ids = []
                already_relayed = set()

        jobs_to_relay = [j for j in due_jobs if j["id"] not in already_relayed]
        if not jobs_to_relay:
            out[product] = {"relayed": [], "skipped": "already relayed today", "error": None}
            continue

        lines = [
            f"Release manager (factory release comms {product})",
            f"Due: {len(jobs_to_relay)} job(s)",
        ]
        for job in jobs_to_relay:
            prs_nums = [_pr_num(p) for p in job.get("prs", []) if _pr_num(p) is not None]
            recap_nums = [_pr_num(p) for p in job.get("recap", []) if _pr_num(p) is not None]
            prs_part = f"PRs {', '.join(f'#{n}' for n in prs_nums)}" if prs_nums else ""
            recap_part = f"recap {', '.join(f'#{n}' for n in recap_nums)}" if recap_nums else ""
            if prs_part and recap_part:
                details = f"{prs_part}, {recap_part}"
            elif prs_part:
                details = prs_part
            elif recap_part:
                details = recap_part
            else:
                details = ""
            status_part = f", status {job['status']}" if job.get("status") else ""
            lines.append(f"- {job['id']}: {job['kind']} email, {details}{status_part}")
        lines.append('Follow the release-manager skill, section "Release comms" (steps 2 to 9).')
        msg = "\n".join(lines)

        with store.lock():
            c_data = comms()
            prev_entry = (c_data.get("relayed") or {}).get(product)
            # Re-check under the claim lock: an overlapping run may have relayed these ids meanwhile.
            claimed_today = set((prev_entry or {}).get("jobs", [])) if (prev_entry or {}).get("date") == today_tunis else set()
            if any(j["id"] in claimed_today for j in jobs_to_relay):
                out[product] = {"relayed": [], "skipped": "already relayed today", "error": None}
                continue
            c_data.setdefault("relayed", {})[product] = {
                "date": today_tunis,
                "jobs": sorted(claimed_today) + [j["id"] for j in jobs_to_relay],
            }
            save_comms(c_data)

        try:
            relay_mod.enqueue_message(thread, msg, kind="release")
        except Exception as e:
            mine = {j["id"] for j in jobs_to_relay}
            with store.lock():
                c_data = comms()
                entry = (c_data.get("relayed") or {}).get(product)
                if entry:  # release only this run's ids; a concurrent run's claims stay
                    entry["jobs"] = [i for i in entry.get("jobs", []) if i not in mine]
                    save_comms(c_data)
            out[product] = {"relayed": [], "skipped": None, "error": str(e)}
            continue

        out[product] = {"relayed": [j["id"] for j in jobs_to_relay], "skipped": None, "error": None}

    return out
