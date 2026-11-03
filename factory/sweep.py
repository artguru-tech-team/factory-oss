"""The error sweep: read new/busy production errors from Sentry and SigNoz every morning
and file each, once, as an `intake` issue in the right product repo.

Plain Python, no LLM. Sentry is read with a read-only auth token, SigNoz with a Viewer-role
API key. Issues are filed with the `intake` label plus `source:sentry` / `source:signoz`.
The Sentry and SigNoz parts are independent: one failing never stops the other.
"""
import copy
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import secrets, sources, store
from .store import FactoryError

SWEEP_REL = "sweep.json"
HISTORY_MAX = 30

# `intake` is shared with the board; source:* labels are created by the sweep as needed.
INTAKE_LABEL = "intake"
INTAKE_LABEL_DESC = "Filed by an automated error sweep"
INTAKE_LABEL_COLOR = "7057ff"
SOURCE_LABEL_DESC = {"sentry": "Filed by the Sentry sweep", "signoz": "Filed by the SigNoz sweep"}
SOURCE_LABEL_COLOR = {"sentry": "3626e3", "signoz": "e16537"}

DEFAULT_SWEEP = {
    "sentry": {"base_url": "https://de.sentry.io", "org": "", "projects": {}},
    "signoz": {"base_url": "", "services": {}, "default_repo": None},
    "min_events": 5,
    "max_per_repo": 5,
    "include_performance": False,
    # Next.js control-flow signals (notFound(), redirect()) are thrown as errors but are not bugs
    "ignore": ["NEXT_HTTP_ERROR_FALLBACK", "NEXT_REDIRECT", "NEXT_NOT_FOUND"],
    "history": [],
}

SENTRY_PAGES_MAX = 5
SIGNOZ_LIMIT = 50


# ---------- config ----------


def sweep():
    """sweep.json, refused unless it has the expected shape (a bad shape would crash a run)."""
    data = store.read(SWEEP_REL, copy.deepcopy(DEFAULT_SWEEP))
    ok = isinstance(data, dict)
    if ok:
        sentry = data.get("sentry")
        signoz = data.get("signoz")
        ok = (
            isinstance(sentry, dict)
            and isinstance(sentry.get("base_url"), str)
            and isinstance(sentry.get("org"), str)
            and isinstance(sentry.get("projects"), dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in sentry.get("projects", {}).items())
            and isinstance(signoz, dict)
            and isinstance(signoz.get("base_url"), str)
            and isinstance(signoz.get("services"), dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in signoz.get("services", {}).items())
            and (signoz.get("default_repo") is None or isinstance(signoz.get("default_repo"), str))
            and type(data.get("min_events")) is int and data.get("min_events") >= 1
            and type(data.get("max_per_repo")) is int and data.get("max_per_repo") >= 1
            and isinstance(data.get("include_performance"), bool)
            and isinstance(data.get("ignore", []), list) and all(isinstance(x, str) for x in data.get("ignore", []))
            and isinstance(data.get("history"), list)
        )
    if not ok:
        raise FactoryError(f"{store.path(SWEEP_REL)} is not a sweep config: fix or restore it")
    return data


def save_sweep(data):
    store.write(SWEEP_REL, data)


def _sentry_conf(cfg):
    sentry = cfg.get("sentry") or {}
    base = (sentry.get("base_url") or "").rstrip("/")
    org = sentry.get("org") or ""
    if not base or not org:
        raise FactoryError("sweep sentry is not configured (factory sweep config set sentry.base_url <url>, sentry.org <slug>)")
    if not (base.startswith("http://") or base.startswith("https://")):
        raise FactoryError(f"bad sentry.base_url {base!r}: must start with http:// or https://")
    return base, org


def _signoz_conf(cfg):
    signoz = cfg.get("signoz") or {}
    base = (signoz.get("base_url") or "").rstrip("/")
    if not base:
        raise FactoryError("sweep signoz is not configured (factory sweep config set signoz.base_url <url>)")
    if not (base.startswith("http://") or base.startswith("https://")):
        raise FactoryError(f"bad signoz.base_url {base!r}: must start with http:// or https://")
    return base


# ---------- HTTP (stdlib urllib only) ----------


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlparse(newurl).netloc or newurl
        raise FactoryError(f"redirected to {host}: refusing to send credentials")


_opener = urllib.request.build_opener(NoRedirectHandler)


USER_AGENT = "factory-sweep/1.0"


def _request(method, url, headers=None, body=None, timeout=30):
    """One HTTP request. Returns (status, headers, body bytes). Raises FactoryError on any
    failure; the message carries the URL, never the Authorization header value."""
    # a named User-Agent: proxies such as Cloudflare block Python's default "Python-urllib/x.y" (403)
    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT, **(headers or {})})
    if body is not None:
        req.data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except FactoryError:
        raise
    except urllib.error.HTTPError as e:
        raise FactoryError(f"{method} {url} -> HTTP {e.code}")
    except urllib.error.URLError as e:
        raise FactoryError(f"{method} {url} -> {e.reason}")


def _json(method, url, headers=None, body=None, source="source"):
    status, hdrs, raw = _request(method, url, headers=headers, body=body)
    if not raw:
        return status, hdrs, None
    try:
        return status, hdrs, json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FactoryError(f"{source} returned non-JSON (login page?)")


def _iso(ts):
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def _parse_ts(ts):
    if not ts:
        return None
    if isinstance(ts, (int, float)):
        sec = ts / 1e9 if ts > 1e11 else ts
        return datetime.fromtimestamp(sec, timezone.utc)
    s = str(ts).strip()
    if s.isdigit():
        val = int(s)
        sec = val / 1e9 if val > 1e11 else val
        return datetime.fromtimestamp(sec, timezone.utc)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _within_24h(ts, now):
    try:
        dt = _parse_ts(ts)
        return dt is not None and dt >= now - timedelta(hours=24)
    except (ValueError, TypeError):
        return False


def _err_text(e):
    """Error text safe for history, logs and doctor. Our own FactoryErrors and network errors keep
    their message; anything else (e.g. http.client's ValueError, which quotes header values)
    keeps only its class name, so a credential can never reach sweep.json or a log."""
    if isinstance(e, (FactoryError, urllib.error.URLError, TimeoutError, json.JSONDecodeError)):
        return (str(e).splitlines() or [type(e).__name__])[0]
    return type(e).__name__


def _origin(url):
    u = urllib.parse.urlparse(url)
    return u.scheme.lower(), (u.hostname or "").lower(), u.port or {"https": 443, "http": 80}.get(u.scheme.lower())


def _to_ns_str(ts):
    if ts is None:
        return ""
    if isinstance(ts, (int, float)):
        return str(int(ts))
    s = str(ts).strip()
    if s.isdigit():
        return s
    # exact nanoseconds: SigNoz matches the timestamp exactly, so no float and no microsecond cut
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$", s)
    if not m:
        return s
    try:
        dt = datetime.fromisoformat(m.group(1) + (m.group(3).replace("Z", "+00:00")))
    except ValueError:
        return s
    frac = int((m.group(2) or "0").ljust(9, "0"))
    return str(int(dt.timestamp()) * 1_000_000_000 + frac)


# ---------- Injection protection ----------


def _clean_source_text(s):
    if s is None:
        return ""
    s = str(s)
    while "<!--" in s or "-->" in s:  # repeat: "<<!--!--" must not leave a "<!--" behind
        s = s.replace("<!--", "").replace("-->", "")
    return s


def _safe_inline(text):
    text = " ".join(_clean_source_text(text).split())  # a newline would end the code span
    if not text:
        return "—"
    runs = re.findall(r"`+", text)
    max_run = max((len(r) for r in runs), default=0)
    delim = "`" * (max_run + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{delim}{pad}{text}{pad}{delim}"


def _safe_fence(lines):
    if isinstance(lines, list):
        text = "\n".join(_clean_source_text(line) for line in lines)
    else:
        text = _clean_source_text(lines)
    runs = re.findall(r"`+", text)
    max_run = max((len(r) for r in runs), default=0)
    fence = "`" * max(3, max_run + 1)
    return [fence] + text.splitlines() + [fence]


def _format_title(source, title):
    clean = re.sub(r"[\r\n]+", " ", _clean_source_text(title)).strip()
    return clean[:200]


# ---------- Sentry ----------


def _link_next(headers, base_url=None):
    """The next-page URL from a `Link` header, or None. Follows Sentry's
    `rel="next"; results="true"` cursor and stops when results="false".
    The Sentry Link next URL is followed only when its scheme+host equal base_url's."""
    link = headers.get("Link") or headers.get("link")
    if not link:
        return None
    for part in link.split(","):
        if 'rel="next"' not in part:
            continue
        if 'results="false"' in part:
            return None
        try:
            nxt = part[part.index("<") + 1:part.index(">")]
        except ValueError:
            return None
        if base_url:
            if _origin(nxt) != _origin(base_url):
                return None
        return nxt
    return None


def _sentry_issues(base, org, project, token):
    """All pages (max SENTRY_PAGES_MAX) of unresolved errors for one project."""
    path = (f"{base}/api/0/projects/{urllib.parse.quote(org)}/{urllib.parse.quote(project)}/issues/"
            "?query=is:unresolved%20lastSeen:-24h&statsPeriod=24h")
    headers = {"Authorization": f"Bearer {token}"}
    issues, url = [], path
    for _ in range(SENTRY_PAGES_MAX):
        status, hdrs, data = _json("GET", url, headers=headers, source="sentry")
        if status != 200:
            raise FactoryError(f"GET {url} -> HTTP {status}")
        if isinstance(data, list):
            issues.extend(data)
        nxt = _link_next(hdrs, base_url=base)
        if not nxt:
            break
        url = nxt
    return issues


def _sentry_event(base, org, issue_id, token):
    status, _, data = _json(
        "GET", f"{base}/api/0/organizations/{urllib.parse.quote(org)}/issues/{issue_id}/events/latest/",
        headers={"Authorization": f"Bearer {token}"}, source="sentry")
    if status != 200:
        raise FactoryError(f"GET latest event -> HTTP {status}")
    return data or {}


def _events_24h(issue):
    total = 0
    for bucket in (issue.get("stats") or {}).get("24h") or []:
        if isinstance(bucket, (list, tuple)) and len(bucket) > 1 and isinstance(bucket[1], (int, float)):
            total += bucket[1]
    return int(total)


def ignored(text, cfg):
    """True when the error text contains one of the config's ignore substrings (case-sensitive)."""
    patterns = cfg.get("ignore", DEFAULT_SWEEP["ignore"])
    return any(p.strip() and p in (text or "") for p in patterns)


def keep_sentry(issue, cfg, now):
    """Whether an unresolved Sentry issue is worth filing: error category always (performance
    only with include_performance), and busy (24 h events >= min_events), new, or regressed."""
    if ignored(f"{issue.get('title') or ''} {(issue.get('metadata') or {}).get('value') or ''}", cfg):
        return False
    cat = issue.get("issueCategory") or ""
    if cat == "performance":
        if not cfg.get("include_performance"):
            return False
    elif cat != "error":
        return False
    if _events_24h(issue) >= cfg["min_events"]:
        return True
    if _within_24h(issue.get("firstSeen"), now):
        return True
    return issue.get("substatus") == "regressed"


def sentry_frames(event):
    """Up to 8 in-app stack frames (filename, function, lineNo) from the exception entry,
    using the LAST exception value that has in-app frames (chained exceptions)."""
    last_frames = []
    for entry in event.get("entries") or []:
        if entry.get("type") != "exception":
            continue
        for value in (entry.get("data") or {}).get("values") or []:
            frames = (value.get("stacktrace") or {}).get("frames") or []
            in_app = [
                {"filename": f.get("filename") or "", "function": f.get("function") or "",
                 "lineNo": f.get("lineNo")}
                for f in frames if f.get("inApp")
            ]
            if in_app:
                last_frames = in_app
    return last_frames[:8]


def collect_sentry(cfg, now):
    """Kept Sentry issues as candidate dicts, one per mapped project."""
    token = secrets.get_secret("sentry-token")
    base, org = _sentry_conf(cfg)
    sentry = cfg.get("sentry") or {}
    out = []
    for project, repo in (sentry.get("projects") or {}).items():
        for issue in _sentry_issues(base, org, project, token):
            if not keep_sentry(issue, cfg, now):
                continue
            key = str(issue.get("shortId") or issue.get("id"))
            out.append({
                "source": "sentry",
                "repo": repo,
                "key": key,
                "marker": marker("sentry", key),
                "count": _events_24h(issue),
                "raw": issue,
                "lastSeen": issue.get("lastSeen"),
                "title": f"[Sentry] {issue.get('title') or 'Unknown error'}",
            })
    return out, []


# ---------- SigNoz ----------


def _ns(now):
    return now.timestamp() * 1_000_000_000


def signoz_list(base, key, now, limit=SIGNOZ_LIMIT):
    """POST /api/v1/listErrors over the last 24 h, ordered by exceptionCount. SigNoz reads
    `start`/`end` as nanosecond timestamps encoded as JSON strings."""
    end_ns = int(_ns(now))
    start_ns = int(_ns(now - timedelta(hours=24)))
    body = {"start": str(start_ns), "end": str(end_ns), "order": "descending",
            "orderParam": "exceptionCount", "limit": limit, "offset": 0}
    status, _, data = _json("POST", f"{base}/api/v1/listErrors",
                            headers={"SIGNOZ-API-KEY": key}, body=body, source="signoz")
    if status != 200:
        raise FactoryError(f"POST listErrors -> HTTP {status}")
    if isinstance(data, dict):
        data = data.get("data") or data.get("errors") or data.get("list") or []
    return data if isinstance(data, list) else []


def signoz_detail(base, key, group_id, last_seen):
    """GET /api/v1/errorFromGroupID for the stacktrace at the group's last seen time."""
    ts_ns = _to_ns_str(last_seen)
    status, _, data = _json(
        "GET", f"{base}/api/v1/errorFromGroupID?groupID={urllib.parse.quote(str(group_id))}"
               f"&timestamp={urllib.parse.quote(ts_ns)}",
        headers={"SIGNOZ-API-KEY": key}, source="signoz")
    if status != 200:
        raise FactoryError(f"GET errorFromGroupID -> HTTP {status}")
    return data or {}


def signoz_frames(stacktrace):
    """The first 8 non-empty lines of a stacktrace string."""
    return [ln for ln in (stacktrace or "").splitlines() if ln.strip()][:8]


def keep_signoz(group, cfg, now):
    if ignored(f"{group.get('exceptionType') or ''} {group.get('exceptionMessage') or ''}", cfg):
        return False
    if (group.get("exceptionCount") or 0) >= cfg["min_events"]:
        return True
    return _within_24h(group.get("firstSeen"), now)


def collect_signoz(cfg, now):
    """Kept SigNoz error groups as candidate dicts, mapped to repos. Returns
    (candidates, unmapped_service_names) so the run can report groups with no repo."""
    key = secrets.get_secret("signoz-key")
    base = _signoz_conf(cfg)
    signoz = cfg.get("signoz") or {}
    out, unmapped = [], []
    for group in signoz_list(base, key, now):
        if not keep_signoz(group, cfg, now):
            continue
        service = group.get("serviceName") or ""
        repo = (signoz.get("services") or {}).get(service) or signoz.get("default_repo")
        if not repo:
            unmapped.append(service or "?")
            continue
        group_id = str(group.get("groupID") or "")
        out.append({
            "source": "signoz",
            "repo": repo,
            "key": group_id,
            "marker": marker("signoz", group_id),
            "count": int(group.get("exceptionCount") or 0),
            "raw": group,
            "lastSeen": group.get("lastSeen"),
            "title": f"[SigNoz] {group.get('exceptionType') or 'error'}: "
                     f"{(group.get('exceptionMessage') or '')[:80]}",
        })
    return out, unmapped


# ---------- issue bodies ----------


def marker(source, key):
    return f"factory-sweep: {source}:{key}"


def _fmt_ts(ts):
    return ts if isinstance(ts, str) else ""


def sentry_body(issue, frames):
    lines = [
        f"24h events: {_events_24h(issue)}",
        f"Total events: {issue.get('count') if issue.get('count') is not None else '—'}",
        f"Users: {issue.get('userCount') if issue.get('userCount') is not None else '—'}",
        f"First seen: {_fmt_ts(issue.get('firstSeen'))}",
        f"Last seen: {_fmt_ts(issue.get('lastSeen'))}",
        f"Culprit: {_safe_inline(issue.get('culprit'))}",
        f"Link: {issue.get('permalink') or ''}",
    ]
    if frames:
        frame_lines = [f"{f['filename']}:{f['lineNo']} in {f['function']}" for f in frames]
        lines += ["", "Stack (in-app frames):"] + _safe_fence(frame_lines)
    lines += ["", marker("sentry", issue.get("shortId") or issue.get("id"))]
    return "\n".join(lines)


def signoz_body(group, frames, base):
    group_id = str(group.get("groupID") or "")
    ts_ns = _to_ns_str(group.get("lastSeen"))
    link = f"{base}/error-detail?groupId={urllib.parse.quote(group_id)}"
    if ts_ns:
        link += f"&timestamp={urllib.parse.quote(ts_ns)}"
    lines = [
        f"24h events: {group.get('exceptionCount') if group.get('exceptionCount') is not None else '—'}",
        f"Service: {_safe_inline(group.get('serviceName'))}",
        f"First seen: {_fmt_ts(group.get('firstSeen'))}",
        f"Last seen: {_fmt_ts(group.get('lastSeen'))}",
        f"Link: {link}",
    ]
    if frames:
        lines += ["", "Stack:"] + _safe_fence(frames)
    lines += ["", marker("signoz", group_id)]
    return "\n".join(lines)


# ---------- filing ----------


def list_sweep_issues(repo, source):
    """{marker: issue} for this repo's existing sweep issues, read once per run with the REST
    issues API (not search: `gh issue list --label` goes through the search index, which lags).
    Only bodies whose LAST non-empty line is exactly a marker count. An open issue wins; among
    closed ones the most recently closed wins. A read failure raises: the caller must not file
    blind (that would duplicate every open issue)."""
    items = []
    for page in range(1, 11):
        url = (f"repos/{repo}/issues?state=all&per_page=100&page={page}&labels="
               + urllib.parse.quote(f"source:{source}"))
        batch = sources.gh_json("api", url)
        if not isinstance(batch, list):
            raise FactoryError(f"listing {repo} issues returned {type(batch).__name__}")
        items.extend(it for it in batch if "pull_request" not in it)
        if len(batch) < 100:
            break
    by_marker = {}
    prefix = f"factory-sweep: {source}:"
    for it in items:
        lines = [ln.strip() for ln in (it.get("body") or "").splitlines() if ln.strip()]
        if not lines or not lines[-1].startswith(prefix):
            continue
        issue = {"number": it.get("number"), "state": (it.get("state") or "").upper(),
                 "stateReason": it.get("state_reason") or "", "closedAt": it.get("closed_at")}
        cur = by_marker.get(lines[-1])
        if cur is None or _better_prior(issue, cur):
            by_marker[lines[-1]] = issue
    return by_marker


def _better_prior(new, cur):
    if cur["state"] == "OPEN":
        return False
    if new["state"] == "OPEN":
        return True
    return (new.get("closedAt") or "") > (cur.get("closedAt") or "")


def candidate_body(c, cfg):
    """The issue body, with stack frames when the detail call works (a failing detail call files
    the issue without frames)."""
    frames = []
    if c["source"] == "sentry":
        try:
            base, org = _sentry_conf(cfg)
            frames = sentry_frames(_sentry_event(base, org, c["raw"].get("id"), secrets.get_secret("sentry-token")))
        except Exception:
            frames = []
        return sentry_body(c["raw"], frames)
    base = _signoz_conf(cfg)
    try:
        detail = signoz_detail(base, secrets.get_secret("signoz-key"), c["key"], c.get("lastSeen"))
        frames = signoz_frames(detail.get("exceptionStacktrace"))
    except Exception:
        frames = []
    return signoz_body(c["raw"], frames, base)


def ensure_labels(repo, source):
    from . import board
    board._ensure_label(repo, INTAKE_LABEL, INTAKE_LABEL_DESC, INTAKE_LABEL_COLOR)
    label = f"source:{source}"
    board._ensure_label(repo, label, SOURCE_LABEL_DESC[source], SOURCE_LABEL_COLOR[source])


def create_issue(repo, title, body, source):
    out = sources.run("gh", "issue", "create", "--repo", repo, "--title", title, "--body", body,
                      "--label", INTAKE_LABEL, "--label", f"source:{source}")
    number = (out or "").strip().rstrip("/").split("/")[-1]
    try:
        return int(number)
    except ValueError:
        return number


# ---------- run ----------


def collect(source, cfg, now):
    if source == "sentry":
        return collect_sentry(cfg, now)
    if source == "signoz":
        return collect_signoz(cfg, now)
    raise FactoryError(f"unknown source {source!r}")


def run(source="all", dry=False, now=None):
    now = now or datetime.now(timezone.utc)
    cfg = sweep()
    which = ["sentry", "signoz"] if source == "all" else [source]
    results, filed, errors = [], [], {}
    candidates, skipped = [], {}

    for src in which:
        try:
            cands, unmapped = collect(src, cfg, now)
            candidates.extend(cands)
            for svc in unmapped:
                skipped.setdefault("no repo mapped", []).append(f"signoz {svc}")
                results.append(f"signoz: skipped no repo mapped for service {svc}")
        except Exception as e:
            # Exception class + first line, and never header values
            err_msg = _err_text(e)
            errors[src] = err_msg
            results.append(f"{src}: error: {err_msg}")

    # biggest 24 h count first, then per-repo cap and dedupe, then file
    candidates.sort(key=lambda c: c["count"], reverse=True)
    per_repo, ensured = {}, set()
    seen_markers = set()
    repo_issues = {}

    def get_prior_issues(repo, src):
        k = (repo, src)
        if k not in repo_issues:
            repo_issues[k] = list_sweep_issues(repo, src)
        return repo_issues[k]

    for c in candidates:
        repo = c["repo"]
        m = c["marker"]
        key = c["key"]
        src = c["source"]

        # Item 2: In-run seen set of markers: never file the same marker twice in one run
        if m in seen_markers:
            skipped.setdefault("already filed", []).append(f"{src} {repo} {key}")
            results.append(f"{src} {repo} {key}: skipped (already filed in run)")
            continue

        try:
            priors = get_prior_issues(repo, src)
        except Exception as e:  # never file blind: that would duplicate every open issue
            skipped.setdefault("cannot list existing issues", []).append(f"{src} {repo} {key}")
            results.append(f"{src} {repo} {key}: skipped (cannot list existing issues: {_err_text(e)})")
            continue
        prior = priors.get(m)
        regression_of = None

        if prior is not None:
            st = (prior.get("state") or "").upper()
            num = prior.get("number")
            if st == "OPEN":
                seen_markers.add(m)
                skipped.setdefault("already filed", []).append(f"{src} {repo} {key}")
                results.append(f"{src} {repo} {key}: skipped (already filed #{num})")
                continue
            elif st == "CLOSED":
                reason = (prior.get("stateReason") or "").lower()
                if reason in ("not_planned", "not planned"):
                    seen_markers.add(m)
                    skipped.setdefault("dismissed", []).append(f"{src} {repo} {key}")
                    results.append(f"{src} {repo} {key}: skipped (dismissed #{num})")
                    continue
                elif reason == "completed":
                    closed_at = _parse_ts(prior.get("closedAt"))
                    last_seen = _parse_ts(c.get("lastSeen"))
                    if last_seen and closed_at and last_seen > closed_at:
                        regression_of = num
                    else:
                        seen_markers.add(m)
                        skipped.setdefault("already filed", []).append(f"{src} {repo} {key}")
                        results.append(f"{src} {repo} {key}: skipped (already filed #{num})")
                        continue
                else:
                    seen_markers.add(m)
                    skipped.setdefault("dismissed", []).append(f"{src} {repo} {key}")
                    results.append(f"{src} {repo} {key}: skipped (dismissed #{num})")
                    continue

        if per_repo.get(repo, 0) >= cfg["max_per_repo"]:
            seen_markers.add(m)
            skipped.setdefault("cap", []).append(f"{src} {repo} {key}")
            results.append(f"{src} {repo} {key}: skipped (cap)")
            continue

        if dry:
            per_repo[repo] = per_repo.get(repo, 0) + 1
            seen_markers.add(m)
            results.append(f"{src} {repo} {key}: would file: {_format_title(src, c['title'])} ({c['count']} in 24 h)")
            continue

        body = candidate_body(c, cfg)  # detail fetch only for issues that will be filed

        if regression_of is not None:
            body = f"Regression of #{regression_of}\n\n{body}"

        title = _format_title(src, c["title"])

        try:  # one failing issue never stops the run
            if (repo, src) not in ensured:
                ensure_labels(repo, src)
                ensured.add((repo, src))
            number = create_issue(repo, title, body, src)
            per_repo[repo] = per_repo.get(repo, 0) + 1
            seen_markers.add(m)
            filed.append({"repo": repo, "number": number, "source": src, "title": title})
            results.append(f"{src} {repo} {key}: filed #{number}")
        except Exception as e:
            err_line = str(e).splitlines()[0] if str(e) else type(e).__name__
            skipped[m] = f"error: {err_line}"
            results.append(f"{src} {repo} {key}: error: {err_line}")

    summary = {"source": source, "dry": dry, "swept": len(candidates),
               "filed": [{"repo": f["repo"], "number": f["number"], "source": f["source"]} for f in filed],
               "skipped": skipped, "error": errors}
    if not dry:
        _record_history(cfg, now, summary)
    text = "\n".join(results) or "no errors to file"
    return summary, text


def _record_history(cfg, now, summary):
    """Append one run to history under the lock, re-reading sweep.json so a concurrent
    `sweep map` / `config set` is kept."""
    source, swept = summary.get("source", "all"), summary.get("swept", 0)
    filed, skipped, errors = summary.get("filed", []), summary.get("skipped", {}), summary.get("error", {})
    entry = {
        "at": store.iso(now),
        "source": source,
        "swept": swept,
        "filed": [{"repo": f["repo"], "number": f["number"], "source": f["source"]} for f in filed],
        "skipped": skipped,
        "error": errors,
    }
    with store.lock():
        fresh = sweep()
        hist = fresh.setdefault("history", [])
        hist.append(entry)
        del hist[:-HISTORY_MAX]
        save_sweep(fresh)
        cfg["history"] = hist


# ---------- doctor ----------


def doctor_sentry():
    if not os.path.exists(store.path(SWEEP_REL)):
        return "not configured"
    cfg = sweep()
    sentry = cfg.get("sentry") or {}
    base = (sentry.get("base_url") or "").rstrip("/")
    org = sentry.get("org") or ""
    if not base or not org:
        return "not configured"
    token = secrets.get_secret("sentry-token")
    _sentry_conf(cfg)
    status, _, _ = _json("GET", f"{base}/api/0/organizations/{urllib.parse.quote(org)}/",
                         headers={"Authorization": f"Bearer {token}"}, source="sentry")
    if status != 200:
        raise FactoryError(f"GET organizations -> HTTP {status}")
    return "ok"


def doctor_signoz():
    if not os.path.exists(store.path(SWEEP_REL)):
        return "not configured"
    cfg = sweep()
    signoz = cfg.get("signoz") or {}
    base = (signoz.get("base_url") or "").rstrip("/")
    if not base:
        return "not configured"
    key = secrets.get_secret("signoz-key")
    _signoz_conf(cfg)
    signoz_list(base, key, datetime.now(timezone.utc), limit=1)
    return "ok"


def doctor_checks():
    out = []

    def check(name, fn):
        try:
            res = fn()
            out.append((name, True, res or "ok"))
        except Exception as e:
            out.append((name, False, str(e)))

    check("sweep sentry", doctor_sentry)
    check("sweep signoz", doctor_signoz)
    return out
