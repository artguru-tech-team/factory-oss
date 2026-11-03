"""Skills registry and usage metrics across Claude, agents, and T3 threads.

Reads:
- ~/.claude/skills/*/SKILL.md
- ~/.agents/skills/*/SKILL.md
- ~/.claude/plugins/installed_plugins.json (plugin skills with SKILL.md)
- ~/.claude/projects/**/*.jsonl (last 30 days) for real skill calls
- ~/.t3/userdata/statev2.sqlite (read-only) to map nativeId -> T3 thread -> agent handle

Antigravity and OpenCode skill calls are not counted (they do not record Skill tool calls
or slash commands in Claude transcript format).
"""
import glob
import hashlib
import json
import os
import re
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone

from . import store, t3

GROUPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills_groups.json")

GROUP_LABELS = {
    "all": "All",
    "orchestration": "Orchestration",
    "review": "Quality & Review",
    "operations": "Operations",
    "platform": "Platform & Frameworks",
    "browsing": "Browsing & Research",
    "design": "Design & Frontend",
    "security": "Security & Secrets",
    "other": "Other",
}

CACHE_FILE = "skills-cache.json"
RE_CMD = re.compile(r"<command-name>/?([a-zA-Z0-9_:-]+)</command-name>")


def load_group_mapping(path=None):
    path = path or GROUPS_FILE
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _clean_fm_val(val):
    val = val.strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        val = val[1:-1]
    # Finding 10: YAML folded scalars (>- , | ) leak into descriptions
    m_marker = re.match(r"^(?:>[-+]?|\|[-+]?)\s*\n?(.*)$", val, re.DOTALL)
    if m_marker:
        rest = m_marker.group(1).strip()
        return " ".join(line.strip() for line in rest.splitlines() if line.strip())
    return val


def parse_frontmatter(content):
    m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return {}
    fm_text = m.group(1)
    data = {}
    cur_key, cur_val = None, []
    in_triggers_list, triggers_list = False, []
    for line in fm_text.splitlines():
        if re.match(r"^[a-zA-Z0-9_-]+:", line):
            if cur_key:
                val = "\n".join(cur_val)
                data[cur_key] = _clean_fm_val(val)
            k, v = line.split(":", 1)
            cur_key = k.strip()
            cur_val = [v.strip()]
            in_triggers_list = (cur_key == "triggers" and not v.strip())
        elif in_triggers_list and re.match(r"^\s*-\s+", line):
            t_item = re.sub(r"^\s*-\s+", "", line).strip().strip("\"'")
            if t_item:
                triggers_list.append(t_item)
        elif cur_key:
            cur_val.append(line.strip())
    if cur_key:
        val = "\n".join(cur_val)
        data[cur_key] = _clean_fm_val(val)
    if triggers_list:
        data["triggers_list"] = triggers_list
    return data


def extract_triggers(fm, desc):
    """Triggers from frontmatter or explicit 'Triggers:' line or quoted phrases in description; otherwise none."""
    if "triggers_list" in fm and fm["triggers_list"]:
        return fm["triggers_list"][:4]

    if not desc:
        return []

    # Check for an explicit 'Triggers:' line / clause
    m_trig = re.search(r"(?:^|\n|\b)Triggers?:\s*(.*?)(?:\n|$)", desc, re.IGNORECASE)
    if m_trig:
        clause = m_trig.group(1).strip()
        quoted = re.findall(r"[\"'\u201c]([^\"'\u201d]+)[\"'\u201d]", clause)
        if quoted:
            return quoted[:4]
        clause_noparens = re.sub(r"\(.*?\)", "", clause)
        parts = [p.strip().rstrip(".") for p in re.split(r"[;,]", clause_noparens) if p.strip().rstrip(".")]
        if parts:
            cleaned = [p for p in parts if len(p) < 40]
            if cleaned:
                return cleaned[:4]
        return []

    # Or quoted phrases in the description (e.g. Use when "foo", "bar")
    quoted = re.findall(r"[\"'\u201c]([^\"'\u201d]+)[\"'\u201d]", desc)
    if quoted:
        valid = [q.strip() for q in quoted if 2 <= len(q.strip()) <= 40]
        if valid:
            return valid[:4]

    return []


def collect_registry(groups=None, claude_skills_dir=None, agents_skills_dir=None, plugins_file=None):
    """Collect every skill from ~/.claude/skills, ~/.agents/skills, and installed plugins. Dedupe by name."""
    groups = groups if groups is not None else load_group_mapping()
    claude_skills_dir = claude_skills_dir or os.environ.get("FACTORY_CLAUDE_SKILLS") or os.path.expanduser("~/.claude/skills")
    agents_skills_dir = agents_skills_dir or os.environ.get("FACTORY_AGENTS_SKILLS") or os.path.expanduser("~/.agents/skills")
    plugins_file = plugins_file or os.environ.get("FACTORY_PLUGINS_FILE") or os.path.expanduser("~/.claude/plugins/installed_plugins.json")

    skills = {}
    sources_counted = []

    # 1. ~/.claude/skills/*/SKILL.md
    if os.path.isdir(claude_skills_dir):
        claude_pattern = os.path.join(claude_skills_dir, "*", "SKILL.md")
        for p in sorted(glob.glob(claude_pattern)):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                fm = parse_frontmatter(content)
                name = fm.get("name") or os.path.basename(os.path.dirname(p))
                if name not in skills:
                    skills[name] = (p, fm, "claude")
            except Exception:
                pass
        sources_counted.append(f"~/.claude/skills ({len(skills)} skills)")

    # 2. ~/.agents/skills/*/SKILL.md
    c_before = len(skills)
    if os.path.isdir(agents_skills_dir):
        agents_pattern = os.path.join(agents_skills_dir, "*", "SKILL.md")
        for p in sorted(glob.glob(agents_pattern)):
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                fm = parse_frontmatter(content)
                name = fm.get("name") or os.path.basename(os.path.dirname(p))
                if name not in skills:
                    skills[name] = (p, fm, "agents")
            except Exception:
                pass
        sources_counted.append(f"~/.agents/skills ({len(skills) - c_before} new skills)")

    # 3. Installed plugin skills
    c_before = len(skills)
    if os.path.exists(plugins_file):
        try:
            with open(plugins_file, "r", encoding="utf-8", errors="ignore") as f:
                pj = json.load(f)
            for plug_name, recs in pj.get("plugins", {}).items():
                if not isinstance(recs, list):
                    continue
                for rec in recs:
                    ip = rec.get("installPath")
                    if not ip or not os.path.exists(ip):
                        continue
                    plugin_mds = []
                    root_md = os.path.join(ip, "SKILL.md")
                    if os.path.isfile(root_md):
                        plugin_mds.append(root_md)
                    sdir = os.path.join(ip, "skills")
                    if os.path.isdir(sdir):
                        plugin_mds.extend(glob.glob(os.path.join(sdir, "**", "SKILL.md"), recursive=True))
                    for sm in sorted(plugin_mds):
                        try:
                            with open(sm, "r", encoding="utf-8", errors="ignore") as f:
                                content = f.read()
                            fm = parse_frontmatter(content)
                            name = fm.get("name") or os.path.basename(os.path.dirname(sm))
                            if name not in skills:
                                skills[name] = (sm, fm, f"plugin:{plug_name}")
                        except Exception:
                            pass
            sources_counted.append(f"installed plugins ({len(skills) - c_before} new skills)")
        except Exception:
            pass

    out = {}
    for name, (path, fm, src) in skills.items():
        grp = groups.get(name, "other")
        grp_label = GROUP_LABELS.get(grp, "Other")
        desc = fm.get("description", "")
        trigs = extract_triggers(fm, desc)
        if src.startswith("plugin:"):
            pname = src.split(":", 1)[1].split("@")[0]
            source_tag = pname
        else:
            source_tag = "local"
        out[name] = {
            "id": name,
            "name": name,
            "source": source_tag,
            "group": grp,
            "groupLabel": grp_label,
            "desc": desc,
            "triggers": trigs,
            "providers": [],
            "agents": [],
            "lastUsed": None,
        }
    return out, sources_counted


def _fallback_sid(filepath):
    parts = os.path.normpath(filepath).split(os.sep)
    if len(parts) >= 3 and parts[-2] == "subagents":
        return parts[-3]
    return os.path.splitext(parts[-1])[0]


def _parse_ts(val):
    if not val:
        return None
    if isinstance(val, (int, float)):
        if val > 1e11:
            val = val / 1000.0
        return datetime.fromtimestamp(val, timezone.utc)
    if isinstance(val, str):
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None
    return None


def _resolve_skill(sk, known_skills):
    if not known_skills:
        return [sk, sk.split(":", 1)[1]] if ":" in sk else [sk]
    if sk in known_skills:
        return [sk]
    if ":" in sk:
        short = sk.split(":", 1)[1]
        if short in known_skills:
            return [short]
    return [sk]


def _scan_lines(lines, fallback_sid, re_cmd):
    hits = []
    for line in lines:
        if (
            b'"name":"Skill"' not in line
            and b'"name": "Skill"' not in line
            and b"<command-name>" not in line
            and b"attributionSkill" not in line
        ):
            continue
        try:
            d = json.loads(line.decode("utf-8", errors="ignore"))
        except Exception:
            continue
        ts = d.get("timestamp")
        sid = d.get("sessionId") or fallback_sid
        sk_names = []
        msg = d.get("message")
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        btype = block.get("type")
                        if btype == "tool_use" and block.get("name") == "Skill":
                            inp = block.get("input") or {}
                            sk = inp.get("skill")
                            if sk:
                                sk_names.append(sk)
                        elif btype == "text":
                            txt = (block.get("text") or "").strip()
                            if txt.startswith("<command-name>") or txt.startswith("<command-message>"):
                                m = re_cmd.search(txt)
                                if m:
                                    sk_names.append(m.group(1))
                                    break
            elif isinstance(content, str):
                txt = content.strip()
                if txt.startswith("<command-name>") or txt.startswith("<command-message>"):
                    m = re_cmd.search(txt)
                    if m:
                        sk_names.append(m.group(1))
        elif isinstance(msg, str):
            txt = msg.strip()
            if txt.startswith("<command-name>") or txt.startswith("<command-message>"):
                m = re_cmd.search(txt)
                if m:
                    sk_names.append(m.group(1))

        if "attributionSkill" in d and d["attributionSkill"]:
            sk_names.append(d["attributionSkill"])

        for sk in sk_names:
            hits.append((sk, sid, ts))
    return hits


BLOCK_SIZE = 4 * 1024 * 1024  # 4 MB blocks


def _scan_stream(fp_in, start_offset, fallback_sid, re_cmd):
    """Read in 4 MB blocks, split each block at its last newline, carry the incomplete
    tail into the next block, and return (new_offset, hits)."""
    current_offset = start_offset
    tail = b""
    hits = []
    while True:
        block = fp_in.read(BLOCK_SIZE)
        if not block:
            break
        data = tail + block
        last_nl = data.rfind(b"\n")
        if last_nl != -1:
            complete_bytes = data[:last_nl + 1]
            tail = data[last_nl + 1:]
            current_offset += len(complete_bytes)
            lines = complete_bytes.splitlines(keepends=True)
            hits.extend(_scan_lines(lines, fallback_sid, re_cmd))
        else:
            tail = data
    return current_offset, hits


def scan_usage(con=None, projects_dir=None, days=30, now=None, known_skills=None):
    """Scan real skill calls in Claude transcripts over the last 30 days and map to T3 handles.
    
    Caches hit tuples (skill, session, ts) per transcript keyed by (path, size, mtime)
    in the factory state dir using store helpers (mode 600).
    """
    now = now or datetime.now(timezone.utc)
    cutoff_dt = now - timedelta(days=days)
    cutoff_sec = cutoff_dt.timestamp()
    projects_dir = projects_dir or os.environ.get("FACTORY_CLAUDE_PROJECTS") or os.path.expanduser("~/.claude/projects")

    # 1. Map nativeId -> thread_id -> handle
    native_to_hc = {}
    if con:
        try:
            roots, threads = t3.load_threads(con)
            hcs = t3.handles_for(threads)
            for row in con.execute("select thread_id, driver, payload_json from orchestration_v2_projection_provider_threads"):
                if isinstance(row, (tuple, list)) and len(row) >= 3:
                    tid, driver, payload = row[0], row[1], row[2]
                    ref = (json.loads(payload or "{}").get("nativeThreadRef") or {}).get("nativeId")
                    if ref and tid in hcs:
                        native_to_hc[ref] = hcs[tid]
        except Exception:
            pass

    # 2. Find transcript files modified in the last 30 days
    files = []
    if os.path.isdir(projects_dir):
        for root, dirs, fnames in os.walk(projects_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]
            for fn in fnames:
                if fn.endswith(".jsonl"):
                    fp = os.path.join(root, fn)
                    try:
                        if os.path.getmtime(fp) >= cutoff_sec:
                            files.append(fp)
                    except OSError:
                        pass

    # 3. Load cache and scan files (reusing hits for unchanged files, reading delta for grown files)
    cache_dirty = False
    try:
        cache = store.read(CACHE_FILE, {})
        if not isinstance(cache, dict):
            cache = {}
            cache_dirty = True
    except Exception:
        cache = {}
        cache_dirty = True

    all_hits = []

    for fp in files:
        try:
            st = os.stat(fp)
            sz, mtime = st.st_size, st.st_mtime
        except OSError:
            continue

        entry = cache.get(fp)
        fallback_sid = _fallback_sid(fp)

        if entry and entry.get("size") == sz and entry.get("mtime") == mtime:
            file_hits = entry.get("hits", [])
        else:
            offset = entry.get("offset", 0) if entry else 0
            is_append = False
            head_hash = ""
            if entry and sz > entry.get("size", 0) and mtime >= entry.get("mtime", 0) and 0 <= offset <= sz:
                try:
                    with open(fp, "rb") as fp_check:
                        head_chunk = fp_check.read(4096)
                        head_hash = hashlib.sha256(head_chunk).hexdigest()
                        if entry.get("head_hash") and entry.get("head_hash") == head_hash:
                            if offset == 0:
                                is_append = True
                            else:
                                fp_check.seek(offset - 1)
                                if fp_check.read(1) == b"\n":
                                    is_append = True
                except Exception:
                    is_append = False

            if is_append:
                new_hits = []
                new_offset = offset
                try:
                    with open(fp, "rb") as fp_in:
                        fp_in.seek(offset)
                        new_offset, new_hits = _scan_stream(fp_in, offset, fallback_sid, RE_CMD)
                except Exception:
                    new_hits = []
                    new_offset = offset
                hits = list(entry.get("hits", [])) + new_hits
                entry["size"] = sz
                entry["mtime"] = mtime
                entry["offset"] = new_offset
                entry["hits"] = hits
                entry["head_hash"] = head_hash
                file_hits = hits
                cache_dirty = True
            else:
                hits = []
                new_offset = 0
                head_hash = ""
                try:
                    with open(fp, "rb") as fp_in:
                        head_chunk = fp_in.read(4096)
                        head_hash = hashlib.sha256(head_chunk).hexdigest()
                        fp_in.seek(0)
                        new_offset, hits = _scan_stream(fp_in, 0, fallback_sid, RE_CMD)
                except Exception:
                    hits = []
                    new_offset = 0
                    head_hash = ""
                cache[fp] = {
                    "size": sz,
                    "mtime": mtime,
                    "offset": new_offset,
                    "head_hash": head_hash,
                    "hits": hits,
                }
                file_hits = hits
                cache_dirty = True

        all_hits.extend(file_hits)

    current_files = set(files)
    stale_keys = [k for k in cache if k not in current_files]
    if stale_keys:
        for k in stale_keys:
            del cache[k]
        cache_dirty = True

    for fp, entry in cache.items():
        raw_hits = entry.get("hits", [])
        valid_hits = []
        for h in raw_hits:
            ts = h[2] if len(h) > 2 else None
            dt = _parse_ts(ts)
            if dt is not None and dt < cutoff_dt:
                continue
            valid_hits.append(h)
        if len(valid_hits) != len(raw_hits):
            entry["hits"] = valid_hits
            cache_dirty = True

    if cache_dirty:
        try:
            store.write_text(CACHE_FILE, json.dumps(cache, separators=(",", ":")) + "\n")
        except Exception:
            pass

    # 4. Filter hits by 30-day cutoff and aggregate into usage dict
    usage = {}
    for sk, sid, ts in all_hits:
        dt = _parse_ts(ts)
        if dt is not None and dt < cutoff_dt:
            continue
        resolved_skills = _resolve_skill(sk, known_skills)
        for r_sk in resolved_skills:
            rec = usage.setdefault(r_sk, {"last_used": None, "agents": set()})
            hc = native_to_hc.get(sid)
            if hc:
                rec["agents"].add(hc)
            if ts:
                if rec["last_used"] is None or ts > rec["last_used"]:
                    rec["last_used"] = ts

    return usage


def collect(now=None, con=None, days=30, projects_dir=None, claude_skills_dir=None, agents_skills_dir=None, plugins_file=None):
    """Collect registry and usage metrics. Returns (skills_list, warnings, duration_sec)."""
    t0 = time.perf_counter()
    warnings = []
    own_con = False

    try:
        reg, sources = collect_registry(
            claude_skills_dir=claude_skills_dir,
            agents_skills_dir=agents_skills_dir,
            plugins_file=plugins_file
        )
    except Exception as e:
        warnings.append(f"failed to load skills registry: {e}")
        reg = {}

    usage = {}
    try:
        if con is None:
            try:
                con = t3.connect()
                own_con = True
            except Exception as e:
                warnings.append(f"T3 database unavailable for skill usage: {e}")
                con = None
        known_skills = set(reg.keys())
        usage = scan_usage(con, projects_dir=projects_dir, days=days, now=now, known_skills=known_skills)
    except Exception as e:
        warnings.append(f"failed to scan skill usage: {e}")
    finally:
        if own_con and con:
            try:
                con.close()
            except Exception:
                pass

    # Merge usage into registry
    used_count = 0
    for name, skill in reg.items():
        u = usage.get(name)
        if u and (u["last_used"] or u["agents"]):
            skill["lastUsed"] = u["last_used"]
            skill["agents"] = sorted(list(u["agents"]))
            skill["providers"] = ["Claude"]
            used_count += 1

    def skill_sort_key(s):
        lu = s.get("lastUsed")
        if lu:
            dt = _parse_ts(lu)
            ts_val = -dt.timestamp() if dt else 0
            return (0, ts_val, s["name"].lower())
        return (1, 0, s["name"].lower())

    skills_list = sorted(reg.values(), key=skill_sort_key)
    duration = time.perf_counter() - t0

    return skills_list, warnings, duration
