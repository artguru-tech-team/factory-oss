"""State in ~/.factory (or $FACTORY_HOME): JSON files, atomic writes, one lock."""
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class FactoryError(Exception):
    """A user-facing error: printed as one line, exit code 1."""


LIVE_PATH = os.path.expanduser("~/factory-live")


def _is_live_checkout():
    return os.path.realpath(os.path.dirname(__file__)) == os.path.join(os.path.realpath(LIVE_PATH), "factory")


def home():
    h = os.environ.get("FACTORY_HOME")
    if h:
        return os.path.expanduser(h)
    if not _is_live_checkout():
        raise FactoryError(
            "this is a development checkout: set FACTORY_HOME "
            "(e.g. export FACTORY_HOME=$(mktemp -d)); "
            "set FACTORY_HOME=~/.factory explicitly to use the live state"
        )
    return os.path.expanduser("~/.factory")


def path(*parts):
    return os.path.join(home(), *parts)


def iso(dt):
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def now_iso():
    return iso(datetime.now(timezone.utc))


def check_id(value, what="id"):
    if not ID.match(value or ""):
        raise FactoryError(f"bad {what} {value!r}: use a-z, 0-9 and '-', 1 to 40 characters")
    return value


def check_mode_600(rel):
    p = path(rel)
    if os.path.exists(p):
        mode = os.stat(p).st_mode & 0o777
        if mode != 0o600:
            raise FactoryError(f"{p} has mode {oct(mode)}, want 0o600 (chmod 600 it)")
    return p


def mkdir(d):
    """Create d and make every directory from home() down to it mode 700 (makedirs sets only the leaf)."""
    os.makedirs(d, exist_ok=True)
    root, d = os.path.abspath(home()), os.path.abspath(d)
    while True:
        os.chmod(d, 0o700)
        if d == root or not d.startswith(root + os.sep):
            break
        d = os.path.dirname(d)


@contextmanager
def lock():
    mkdir(home())
    with open(path(".lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def read(rel, default):
    try:
        with open(path(rel), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError as e:
        raise FactoryError(f"{path(rel)} is not valid JSON ({e}); fix or restore it")


def write(rel, data):
    write_text(rel, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def write_text(rel, text, mode=0o600):
    """Write through a temp file of its own (two writers never share one), then rename into place."""
    p = path(rel)
    mkdir(os.path.dirname(p))
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix=os.path.basename(p) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:  # owns fd from here, so it closes on any error
            os.fchmod(f.fileno(), mode)
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def append_line(rel, obj):
    p = path(rel)
    mkdir(os.path.dirname(p))
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, allow_nan=False) + "\n")


def read_lines(rel):
    try:
        with open(path(rel), encoding="utf-8") as f:
            lines = [line for line in f if line.strip()]
    except FileNotFoundError:
        return []
    out = []
    for n, line in enumerate(lines, 1):
        try:
            out.append(json.loads(line))
        except ValueError:
            raise FactoryError(f"{path(rel)} line {n} is not valid JSON; fix or remove that line")
    return out


def key():
    """The ?k= key, created on first use with mode 600. An empty key file is replaced."""
    try:
        with open(path(".key"), encoding="utf-8") as f:
            k = f.read().strip()
        if k:
            return k
    except FileNotFoundError:
        pass
    with lock():
        try:
            with open(path(".key"), encoding="utf-8") as f:
                k = f.read().strip()
        except FileNotFoundError:
            k = ""
        if not k:
            k = secrets.token_urlsafe(24)
            write_text(".key", k + "\n")
    return k


def rotate_key():
    """Under store.lock(), replace .key with a new random key (mode 600, atomic)."""
    with lock():
        k = secrets.token_urlsafe(24)
        write_text(".key", k + "\n")
        return k


# ---------- products and projects ----------

def inbox():
    return read("inbox.json", {"seq": 0, "items": [], "resolved": []})


def add_ask_item(project, kind, title, short, context, options, reco, why, sender, thread, at=None,
                 dedupe_title=None, dedupe_day=None):
    """Insert one open inbox item (under store.lock()): the shared write path for `factory ask`
    and the autopilot "needs you" ask. With dedupe_title, an open item with the same title (any
    day) or a resolved item with the same title resolved or created on dedupe_day is skipped."""
    with lock():
        box = inbox()
        if dedupe_title:
            if any(isinstance(it, dict) and it.get("title") == dedupe_title
                   for it in box.get("items", [])):
                return None
            day = dedupe_day or datetime.now(timezone.utc).date().isoformat()
            if any(isinstance(it, dict) and it.get("title") == dedupe_title
                   and ((it.get("resolvedAt") or "").startswith(day)
                        or (it.get("at") or "").startswith(day))
                   for it in box.get("resolved", [])):
                return None
        box["seq"] += 1
        item = {"id": ("a" if kind == "action" else "d") + str(box["seq"]), "project": project, "kind": kind,
                "title": title, "short": short, "from": sender, "thread": thread, "at": at or now_iso(),
                "context": context, "options": options, "reco": reco, "why": why}
        box["items"].append(item)
        write("inbox.json", box)
    return item


def products():
    return read("products.json", {})


def project_rel(product, pid):
    return os.path.join("products", product, "projects", pid + ".json")


def projects():
    out = []
    root = path("products")
    if not os.path.isdir(root):
        return out
    for product in sorted(os.listdir(root)):
        d = os.path.join(root, product, "projects")
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".json"):
                out.append(read(project_rel(product, name[:-5]), None))
    return [p for p in out if p]


def find_project(pid):
    for p in projects():
        if p["id"] == pid:
            return p
    raise FactoryError(f"no project {pid!r} (see: factory project list)")


def why_hidden(p):
    """Why a project cannot be drawn yet, or None. The build skips such projects; ask refuses them."""
    if p["kind"] == "build" and p.get("source") != "ds-kit":
        if not p.get("phases"):
            return f"no phases yet (factory plan phase {p['id']} ...)"
        if not sum(ph["total"] for ph in p["phases"]):
            return f"its phases hold 0 units (factory plan phase {p['id']} <name> --total N ...)"
    if p["kind"] == "loop" and (not p.get("exps") or p.get("target") is None):
        return f"no baseline or target yet (factory loop meta {p['id']} ...)"
    return None


def snapshot(p):
    """Record today's scope and merged count: the project graph is drawn from these."""
    p["history"][datetime.now(timezone.utc).date().isoformat()] = [sum(x["total"] for x in p["phases"]),
                                             sum(x["merged"] for x in p["phases"])]


def save_project(p):
    write(project_rel(p["product"], p["id"]), p)


# ---------- prototypes ----------

PROTOS_REL = "protos.json"


def protos():
    """protos.json, refused unless it is a list of {id, versions: [dict, ...]} (a bad shape would blank the page)."""
    data = read(PROTOS_REL, [])
    ok = isinstance(data, list) and all(
        isinstance(p, dict) and isinstance(p.get("id"), str) and isinstance(p.get("versions"), list)
        and p["versions"] and all(isinstance(v, dict) and type(v.get("v")) is int for v in p["versions"])
        for p in data)
    if not ok:
        raise FactoryError(f"{path(PROTOS_REL)} is not a list of prototypes: fix or restore it")
    return data


def proto_token(rel):
    """Opens only this one prototype file: the page links use it instead of the dashboard key,
    so a prototype's own scripts never see the key."""
    return hmac.new(key().encode(), rel.encode(), hashlib.sha256).hexdigest()[:32]


def save_protos(protos):
    write(PROTOS_REL, protos)


def find_proto(protos, pid):
    return next((p for p in protos if p.get("id") == pid), None)


# ---------- autopilot ----------

AUTOPILOT_REL = "autopilot.json"


def autopilot():
    """autopilot.json, refused unless it is {paused, projects: {id: {thread, goal, ...}}}
    (a bad shape would blank the Autopilot view)."""
    data = read(AUTOPILOT_REL, {"paused": False, "pausedAt": None, "daily_cap": 12, "projects": {}})

    def entry_ok(v):
        if not isinstance(v, dict):
            return False
        if not isinstance(v.get("thread"), str) or not isinstance(v.get("goal"), str):
            return False
        for k in ("every_hours", "max_per_day"):
            if type(v.get(k)) is not int or not 1 <= v[k] <= 1000:
                return False
        if not isinstance(v.get("enabled"), bool):
            return False
        hist = v.get("history", [])
        if not isinstance(hist, list) or not all(isinstance(h, dict) for h in hist):
            return False
        nudged = v.get("nudged_on")
        if nudged is not None and not isinstance(nudged, dict):
            return False
        return True

    ok = (isinstance(data, dict) and isinstance(data.get("projects"), dict)
          and type(data.get("daily_cap", 12)) is int
          and all(isinstance(k, str) and entry_ok(v) for k, v in data.get("projects", {}).items()))
    if not ok:
        raise FactoryError(f"{path(AUTOPILOT_REL)} is not an autopilot config: fix or restore it")
    return data


def save_autopilot(data):
    write(AUTOPILOT_REL, data)
