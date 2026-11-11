"""The daily fix lane: pick approved issues in level >= 2 repos, export tasks to the fix gate,
and collect results once the agent finishes.

Plain Python, no LLM. State in ~/.factory/fixlane.json (or $FACTORY_HOME/fixlane.json).
"""
import copy
import fcntl
import json
import os
import shutil
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import board, config, relay, sources, store
from .store import FactoryError

FIXLANE_REL = "fixlane.json"
DEFAULT_MAX_PER_DAY = 3
DEFAULT_TASKS_DIR = "/var/spool/fix-gate/tasks"
DEFAULT_BASE = "develop"
TIMEOUT_HOURS = 6
FENCE = "`" * 3


class FixlaneBusy(Exception):
    pass


@contextmanager
def fixlane_flock():
    store.mkdir(store.home())
    lock_path = store.path("fixlane.lock")
    try:
        f = open(lock_path, "a+")
    except OSError as e:
        raise FactoryError(f"cannot open {lock_path}: {e}")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        f.close()
        raise FixlaneBusy()
    try:
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        f.close()


def get_tasks_dir():
    env_tasks = os.environ.get("FIXLANE_TASKS")
    if env_tasks:
        return os.path.expanduser(env_tasks)
    cfg_tasks = config.get("fixlane.tasks")
    if cfg_tasks:
        return os.path.expanduser(cfg_tasks)
    return DEFAULT_TASKS_DIR


def get_spool_dir(tasks_dir=None):
    env_spool = os.environ.get("FIXLANE_SPOOL")
    if env_spool:
        return os.path.expanduser(env_spool)
    cfg_spool = config.get("fixlane.spool")
    if cfg_spool:
        return os.path.expanduser(cfg_spool)
    t_dir = (tasks_dir or get_tasks_dir()).rstrip("/")
    return os.path.dirname(t_dir)


def load_state():
    data = store.read(FIXLANE_REL, {"tasks": {}})
    if not isinstance(data, dict):
        raise FactoryError(f"{FIXLANE_REL} is not a valid JSON object")
    if "tasks" not in data or not isinstance(data["tasks"], dict):
        data["tasks"] = {}
    return data


def save_state(state):
    store.write(FIXLANE_REL, state)


def load_rules(rules_path=None):
    path = rules_path or config.get("fixlane.rules")
    if not path:
        raise FactoryError("fixlane.rules is not configured (factory config set fixlane.rules <path>)")
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        raise FactoryError(f"cannot read fixlane rules at {expanded}")
    try:
        with open(expanded, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise FactoryError(f"fixlane rules at {expanded} is not valid JSON ({e})")
    if not isinstance(data, dict):
        raise FactoryError(f"fixlane rules at {expanded} is not a JSON object")
    return data


def match_repo_rule(repo, rules):
    """Find matching repo in rules['repos'] for a GitHub repo (e.g. 'acme/app' or 'o/r')."""
    repos_dict = rules.get("repos", {})
    if not isinstance(repos_dict, dict):
        return None, None

    # First pass: exact github match over all entries
    for gate_name, rcfg in repos_dict.items():
        if not isinstance(rcfg, dict):
            continue
        gh = rcfg.get("github")
        if gh and gh.lower() == repo.lower():
            return gate_name, rcfg

    # Second pass: gate name match
    for gate_name, rcfg in repos_dict.items():
        if not isinstance(rcfg, dict):
            continue
        if gate_name.lower() == repo.lower():
            return gate_name, rcfg
        if repo.lower().endswith("/" + gate_name.lower()):
            return gate_name, rcfg
        if gate_name.lower() == repo.lower().split("/")[-1]:
            return gate_name, rcfg
    return None, None


def resolve_base(rcfg, rules, repo=None):
    repo_bases = rcfg.get("bases")
    if not isinstance(repo_bases, list):
        repo_name = repo or rcfg.get("github") or "repo"
        sys.stderr.write(f"fixlane: {repo_name}: bases is not a list\n")
        return None
    global_bases = rules.get("bases")
    if not isinstance(global_bases, list):
        return None
    allowed = [b for b in repo_bases if b in global_bases]
    if allowed:
        return allowed[0]
    return None


def fetch_base_sha(clone_path, base):
    """Fetch exactly origin/<base> and return its SHA; any failure stops the pick (never a stale or local ref)."""
    if not os.path.isdir(clone_path):
        raise FactoryError(f"clone path does not exist: {clone_path}")
    sources.run("git", "-C", clone_path, "fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}")
    return sources.run("git", "-C", clone_path, "rev-parse", "--verify", f"refs/remotes/origin/{base}^{{commit}}").strip()


def _safe_read_task_json(task_json_path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK

    try:
        fd = os.open(task_json_path, flags)
    except OSError:
        return None

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        raw = os.read(fd, 65536)
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _resolve_group_gid(group_name):
    if group_name is None:
        return None
    try:
        gid = int(group_name)
        try:
            import grp
            grp.getgrgid(gid)
            return gid
        except (KeyError, OverflowError):
            return None
    except (ValueError, TypeError):
        try:
            import grp
            return grp.getgrnam(str(group_name)).gr_gid
        except (KeyError, ImportError, OverflowError):
            return None


def _require_group_member(gid, group_name):
    # A process started before the user joined the group lacks it, and the kernel then silently drops the
    # setgid bit on chmod: the agent's failed.txt would get its own group and the lane could not read it.
    # Cron and a fresh login get it; an older shell needs `sg <group>`.
    groups = set(os.getgroups()) | {os.getgid(), os.getegid()}
    if gid not in groups:
        raise FactoryError(
            f"this process is not in group {group_name!r} (joined after this shell started? use: sg {group_name} -c ...)"
        )


def has_task_dir(tasks_dir, repo, number, state=None):
    if not os.path.isdir(tasks_dir):
        return False
    try:
        entries = os.listdir(tasks_dir)
    except OSError:
        return False
    if state is None:
        state = load_state()
    tasks = state.get("tasks", {})
    for entry in entries:
        clean = entry.lstrip(".")
        t = tasks.get(clean)
        if t:
            t_repo = t.get("github") or t.get("repo") or ""
            t_gate = t.get("repo") or ""
            if (
                t_repo.lower() == repo.lower()
                or t_gate.lower() == repo.lower()
                or repo.lower().endswith("/" + t_gate.lower())
                or t_repo.lower().endswith("/" + repo.lower())
            ) and t.get("number") == number:
                if t.get("state") in ("running", "pushed"):
                    return True
        else:
            t_json = os.path.join(tasks_dir, entry, "task.json")
            data = _safe_read_task_json(t_json)
            if data and isinstance(data, dict):
                d_repo = data.get("repo") or ""
                if (
                    d_repo.lower() == repo.lower()
                    or repo.lower().endswith("/" + d_repo.lower())
                ) and data.get("number") == number:
                    return True
    return False


def has_task_in_state(state, repo, number, now=None):
    now = now or datetime.now(timezone.utc)
    for t in state.get("tasks", {}).values():
        t_repo = t.get("github") or t.get("repo") or ""
        t_gate = t.get("repo") or ""
        if (
            t_repo.lower() == repo.lower()
            or t_gate.lower() == repo.lower()
            or repo.lower().endswith("/" + t_gate.lower())
            or t_repo.lower().endswith("/" + repo.lower())
        ) and t.get("number") == number:
            st = t.get("state")
            if st in ("running", "pushed"):
                return True
            if st == "stuck":
                collected_at = t.get("collectedAt") or t.get("startedAt")
                if collected_at:
                    try:
                        c_dt = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
                        if (now - c_dt) < timedelta(hours=24):
                            return True
                    except (ValueError, TypeError):
                        pass
    return False


def count_started_today(state, now):
    today = now.date().isoformat()
    count = 0
    for t in state.get("tasks", {}).values():
        started = t.get("startedAt", "")
        if started.startswith(today):
            count += 1
    return count


def get_ready_issues(rules, now=None):
    products = store.products()
    if not products:
        products = {
            k: {"name": k, "repos": [rcfg.get("github") or k]}
            for k, rcfg in rules.get("repos", {}).items()
            if isinstance(rcfg, dict)
        }
    issues, repo_errors, _ = board.search_issues(products)
    open_by_closed, _ = board._collect_pr_index(products, now, repo_errors)

    candidates = []
    for it in issues:
        if (it.get("state") or "").lower() != "open":
            continue
        labels = board._labels(it)
        if "factory" not in labels:
            continue
        if "building" in labels or "needs-decision" in labels or "stuck" in labels:
            continue
        repo = board._repo_from_url(it.get("repository_url")) or board._repo_from_url(it.get("html_url"))
        number = it.get("number")
        if not repo or not number:
            continue
        if repo in repo_errors:
            continue
        linked = open_by_closed.get((repo, number), [])
        if linked:
            continue
        gate_name, rcfg = match_repo_rule(repo, rules)
        if not rcfg:
            continue
        level = rcfg.get("level", 1)
        if not isinstance(level, int) or isinstance(level, bool):
            level = 1
        if level < 2:
            continue
        base = resolve_base(rcfg, rules, repo=repo)
        if not base:
            continue
        candidates.append({
            "repo": repo,
            "gate_repo": gate_name,
            "number": number,
            "title": it.get("title", ""),
            "body": it.get("body", ""),
            "labels": sorted(labels),
            "created_at": it.get("created_at") or "",
            "url": it.get("html_url") or it.get("url") or "",
            "rcfg": rcfg,
            "base": base,
        })
    candidates.sort(key=lambda x: x["created_at"])
    return candidates


def run(dry=False, now=None):
    try:
        with fixlane_flock():
            return _run_locked(dry=dry, now=now)
    except FixlaneBusy:
        return {"locked": True}, "fixlane busy"


def _run_locked(dry=False, now=None):
    now = now or datetime.now(timezone.utc)
    enabled = config.get("fixlane.enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("true", "1", "yes")
    if not enabled:
        return {"enabled": False, "picked": []}, "fixlane is disabled (config fixlane.enabled is false)"

    rules = load_rules()
    candidates = get_ready_issues(rules, now=now)

    state = load_state()
    max_per_day = int(config.get("fixlane.max_per_day", DEFAULT_MAX_PER_DAY))
    started_today = count_started_today(state, now)
    available_today = max(0, max_per_day - started_today)

    tasks_dir = get_tasks_dir()
    filtered = []
    for c in candidates:
        if has_task_dir(tasks_dir, c["repo"], c["number"], state=state):
            continue
        if has_task_in_state(state, c["repo"], c["number"], now=now):
            continue
        filtered.append(c)

    if dry:
        to_pick = filtered[:available_today]
        lines = [f"would pick: {c['repo']}#{c['number']} ({c['gate_repo']}) - {c['title']}" for c in to_pick]
        text = "\n".join(lines) or "no issues to pick"
        return {"dry": True, "picked": [c["number"] for c in to_pick], "candidates": to_pick}, text

    picked_results = []
    for c in filtered:
        if len(picked_results) >= available_today:
            break
        utc_stamp = now.strftime("%Y%m%d%H%M")
        repo_slug = c["gate_repo"].replace("/", "-")
        task_id = f"{repo_slug}-{c['number']}-{utc_stamp}"

        tmp_dir = os.path.join(tasks_dir, f".{task_id}")
        final_dir = os.path.join(tasks_dir, task_id)
        try:
            rcfg = c["rcfg"]
            base = c.get("base") or resolve_base(rcfg, rules, repo=c["repo"])
            if not base:
                continue
            clone_path = os.path.expanduser(rcfg.get("path", ""))
            base_sha = fetch_base_sha(clone_path, base)

            os.makedirs(tasks_dir, exist_ok=True)
            # Private (0700) while it is filled: the agent can list tasks/ and must not get a window to plant a
            # symlink the factory would write through. Group access is granted only once both files exist.
            group_name = config.get("fixlane.group", "fixspool")
            gid = _resolve_group_gid(group_name)
            if gid is None:
                raise FactoryError(f"group {group_name!r} does not exist")
            _require_group_member(gid, group_name)
            os.mkdir(tmp_dir, 0o700)

            task_json = {
                "id": task_id,
                "repo": c["gate_repo"],
                "number": c["number"],
                "title": c["title"],
                "body": c["body"],
                "labels": c["labels"],
                "base": base,
                "base_sha": base_sha,
                "url": c["url"],
            }
            task_json_path = os.path.join(tmp_dir, "task.json")
            fd = os.open(task_json_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(task_json, f, indent=2)
                f.write("\n")

            tar_path = os.path.join(tmp_dir, "src.tar")
            sources.run("git", "-C", clone_path, "archive", "--format=tar", "-o", tar_path, base_sha)

            for path in (task_json_path, tar_path):
                os.chown(path, -1, gid)
                os.chmod(path, 0o640)
            os.chown(tmp_dir, -1, gid)
            os.chmod(tmp_dir, 0o3770)  # sticky: the agent may add failed.txt, never replace the two files
            os.replace(tmp_dir, final_dir)

            with store.lock():
                st = load_state()
                st.setdefault("tasks", {})[task_id] = {
                    "id": task_id,
                    "repo": c["gate_repo"],
                    "github": c["repo"],
                    "number": c["number"],
                    "title": c["title"],
                    "state": "running",
                    "labelled": False,
                    "commented": False,
                    "base": base,
                    "base_sha": base_sha,
                    "startedAt": store.iso(now),
                    "url": c["url"],
                }
                save_state(st)

            picked_results.append({"id": task_id, "repo": c["repo"], "number": c["number"]})

            labelled, commented = False, False
            try:
                _ensure_lane_labels(c["repo"])
                sources.run("gh", "issue", "edit", str(c["number"]), "--repo", c["repo"], "--add-label", "building")
                labelled = True
            except Exception as e:
                sys.stderr.write(f"fixlane: {c['repo']}#{c['number']}: {e}\n")

            try:
                sources.run("gh", "issue", "comment", str(c["number"]), "--repo", c["repo"], "--body", f"Fix lane started: {task_id}")
                commented = True
            except Exception as e:
                sys.stderr.write(f"fixlane: {c['repo']}#{c['number']}: {e}\n")

            if labelled or commented:
                with store.lock():
                    st = load_state()
                    if task_id in st.get("tasks", {}):
                        if labelled:
                            st["tasks"][task_id]["labelled"] = True
                        if commented:
                            st["tasks"][task_id]["commented"] = True
                        save_state(st)

        except Exception as e:
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
            sys.stderr.write(f"fixlane: {c['repo']}#{c['number']}: {e}\n")
            continue

    lines = [f"Fix lane started: {p['id']}" for p in picked_results]
    text = "\n".join(lines) or "no issues to pick"
    return {"dry": False, "picked": picked_results}, text


def _quoted(lines):
    """Agent- or gate-written text, shown verbatim in a code block: no mention, link or issue reference fires."""
    text = "\n".join(lines).replace(FENCE, "` ` `")
    return "Fix lane stopped:\n" + FENCE + "\n" + text + "\n" + FENCE


LANE_LABELS = {"building": ("The fix lane is working on this issue", "FBCA04"),
               "stuck": ("The fix lane stopped on this issue; see its last comment", "D93F0B")}
_labels_ready = set()


def _ensure_lane_labels(repo):
    """gh issue edit --add-label fails on a label the repo lacks: create ours once per repo per process."""
    if repo not in _labels_ready:
        for name, (desc, color) in LANE_LABELS.items():
            board._ensure_label(repo, name, desc, color)
        _labels_ready.add(repo)


def _stop(task_id, task, outcome, reasons, now):
    """Label the issue stuck, post why, record the outcome."""
    github_repo, number = task.get("github") or task.get("repo"), str(task["number"])
    _ensure_lane_labels(github_repo)
    sources.run("gh", "issue", "edit", number, "--repo", github_repo, "--remove-label", "building", "--add-label", "stuck")
    sources.run("gh", "issue", "comment", number, "--repo", github_repo, "--body", _quoted(reasons))
    _record(task_id, now, state=outcome, reasons=reasons)
    return {"id": task_id, "outcome": outcome, "reasons": reasons}


def _record(task_id, now, **fields):
    with store.lock():
        st = load_state()
        if task_id in st.get("tasks", {}):
            st["tasks"][task_id].update(fields, collectedAt=store.iso(now))
            save_state(st)


def _get_agent_uid():
    agent_user = config.get("fixlane.agent_user", "fixagent")
    try:
        return int(agent_user)
    except (ValueError, TypeError):
        pass
    try:
        import pwd
        return pwd.getpwnam(str(agent_user)).pw_uid
    except (KeyError, ImportError, OverflowError):
        return None


def read_failed_txt(failed_file):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK

    try:
        fd = os.open(failed_file, flags)
    except OSError as e:
        return False, f"failed.txt rejected ({e.strerror or e})"

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return False, "failed.txt rejected (not a regular file)"
        expected_uid = _get_agent_uid()
        if expected_uid is None:
            return False, "failed.txt rejected (cannot resolve agent_user uid)"
        if st.st_uid != expected_uid:
            return False, f"failed.txt rejected (owner {st.st_uid} != expected {expected_uid})"
        if st.st_nlink != 1:
            return False, f"failed.txt rejected (hardlink count {st.st_nlink} != 1)"
        raw = os.read(fd, 4096)
        text = raw.decode("utf-8", errors="replace").strip()
        return True, text
    except Exception as e:
        return False, f"failed.txt rejected ({e})"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def collect(now=None):
    try:
        with fixlane_flock():
            return _collect_locked(now=now)
    except FixlaneBusy:
        return {"locked": True}, "fixlane busy"


def _collect_locked(now=None):
    now = now or datetime.now(timezone.utc)
    tasks_dir = get_tasks_dir()
    spool_dir = get_spool_dir(tasks_dir)
    out_push_dir = os.path.join(spool_dir, "out", "push")
    review_thread = config.get("fixlane.review_thread")

    collected = []
    with store.lock():
        state = load_state()
        tasks = copy.deepcopy(state.get("tasks", {}))

    for task_id, task in tasks.items():
        github_repo = task.get("github") or task.get("repo")
        number = task.get("number")
        try:
            st_state = task.get("state")
            is_running = (st_state == "running")
            is_stuck_timeout = False
            if st_state == "stuck":
                reasons = task.get("reasons") or []
                if "no result in 6 h" in reasons:
                    collected_at = task.get("collectedAt")
                    if collected_at:
                        try:
                            c_dt = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
                            if (now - c_dt) < timedelta(hours=24):
                                is_stuck_timeout = True
                        except (ValueError, TypeError):
                            pass

            if not is_running and not is_stuck_timeout:
                continue

            if is_running:
                if not task.get("labelled"):
                    try:
                        _ensure_lane_labels(github_repo)
                        sources.run("gh", "issue", "edit", str(number), "--repo", github_repo, "--add-label", "building")
                        _record(task_id, now, labelled=True)
                        task["labelled"] = True
                    except Exception as e:
                        sys.stderr.write(f"fixlane: {github_repo}#{number}: {e}\n")

                if not task.get("commented"):
                    try:
                        sources.run("gh", "issue", "comment", str(number), "--repo", github_repo, "--body", f"Fix lane started: {task_id}")
                        _record(task_id, now, commented=True)
                        task["commented"] = True
                    except Exception as e:
                        sys.stderr.write(f"fixlane: {github_repo}#{number}: {e}\n")

            result_file = os.path.join(out_push_dir, f"{task_id}.json.result.json")
            failed_file = os.path.join(tasks_dir, task_id, "failed.txt")

            if os.path.isfile(result_file):
                try:
                    with open(result_file, encoding="utf-8") as f:
                        res = json.load(f)
                except Exception as e:
                    res = {"pushed": False, "reasons": [f"invalid result JSON: {e}"]}

                if res.get("pushed") is True:
                    pr = res.get("pr")
                    gate_repo = task.get("repo")
                    expected_head = f"agent/{gate_repo}-{number}"
                    expected_sha = res.get("head_sha")
                    try:
                        out = sources.run("gh", "pr", "view", str(pr), "--repo", github_repo, "--json", "headRefName,headRefOid,url")
                        pr_data = json.loads(out)
                    except Exception as e:
                        sys.stderr.write(f"fixlane: {github_repo}#{number}: pr check failed: {e}\n")
                        continue

                    if not (
                        pr_data.get("headRefName") == expected_head
                        and pr_data.get("headRefOid") == expected_sha
                        and pr_data.get("url")
                    ):
                        if is_running:
                            collected.append(_stop(task_id, task, "refused", ["gate result does not match this task"], now))
                        continue

                    pr_url = pr_data["url"]
                    sources.run("gh", "issue", "comment", str(number), "--repo", github_repo, "--body", pr_url)
                    if is_stuck_timeout:
                        sources.run("gh", "issue", "edit", str(number), "--repo", github_repo, "--remove-label", "stuck")
                    if review_thread:
                        msg_text = (
                            f"factory fixlane: gate PR {pr_url} for {github_repo}#{number} is open; "
                            "run the review loop on it (factory/review, then the merge ask)."
                        )
                        relay.enqueue_message(review_thread, msg_text, kind="autopilot")
                    _record(task_id, now, state="pushed", pr=pr, pr_url=pr_url)
                    collected.append({"id": task_id, "outcome": "pushed", "pr_url": pr_url})
                    continue
                else:
                    if is_running:
                        reasons = res.get("reasons") or []
                        if not isinstance(reasons, list):
                            reasons = [str(reasons)]
                        collected.append(_stop(task_id, task, "refused", [str(r)[:200] for r in reasons[:5]] or ["refused by the gate"], now))
                    continue

            if is_stuck_timeout:
                continue

            if os.path.lexists(failed_file):
                ok, text_or_err = read_failed_txt(failed_file)
                if ok:
                    reasons = [text_or_err[:200] or "agent failure"]
                else:
                    reasons = [text_or_err]
                collected.append(_stop(task_id, task, "failed", reasons, now))
                continue

            started_at_str = task.get("startedAt")
            if started_at_str:
                try:
                    started_dt = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
                    if (now - started_dt) >= timedelta(hours=TIMEOUT_HOURS):
                        collected.append(_stop(task_id, task, "stuck", ["no result in 6 h"], now))
                        continue
                except (ValueError, TypeError):
                    pass
        except Exception as e:
            sys.stderr.write(f"fixlane: {github_repo}#{number}: {e}\n")
            continue

    lines = [f"{c['id']}: {c['outcome']}" for c in collected]
    text = "\n".join(lines) or "no tasks to collect"
    return {"collected": collected}, text


def retry(target):
    if "#" not in target:
        raise FactoryError(f"bad issue reference {target!r}: use <repo>#<number>, e.g. owner/repo#123")
    repo_part, _, num_str = target.rpartition("#")
    try:
        number = int(num_str)
    except ValueError:
        raise FactoryError(f"bad issue number in {target!r}")

    with store.lock():
        st = load_state()
        tasks = st.get("tasks", {})
        matching = []
        for tid, t in tasks.items():
            t_repo = t.get("github") or t.get("repo") or ""
            t_gate = t.get("repo") or ""
            if (
                t_repo.lower() == repo_part.lower()
                or t_gate.lower() == repo_part.lower()
                or repo_part.lower().endswith("/" + t_gate.lower())
                or t_repo.lower().endswith("/" + repo_part.lower())
            ) and t.get("number") == number:
                matching.append(t)

        if not matching:
            raise FactoryError(f"no task found for {target}")

        matching.sort(key=lambda x: x.get("startedAt") or "", reverse=True)
        latest = matching[0]

        latest_state = latest.get("state")
        if latest_state == "running":
            raise FactoryError(f"task {latest['id']} is running")

        github_repo = latest.get("github") or latest.get("repo") or repo_part

        if latest_state == "pushed":
            pr = latest.get("pr")
            pr_url = latest.get("pr_url")
            if not pr and pr_url:
                pr = pr_url.rstrip("/").split("/")[-1]
            if pr:
                out = sources.run("gh", "pr", "view", str(pr), "--repo", github_repo, "--json", "state,url")
                pr_data = json.loads(out)
                if (pr_data.get("state") or "").upper() == "OPEN":
                    url = pr_data.get("url") or pr_url or f"https://github.com/{github_repo}/pull/{pr}"
                    raise FactoryError(f"close or merge PR {url} and delete its branch first")

        sources.run("gh", "issue", "edit", str(number), "--repo", github_repo, "--remove-label", "stuck", "--remove-label", "building")

        latest["state"] = "retired"
        latest["retiredAt"] = store.now_iso()
        save_state(st)

    return latest, f"ok: retired task {latest['id']} for {target}"


def status():
    state = load_state()
    tasks = list(state.get("tasks", {}).values())
    tasks.sort(key=lambda x: x.get("startedAt") or "", reverse=True)
    lines = [
        f"{t.get('id')}\t{t.get('repo')}\t#{t.get('number')}\t{t.get('state')}\t{t.get('startedAt')}"
        for t in tasks
    ]
    text = "\n".join(lines) or "no fixlane tasks"
    return {"tasks": tasks}, text


def doctor():
    rules_val = config.get("fixlane.rules")
    if not rules_val:
        return "not configured"
    rules_path = os.path.expanduser(rules_val)
    if not os.path.exists(rules_path):
        raise FactoryError(f"cannot read fixlane rules at {rules_path}")
    if not os.access(rules_path, os.R_OK):
        raise FactoryError(f"fixlane rules at {rules_path} is not readable")
    try:
        with open(rules_path, encoding="utf-8") as f:
            json.load(f)
    except Exception as e:
        raise FactoryError(f"fixlane rules at {rules_path} is not valid JSON ({e})")

    tasks_dir = get_tasks_dir()
    if os.path.exists(tasks_dir):
        if not os.access(tasks_dir, os.W_OK):
            raise FactoryError(f"tasks dir {tasks_dir} is not writable")
    else:
        parent = os.path.dirname(tasks_dir)
        if not os.path.exists(parent) or not os.access(parent, os.W_OK):
            raise FactoryError(f"tasks dir {tasks_dir} does not exist and {parent} is not writable")

    enabled = config.get("fixlane.enabled", False)
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("true", "1", "yes")

    if enabled:
        group_name = config.get("fixlane.group", "fixspool")
        gid = _resolve_group_gid(group_name)
        if gid is None:
            raise FactoryError(f"fixlane group {group_name!r} does not exist")
        _require_group_member(gid, group_name)

        review_thread = config.get("fixlane.review_thread")
        if not review_thread:
            raise FactoryError("fixlane.review_thread is not configured")
        spool_dir = get_spool_dir(tasks_dir)
        out_push = os.path.join(spool_dir, "out", "push")
        if not os.path.exists(out_push) or not os.access(out_push, os.R_OK):
            raise FactoryError(f"push dir {out_push} is not readable")

    return "ok"
