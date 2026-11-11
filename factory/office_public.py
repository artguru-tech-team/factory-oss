"""Public feed builder: builds the redacted, allowlist-only public office feed."""
import hashlib
import re
import hmac
import secrets
from datetime import datetime, timezone

from . import store, t3

LABEL_TABLE = {
    "feat": "Building a feature for {p}",
    "fix": "Fixing a bug on {p}",
    "refactor": "Cleaning up code in {p}",
    "chore": "Maintenance on {p}",
    "docs": "Writing docs for {p}",
    "perf": "Speeding up {p}",
    "test": "Writing tests for {p}",
}

PROVIDER_MAP = {
    "claude": "Claude",
    "antigravity": "Antigravity",
    "opencode": "OpenCode",
    "codex": "Codex",
}

def public_salt() -> bytes:
    """Read or create ~/.factory/.public-salt (mode 0o600, 64 hex chars).

    The caller must hold store.lock() (build.render does): two first builds would otherwise write
    different salts. A file that is not 64 hex chars is replaced, which changes every public id once.
    """
    p = store.path(".public-salt")
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if re.fullmatch(r"[0-9a-f]{64}", raw):
            return raw.encode("utf-8")
    except FileNotFoundError:
        pass
    token = secrets.token_hex(32)
    store.write_text(".public-salt", token + "\n")
    return token.encode("utf-8")


def _agent_label(task: str, product_name: str) -> str:
    """Generate generic label: 2nd segment of task when >= 3 segments, else generic."""
    segments = task.split("/")
    kind = segments[1].strip() if len(segments) >= 3 else ""
    template = LABEL_TABLE.get(kind, "Working on {p}")
    return template.format(p=product_name)


def _agent_provider(raw_prov: str) -> str:
    """Map provider name to allowlisted title case, else 'AI agent'."""
    key = (raw_prov or "").strip().lower()
    return PROVIDER_MAP.get(key, "AI agent")


def _s(a: dict, key: str) -> str:
    """a[key] when it is a string, else "": one odd row must not break the whole feed."""
    v = a.get(key)
    return v if isinstance(v, str) else ""


def _since(raw: str):
    """A parsed ISO timestamp, re-serialised, else None: only a date can leave through this field."""
    try:
        dt = t3.ts(raw)
    except ValueError:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return store.iso(dt.astimezone(timezone.utc))  # no source time zone


def _agent_public_id(salt: bytes, a: dict) -> str:
    """HMAC-SHA256(salt, thread id or hc), first 10 hex chars."""
    identifier = _s(a, "thread") or _s(a, "hc")
    return hmac.new(salt, identifier.encode("utf-8"), hashlib.sha256).hexdigest()[:10]


def build_public_feed(data: dict, salt: bytes) -> dict:
    """Assemble public feed from build.collect() data and salt."""
    built_str = data.get("builtAt") or store.iso(datetime.now(timezone.utc))

    products_raw = data.get("products", {})
    products = {}
    for k, v in products_raw.items():
        name = v.get("name") if isinstance(v, dict) else None
        products[k] = {"name": name if isinstance(name, str) and name else k}


    candidates = []
    seen: set[str] = set()
    for a in data.get("agents") or []:
        if not isinstance(a, dict) or _s(a, "task").strip() == "probe":
            continue
        # done rows are not sent: the page shows only agents in the office
        state = _s(a, "state")
        if state not in ("running", "waiting", "stalled"):
            continue
        prod_key = _s(a, "product")
        if prod_key not in products:
            continue
        if not (_s(a, "thread") or _s(a, "hc")):
            continue  # no identity to hash: every such row would share one id
        pub_id = _agent_public_id(salt, a)
        if pub_id in seen:
            continue  # same thread listed twice: one character
        seen.add(pub_id)
        candidates.append((a, state, prod_key, pub_id))

    # Precompute public id mapping for parent resolution
    hc_to_id: dict[str, str] = {}
    for a, _, _, pub_id in candidates:
        for key in ("hc", "thread"):
            if _s(a, key):
                hc_to_id[_s(a, key)] = pub_id

    public_agents = []
    for a, state, prod_key, pub_id in candidates:
        prod_name = products[prod_key]["name"]
        label = _agent_label(_s(a, "task"), prod_name)
        provider = _agent_provider(_s(a, "provider"))

        parent_ref = _s(a, "parent")
        if (
            not parent_ref
            or parent_ref == _s(a, "hc")
            or parent_ref == _s(a, "thread")
        ):
            parent = None
        else:
            parent = hc_to_id.get(parent_ref)

        public_agents.append(
            {
                "id": pub_id,
                "parent": parent,
                "product": prod_key,
                "state": state,
                "provider": provider,
                "since": _since(_s(a, "since")),
                "label": label,
            }
        )

    return {
        "builtAt": built_str,
        "products": products,
        "agents": public_agents,
    }
