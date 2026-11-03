"""Secrets in $FACTORY_HOME/secrets/<name>: one value per file, mode 0600 in a 0700 dir.

A secret is read from stdin by `factory secret set <name>` and only ever printed by name,
never by value. A missing secret raises a clear FactoryError naming the `secret set` fix.
"""
import os
import sys

from . import store
from .store import FactoryError

SECRETS_DIR = "secrets"


def _check_name(name):
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name in (".", ".."):
        raise FactoryError(f"bad secret name {name!r}: a single path component like 'sentry-token'")
    return name


def _rel(name):
    return os.path.join(SECRETS_DIR, name)


def set_secret(name):
    """Read the value from stdin (surrounding whitespace stripped) and write it mode 0600.
    Returns the name only; the value is never printed."""
    _check_name(name)
    value = sys.stdin.read().strip()
    if not value:
        raise FactoryError("no value on stdin (echo '<value>' | factory secret set <name>)")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise FactoryError("bad secret value: paste the key on one line")
    store.write_text(_rel(name), value + "\n")
    return {"name": name}


def get_secret(name):
    """The secret value, or a clear FactoryError naming the `factory secret set` fix."""
    _check_name(name)
    store.check_mode_600(_rel(name))
    try:
        with open(store.path(_rel(name)), encoding="utf-8") as f:
            value = f.read().strip()
    except FileNotFoundError:
        value = ""
    if not value:
        raise FactoryError(f"secret {name!r} is not set (set it: factory secret set {name})")
    if any(ch.isspace() or ord(ch) < 32 for ch in value):  # never let a bad value reach a header
        raise FactoryError(f"secret {name!r} has spaces or line breaks: set it again on one line (factory secret set {name})")
    return value


def list_secrets():
    """Names only, never values."""
    d = store.path(SECRETS_DIR)
    return sorted(f for f in os.listdir(d) if not f.endswith(".tmp")) if os.path.isdir(d) else []
