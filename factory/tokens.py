"""Token counts and costs across Claude, Antigravity, and OpenCode telemetry.

Telemetry sources:
- Claude: T3's usage scan cache (~/.t3/userdata/usage-scan-cache-v9.json)
- Antigravity: SQLite conversation DBs (~/.t3/userdata/providers/antigravity/*/antigravity-acp/conversations/<id>.db)
  table gen_metadata (protobuf data blob)
- OpenCode: SQLite database (~/.local/share/opencode/opencode.db) table session_message and message

Counting rule:
input + cache writes + output counted towards total; cache reads tracked separately.
Est. cost from per-model USD price table in MODEL_PRICES, EUR beside it with fixed rate constant EUR_USD_RATE.
"""
import glob
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

from . import store
from .store import FactoryError, iso

VERSION = 9
CACHE_VERSION = 2
WINDOW = timedelta(hours=24)
EUR_USD_RATE = 0.92  # 1 USD = 0.92 EUR (fixed rate)

# Per-model prices in USD per 1,000,000 tokens (official first-party rates)
# 5-min cache write = 1.25x input
MODEL_PRICES = {
    # Claude models
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_read": 0.50, "cache_write": 6.25},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.50},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.20, "cache_write": 2.50},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.10, "cache_write": 1.25},
    "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.50},
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.50},
}

MODEL_ALIASES = {
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
}


def normalize_model(model):
    if not model:
        return ""
    m = model.lower().strip()
    for prefix in ("opencode/", "opencode-go/", "antigravity/", "claude/"):
        if m.startswith(prefix):
            m = m[len(prefix):]
    return m


def model_cost(model, input_tokens, output_tokens, cache_read=0, cache_write=0):
    m = normalize_model(model)
    m = MODEL_ALIASES.get(m, m)
    p = MODEL_PRICES.get(m)
    if not p:
        return None
    cost = (input_tokens * p["input"] + output_tokens * p["output"]
            + cache_read * p["cache_read"] + cache_write * p["cache_write"]) / 1_000_000.0
    return cost


def cache_path():
    return os.environ.get("FACTORY_USAGE_CACHE") or os.path.expanduser("~/.t3/userdata/usage-scan-cache-v9.json")


def opencode_db_path():
    return os.environ.get("FACTORY_OPENCODE_DB") or os.path.expanduser("~/.local/share/opencode/opencode.db")


def antigravity_dir():
    return os.environ.get("FACTORY_ANTIGRAVITY_DIR") or os.path.expanduser("~/.t3/userdata/providers/antigravity")


def load(path=None):
    path = path or cache_path()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except FileNotFoundError:
        raise FactoryError(f"T3 usage cache not found: {path}")
    if d.get("version") != VERSION or not isinstance(d.get("sessions"), list) or not isinstance(d.get("files"), dict):
        raise FactoryError(f"T3 usage cache format changed (expected version {VERSION} with sessions and files): {path}")
    return d, datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)


def per_session(d, now=None):
    """Tokens in the last 24 h per Claude session id, and the total over every session."""
    since = ((now or datetime.now(timezone.utc)) - WINDOW).timestamp() * 1000
    best = {}
    for f in d.get("files", {}).values():
        if f.get("p") != "claude":
            continue
        for i, r in enumerate(f.get("r", [])):
            if len(r) < 7 or r[0] < since:
                continue
            k = r[8] if len(r) > 8 and r[8] else (id(f), i)
            if k not in best or r[6] > best[k][6]:
                best[k] = r
    sessions, by, total = d.get("sessions", []), {}, 0
    for r in best.values():
        n = r[3] + r[5] + r[6]
        total += n
        if 0 <= r[2] < len(sessions):
            by[sessions[r[2]]] = by.get(sessions[r[2]], 0) + n
    return by, total


def decode_antigravity_blob(data):
    """Pure-Python varint/wire decoder for Antigravity gen_metadata protobuf BLOB.

    Extracts:
      field 1 -> sub-message
        field 4 = UsageMetadata (tag 2: prompt, 3: cand, 5: cached, 9: thought)
        field 9 -> field 4 -> field 1 = step epoch seconds
        field 21 = model name
    """
    try:
        pos, L = 0, len(data)
        f1_data = None
        while pos < L:
            b = data[pos]; pos += 1; tag = b & 0x7F; shift = 7
            while b & 0x80 and pos < L:
                b = data[pos]; pos += 1; tag |= (b & 0x7F) << shift; shift += 7
            wire, fn = tag & 7, tag >> 3
            if wire == 0:
                while pos < L and (data[pos] & 0x80): pos += 1
                if pos < L: pos += 1
            elif wire == 2:
                b = data[pos]; pos += 1; l = b & 0x7F; shift = 7
                while b & 0x80 and pos < L:
                    b = data[pos]; pos += 1; l |= (b & 0x7F) << shift; shift += 7
                if fn == 1: f1_data = data[pos:pos+l]; break
                pos += l
            elif wire == 1: pos += 8
            elif wire == 5: pos += 4
            else: break
        if not f1_data:
            return None

        pos, L = 0, len(f1_data)
        usage = None; f9_data = None; model = ""
        while pos < L:
            b = f1_data[pos]; pos += 1; tag = b & 0x7F; shift = 7
            while b & 0x80 and pos < L:
                b = f1_data[pos]; pos += 1; tag |= (b & 0x7F) << shift; shift += 7
            wire, fn = tag & 7, tag >> 3
            if wire == 0:
                while pos < L and (f1_data[pos] & 0x80): pos += 1
                if pos < L: pos += 1
            elif wire == 2:
                b = f1_data[pos]; pos += 1; l = b & 0x7F; shift = 7
                while b & 0x80 and pos < L:
                    b = f1_data[pos]; pos += 1; l |= (b & 0x7F) << shift; shift += 7
                sub = f1_data[pos:pos+l]; pos += l
                if fn == 4: usage = sub
                elif fn == 9: f9_data = sub
                elif fn == 21: model = sub.decode("utf-8", "replace")
            elif wire == 1: pos += 8
            elif wire == 5: pos += 4
            else: break

        prompt = cand = cached = thought = 0
        if usage:
            pos, L = 0, len(usage)
            while pos < L:
                b = usage[pos]; pos += 1; tag = b & 0x7F; shift = 7
                while b & 0x80 and pos < L:
                    b = usage[pos]; pos += 1; tag |= (b & 0x7F) << shift; shift += 7
                wire, fn = tag & 7, tag >> 3
                if wire == 0:
                    b = usage[pos]; pos += 1; val = b & 0x7F; shift = 7
                    while b & 0x80 and pos < L:
                        b = usage[pos]; pos += 1; val |= (b & 0x7F) << shift; shift += 7
                    if fn == 2: prompt = val
                    elif fn == 3: cand = val
                    elif fn == 5: cached = val
                    elif fn == 9: thought = val
                elif wire == 2:
                    b = usage[pos]; pos += 1; l = b & 0x7F; shift = 7
                    while b & 0x80 and pos < L:
                        b = usage[pos]; pos += 1; l |= (b & 0x7F) << shift; shift += 7
                    pos += l
                elif wire == 1: pos += 8
                elif wire == 5: pos += 4
                else: break

        sec = 0
        if f9_data:
            pos, L = 0, len(f9_data); f9_4 = None
            while pos < L:
                b = f9_data[pos]; pos += 1; tag = b & 0x7F; shift = 7
                while b & 0x80 and pos < L:
                    b = f9_data[pos]; pos += 1; tag |= (b & 0x7F) << shift; shift += 7
                wire, fn = tag & 7, tag >> 3
                if wire == 2:
                    b = f9_data[pos]; pos += 1; l = b & 0x7F; shift = 7
                    while b & 0x80 and pos < L:
                        b = f9_data[pos]; pos += 1; l |= (b & 0x7F) << shift; shift += 7
                    if fn == 4: f9_4 = f9_data[pos:pos+l]; break
                    pos += l
                elif wire == 0:
                    while pos < L and (f9_data[pos] & 0x80): pos += 1
                    if pos < L: pos += 1
                elif wire == 1: pos += 8
                elif wire == 5: pos += 4
                else: break
            if f9_4:
                pos, L = 0, len(f9_4)
                while pos < L:
                    b = f9_4[pos]; pos += 1; tag = b & 0x7F; shift = 7
                    while b & 0x80 and pos < L:
                        b = f9_4[pos]; pos += 1; tag |= (b & 0x7F) << shift; shift += 7
                    wire, fn = tag & 7, tag >> 3
                    if wire == 0 and fn == 1:
                        b = f9_4[pos]; pos += 1; sec = b & 0x7F; shift = 7
                        while b & 0x80 and pos < L:
                            b = f9_4[pos]; pos += 1; sec |= (b & 0x7F) << shift; shift += 7
                        break
                    elif wire == 0:
                        while pos < L and (f9_4[pos] & 0x80): pos += 1
                        if pos < L: pos += 1
                    elif wire == 2:
                        b = f9_4[pos]; pos += 1; l = b & 0x7F; shift = 7
                        while b & 0x80 and pos < L:
                            b = f9_4[pos]; pos += 1; l |= (b & 0x7F) << shift; shift += 7
                        pos += l
                    elif wire == 1: pos += 8
                    elif wire == 5: pos += 4
                    else: break

        return {"model": model, "prompt": prompt, "cand": cand, "cached": cached, "thought": thought, "sec": sec}
    except Exception:
        return None


def load_tokens_cache():
    p = store.path("tokens-cache.json")
    default = {"version": CACHE_VERSION, "antigravity": {}, "claude": {}}
    try:
        if not os.path.exists(p) or os.path.getsize(p) == 0:
            return default
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return default

    if not isinstance(d, dict) or d.get("version") != CACHE_VERSION:
        return default
    if not isinstance(d.get("antigravity"), dict) or not isinstance(d.get("claude"), dict):
        return default
    return d


def save_tokens_cache(cache_data, dirty=True):
    if not dirty:
        return
    try:
        cache_data["version"] = CACHE_VERSION
        store.write_text("tokens-cache.json", json.dumps(cache_data, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_claude_events(since_sec, warnings=None, now=None, cache_data=None):
    """Load Claude events since since_sec from T3 usage scan cache."""
    events = []
    scanned_at = None
    path = cache_path()
    if not os.path.exists(path):
        if warnings is not None:
            warnings.append(f"Claude usage cache not found: {path}")
        return events, None

    try:
        mtime = os.path.getmtime(path)
        claude_cache = (cache_data or {}).get("claude", {})
        if claude_cache.get("mtime") == mtime and "events" in claude_cache:
            raw_events = [e for e in claude_cache["events"] if e["time"] >= since_sec]
            # Compute cost dynamically at read time!
            events = [
                {**e, "cost": model_cost(e.get("model", ""), e.get("input", 0), e.get("output", 0),
                                        e.get("cache_read", 0), e.get("cache_write", 0))}
                for e in raw_events
            ]
            scanned_at = datetime.fromisoformat(claude_cache["scanned_at"]) if claude_cache.get("scanned_at") else datetime.fromtimestamp(mtime, timezone.utc)
            return events, scanned_at

        cache, scanned_at = load(path)
    except Exception as e:
        if warnings is not None:
            warnings.append(f"Claude usage cache unreadable: {e}")
        return events, None

    since_ms = since_sec * 1000
    best = {}
    for f in cache.get("files", {}).values():
        if f.get("p") != "claude":
            continue
        for i, r in enumerate(f.get("r", [])):
            # Keep only 30 days of events
            if len(r) < 7 or r[0] < since_ms:
                continue
            k = r[8] if len(r) > 8 and r[8] else (id(f), i)
            if k not in best or r[6] > best[k][6]:
                best[k] = r

    sessions = cache.get("sessions", [])
    models = cache.get("models", [])
    cached_claude_events = []
    for r in best.values():
        sess_id = sessions[r[2]] if 0 <= r[2] < len(sessions) else None
        model_name = models[r[1]] if 0 <= r[1] < len(models) else ""
        inp = r[3]
        cr = r[4]
        cw = r[5]
        out = r[6]
        tot = inp + cw + out
        # Cache token counts only, no cost
        cached_claude_events.append({
            "provider": "Claude",
            "session_id": sess_id,
            "time": r[0] / 1000.0,
            "model": model_name,
            "input": inp,
            "output": out,
            "cache_read": cr,
            "cache_write": cw,
            "total": tot,
        })

    if cache_data is not None:
        cache_data["claude"] = {
            "mtime": mtime,
            "scanned_at": scanned_at.isoformat(),
            "events": cached_claude_events,
        }

    events = [
        {**e, "cost": model_cost(e.get("model", ""), e.get("input", 0), e.get("output", 0),
                                e.get("cache_read", 0), e.get("cache_write", 0))}
        for e in cached_claude_events
    ]
    return events, scanned_at


def load_opencode_events(since_sec, warnings=None):
    """Load OpenCode events since since_sec from opencode.db (read-only)."""
    events = []
    path = opencode_db_path()
    if not os.path.exists(path):
        # Finding 1: missing OpenCode DB is not an error
        return events
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        since_ms = since_sec * 1000

        v2_session_ids = set()
        has_v2 = bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_v2'").fetchone())
        if has_v2:
            for (sid,) in con.execute("SELECT id FROM session_v2"):
                v2_session_ids.add(sid)

        has_sm = bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_message'").fetchone())
        if has_sm:
            cur = con.execute("SELECT session_id, time_created, data FROM session_message WHERE time_created >= ?", (since_ms,))
            for sess_id, time_created, data_str in cur:
                try:
                    d = json.loads(data_str or "{}")
                except Exception:
                    continue
                tok = d.get("tokens")
                if not tok:
                    continue
                m = d.get("modelID") or (d.get("model") if isinstance(d.get("model"), str) else (d.get("model") or {}).get("modelID") or (d.get("model") or {}).get("id")) or ""
                inp = tok.get("input", 0)
                out = tok.get("output", 0) + tok.get("reasoning", 0)
                cw = (tok.get("cache") or {}).get("write", 0)
                cr = (tok.get("cache") or {}).get("read", 0)
                tot = inp + cw + out
                raw_cost = d.get("cost")
                if raw_cost is not None:
                    try:
                        cost = float(raw_cost)
                    except (ValueError, TypeError):
                        cost = model_cost(m, inp, out, cr, cw)
                else:
                    cost = model_cost(m, inp, out, cr, cw)
                events.append({
                    "provider": "OpenCode",
                    "session_id": sess_id,
                    "time": time_created / 1000.0,
                    "model": m,
                    "input": inp,
                    "output": out,
                    "cache_read": cr,
                    "cache_write": cw,
                    "total": tot,
                    "cost": cost,
                })

        has_msg = bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message'").fetchone())
        if has_msg:
            cur = con.execute("SELECT session_id, time_created, data FROM message WHERE time_created >= ?", (since_ms,))
            for sess_id, time_created, data_str in cur:
                if sess_id in v2_session_ids:
                    continue  # de-duplicate legacy messages whose sessions also exist in v2
                try:
                    d = json.loads(data_str or "{}")
                except Exception:
                    continue
                tok = d.get("tokens")
                if not tok:
                    continue
                m = d.get("modelID") or (d.get("model") if isinstance(d.get("model"), str) else (d.get("model") or {}).get("modelID")) or ""
                inp = tok.get("input", 0)
                out = tok.get("output", 0) + tok.get("reasoning", 0)
                cw = (tok.get("cache") or {}).get("write", 0)
                cr = (tok.get("cache") or {}).get("read", 0)
                tot = inp + cw + out
                raw_cost = d.get("cost")
                if raw_cost is not None:
                    try:
                        cost = float(raw_cost)
                    except (ValueError, TypeError):
                        cost = model_cost(m, inp, out, cr, cw)
                else:
                    cost = model_cost(m, inp, out, cr, cw)
                events.append({
                    "provider": "OpenCode",
                    "session_id": sess_id,
                    "time": time_created / 1000.0,
                    "model": m,
                    "input": inp,
                    "output": out,
                    "cache_read": cr,
                    "cache_write": cw,
                    "total": tot,
                    "cost": cost,
                })
        con.close()
    except Exception as e:
        if warnings is not None:
            warnings.append(f"OpenCode database read error ({path}): {e}")
    return events


def load_antigravity_events(since_sec, warnings=None, cache_data=None):
    """Load Antigravity events since since_sec from conversation DBs (read-only)."""
    events = []
    base = antigravity_dir()
    db_env = os.environ.get("FACTORY_ANTIGRAVITY_DB")
    if db_env:
        dbs = [db_env]
    else:
        dbs = glob.glob(os.path.join(base, "*", "antigravity-acp", "conversations", "*.db"))
    if not dbs:
        return events

    ag_cache = (cache_data or {}).get("antigravity", {})
    new_cache = {}

    for db_path in dbs:
        try:
            mtime = os.path.getmtime(db_path)
            size = os.path.getsize(db_path)
            wal_path = db_path + "-wal"
            if os.path.exists(wal_path):
                try:
                    wal_mtime = os.path.getmtime(wal_path)
                    mtime = max(mtime, wal_mtime)
                    size += os.path.getsize(wal_path)
                except OSError:
                    pass

            # Skip if file freshness is older than window (Finding 8: keep 30 days)
            if mtime < since_sec:
                continue

            cache_key = [mtime, size, CACHE_VERSION]
            cached_entry = ag_cache.get(db_path)
            if cached_entry and cached_entry.get("key") == cache_key:
                raw_events = cached_entry.get("events", [])
                new_cache[db_path] = cached_entry
                for ev in raw_events:
                    if ev["time"] >= since_sec:
                        # Finding 4: compute cost at read time!
                        cost = model_cost(ev.get("model", ""), ev.get("input", 0), ev.get("output", 0),
                                          ev.get("cache_read", 0), ev.get("cache_write", 0))
                        events.append({**ev, "cost": cost})
                continue

            conv_id = os.path.splitext(os.path.basename(db_path))[0]
            db_events = []
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            for (blob,) in con.execute("SELECT data FROM gen_metadata WHERE length(data) > 20"):
                res = decode_antigravity_blob(blob)
                if not res:
                    continue
                inp = res["prompt"]
                out = res["cand"]
                cr = res["cached"]
                cw = 0
                tot = inp + cw + out
                # Finding 4: cache token counts only, no cost
                ev = {
                    "provider": "Antigravity",
                    "session_id": conv_id,
                    "time": float(res["sec"]),
                    "model": res["model"],
                    "input": inp,
                    "output": out,
                    "cache_read": cr,
                    "cache_write": cw,
                    "total": tot,
                }
                db_events.append(ev)
                if ev["time"] >= since_sec:
                    cost = model_cost(res["model"], inp, out, cr, cw)
                    events.append({**ev, "cost": cost})
            con.close()
            new_cache[db_path] = {"key": cache_key, "events": db_events}
        except Exception as e:
            if warnings is not None:
                warnings.append(f"Antigravity conversation read error ({os.path.basename(db_path)}): {e}")

    if cache_data is not None:
        cache_data["antigravity"] = new_cache

    return events


def build_bars_24h(events, now):
    """Stacked bars for 24 h view covering rolling 24 h window with aligned ticks."""
    cutoff = now - timedelta(hours=24)
    t = cutoff.replace(minute=0, second=0, microsecond=0)
    t -= timedelta(hours=t.hour % 4)
    ticks = []
    while True:
        t += timedelta(hours=4)
        if t >= now:
            break
        if t > cutoff:
            ticks.append(t)
    edges = [cutoff] + ticks + [now]

    t_edges = [dt.timestamp() for dt in edges]
    bars = []
    n_slots = len(edges) - 1
    for i in range(n_slots):
        t_start = t_edges[i]
        is_last = (i == n_slots - 1)
        if is_last:
            slot_events = [e for e in events if e["time"] >= t_start]
        else:
            t_end = t_edges[i + 1]
            slot_events = [e for e in events if t_start <= e["time"] < t_end]

        label = edges[0].strftime("%H:%M") if i == 0 else edges[i].strftime("%H:00")
        if i == 0 and n_slots > 1 and label == edges[-2].strftime("%H:00"):
            label = edges[0].strftime("%H:%M:%S")  # yesterday's hh:00:ss would repeat today's hh:00 (cron at hh:00)
        provs = {}
        tot = 0
        for e in slot_events:
            p = e["provider"]
            provs[p] = provs.get(p, 0) + e["total"]
            tot += e["total"]
        bars.append({
            "label": label,
            "total": tot,
            "providers": provs,
            "claude": provs.get("Claude", 0),
            "antigravity": provs.get("Antigravity", 0),
            "opencode": provs.get("OpenCode", 0),
            "litellm": provs.get("LiteLLM", 0),
        })
    return bars


def build_bars_daily(events, num_days, start_date):
    """Daily stacked bars for 7 d or 30 d view."""
    bars = []
    for i in range(num_days):
        d = start_date + timedelta(days=i)
        d_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()
        d_end = d_start + 86400
        slot_events = [e for e in events if d_start <= e["time"] < d_end]
        provs = {}
        tot = 0
        for e in slot_events:
            p = e["provider"]
            provs[p] = provs.get(p, 0) + e["total"]
            tot += e["total"]
        label = d.strftime("%a %d") if num_days == 7 else d.strftime("%d")
        bars.append({
            "label": label,
            "date": d.isoformat(),
            "total": tot,
            "providers": provs,
            "claude": provs.get("Claude", 0),
            "antigravity": provs.get("Antigravity", 0),
            "opencode": provs.get("OpenCode", 0),
            "litellm": provs.get("LiteLLM", 0),
        })
    return bars


def summarize_window(events, bars):
    tot = sum(e["total"] for e in events)
    inp = sum(e["input"] for e in events)
    out = sum(e["output"] for e in events)
    cr = sum(e["cache_read"] for e in events)
    cw = sum(e["cache_write"] for e in events)
    by_prov = {}
    cost = 0.0
    has_cost = False
    priced_tot = 0
    unpriced_tot = 0
    for e in events:
        p = e["provider"]
        by_prov[p] = by_prov.get(p, 0) + e["total"]
        if e.get("cost") is not None:
            cost += e["cost"]
            has_cost = True
            priced_tot += e["total"]
        else:
            unpriced_tot += e["total"]

    return {
        "tot": tot,
        "input": inp,
        "output": out,
        "cache": cr,
        "cache_write": cw,
        "cost": round(cost, 2) if has_cost else None,
        "priced": priced_tot,
        "unpriced": unpriced_tot,
        "byProvider": by_prov,
        "claude": by_prov.get("Claude", 0),
        "antigravity": by_prov.get("Antigravity", 0),
        "opencode": by_prov.get("OpenCode", 0),
        "litellm": by_prov.get("LiteLLM", 0),
        "bars": bars,
    }


def token_history_and_agents(rows, sessions, now=None, warnings=None, thread_map=None, hcs=None, token_warnings=None):
    """Collect telemetry across providers, produce tokenHistory and agentTokens."""
    now = now or datetime.now(timezone.utc)
    today = now.date()

    tok_warn = token_warnings if token_warnings is not None else []

    # 1. Window cutoffs
    # Finding 2: Totals and per-agent counts: rolling now-24h; only the bar buckets are aligned.
    cutoff_rolling_24h = (now - WINDOW).timestamp()

    start_date_7d = today - timedelta(days=6)
    cutoff_7d = datetime(start_date_7d.year, start_date_7d.month, start_date_7d.day, tzinfo=timezone.utc).timestamp()

    start_date_30d = today - timedelta(days=29)
    cutoff_30d = datetime(start_date_30d.year, start_date_30d.month, start_date_30d.day, tzinfo=timezone.utc).timestamp()

    # 2. Load telemetry events with persistence cache
    tokens_cache = load_tokens_cache()
    orig_cache_str = json.dumps(tokens_cache, sort_keys=True)

    c_events, scanned_at = load_claude_events(cutoff_30d, tok_warn, now, tokens_cache)
    oc_events = load_opencode_events(cutoff_30d, tok_warn)
    ag_events = load_antigravity_events(cutoff_30d, tok_warn, tokens_cache)

    new_cache_str = json.dumps(tokens_cache, sort_keys=True)
    dirty = (orig_cache_str != new_cache_str)
    # Finding 8: write only when something changed
    save_tokens_cache(tokens_cache, dirty=dirty)

    claude_loaded = os.path.exists(cache_path()) and scanned_at is not None
    opencode_loaded = os.path.exists(opencode_db_path())
    antigravity_loaded = bool(os.environ.get("FACTORY_ANTIGRAVITY_DB")) or bool(glob.glob(os.path.join(antigravity_dir(), "*", "antigravity-acp", "conversations", "*.db")))
    sources_loaded = claude_loaded or opencode_loaded or antigravity_loaded or bool(c_events or oc_events or ag_events)

    all_events = c_events + oc_events + ag_events

    if warnings is not None and token_warnings is None:
        warnings.extend(tok_warn)

    ev_rolling_24h = [e for e in all_events if e["time"] >= cutoff_rolling_24h]
    ev_7d = [e for e in all_events if e["time"] >= cutoff_7d]
    ev_30d = all_events

    # 3. Stacked bars and summaries
    bars_24h = build_bars_24h(ev_rolling_24h, now)
    bars_7d = build_bars_daily(ev_7d, 7, start_date_7d)
    bars_30d = build_bars_daily(ev_30d, 30, start_date_30d)

    summary_24h = summarize_window(ev_rolling_24h, bars_24h)
    summary_7d = summarize_window(ev_7d, bars_7d)
    summary_30d = summarize_window(ev_30d, bars_30d)

    # 4. Helper to aggregate tokens for a group of events
    def agg_events(match_events):
        if not match_events:
            return 0, 0, 0, 0, 0, None
        inp = sum(e["input"] for e in match_events)
        out = sum(e["output"] for e in match_events)
        cr = sum(e["cache_read"] for e in match_events)
        cw = sum(e["cache_write"] for e in match_events)
        tot = sum(e["total"] for e in match_events)
        cost = 0.0
        has_cost = False
        for e in match_events:
            if e.get("cost") is not None:
                cost += e["cost"]
                has_cost = True
        return inp, out, cr, cw, tot, (round(cost, 2) if has_cost else None)

    # Session attribution map
    session_to_thread = dict(thread_map or {})
    rows_by_hc = {r["hc"]: r for r in rows if "hc" in r}
    for hc, sids in (sessions or {}).items():
        r_info = rows_by_hc.get(hc)
        for sid in sids:
            if sid not in session_to_thread:
                session_to_thread[sid] = {
                    "thread": r_info.get("thread") if r_info else None,
                    "hc": hc,
                    "who": r_info.get("who", hc) if r_info else hc,
                    "provider": r_info.get("provider", "unknown") if r_info else "unknown",
                    "model": r_info.get("model", "") if r_info else "",
                    "via": r_info.get("via", "") if r_info else "",
                    "project": r_info.get("project") if r_info else None,
                    "parent": r_info.get("parent") if (r_info and r_info.get("who") == "Subagent") else None,
                }

    # Finding 3: Build table directly from events
    def build_table_from_events(period_events):
        sess_ev = {}
        for e in period_events:
            sid = e.get("session_id")
            if sid:
                sess_ev.setdefault(sid, []).append(e)

        threads_agg = {}
        for sid, ev_list in sess_ev.items():
            tinfo = session_to_thread.get(sid)
            if tinfo:
                key = tinfo["hc"]
                if key not in threads_agg:
                    threads_agg[key] = {
                        "hc": tinfo["hc"],
                        "who": tinfo["who"],
                        "provider": tinfo["provider"],
                        "model": tinfo["model"] or (ev_list[0]["model"] if ev_list else ""),
                        "project": tinfo["project"],
                        "parent": tinfo.get("parent"),
                        "sids": set(),
                        "events": [],
                    }
                threads_agg[key]["sids"].add(sid)
                threads_agg[key]["events"].extend(ev_list)
            else:
                key = sid
                prov = ev_list[0]["provider"] if ev_list else "unknown"
                mod = ev_list[0]["model"] if ev_list else ""
                threads_agg[key] = {
                    "hc": sid[:8].upper(),
                    "who": f"Session {sid[:8]}",
                    "provider": prov,
                    "model": mod,
                    "project": None,
                    "parent": None,
                    "sids": {sid},
                    "events": ev_list,
                }

        table_rows = []
        for tagg in threads_agg.values():
            inp, out, cr, cw, tot, cost = agg_events(tagg["events"])
            table_rows.append({
                "hc": tagg["hc"],
                "who": tagg["who"],
                "provider": tagg["provider"],
                "model": tagg["model"],
                "project": tagg["project"],
                "parent": tagg.get("parent"),
                "input": inp,
                "output": out,
                "cache": cr,
                "cache_write": cw,
                "tot": tot,
                "cost": cost,
                "unmetered": False,
                "why": "",
            })
        table_rows.sort(key=lambda r: (1 if r["unmetered"] or r["tot"] is None else 0, -(r["tot"] or 0)))
        return table_rows

    # Build all three tables from events the same way
    agents_24h_from_events = build_table_from_events(ev_rolling_24h)
    agents_7d = build_table_from_events(ev_7d)
    agents_30d = build_table_from_events(ev_30d)

    found_24h_hcs = {a["hc"]: a for a in agents_24h_from_events}
    for r in rows:
        hc = r.get("hc")
        if hc in found_24h_hcs:
            r["tokens"] = found_24h_hcs[hc]["tot"]
        else:
            r["tokens"] = None

    # Merge in 24 h agent rows only for "counted in parent" / "No data" entries
    merged_24h_agents = list(agents_24h_from_events)
    for r in rows:
        hc = r.get("hc")
        if hc not in found_24h_hcs:
            # Finding 7: "counted in parent" apply only when who == "Subagent"
            is_subagent = (r.get("who") == "Subagent") and bool(r.get("parent"))
            if is_subagent:
                merged_24h_agents.append({
                    "hc": hc,
                    "who": r.get("who", hc),
                    "provider": r.get("provider", "unknown"),
                    "model": r.get("model", ""),
                    "project": r.get("project"),
                    "parent": r.get("parent"),
                    "input": None, "output": None, "cache": None, "cache_write": None, "tot": None, "cost": None,
                    "unmetered": True,
                    "why": f"Tokens counted in parent session {r['parent']}.",
                })
            else:
                why = "Agent created outside T3 (via factory CLI); no telemetry stream." if (r.get("via") == "factory CLI") else f"No telemetry recorded for {r.get('provider', 'agent')} in 24h."
                merged_24h_agents.append({
                    "hc": hc,
                    "who": r.get("who", hc),
                    "provider": r.get("provider", "unknown"),
                    "model": r.get("model", ""),
                    "project": r.get("project"),
                    "parent": None,
                    "input": None, "output": None, "cache": None, "cache_write": None, "tot": None, "cost": None,
                    "unmetered": True,
                    "why": why,
                })

    merged_24h_agents.sort(key=lambda a: (1 if a["unmetered"] or a["tot"] is None else 0, -(a["tot"] or 0)))

    token_history = {
        "24h": {**summary_24h, "agents": merged_24h_agents},
        "7d": {**summary_7d, "agents": agents_7d},
        "30d": {**summary_30d, "agents": agents_30d},
        "sourcesLoaded": sources_loaded,
    }

    claude_all_24h = token_history["24h"]["claude"]
    meta = {
        "window": "24 h",
        "scannedAt": iso(scanned_at or now),
        "claudeAll": claude_all_24h,
        "note": "Recorded tokens: input + cache writes + output from Claude, Antigravity, and OpenCode telemetry. "
                "Cache reads excluded from total. Agents outside T3 or without recorded telemetry show 'No data'.",
    }
    return token_history, merged_24h_agents, meta
