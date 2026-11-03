"""Configuration in ~/.factory/config.json (or $FACTORY_HOME/config.json)."""
import json
import os
from . import store
from .store import FactoryError

CONFIG_REL = "config.json"


def load():
    """Load config dictionary from config.json."""
    cfg = store.read(CONFIG_REL, {})
    if not isinstance(cfg, dict):
        raise FactoryError(f"{CONFIG_REL} is not a JSON object")
    return cfg


def get(key, default=None):
    """Get a config value by dotted key, returning default if unset."""
    cur = load()
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def owner():
    """The dashboard owner's name for messages and errors (config owner.name)."""
    try:
        name = get("owner.name")
    except FactoryError:  # a broken config.json must never stop an answer reaching its agent
        name = None
    return name.strip() if isinstance(name, str) and name.strip() else "the owner"


def set_value(key, val_str, as_json=False):
    """Set a config value by dotted key. Stored as a string unless as_json (then parsed as JSON)."""
    if not key or not all(k for k in key.split(".")):
        raise FactoryError(f"bad config key {key!r}")
    if as_json:
        try:
            val = json.loads(val_str)
        except ValueError as e:
            raise FactoryError(f"value is not JSON: {e}")
    else:
        val = val_str

    with store.lock():
        cfg = load()
        cur = cfg
        parts = key.split(".")
        for i, part in enumerate(parts[:-1]):
            cur = cur.setdefault(part, {})
            if not isinstance(cur, dict):
                raise FactoryError(f"{'.'.join(parts[:i + 1])} is a value, not a group")
        if isinstance(cur.get(parts[-1]), dict) and not isinstance(val, dict):
            raise FactoryError(f"{key} is a group of keys; set its keys one by one")
        cur[parts[-1]] = val
        store.write(CONFIG_REL, cfg)
    return val


def flatten(d, prefix=""):
    """Flatten nested dict into list of (dotted_key, value) sorted by key."""
    items = []
    if not isinstance(d, dict):
        return items
    for k in sorted(d.keys()):
        v = d[k]
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            items.extend(flatten(v, full_key))
        else:
            items.append((full_key, v))
    return items
