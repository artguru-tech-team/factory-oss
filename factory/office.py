"""The Office: data feed (site/office.json) and page installation (site/office.html)."""
import json
import os
import re
import socket
from datetime import datetime, timedelta, timezone

from . import config, store, t3
from .store import FactoryError, iso

MAX_OFFICE_SIZE = 8 * 1024 * 1024
ONE_HOUR = timedelta(hours=1)


ONLINE_WINDOW = timedelta(minutes=3)


def machines_doc(strict=False):
    """machines.json as {id: dict}. Readers (the Office, the push route, doctor) lose only the
    remote buildings on a malformed file; strict=True (the CLI, which rewrites the file) refuses it
    instead, so a corrupt file is never overwritten with what is left."""
    try:
        doc = store.read("machines.json", {})
    except Exception:
        if strict:
            raise
        return {}
    if not isinstance(doc, dict):
        if strict:
            raise FactoryError("machines.json is not an object; fix or restore it")
        return {}
    return {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, dict)}


def _is_online(last_push, now):
    try:
        dt = t3.ts(last_push) if isinstance(last_push, str) else None
    except (ValueError, TypeError):
        return False
    if not dt:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return bool(now) and (now - dt) <= ONLINE_WINDOW


def machine_slug(name):
    """Short stable slug of the hostname: 'my-box.corp.local' -> 'my-box'."""
    short = (name or "").split(".")[0].lower()
    out = re.sub(r"[^a-z0-9]+", "-", short).strip("-")
    return out or "machine"


def machine_info():
    """{"id", "label"} for the machine: config override or hostname default."""
    host = socket.gethostname()
    m_id = config.get("machine.id")
    if isinstance(m_id, str) and m_id.strip():
        mid = m_id.strip()
    else:
        mid = machine_slug(host)

    m_label = config.get("machine.label")
    if isinstance(m_label, str) and m_label.strip():
        mlabel = m_label.strip()
    else:
        mlabel = host

    return {"id": mid, "label": mlabel}


def filter_office_agents(agents, built_dt):
    """The same rows as the page, dropping rows in state 'done' older than 1 h."""
    out = []
    if built_dt and built_dt.tzinfo is None:
        built_dt = built_dt.replace(tzinfo=timezone.utc)
    for a in agents:
        if a.get("state") == "done" and a.get("since"):
            try:
                st = t3.ts(a["since"])
                if st:
                    if st.tzinfo is None:
                        st = st.replace(tzinfo=timezone.utc)
                    if built_dt and (built_dt - st) > ONE_HOUR:
                        continue
            except (ValueError, TypeError):
                pass
        out.append(a)
    return out


def get_machines_and_agents(loc_machine, last_seen_local, now_dt):
    """Return (machines_list, remote_agents) using stored machines.json."""
    if now_dt and now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    doc = machines_doc()
    machines_list = [{
        "id": loc_machine["id"],
        "label": loc_machine["label"],
        "online": True,
        "lastSeen": last_seen_local,
    }]
    remote_agents = []
    for mid in sorted(doc):
        m = doc[mid]
        last_push = m.get("lastPushAt")
        is_online = _is_online(last_push, now_dt)
        machines_list.append({
            "id": mid,
            "label": m.get("label", mid),
            "online": is_online,
            "lastSeen": last_push,
        })
        if is_online:
            m_agents = [dict(a) for a in (m.get("agents") or []) if isinstance(a, dict)]
            for r in m_agents:
                r["machine"] = mid
            remote_agents.extend(filter_office_agents(m_agents, now_dt))
    return machines_list, remote_agents


def build_office_data(data, now=None):
    """Assemble office.json from collect() data."""
    now_dt = now or datetime.now(timezone.utc)
    built_str = data.get("builtAt") or iso(now_dt)
    built_dt = t3.ts(built_str) or now_dt
    loc_machine = machine_info()
    local_agents = [dict(a) for a in data.get("agents", [])]
    for a in local_agents:
        a["machine"] = loc_machine["id"]
    return {
        "builtAt": built_str,
        "machine": loc_machine,
        "products": data.get("products", {}),
        "projects": data.get("projects", []),
        "agents": filter_office_agents(local_agents, built_dt),
        "tokens": data.get("tokens", {}),
        "provColors": data.get("provColors", {}),
        "relayConfigured": bool(data.get("relayConfigured", False)),
    }


def _read_page(src_path):
    if not os.path.isfile(src_path):
        raise FactoryError(f"file not found: {src_path!r}")
    size = os.path.getsize(src_path)
    if size > MAX_OFFICE_SIZE:
        raise FactoryError(f"file too large ({size} bytes, max 8 MB)")
    with open(src_path, "rb") as f:
        content = f.read()
    if not content.lstrip().lower().startswith(b"<!doctype html"):
        raise FactoryError("file must start with <!doctype html")
    return content


def install(src_path):
    """Install a built Office HTML file into site/office.html."""
    content = _read_page(src_path)
    store.write_bytes(os.path.join("site", "office.html"), content)
    return {
        "installed": True,
        "path": store.path("site", "office.html"),
        "size": len(content),
    }


def install_public(src_path):
    """Install a built Office HTML file into site-public/index.html."""
    content = _read_page(src_path)
    if b"api/" in content:
        m = re.search(rb"api/[^\s\"'<>`();,]*", content)
        matched = m.group(0).decode("utf-8", errors="replace") if m else "api/"
        raise FactoryError(f"public page must not contain private API calls ({matched})")
    store.write_bytes(os.path.join("site-public", "index.html"), content)
    return {
        "installed": True,
        "path": store.path("site-public", "index.html"),
        "size": len(content),
    }


def status():
    """Return dict with installed status, size, and mtime."""
    p = store.path("site", "office.html")
    if not os.path.exists(p):
        return {
            "installed": False,
            "size": None,
            "mtime": None,
        }
    st = os.stat(p)
    dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    return {
        "installed": True,
        "size": st.st_size,
        "mtime": iso(dt),
    }


def size_mb(n):
    return f"{round(n / (1024 * 1024), 1):g}"


def doctor_status():
    """One line status for factory doctor."""
    s = status()
    if not s["installed"]:
        return "not installed"
    return f"installed ({size_mb(s['size'])} MB, built {s['mtime'][:10]})"


def doctor_machines():
    """One line status for factory doctor: machines: N registered, M online."""
    machines = machines_doc()
    now = datetime.now(timezone.utc)
    m = sum(_is_online(d.get("lastPushAt"), now) for d in machines.values())
    return f"{len(machines)} registered, {m} online"
