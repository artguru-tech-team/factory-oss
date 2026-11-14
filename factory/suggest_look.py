"""AI first look for a new public suggestion: how it fits the public roadmap, and a rough effort.

One call to the Jev model (api.typesafe.ai) per public idea. Only the product key, the idea text and
the public roadmap titles leave the box. The reply carries fixed strings only, never model text.
Config: office.publicRoadmap.<product> (JSON list of titles), office.firstLook.enabled (default true),
office.firstLook.dailyMax (default 300). Secret: typesafe-key.
"""
import json
import math
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

from . import config, secrets, suggestions

URL = "https://api.typesafe.ai/v1/systemone"
TIMEOUT = 3  # seconds for the whole call (socket timeout plus a wall-clock wait on the worker)
MAX_BODY = 65536
NONE = "none"  # the choice that means "no roadmap item"; a roadmap title with this name is dropped
MAX_ITEMS = 30
MAX_TITLE = 120
DEFAULT_DAILY_MAX = 300
EFFORTS = ["A few hours", "About a day", "A few days", "One to two weeks", "More than two weeks"]

# ponytail: in-memory cap resets on restart; store it in the db if that matters
_lock = threading.Lock()
_day = None
_count = 0
# At most this many calls in flight. A call runs in a daemon thread (never blocks process exit) and
# stops reading at its own deadline; the visitor's request stops waiting at TIMEOUT either way.
MAX_IN_FLIGHT = 4
_slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a 3xx becomes an HTTPError: the bearer key never follows a redirect


_OPENER = urllib.request.build_opener(_NoRedirect)


def _enabled():
    """Only an explicit yes (or unset) turns it on: a typo in the kill switch switches it off."""
    v = config.get("office.firstLook.enabled", True)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "on")
    return v is True or (type(v) is int and v == 1)


def _daily_max():
    """Unset gives the default; anything that is not a whole number >= 0 gives 0 (off)."""
    v = config.get("office.firstLook.dailyMax", DEFAULT_DAILY_MAX)
    if isinstance(v, str) and re.fullmatch(r"[0-9]+", v.strip()):
        v = int(v)  # `factory config set` stores strings unless --as-json
    return v if type(v) is int and v >= 0 else 0


def _take_slot():
    """Count a call before making it; False once today's cap is used."""
    global _day, _count
    cap = _daily_max()  # read the config outside the lock
    today = datetime.now(timezone.utc).date()
    with _lock:
        if _day != today:
            _day, _count = today, 0
        if _count >= cap:
            return False
        _count += 1
        return True


class Busy(Exception):
    """MAX_IN_FLIGHT calls are still running: nothing was sent."""


def _give_back_slot():
    global _count
    with _lock:
        _count = max(0, _count - 1)


def _request(body, key):
    req = urllib.request.Request(
        URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "factory-suggest/1.0"},
        method="POST",
    )
    # ponytail: the deadline starts after the headers; a header drip or slow DNS/TLS can keep this
    # daemon thread up to a few socket timeouts longer (it holds a slot, so later calls fail fast as
    # busy). A total connect deadline needs raw sockets or an async client if that ever matters.
    deadline = time.monotonic() + TIMEOUT
    chunks, size = [], 0
    with _OPENER.open(req, timeout=TIMEOUT) as resp:
        while True:  # small reads against one deadline: a slow-drip reply cannot hold the thread
            if time.monotonic() > deadline:
                raise TimeoutError("first look deadline")
            chunk = resp.read1(4096)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BODY:
                raise ValueError("first look reply too large")
            chunks.append(chunk)
    return json.loads(b"".join(chunks).decode("utf-8"))


def _post(body, key):
    """Run _request in a daemon thread and wait at most TIMEOUT. With MAX_IN_FLIGHT calls still
    running (a stalled upstream) it raises Busy at once instead of queueing; the thread frees its
    slot when it really ends."""
    if not _slots.acquire(blocking=False):
        raise Busy()
    out = {}

    def run():
        try:
            out["value"] = _request(body, key)
        except Exception as e:
            out["error"] = e
        finally:
            _slots.release()

    try:
        t = threading.Thread(target=run, name="first-look", daemon=True)
        t.start()
    except Exception:
        _slots.release()
        raise
    t.join(TIMEOUT)
    if t.is_alive():
        raise TimeoutError("first look timed out")
    if "error" in out:
        raise out["error"]
    return out["value"]


def _prob(v):
    if type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1:
        raise ValueError("bad probability")
    return float(v)


def _effort(probs):
    """Every level exactly once, probabilities that add up to about 1, else the reply is not trusted."""
    scores = {int(k): _prob(v) for k, v in probs.items()}
    if len(scores) != len(probs) or set(scores) != set(range(len(EFFORTS))) or abs(sum(scores.values()) - 1) > 0.05:
        raise ValueError("bad effort levels")
    return EFFORTS[max(scores, key=scores.get)]


def _fail(reason):
    print(f"first look failed: {reason}", file=sys.stderr)
    return None


def first_look(product: str, text: str) -> dict | None:
    """{"fit", "effort", "source"} or None; never raises."""
    try:
        if not _enabled():
            return None
        try:
            key = secrets.get_secret("typesafe-key")
        except Exception as e:
            return _fail(f"secret typesafe-key not usable ({type(e).__name__})")
        titles = (t.strip() for t in suggestions.config_list(f"office.publicRoadmap.{product}"))
        items = list(dict.fromkeys(t for t in titles if t and len(t) <= MAX_TITLE and t.lower() != NONE))[:MAX_ITEMS]
        if not _take_slot():
            return _fail("daily cap reached")
        questions = {"effort": {
            "type": "score",
            "instructions": "How much engineering work would `idea` take to build in `product`?",
            "criteria": EFFORTS,
        }}
        if items:
            criteria = {t: None for t in items}
            criteria[NONE] = "None of the listed roadmap items covers this idea"
            questions["roadmap"] = {
                "type": "choice",
                "instructions": "Which item of `public_roadmap` already covers `idea`?",
                "criteria": criteria,
            }
        state = {"product": product, "idea": text, "public_roadmap": items}
        answers = _post({"model": "jev-latest", "state": state, "questions": questions}, key)["answers"]
        effort = _effort(answers["effort"]["probabilities"])
        if not items:
            fit = "No public roadmap for this product yet."
        else:
            r = answers["roadmap"]
            choice = r["choice"]
            if choice in items and _prob(r["probabilities"][choice]) >= 0.5:
                fit = f"Close to a planned item: {choice}"
            else:
                fit = "Not on the public roadmap yet."
        return {"fit": fit, "effort": effort, "source": "jev"}
    except Busy:
        _give_back_slot()  # nothing was sent, so the call does not count against the day
        return _fail("Busy")
    except Exception as e:
        return _fail(type(e).__name__)  # the class only: messages could carry the key or the idea
