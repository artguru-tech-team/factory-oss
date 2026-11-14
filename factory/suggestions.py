"""Office suggestions store: SQLite database for feature suggestions and votes.

Tables:
  ideas: id, product, text, email, status, created_at, ip_hash, screened, hidden
  votes: idea_id, account_hash, created_at, UNIQUE(idea_id, account_hash)
  vote_log: account_hash, created_at (one row per on:true attempt, feeds the vote rate limit)
"""
import contextlib
import json
import os
import re
import secrets as stdlib_secrets
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

from . import config, store
from .store import FactoryError

MAX_ID = 2**63 - 1  # SQLite INTEGER limit
STATUSES = ("under-review", "planned", "building", "done", "declined")

# URLs: scheme (http, https, ftp), www., or domain names with common TLDs
_TLDS = (
    "com|org|net|edu|gov|io|ai|dev|app|co|me|biz|info|xyz|site|online|tech|store|cc|tv|us|uk|de|fr|eu"
    "|ru|link|gg|ly|ws|su|cn|br"  # no English words (in, to, top, live, page, sh...): "this.In the" is prose
)
# a dot, or one of its common disguises: [.] (.) {.}, fullwidth dot, ideographic full stop
# ("x dot com" is left out: it held plain prose such as "a dot in the UI")
_DOT = r"(?:\.|\[\.\]|\(\.\)|\{\.\}|\uFF0E|\u3002)"
_URL_RE = re.compile(
    r"(?i)\b(?:https?://|ftp://|www\.)\S+"
    r"|\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?" + _DOT + r"(?:" + _TLDS + r")\b(?:/[^\s]*)?"
)
_EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")


def suggest_salt() -> bytes:
    """Read or create ~/.factory/.suggest-salt (mode 0o600, 64 hex chars).

    Vote uniqueness depends on this value: a new salt gives every account a new hash, so each could
    vote again. So a corrupt file is an error, never silently replaced. `suggest_serve.serve` calls
    this once at start, before any request thread runs, so two threads never create it at once.
    """
    p = store.path(".suggest-salt")
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        token = stdlib_secrets.token_hex(32)
        store.write_text(".suggest-salt", token + "\n")
        return token.encode("utf-8")
    if not re.fullmatch(r"[0-9a-f]{64}", raw):
        raise FactoryError(f"{p} is not 64 hex chars; restore it from a backup (a new salt would allow double votes)")
    return raw.encode("utf-8")


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product TEXT NOT NULL,
            text TEXT NOT NULL,
            email TEXT,
            status TEXT DEFAULT 'under-review',
            created_at TEXT NOT NULL,
            ip_hash TEXT NOT NULL,
            screened INTEGER DEFAULT 0,
            hidden INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS votes (
            idea_id INTEGER NOT NULL,
            account_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(idea_id, account_hash)
        );
        CREATE TABLE IF NOT EXISTS vote_log (
            account_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_vote_log_account_created ON vote_log(account_hash, created_at);
        CREATE INDEX IF NOT EXISTS idx_ideas_screened_hidden ON ideas(screened, hidden);
        CREATE INDEX IF NOT EXISTS idx_ideas_ip_created ON ideas(ip_hash, created_at);
        CREATE INDEX IF NOT EXISTS idx_votes_account_created ON votes(account_hash, created_at);
        CREATE INDEX IF NOT EXISTS idx_votes_idea ON votes(idea_id);
        """
    )


def _connect(autocommit: bool = False) -> sqlite3.Connection:
    """autocommit=True opens with isolation_level=None so the caller controls BEGIN/COMMIT."""
    db_path = store.path("suggestions.db")
    store.mkdir(os.path.dirname(db_path))
    os.close(os.open(db_path, os.O_CREAT | os.O_WRONLY, 0o600))  # 0600 before SQLite makes -wal/-shm from its mode
    conn = sqlite3.connect(db_path, timeout=10.0, isolation_level=None if autocommit else "")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _init_db(conn)  # every statement is IF NOT EXISTS, so old databases gain new tables
    os.chmod(db_path, 0o600)
    return conn


def config_list(key: str, words: bool = False) -> list[str]:
    """A config value as a list of strings; unset or broken gives [] (fail closed), never raises.
    Default (office.publicProducts): only a real list, the same rule as build.public_view, so the
    service never accepts products the office page does not show. words=True (suggestions.blocklist):
    also a plain string, split on whitespace and commas into words (surrounding quotes stripped)."""
    try:
        v = config.get(key)
        if isinstance(v, str):
            raw = v
            try:
                v = json.loads(raw)
            except ValueError:
                v = None
            if not words:
                return []
            if not isinstance(v, list):
                parts = raw.split(",") if "," in raw else raw.split()  # commas allow phrases: "free money, casino"
                v = [w.strip().strip("\"'") for w in parts]
        if not isinstance(v, list):
            return []
        return [x for x in v if isinstance(x, str) and x]
    except Exception:
        return []


def add_idea(product: str, text: str, email: str | None, ip_hash: str) -> int:
    """Add a new idea. Screened defaults to 0 (pending) and hidden to 0."""
    created_at = store.now_iso()
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ideas (product, text, email, status, created_at, ip_hash, screened, hidden)
            VALUES (?, ?, ?, 'under-review', ?, ?, 0, 0)
            """,
            (product, text, email, created_at, ip_hash),
        )
        conn.commit()
        return cur.lastrowid


def add_idea_limited(product: str, text: str, email: str | None, ip_hash: str, limit: int = 5) -> tuple[int, int] | None:
    """Count this IP's ideas in the last hour, screen the text and insert, in one write transaction.
    Returns (id, screened) with screened 1 (public) or 2 (held), or None when the IP is at the limit."""
    now = datetime.now(timezone.utc)
    cutoff = store.iso(now - timedelta(seconds=3600))
    with contextlib.closing(_connect(autocommit=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM ideas WHERE ip_hash = ? AND created_at >= ?", (ip_hash, cutoff)
            ).fetchone()[0]
            if n >= limit:
                conn.execute("ROLLBACK")
                return None
            screened = screen_verdict(conn, product, text)
            cur = conn.execute(
                """
                INSERT INTO ideas (product, text, email, status, created_at, ip_hash, screened, hidden)
                VALUES (?, ?, ?, 'under-review', ?, ?, ?, 0)
                """,
                (product, text, email, store.iso(now), ip_hash, screened),
            )
            conn.execute("COMMIT")
            return cur.lastrowid, screened
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def set_vote(idea_id: int, account_hash: str, on: bool, limit: int | None = None) -> int | None:
    """Toggle vote on or off for an account. Returns the updated vote count.
    Every on:true attempt is logged in vote_log in the same transaction. With a limit, returns None
    when the account already made `limit` on:true attempts in the last hour. on:false is never limited."""
    now = datetime.now(timezone.utc)
    cutoff = store.iso(now - timedelta(seconds=3600))
    created_at = store.iso(now)
    with contextlib.closing(_connect(autocommit=True)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM vote_log WHERE created_at < ?", (cutoff,))  # older rows never count
            if on:
                if limit is not None:
                    n = conn.execute(
                        "SELECT COUNT(*) FROM vote_log WHERE account_hash = ? AND created_at >= ?",
                        (account_hash, cutoff),
                    ).fetchone()[0]
                    if n >= limit:
                        conn.execute("ROLLBACK")
                        return None
                conn.execute("INSERT INTO vote_log (account_hash, created_at) VALUES (?, ?)", (account_hash, created_at))
                conn.execute(
                    "INSERT OR IGNORE INTO votes (idea_id, account_hash, created_at) VALUES (?, ?, ?)",
                    (idea_id, account_hash, created_at),
                )
            else:
                conn.execute("DELETE FROM votes WHERE idea_id = ? AND account_hash = ?", (idea_id, account_hash))
            count = conn.execute("SELECT COUNT(*) FROM votes WHERE idea_id = ?", (idea_id,)).fetchone()[0]
            conn.execute("COMMIT")
            return count
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


def get_idea(idea_id: int) -> dict | None:
    """Fetch a single idea by id, including votes count."""
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT i.id, i.product, i.text, i.email, i.status, COUNT(v.account_hash) AS votes,
                   i.created_at, i.screened, i.hidden
            FROM ideas i
            LEFT JOIN votes v ON i.id = v.idea_id
            WHERE i.id = ?
            GROUP BY i.id
            """,
            (idea_id,),
        )
        r = cur.fetchone()
        return dict(r) if r else None


def public_ideas() -> list[dict]:
    """Public ideas: screened=1 and hidden=0 only.
    Contains ONLY: id, product, text, status, votes, created_at.
    """
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT i.id, i.product, i.text, i.status, COUNT(v.account_hash) AS votes, i.created_at
            FROM ideas i
            LEFT JOIN votes v ON i.id = v.idea_id
            WHERE i.screened = 1 AND i.hidden = 0
            GROUP BY i.id
            ORDER BY votes DESC, i.id DESC
            """
        )
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def list_ideas(product: str | None = None, status: str | None = None, held: bool = False) -> list[dict]:
    """List ideas for the owner. Includes email."""
    query = """
        SELECT i.id, i.product, i.text, i.email, i.status, COUNT(v.account_hash) AS votes,
               i.created_at, i.screened, i.hidden
        FROM ideas i
        LEFT JOIN votes v ON i.id = v.idea_id
        WHERE 1=1
    """
    params: list = []
    if held:
        query += " AND i.screened = 2"
    else:
        query += " AND i.screened != 2"

    if product:
        query += " AND i.product = ?"
        params.append(product)
    if status:
        query += " AND i.status = ?"
        params.append(status)

    query += " GROUP BY i.id ORDER BY i.created_at DESC, i.id DESC"

    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]


def _update_idea(idea_id: int, **cols) -> None:
    """Set columns of one idea in one statement. Column names are literals from this module."""
    if not 1 <= idea_id <= MAX_ID:
        raise FactoryError(f"suggestion #{idea_id} not found")
    with _connect() as conn:
        if not conn.execute("SELECT 1 FROM ideas WHERE id = ?", (idea_id,)).fetchone():
            raise FactoryError(f"suggestion #{idea_id} not found")
        sets = ", ".join(f"{c} = ?" for c in cols)
        conn.execute(f"UPDATE ideas SET {sets} WHERE id = ?", (*cols.values(), idea_id))


def set_status(idea_id: int, status: str) -> None:
    """Update an idea's status."""
    if status not in STATUSES:
        raise FactoryError(f"unknown status {status!r}; choose from {', '.join(STATUSES)}")
    _update_idea(idea_id, status=status)


def hide(idea_id: int) -> None:
    """Hide an idea from the public feed."""
    _update_idea(idea_id, hidden=1)


def unhide(idea_id: int) -> None:
    _update_idea(idea_id, hidden=0)


def pass_idea(idea_id: int) -> None:
    """Owner override: publish a held, pending or hidden idea (screened=1, hidden=0)."""
    _update_idea(idea_id, screened=1, hidden=0)


def _has_url(text: str) -> bool:
    return bool(_URL_RE.search(text))


def _has_email(text: str) -> bool:
    return bool(_EMAIL_RE.search(text))


def _has_phone(text: str) -> bool:
    text = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)  # ISO dates are not phone numbers
    for match in re.finditer(r"(?:\+?\d[\s().-]*){8,}", text):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) >= 8:
            return True
    return False


def _has_blocklist_word(text: str) -> bool:
    for word in config_list("suggestions.blocklist", words=True):
        w = word.strip()
        if not w:
            continue
        pattern = r"(?i)\b" + re.escape(w) + r"\b" if re.match(r"^\w+$", w) else re.escape(w)
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _is_duplicate_text(conn: sqlite3.Connection, product: str, text: str, exclude_id: int | None) -> bool:
    """Same text (case and edge spaces ignored) already stored for this product. For a stored idea
    (exclude_id), only older ideas or ones already screened count, so two pending twins keep the older."""
    q = "SELECT 1 FROM ideas WHERE product = ? AND LOWER(TRIM(text)) = LOWER(TRIM(?))"
    params: list = [product, text]
    if exclude_id is not None:
        q += " AND id != ? AND (screened != 0 OR id < ?)"
        params += [exclude_id, exclude_id]
    return conn.execute(q + " LIMIT 1", params).fetchone() is not None


def screen_verdict(conn: sqlite3.Connection, product: str, text: str, exclude_id: int | None = None) -> int:
    """1 = pass (public), 2 = hold: URL, email, 8+ digit phone, duplicate text for that product,
    or blocklisted word."""
    # fold look-alikes (fullwidth letters, invisible joiners) before the pattern checks
    plain = unicodedata.normalize("NFKC", text).replace("\u200c", "").replace("\u200d", "")
    held = (
        _has_url(plain)
        or _has_email(plain)
        or _has_phone(plain)
        or _is_duplicate_text(conn, product, text, exclude_id)
        or _has_blocklist_word(text)
    )
    return 2 if held else 1


def screen_pending() -> dict:
    """Run the automatic filter on screened=0 ideas (see screen_verdict)."""
    with _connect() as conn:
        rows = conn.execute("SELECT id, product, text FROM ideas WHERE screened = 0 ORDER BY id ASC").fetchall()
        passed = 0
        for idea_id, product, text in rows:
            verdict = screen_verdict(conn, product, text, exclude_id=idea_id)
            conn.execute("UPDATE ideas SET screened = ? WHERE id = ?", (verdict, idea_id))
            passed += verdict == 1
        conn.commit()
        return {"screened": len(rows), "passed": passed, "held": len(rows) - passed}


def ideas_count_last_hour(ip_hash: str) -> int:
    cutoff = store.iso(datetime.now(timezone.utc) - timedelta(seconds=3600))
    with _connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM ideas WHERE ip_hash = ? AND created_at >= ?",
            (ip_hash, cutoff),
        )
        return cur.fetchone()[0]


def votes_count_last_hour(account_hash: str) -> int:
    """on:true vote attempts (vote_log rows) in the last hour."""
    cutoff = store.iso(datetime.now(timezone.utc) - timedelta(seconds=3600))
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM vote_log WHERE account_hash = ? AND created_at >= ?",
            (account_hash, cutoff),
        ).fetchone()[0]
