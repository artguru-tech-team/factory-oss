"""Roadmap view: store, Notion source sync, and rules for Now / Next / Later / Done."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

from . import config, secrets, store
from .store import FactoryError, now_iso

LANES = ("now", "next", "later", "icebox", "done")
DEFAULT_CAP = 7
ROADMAP_LOCK = ".roadmap.lock"
DEFAULT_DONE_STATUS = ["Released", "Done"]
USER_AGENT = "factory-roadmap/1.0"
TIMEOUT = 15


class RoadmapError(FactoryError):
    def __init__(self, message, code=None, now=None, status=None):
        super().__init__(message)
        self.code = code
        self.now = now
        self.status = status


def cap():
    c = config.get("roadmap.cap", DEFAULT_CAP)
    try:
        val = int(c)
        return max(1, val)
    except (ValueError, TypeError):
        return DEFAULT_CAP


def load():
    return store.roadmap()


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _require_writable(prod, data):
    err = source_error(prod, data)
    if err:
        raise RoadmapError(f"product {prod!r} is read-only (source in error: {err})", code="readonly")


def _require_now_room(data, prod):
    cur = [i for i in data["items"] if i.get("product") == prod and i.get("lane") == "now"]
    if len(cur) >= cap():
        raise RoadmapError("now is full", code="now_full", now=cur)


def source_error(product, data=None):
    data = data or load()
    return data.get("sources", {}).get(product, {}).get("error")


def get_notion_config(product):
    raw = config.get(f"roadmap.sources.{product}")
    if raw is None:
        return None
    if not isinstance(raw, dict) or "notion_database" not in raw or "lanes" not in raw or not isinstance(raw.get("lanes"), dict):
        raise FactoryError(f"roadmap.sources.{product} must be an object with notion_database and lanes")
    return raw


def items(product=None, data=None):
    data = data or load()
    all_items = data.get("items", [])
    if product and product != "all":
        all_items = [i for i in all_items if i.get("product") == product]

    lane_order = {l: k for k, l in enumerate(LANES)}
    done = sorted((i for i in all_items if i.get("lane") == "done"), key=lambda i: str(i.get("done") or ""), reverse=True)
    rest = sorted((i for i in all_items if i.get("lane") != "done"), key=lambda i: (lane_order.get(i.get("lane"), 99), i.get("rank", 999)))
    # done is the last known lane; unknown lanes (bad data) go after it
    return [i for i in rest if i.get("lane") in lane_order] + done + [i for i in rest if i.get("lane") not in lane_order]


def renumber(items_list, product, lane):
    sibs = [i for i in items_list if i.get("product") == product and i.get("lane") == lane]
    sibs.sort(key=lambda x: x.get("rank", 1))
    for k, s in enumerate(sibs):
        s["rank"] = k + 1


def link_project(item, projects=None):
    if item.get("project"):
        return
    if projects is None:
        try:
            projects = store.projects()
        except Exception:
            projects = []
    iid = item.get("id")
    for p in projects:
        if p.get("feat") and p.get("feat") == iid:
            item["project"] = p["id"]
            break


def _notion_http(method, url, token, body=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    data_bytes = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            content = resp.read()
            return json.loads(content.decode("utf-8")) if content else {}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            err_data = json.loads(raw.decode("utf-8"))
            msg = err_data.get("message") or f"Notion HTTP {e.code}"
        except Exception:
            msg = f"Notion HTTP {e.code}"
        raise RoadmapError(msg, code="notion_error", status=e.code)
    except urllib.error.URLError as e:
        raise RoadmapError(f"Notion connection error: {e.reason}", code="notion_error")
    except Exception as e:
        raise RoadmapError(f"Notion error: {e}", code="notion_error")


def _priority_for_lane(lanes_cfg, lane):
    val = lanes_cfg.get(lane)
    if not val:
        return None
    return val[0] if isinstance(val, list) else val


def _build_patch_props(source_cfg, item, from_lane=None, new_rank=None, title_changed=False, due_changed=False):
    props = {}
    target_lane = item.get("lane")
    lanes_cfg = source_cfg.get("lanes", {})

    if from_lane is not None and from_lane != target_lane:
        prio = _priority_for_lane(lanes_cfg, target_lane)
        if prio:
            props["Priority"] = {"select": {"name": prio}}
        if target_lane == "done" and from_lane != "done":
            done_statuses = source_cfg.get("done_status", DEFAULT_DONE_STATUS)
            props["Status"] = {"status": {"name": done_statuses[0]}}
        elif from_lane == "done" and target_lane != "done":
            reopen_status = source_cfg.get("reopen_status", source_cfg.get("new_status", "Backlog"))
            props["Status"] = {"status": {"name": reopen_status}}

    if title_changed:
        rank_pfx = source_cfg.get("rank_prefix", True)
        clean_title = re.sub(r"^P\d+\s*[-—–]\s*", "", item.get("title", ""))
        rank = new_rank if new_rank is not None else item.get("rank", 1)
        if rank_pfx and target_lane == "now":
            full_title = f"P{rank} — {clean_title}"
        else:
            full_title = clean_title
        props["Project name"] = {"title": [{"text": {"content": full_title}}]}

    if due_changed:
        due = item.get("due")
        props["Target Launch"] = {"date": {"start": due}} if due else {"date": None}

    return props


def _execute_notion_writes(writes, token):
    succeeded = 0
    for page_id, props in writes:
        if not props or not page_id:
            continue
        try:
            _notion_http("PATCH", f"https://api.notion.com/v1/pages/{page_id}", token, {"properties": props})
            succeeded += 1
        except Exception as e:
            if succeeded > 0:
                raise RoadmapError(f"Notion partly updated: {e}", code="notion_partial") from e
            raise


def _apply_move(data, id, lane, before=None):
    item = next((i for i in data["items"] if i.get("id") == id), None)
    if not item:
        raise RoadmapError(f"unknown item {id!r}")
    prod = item.get("product")
    from_lane = item.get("lane")

    if before == id and lane == from_lane:  # dropped on itself
        return item
    if before == id:
        before = None

    if lane == "done" and from_lane != "done":
        item["done"] = _today()
    elif from_lane == "done" and lane != "done":
        item.pop("done", None)

    item["lane"] = lane

    if lane != "done":
        sibs = [i for i in data["items"] if i.get("product") == prod and i.get("lane") == lane and i.get("id") != id]
        sibs.sort(key=lambda x: x.get("rank", 1))
        idx = next((k for k, x in enumerate(sibs) if x.get("id") == before), len(sibs)) if before else len(sibs)
        sibs.insert(idx, item)
        for k, x in enumerate(sibs):
            x["rank"] = k + 1

    if from_lane != lane and from_lane != "done":
        renumber(data["items"], prod, from_lane)

    return item


def _apply_swap(data, id_in, id_out, before=None):
    item_in = next((i for i in data["items"] if i.get("id") == id_in), None)
    item_out = next((i for i in data["items"] if i.get("id") == id_out), None)
    if not item_in or not item_out:  # removed by a sync between planning and saving
        raise RoadmapError(f"item {id_in if not item_in else id_out!r} is gone; reload the roadmap")
    prod = item_in.get("product")
    from_lane = item_in.get("lane")

    out_old_rank = item_out.get("rank", 1)

    item_out["lane"] = "next"
    next_sibs = [i for i in data["items"] if i.get("product") == prod and i.get("lane") == "next" and i.get("id") != id_out]
    next_sibs.sort(key=lambda x: x.get("rank", 1))
    next_sibs.insert(0, item_out)
    for k, x in enumerate(next_sibs):
        x["rank"] = k + 1

    if from_lane == "done":
        item_in.pop("done", None)
    item_in["lane"] = "now"

    now_sibs = [i for i in data["items"] if i.get("product") == prod and i.get("lane") == "now" and i.get("id") not in (id_in, id_out)]
    now_sibs.sort(key=lambda x: x.get("rank", 1))

    if before == id_out:
        idx = max(0, min(out_old_rank - 1, len(now_sibs)))
    elif before:
        idx = next((k for k, x in enumerate(now_sibs) if x.get("id") == before), len(now_sibs))
    else:
        idx = len(now_sibs)

    now_sibs.insert(idx, item_in)
    for k, x in enumerate(now_sibs):
        x["rank"] = k + 1

    if from_lane != "now" and from_lane != "done":
        renumber(data["items"], prod, from_lane)

    return {"in": item_in, "out": item_out}


def _writing():
    """Held for a whole roadmap change, Notion calls included: roadmap writes run one at a time,
    so the plan, the Notion writes and the save always see the same state. The global
    store lock is only taken around the save, so other factory writers never wait on Notion."""
    return store.lock(ROADMAP_LOCK)


def _save(data):
    with store.lock():
        store.save_roadmap(data)


def _find(data, id):
    item = next((i for i in data["items"] if i.get("id") == id), None)
    if not item:
        raise RoadmapError(f"unknown item {id!r}")
    return item


def _require_mapped_lane(source_cfg, lane):
    """A Notion item can only go to a lane that has a Priority (or done): otherwise sync undoes it."""
    if lane != "done" and not _priority_for_lane(source_cfg.get("lanes", {}), lane):
        raise RoadmapError(f"lane {lane!r} has no Notion priority mapping", code="bad_lane")


def _now_ranks(data, prod):
    return {i["id"]: i.get("rank") for i in data["items"] if i.get("product") == prod and i.get("lane") == "now"}


def _shifted_now_writes(source_cfg, data, prod, old_ranks, skip):
    """Title rewrites for every other Notion item in Now whose rank changed."""
    if not source_cfg.get("rank_prefix", True):
        return []
    return [(o["sourceId"], _build_patch_props(source_cfg, o, title_changed=True))
            for o in data["items"]
            if o.get("product") == prod and o.get("lane") == "now" and o.get("id") not in skip
            and o.get("source") == "notion" and o.get("sourceId") and o.get("rank") != old_ranks.get(o.get("id"))]


def move(id, lane, before=None):
    if lane not in LANES:
        raise RoadmapError(f"unknown lane {lane!r}")
    with _writing():
        data = load()
        item = _find(data, id)
        prod = item.get("product")
        _require_writable(prod, data)
        from_lane = item.get("lane")
        if lane == "now" and from_lane != "now":
            _require_now_room(data, prod)
        source_cfg = get_notion_config(prod) if item.get("source") == "notion" else None
        if source_cfg:
            _require_mapped_lane(source_cfg, lane)
        old_ranks = _now_ranks(data, prod)
        _apply_move(data, id, lane, before=before)
        if source_cfg:
            rank_moved = (lane == "now" and item.get("rank") != old_ranks.get(id)) or (from_lane == "now" and lane != "now")
            writes = [(item.get("sourceId"), _build_patch_props(source_cfg, item, from_lane=from_lane if from_lane != lane else None,
                                                                title_changed=rank_moved))]
            writes += _shifted_now_writes(source_cfg, data, prod, old_ranks, (id,))
            _execute_notion_writes(writes, secrets.get_secret("notion-token"))
        item["updatedAt"] = now_iso()
        _save(data)
        return item


def swap(id_in, id_out, before=None):
    with _writing():
        data = load()
        item_in, item_out = _find(data, id_in), _find(data, id_out)
        if item_in.get("product") != item_out.get("product"):
            raise RoadmapError("cannot swap items from different products")
        if item_out.get("lane") != "now":
            raise RoadmapError(f"item {id_out!r} is not in Now")
        if item_in.get("lane") == "now":
            raise RoadmapError(f"item {id_in!r} is already in Now")
        prod = item_in.get("product")
        _require_writable(prod, data)
        from_lane = item_in.get("lane")
        source_cfg = get_notion_config(prod)
        if source_cfg and item_out.get("source") == "notion":
            _require_mapped_lane(source_cfg, "next")
        if source_cfg and item_in.get("source") == "notion":
            _require_mapped_lane(source_cfg, "now")
        old_ranks = _now_ranks(data, prod)
        _apply_swap(data, id_in, id_out, before=before)
        if source_cfg:
            writes = [(it.get("sourceId"), _build_patch_props(source_cfg, it, from_lane=fl, title_changed=True))
                      for it, fl in ((item_out, "now"), (item_in, from_lane)) if it.get("source") == "notion"]
            writes += _shifted_now_writes(source_cfg, data, prod, old_ranks, (id_in, id_out))
            _execute_notion_writes(writes, secrets.get_secret("notion-token"))
        item_in["updatedAt"] = item_out["updatedAt"] = now_iso()
        _save(data)
        return {"in": item_in, "out": item_out}


def add(product, lane, title):
    """Add an idea on top of a lane. Returns the item; with a Notion source whose page was
    created but whose other Now titles could not be renumbered, the result carries "warning"."""
    if product not in store.products():
        raise FactoryError(f"unknown product {product!r}")
    if lane not in LANES:
        raise RoadmapError(f"unknown lane {lane!r}")
    title = _check_title(title)

    with _writing():
        data = load()
        _require_writable(product, data)
        if lane == "now":
            _require_now_room(data, product)
        source_cfg = get_notion_config(product)
        new_item = {"id": None, "product": product, "title": title, "lane": lane, "rank": 1, "source": "factory"}

        if source_cfg:
            _require_mapped_lane(source_cfg, lane)
            token = secrets.get_secret("notion-token")
            done_statuses = source_cfg.get("done_status", DEFAULT_DONE_STATUS)
            status = done_statuses[0] if lane == "done" else source_cfg.get("new_status", "Backlog")
            rank_pfx = source_cfg.get("rank_prefix", True)
            props = {"Project name": {"title": [{"text": {"content": f"P1 — {title}" if (rank_pfx and lane == "now") else title}}]},
                     "Status": {"status": {"name": status}}}
            prio = _priority_for_lane(source_cfg.get("lanes", {}), lane)
            if prio and lane != "done":
                props["Priority"] = {"select": {"name": prio}}
            res = _notion_http("POST", "https://api.notion.com/v1/pages", token,
                               {"parent": {"database_id": source_cfg["notion_database"]}, "properties": props})
            uid = (res.get("properties", {}).get("ID") or {}).get("unique_id")
            if isinstance(uid, dict) and uid.get("number") is not None:
                prefix = uid.get("prefix") or source_cfg.get("id_prefix", "FEAT-").rstrip("-")
                new_item["id"] = f"{prefix}-{uid['number']}"
            else:
                new_item["id"] = res.get("id")
            new_item.update(source="notion", sourceId=res.get("id"), sourceStatus=status)
            if res.get("url"):
                new_item["url"] = res["url"]
        else:
            existing = {i.get("id") for i in data["items"]}
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:16] or "idea"
            new_item["id"], n = f"{product}-{slug}", 2
            while new_item["id"] in existing:
                new_item["id"], n = f"{product}-{slug}-{n}", n + 1

        old_ranks = _now_ranks(data, product)
        for s in data["items"]:
            if s.get("product") == product and s.get("lane") == lane:
                s["rank"] = s.get("rank", 1) + 1
        if lane == "done":
            new_item["done"] = _today()
        new_item["updatedAt"] = now_iso()
        link_project(new_item)
        data["items"].append(new_item)
        _save(data)  # the Notion page exists now: keep it even if the renumbering below fails

        if source_cfg and lane == "now":
            try:
                _execute_notion_writes(_shifted_now_writes(source_cfg, data, product, old_ranks, (new_item["id"],)), token)
            except RoadmapError as e:
                return dict(new_item, warning=f"added, but other Now titles were not renumbered: {e}")
        return new_item


def _check_title(t):
    """A title is one line: agents read the roadmap as text, line by line."""
    t = (t or "").strip()
    if not t:
        raise RoadmapError("title is required")
    if len(t.splitlines()) > 1 or any(ord(c) < 32 for c in t):
        raise RoadmapError("title must be one line")
    return t


def _apply_update(item, title, due, note, project):
    """Validate and apply the given fields to item. Returns (title_changed, due_changed)."""
    title_changed = due_changed = False
    if title is not None:
        t = _check_title(title)
        title_changed = t != item.get("title")
        item["title"] = t
    if due is not None:
        d = due.strip()
        if d in ("none", ""):
            due_changed = item.pop("due", None) is not None
        else:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                    raise ValueError
                date.fromisoformat(d)
            except ValueError:
                raise RoadmapError(f"due date must be YYYY-MM-DD or none, got {d!r}")
            due_changed = item.get("due") != d
            item["due"] = d
    if note is not None:
        n = note.strip()
        if n:
            item["note"] = n
        else:
            item.pop("note", None)
    if project is not None:
        p = project.strip()
        if p in ("none", ""):
            item.pop("project", None)
        else:
            item["project"] = p
    return title_changed, due_changed


def update(id, title=None, due=None, note=None, project=None):
    if project is not None:
        p = project.strip()
        if p and p != "none":
            store.check_id(p, "project")
    with _writing():
        data = load()
        item = _find(data, id)
        prod = item.get("product")
        _require_writable(prod, data)
        title_changed, due_changed = _apply_update(item, title, due, note, project)
        source_cfg = get_notion_config(prod) if item.get("source") == "notion" else None
        if source_cfg and (title_changed or due_changed):
            _execute_notion_writes([(item.get("sourceId"), _build_patch_props(source_cfg, item, title_changed=title_changed,
                                                                               due_changed=due_changed))],
                                   secrets.get_secret("notion-token"))
        item["updatedAt"] = now_iso()
        local_rec = data.setdefault("local", {}).setdefault(id, {})
        for k in ("note", "project"):
            if k in item:
                local_rec[k] = item[k]
            else:
                local_rec.pop(k, None)
        _save(data)
        return item


def flags(items_list, projects=None, agents=None):
    """Return items that violate roadmap rules:
    - built, not in Now: project has an active agent, but item is not in Now
    - finished, not marked done: linked project is build, all phase units merged, but not in Done
    """
    if projects is None:
        try:
            projects = store.projects()
        except Exception:
            projects = []
    if agents is None:
        try:
            from . import t3
            agents = t3.agents(store.products(), projects)[0]
        except Exception:
            agents = []

    proj_map = {p.get("id"): p for p in projects}
    active_projects = {a.get("project") for a in agents if a.get("project") and a.get("state") in ACTIVE}

    for it in items_list:
        pid = it.get("project")
        lane = it.get("lane")
        flag = None
        flag_txt = None

        if pid:
            # Check built, not in Now
            if lane not in ("now", "done") and pid in active_projects:
                flag = "building"
                flag_txt = "Agents are building it, but it is not in Now"
            # Check finished, not marked done
            elif lane != "done":
                p_obj = proj_map.get(pid)
                units = _units(p_obj)
                if units and units[1] > 0 and units[0] >= units[1]:
                    flag = "finished"
                    flag_txt = f"Plan {units[0]}/{units[1]}, not marked done"

        if flag:
            it["flag"] = flag
            it["flagTxt"] = flag_txt
        else:
            it.pop("flag", None)
            it.pop("flagTxt", None)

    return [i for i in items_list if i.get("flag")]


ACTIVE = ("running", "waiting", "stalled")


def _units(project):
    """[merged, total] over a build project'''s phases, else None."""
    if not project or project.get("kind") != "build":
        return None
    merged = total = 0
    for ph in project.get("phases") or []:
        if isinstance(ph, dict):  # stored project (store.projects())
            merged += ph.get("merged", 0)
            total += ph.get("total", 0)
        elif isinstance(ph, (list, tuple)) and len(ph) >= 3:  # page row from build: [name, merged, total, ...]
            merged += ph[1]
            total += ph[2]
    return [merged, total]


def annotate(items_list, projects, agents, today):
    """Attach flags, agents (running count), prog ([merged, total] or None) and late to each item."""
    fl = flags(items_list, projects, agents)
    proj_map = {p.get("id"): p for p in projects}
    for it in items_list:
        pid = it.get("project")
        it["agents"] = sum(1 for a in agents if pid and a.get("project") == pid and a.get("state") in ACTIVE)
        it["prog"] = _units(proj_map.get(pid)) if pid else None
        it["late"] = bool(it.get("due") and it.get("lane") != "done" and it["due"] < today)
    return fl


def view(product=None):
    """Items of one product (or all) annotated with live agents and plan progress, for the CLI and the API."""
    from . import t3
    its = items(product)
    projects = store.projects()
    try:
        agents = t3.agents(store.products(), projects)[0]
    except Exception:
        agents = []
    fl = annotate(its, projects, agents, _today())
    data = load()
    return {"items": its, "flags": fl, "sources": data.get("sources", {}), "cap": cap()}


def sync(product=None):
    """Sync roadmap items from Notion databases for products with configured sources."""
    prods = [product] if product else list(store.products().keys())
    synced = []

    for p in prods:
        try:
            cfg = get_notion_config(p)
        except Exception as e:
            with _writing(), store.lock():
                cur_data = load()
                cur_data.setdefault("sources", {})[p] = {"error": str(e)}
                store.save_roadmap(cur_data)
            if product:
                raise RoadmapError(f"sync error for {p}: {e}", code="notion_error")
            continue

        if not cfg:
            with _writing(), store.lock():
                cur_data = load()
                if "sources" in cur_data and p in cur_data["sources"]:
                    del cur_data["sources"][p]
                    store.save_roadmap(cur_data)
            continue

        try:
            token = secrets.get_secret("notion-token")
            db_id = cfg.get("notion_database")
            if not db_id:
                raise FactoryError("missing notion_database in roadmap source config")

            done_statuses = set(cfg.get("done_status", DEFAULT_DONE_STATUS))
            skip_statuses = set(cfg.get("skip_status", ["Canceled"]))
            lanes_map = cfg.get("lanes", {})
            prio_to_lane = {}
            for lane_name, prios in lanes_map.items():
                if isinstance(prios, list):
                    for pr in prios:
                        prio_to_lane[pr] = lane_name
                else:
                    prio_to_lane[prios] = lane_name

            rank_prefix = cfg.get("rank_prefix", True)
            id_prefix = cfg.get("id_prefix", "FEAT-").rstrip("-")

            fetch_start = now_iso()

            pages = []
            cursor = None
            while True:
                body = {}
                if cursor:
                    body["start_cursor"] = cursor
                res = _notion_http("POST", f"https://api.notion.com/v1/databases/{db_id}/query", token, body)
                pages.extend(res.get("results", []))
                if not res.get("has_more"):
                    break
                cursor = res.get("next_cursor")
                if not cursor:
                    raise RoadmapError("Notion pagination error: has_more is true but next_cursor is empty", code="notion_error")

            notion_items = []
            for pg in pages:
                props = pg.get("properties", {})
                st_obj = props.get("Status", {}).get("status") if isinstance(props.get("Status"), dict) else None
                st_name = st_obj.get("name") if st_obj else None
                if st_name and st_name in skip_statuses:
                    continue

                if st_name and st_name in done_statuses:
                    lane = "done"
                else:
                    pr_obj = props.get("Priority", {}).get("select") if isinstance(props.get("Priority"), dict) else None
                    pr_name = pr_obj.get("name") if pr_obj else None
                    if not pr_name or pr_name not in prio_to_lane:
                        continue
                    lane = prio_to_lane[pr_name]

                uid = props.get("ID", {}).get("unique_id") if isinstance(props.get("ID"), dict) else None
                if isinstance(uid, dict) and uid.get("number") is not None:
                    pfx = uid.get("prefix") or id_prefix
                    item_id = f"{pfx}-{uid['number']}"
                else:
                    item_id = pg.get("id")

                title_parts = props.get("Project name", {}).get("title", [])
                raw_title = " ".join("".join(t.get("plain_text", "") for t in title_parts).split())  # one line, before the P<n> check
                initial_rank = None
                if rank_prefix and lane == "now":
                    m = re.match(r"^P(\d+)\s*[-—–]\s*(.*)$", raw_title)
                    if m:
                        initial_rank = int(m.group(1))
                        clean_title = m.group(2)
                    else:
                        clean_title = raw_title
                else:
                    clean_title = re.sub(r"^P\d+\s*[-—–]\s*", "", raw_title)

                due = props.get("Target Launch", {}).get("date") if isinstance(props.get("Target Launch"), dict) else None
                due_val = due.get("start") if due else None

                done_date = None
                if lane == "done":
                    let = pg.get("last_edited_time") or ""
                    done_date = let[:10] if let else _today()

                it = {
                    "id": item_id,
                    "product": p,
                    "title": clean_title,
                    "lane": lane,
                    "rank": initial_rank,
                    "source": "notion",
                    "sourceId": pg.get("id"),
                    "sourceStatus": st_name,
                    "url": pg.get("url"),
                }
                if due_val:
                    it["due"] = due_val
                if done_date:
                    it["done"] = done_date
                notion_items.append(it)

            with _writing(), store.lock():
                cur_data = load()
                existing_items = cur_data.get("items", [])
                existing_p_items = [i for i in existing_items if i.get("product") == p]
                existing_notion_items = [i for i in existing_p_items if i.get("source") == "notion"]

                if len(notion_items) == 0 and len(existing_notion_items) > 0:
                    # an emptied database is far likelier a lost share or a wrong config than a real wipe:
                    # keep the items, stay writable, and say so on the page
                    cur_src = cur_data.setdefault("sources", {}).setdefault(p, {})
                    cur_src["warning"] = "Notion returned no rows: showing the last copy"
                    store.save_roadmap(cur_data)
                    synced.append((p, 0))
                    continue

                old_map = {i["id"]: i for i in existing_p_items}
                local_map = cur_data.get("local", {})
                projects = store.projects()

                notion_ids = {it["id"] for it in notion_items}
                local_surviving_items = [i for i in existing_p_items if i.get("source") != "notion" and i.get("id") not in notion_ids]
                merged_p_items = []

                for it in notion_items:
                    iid = it["id"]
                    old = old_map.get(iid)
                    if old and old.get("updatedAt") and old["updatedAt"] >= fetch_start:
                        merged_p_items.append(old)
                        continue

                    stored_local = local_map.get(iid, {})
                    note = (old.get("note") if old else None) or stored_local.get("note")
                    proj_id = (old.get("project") if old else None) or stored_local.get("project")
                    if note:
                        it["note"] = note
                    if proj_id:
                        it["project"] = proj_id
                    if it["lane"] == "done" and old and old.get("done"):
                        it["done"] = old["done"]

                    if rank_prefix and it["lane"] == "now":
                        if it["rank"] is None:
                            it["rank"] = (old.get("rank") if old and old.get("lane") == "now" else 999)
                    else:
                        if old and old.get("lane") == it["lane"] and old.get("rank") is not None:
                            it["rank"] = old["rank"]
                        else:
                            it["rank"] = 999

                    link_project(it, projects)
                    if it.get("note") or it.get("project"):
                        rec = cur_data.setdefault("local", {}).setdefault(iid, {})
                        if it.get("note"):
                            rec["note"] = it["note"]
                        if it.get("project"):
                            rec["project"] = it["project"]

                    merged_p_items.append(it)

                all_p_items = local_surviving_items + merged_p_items

                for l in LANES:
                    if l != "done":
                        lane_items = [i for i in all_p_items if i["lane"] == l]
                        lane_items.sort(key=lambda x: x.get("rank", 999))
                        for k, item in enumerate(lane_items):
                            item["rank"] = k + 1

                cur_data["items"] = [i for i in existing_items if i.get("product") != p] + all_p_items
                cur_data.setdefault("sources", {})[p] = {"syncedAt": now_iso(), "error": None}
                store.save_roadmap(cur_data)
                synced.append((p, len(all_p_items)))

        except Exception as e:
            with _writing(), store.lock():
                cur_data = load()
                cur_src = cur_data.setdefault("sources", {}).setdefault(p, {})
                cur_src["error"] = str(e)
                store.save_roadmap(cur_data)
            if product:
                raise RoadmapError(f"sync error for {p}: {e}", code="notion_error")

    return synced
