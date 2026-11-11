import fcntl
import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from factory import config, roadmap, secrets, serve, store
from factory.store import FactoryError


@pytest.fixture(autouse=True)
def temp_factory_home():
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("FACTORY_HOME")
        os.environ["FACTORY_HOME"] = tmp
        store.write("products.json", {"acme": {"name": "Acme", "repos": ["o/r"]}})
        store.write("roadmap.json", {"items": [], "sources": {}})
        store.key()
        with patch("sys.stdin.read", return_value="fake-token"):
            secrets.set_secret("notion-token")
        try:
            yield tmp
        finally:
            if old:
                os.environ["FACTORY_HOME"] = old
            else:
                os.environ.pop("FACTORY_HOME", None)


def test_move_and_renumber():
    # Seed 3 items in Next and 2 in Now
    store.write("roadmap.json", {
        "items": [
            {"id": "n1", "product": "acme", "title": "Now 1", "lane": "now", "rank": 1, "source": "factory"},
            {"id": "n2", "product": "acme", "title": "Now 2", "lane": "now", "rank": 2, "source": "factory"},
            {"id": "x1", "product": "acme", "title": "Next 1", "lane": "next", "rank": 1, "source": "factory"},
            {"id": "x2", "product": "acme", "title": "Next 2", "lane": "next", "rank": 2, "source": "factory"},
            {"id": "x3", "product": "acme", "title": "Next 3", "lane": "next", "rank": 3, "source": "factory"},
        ],
        "sources": {}
    })

    # Move x2 into Now before n2
    it = roadmap.move("x2", "now", before="n2")
    assert it["lane"] == "now"
    assert it["rank"] == 2

    data = roadmap.load()
    now_items = [i for i in data["items"] if i["lane"] == "now"]
    now_items.sort(key=lambda x: x["rank"])
    assert [i["id"] for i in now_items] == ["n1", "x2", "n2"]
    assert [i["rank"] for i in now_items] == [1, 2, 3]

    # Check Next lane renumbered
    next_items = [i for i in data["items"] if i["lane"] == "next"]
    next_items.sort(key=lambda x: x["rank"])
    assert [i["id"] for i in next_items] == ["x1", "x3"]
    assert [i["rank"] for i in next_items] == [1, 2]

    # Reorder within Now: move n2 to top (before n1)
    roadmap.move("n2", "now", before="n1")
    data = roadmap.load()
    now_items = [i for i in data["items"] if i["lane"] == "now"]
    now_items.sort(key=lambda x: x["rank"])
    assert [i["id"] for i in now_items] == ["n2", "n1", "x2"]
    assert [i["rank"] for i in now_items] == [1, 2, 3]


def test_cap_now_full():
    config.set_value("roadmap.cap", "3")
    store.write("roadmap.json", {
        "items": [
            {"id": "n1", "product": "acme", "title": "N1", "lane": "now", "rank": 1, "source": "factory"},
            {"id": "n2", "product": "acme", "title": "N2", "lane": "now", "rank": 2, "source": "factory"},
            {"id": "n3", "product": "acme", "title": "N3", "lane": "now", "rank": 3, "source": "factory"},
            {"id": "x1", "product": "acme", "title": "X1", "lane": "next", "rank": 1, "source": "factory"},
        ],
        "sources": {}
    })

    with pytest.raises(roadmap.RoadmapError) as excinfo:
        roadmap.move("x1", "now")
    assert excinfo.value.code == "now_full"
    assert len(excinfo.value.now) == 3

    with pytest.raises(roadmap.RoadmapError) as excinfo:
        roadmap.add("acme", "now", "N4")
    assert excinfo.value.code == "now_full"


def test_swap():
    config.set_value("roadmap.cap", "3")
    store.write("roadmap.json", {
        "items": [
            {"id": "n1", "product": "acme", "title": "N1", "lane": "now", "rank": 1, "source": "factory"},
            {"id": "n2", "product": "acme", "title": "N2", "lane": "now", "rank": 2, "source": "factory"},
            {"id": "n3", "product": "acme", "title": "N3", "lane": "now", "rank": 3, "source": "factory"},
            {"id": "x1", "product": "acme", "title": "X1", "lane": "next", "rank": 1, "source": "factory"},
            {"id": "x2", "product": "acme", "title": "X2", "lane": "next", "rank": 2, "source": "factory"},
        ],
        "sources": {}
    })

    res = roadmap.swap(id_in="x1", id_out="n2")
    assert res["in"]["id"] == "x1"
    assert res["out"]["id"] == "n2"

    data = roadmap.load()
    now_items = [i for i in data["items"] if i["lane"] == "now"]
    now_items.sort(key=lambda x: x["rank"])
    assert [i["id"] for i in now_items] == ["n1", "n3", "x1"]
    assert [i["rank"] for i in now_items] == [1, 2, 3]

    next_items = [i for i in data["items"] if i["lane"] == "next"]
    next_items.sort(key=lambda x: x["rank"])
    assert [i["id"] for i in next_items] == ["n2", "x2"]
    assert [i["rank"] for i in next_items] == [1, 2]


def test_done_date_set_and_cleared():
    store.write("roadmap.json", {
        "items": [
            {"id": "x1", "product": "acme", "title": "X1", "lane": "next", "rank": 1, "source": "factory"}
        ],
        "sources": {}
    })
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Move to done
    it = roadmap.move("x1", "done")
    assert it["lane"] == "done"
    assert it.get("done") == today

    # Move out of done
    it = roadmap.move("x1", "later")
    assert it["lane"] == "later"
    assert it.get("done") is None


def test_add():
    store.write("roadmap.json", {
        "items": [
            {"id": "x1", "product": "acme", "title": "Existing", "lane": "next", "rank": 1, "source": "factory"}
        ],
        "sources": {}
    })
    # Seed a project with feat
    store.save_project({
        "id": "proj-foo", "product": "acme", "kind": "build", "name": "Foo",
        "feat": "acme-new-idea", "prio": 1, "next": [], "prs": [], "createdAt": store.now_iso()
    })

    it = roadmap.add("acme", "next", "New Idea")

    assert it["lane"] == "next"
    assert it["rank"] == 1
    assert it["project"] == "proj-foo"  # Auto-linked!

    data = roadmap.load()
    next_items = [i for i in data["items"] if i["lane"] == "next"]
    next_items.sort(key=lambda x: x["rank"])
    assert next_items[0]["id"] == it["id"]
    assert next_items[1]["id"] == "x1"
    assert next_items[1]["rank"] == 2


def test_flags():
    projects = [
        {"id": "p-build", "kind": "build", "phases": [{"name": "Ph1", "merged": 5, "total": 5}]},
        {"id": "p-active", "kind": "build", "phases": [{"name": "Ph1", "merged": 1, "total": 5}]},
    ]
    agents = [
        {"project": "p-active", "state": "running"},
        {"project": "p-build", "state": "done"},
    ]

    items = [
        # building: lane not in now/done, project has active agent
        {"id": "i1", "lane": "later", "project": "p-active"},
        # lane is now -> building flag NOT set
        {"id": "i2", "lane": "now", "project": "p-active"},
        # finished: lane not in done, all units merged (total > 0)
        {"id": "i3", "lane": "now", "project": "p-build"},
        # lane is done -> finished flag NOT set
        {"id": "i4", "lane": "done", "project": "p-build"},
    ]

    fl = roadmap.flags(items, projects, agents)
    assert items[0].get("flag") == "building"
    assert "Agents are building it" in items[0]["flagTxt"]

    assert items[1].get("flag") is None

    assert items[2].get("flag") == "finished"
    assert "Plan 5/5, not marked done" in items[2]["flagTxt"]

    assert items[3].get("flag") is None


def test_notion_read_mapping():
    # Setup Notion config for acme
    config.set_value("roadmap.sources.acme", json.dumps({
        "notion_database": "test-db-123",
        "lanes": {"now": "P0", "next": "P1", "later": ["P2", "P3"], "icebox": "Backlog"},
        "done_status": ["Released", "Done"],
        "skip_status": ["Canceled"],
        "new_status": "Backlog",
        "rank_prefix": True,
        "id_prefix": "FEAT-"
    }), as_json=True)

    with patch("sys.stdin.read", return_value="secret-token-xyz"):
        secrets.set_secret("notion-token")

    # Seed an existing note and project in roadmap.json
    store.write("roadmap.json", {
        "items": [
            {"id": "FEAT-10", "product": "acme", "title": "Old Title", "lane": "next", "rank": 1, "note": "Local note preserved", "project": "local-proj"}
        ],
        "sources": {}
    })

    fake_notion_response = {
        "results": [
            {
                "id": "page-1",
                "properties": {
                    "Project name": {"title": [{"plain_text": "P1 — High Priority Feat"}]},
                    "Priority": {"select": {"name": "P0"}},
                    "Status": {"status": {"name": "In dev"}},
                    "ID": {"unique_id": {"prefix": "FEAT", "number": 1}},
                    "Target Launch": {"date": {"start": "2026-12-01"}},
                },
                "last_edited_time": "2026-11-01T10:00:00.000Z",
                "url": "https://notion.so/page-1"
            },
            {
                "id": "page-2",
                "properties": {
                    "Project name": {"title": [{"plain_text": "Released Feature"}]},
                    "Priority": {"select": {"name": "P1"}},
                    "Status": {"status": {"name": "Released"}},
                    "ID": {"unique_id": {"prefix": "FEAT", "number": 2}},
                },
                "last_edited_time": "2026-10-15T10:00:00.000Z",
                "url": "https://notion.so/page-2"
            },
            {
                "id": "page-3",
                "properties": {
                    "Project name": {"title": [{"plain_text": "Canceled Feature"}]},
                    "Priority": {"select": {"name": "P2"}},
                    "Status": {"status": {"name": "Canceled"}},
                    "ID": {"unique_id": {"prefix": "FEAT", "number": 3}},
                }
            },
            {
                "id": "page-10",
                "properties": {
                    "Project name": {"title": [{"plain_text": "Next Feature 10"}]},
                    "Priority": {"select": {"name": "P1"}},
                    "Status": {"status": {"name": "Backlog"}},
                    "ID": {"unique_id": {"prefix": "FEAT", "number": 10}},
                }
            }
        ],
        "has_more": False
    }

    with patch.object(roadmap, "_notion_http", return_value=fake_notion_response) as mock_http:
        synced = roadmap.sync("acme")
        assert synced == [("acme", 3)]  # page-3 was canceled so skipped

    data = roadmap.load()
    items_by_id = {i["id"]: i for i in data["items"]}

    # Check FEAT-1 in Now: prefix stripped, rank 1
    f1 = items_by_id["FEAT-1"]
    assert f1["lane"] == "now"
    assert f1["title"] == "High Priority Feat"
    assert f1["rank"] == 1
    assert f1["due"] == "2026-12-01"
    assert f1["source"] == "notion"

    # Check FEAT-2 in Done: lane done, done date from last_edited_time
    f2 = items_by_id["FEAT-2"]
    assert f2["lane"] == "done"
    assert f2["done"] == "2026-10-15"

    # Check FEAT-10: preserved local note and project
    f10 = items_by_id["FEAT-10"]
    assert f10["lane"] == "next"
    assert f10["note"] == "Local note preserved"
    assert f10["project"] == "local-proj"

    # Check source syncedAt
    assert data["sources"]["acme"]["syncedAt"] is not None
    assert data["sources"]["acme"]["error"] is None


def test_rank_prefix_strip_and_rewrite_on_reorder():
    config.set_value("roadmap.sources.acme", json.dumps({
        "notion_database": "test-db-123",
        "lanes": {"now": "P0", "next": "P1"},
        "rank_prefix": True
    }), as_json=True)

    with patch("sys.stdin.read", return_value="secret-token-xyz"):
        secrets.set_secret("notion-token")

    store.write("roadmap.json", {
        "items": [
            {"id": "FEAT-1", "product": "acme", "title": "Feat 1", "lane": "now", "rank": 1, "source": "notion", "sourceId": "p1"},
            {"id": "FEAT-2", "product": "acme", "title": "Feat 2", "lane": "now", "rank": 2, "source": "notion", "sourceId": "p2"},
        ],
        "sources": {}
    })

    patches = []

    def fake_notion_http(method, url, token, body=None):
        if method == "PATCH":
            patches.append((url, body))
        return {}

    with patch.object(roadmap, "_notion_http", side_effect=fake_notion_http):
        # Move FEAT-2 before FEAT-1
        roadmap.move("FEAT-2", "now", before="FEAT-1")

    # Both items should be patched with their new P ranks:
    # FEAT-2 becomes P1, FEAT-1 becomes P2
    titles_patched = {}
    for url, body in patches:
        pid = url.split("/")[-1]
        t = body["properties"].get("Project name", {}).get("title", [{}])[0].get("text", {}).get("content")
        if t:
            titles_patched[pid] = t

    assert titles_patched["p2"] == "P1 — Feat 2"
    assert titles_patched["p1"] == "P2 — Feat 1"

    # Now move FEAT-2 to Next: rank prefix must be stripped
    patches.clear()
    with patch.object(roadmap, "_notion_http", side_effect=fake_notion_http):
        roadmap.move("FEAT-2", "next")

    for url, body in patches:
        if url.endswith("/p2"):
            t = body["properties"].get("Project name", {}).get("title", [{}])[0].get("text", {}).get("content")
            assert t == "Feat 2"  # prefix stripped!


def test_notion_write_error_leaves_store_unchanged():
    config.set_value("roadmap.sources.acme", json.dumps({
        "notion_database": "test-db-123",
        "lanes": {"now": "P0", "next": "P1"},
    }), as_json=True)

    with patch("sys.stdin.read", return_value="secret-token-xyz"):
        secrets.set_secret("notion-token")

    initial_items = [
        {"id": "FEAT-1", "product": "acme", "title": "Feat 1", "lane": "next", "rank": 1, "source": "notion", "sourceId": "p1"}
    ]
    store.write("roadmap.json", {"items": list(initial_items), "sources": {}})

    def fail_patch(*args, **kwargs):
        raise roadmap.RoadmapError("Notion 502 gateway error", code="notion_error", status=502)

    with patch.object(roadmap, "_notion_http", side_effect=fail_patch):
        with pytest.raises(roadmap.RoadmapError) as excinfo:
            roadmap.move("FEAT-1", "now")
        assert "502" in str(excinfo.value)

    # Store must remain unchanged
    data = roadmap.load()
    assert data["items"][0]["lane"] == "next"


def test_sync_error_keeps_items_and_marks_source():
    config.set_value("roadmap.sources.acme", json.dumps({
        "notion_database": "test-db-123",
        "lanes": {"now": "P0"},
    }), as_json=True)

    with patch("sys.stdin.read", return_value="secret-token-xyz"):
        secrets.set_secret("notion-token")

    store.write("roadmap.json", {
        "items": [
            {"id": "FEAT-1", "product": "acme", "title": "Feat 1", "lane": "now", "rank": 1, "source": "notion"}
        ],
        "sources": {"acme": {"syncedAt": "2026-11-08T12:00:00Z", "error": None}}
    })

    def fail_query(*args, **kwargs):
        raise urllib.error.URLError("Connection refused")

    with patch("urllib.request.urlopen", side_effect=fail_query):
        # sync for all products (does not raise for background sync)
        roadmap.sync()

    data = roadmap.load()
    assert len(data["items"]) == 1
    assert data["sources"]["acme"]["error"] is not None
    assert "Connection refused" in data["sources"]["acme"]["error"]
    assert data["sources"]["acme"]["syncedAt"] == "2026-11-08T12:00:00Z"


def test_api_status_codes():
    config.set_value("roadmap.cap", "2")
    store.write("roadmap.json", {
        "items": [
            {"id": "n1", "product": "acme", "title": "N1", "lane": "now", "rank": 1, "source": "factory"},
            {"id": "n2", "product": "acme", "title": "N2", "lane": "now", "rank": 2, "source": "factory"},
            {"id": "x1", "product": "acme", "title": "X1", "lane": "next", "rank": 1, "source": "factory"},
        ],
        "sources": {}
    })

    # 409 when now is full
    status, body = serve.save_roadmap({"action": "move", "id": "x1", "lane": "now"})
    assert status == 409
    assert body["error"] == "now_full"
    assert len(body["now"]) == 2

    # 502 when Notion write fails
    config.set_value("roadmap.sources.acme", json.dumps({
        "notion_database": "test-db",
        "lanes": {"now": "P0", "next": "P1"}
    }), as_json=True)
    with patch("sys.stdin.read", return_value="token"):
        secrets.set_secret("notion-token")
    store.write("roadmap.json", {
        "items": [
            {"id": "FEAT-1", "product": "acme", "title": "F1", "lane": "next", "rank": 1, "source": "notion", "sourceId": "p1"}
        ],
        "sources": {}
    })
    with patch.object(roadmap, "_notion_http", side_effect=roadmap.RoadmapError("Notion timeout", code="notion_error")):
        status, body = serve.save_roadmap({"action": "move", "id": "FEAT-1", "lane": "now"})
        assert status == 502
        assert "Notion timeout" in body["error"]

    # 423 when product source has error (read-only)
    store.write("roadmap.json", {
        "items": [
            {"id": "FEAT-1", "product": "acme", "title": "F1", "lane": "next", "rank": 1, "source": "notion"}
        ],
        "sources": {"acme": {"error": "Notion unreachable"}}
    })
    status, body = serve.save_roadmap({"action": "move", "id": "FEAT-1", "lane": "now"})
    assert status == 423
    assert "read-only" in body["error"]


def test_cli_commands():
    from factory import cli

    # 1. Add
    ret = cli.main(["roadmap", "add", "acme", "now", "CLI Item 1"])
    assert ret == 0

    ret = cli.main(["roadmap", "add", "acme", "now", "CLI Item 2"])
    assert ret == 0

    ret = cli.main(["roadmap", "add", "acme", "next", "CLI Item 3"])
    assert ret == 0

    # 2. List
    ret = cli.main(["roadmap", "list", "acme"])
    assert ret == 0

    ret = cli.main(["roadmap", "list", "--json"])
    assert ret == 0

    # 3. Move
    data = roadmap.load()
    i3 = next(i for i in data["items"] if i["title"] == "CLI Item 3")
    ret = cli.main(["roadmap", "move", i3["id"], "later"])
    assert ret == 0

    # 4. Set
    ret = cli.main(["roadmap", "set", i3["id"], "--due", "2026-12-25", "--note", "Christmas release"])
    assert ret == 0
    data = roadmap.load()
    it = next(i for i in data["items"] if i["id"] == i3["id"])
    assert it["due"] == "2026-12-25"
    assert it["note"] == "Christmas release"

    # 5. Swap
    i1 = next(i for i in data["items"] if i["title"] == "CLI Item 1")
    ret = cli.main(["roadmap", "swap", i3["id"], i1["id"]])
    assert ret == 0
    data = roadmap.load()
    it_in = next(i for i in data["items"] if i["id"] == i3["id"])
    it_out = next(i for i in data["items"] if i["id"] == i1["id"])
    assert it_in["lane"] == "now"
    assert it_out["lane"] == "next"

    # 6. Exit non-zero on now_full or bad lane
    config.set_value("roadmap.cap", "2")
    # Now has 2 items: CLI Item 2 and CLI Item 3
    ret = cli.main(["roadmap", "move", it_out["id"], "now"])
    assert ret == 1

    ret = cli.main(["roadmap", "move", it_out["id"], "invalid_lane"])
    assert ret == 1


def test_add_same_title_twice_gets_a_new_id():
    a = roadmap.add("acme", "next", "Gift cards")
    b = roadmap.add("acme", "next", "Gift cards")
    assert a["id"] == "acme-gift-cards" and b["id"] == "acme-gift-cards-2"


def test_view_uses_live_agents_for_flags_and_counts():
    store.write("roadmap.json", {"items": [
        {"id": "l1", "product": "acme", "title": "Later 1", "lane": "later", "rank": 1, "source": "factory", "project": "p1"},
    ], "sources": {}})
    rows = [{"project": "p1", "state": "running"}, {"project": "p1", "state": "done"}]
    with patch("factory.t3.agents", return_value=(rows, [])), patch("factory.store.projects", return_value=[{"id": "p1", "kind": "build", "phases": [{"merged": 1, "total": 3}]}]):
        v = roadmap.view("acme")
    it = v["items"][0]
    assert it["flag"] == "building" and it["agents"] == 1 and it["prog"] == [1, 3]


def test_api_rejects_non_string_fields():
    assert serve.save_roadmap({"action": "add", "product": "acme", "lane": "next", "title": 5})[0] == 400

def test_f5_move_before_self():
    store.write("roadmap.json", {"items": [
        {"id": "n1", "product": "acme", "title": "N1", "lane": "now", "rank": 1, "source": "factory"},
        {"id": "n2", "product": "acme", "title": "N2", "lane": "now", "rank": 2, "source": "factory"},
    ], "sources": {}})
    it = roadmap.move("n1", "now", before="n1")
    assert it["rank"] == 1
    items = roadmap.items("acme")
    assert [i["id"] for i in items] == ["n1", "n2"]
    assert [i["rank"] for i in items] == [1, 2]


def test_f5_swap_before_outgoing():
    store.write("roadmap.json", {"items": [
        {"id": "n1", "product": "acme", "title": "N1", "lane": "now", "rank": 1, "source": "factory"},
        {"id": "n2", "product": "acme", "title": "N2", "lane": "now", "rank": 2, "source": "factory"},
        {"id": "n3", "product": "acme", "title": "N3", "lane": "now", "rank": 3, "source": "factory"},
        {"id": "x1", "product": "acme", "title": "X1", "lane": "next", "rank": 1, "source": "factory"},
    ], "sources": {}})
    res = roadmap.swap(id_in="x1", id_out="n2", before="n2")
    now_items = [i for i in roadmap.load()["items"] if i["lane"] == "now"]
    now_items.sort(key=lambda x: x["rank"])
    assert [i["id"] for i in now_items] == ["n1", "x1", "n3"]
    assert [i["rank"] for i in now_items] == [1, 2, 3]


def test_f6_due_date_invalid():
    store.write("roadmap.json", {"items": [
        {"id": "n1", "product": "acme", "title": "N1", "lane": "now", "rank": 1, "source": "factory"},
    ], "sources": {}})
    with pytest.raises(roadmap.RoadmapError):
        roadmap.update("n1", due="2026-13-45")
    code, _ = serve.save_roadmap({"action": "set", "id": "n1", "due": "2026-13-45"})
    assert code == 400


def test_f7_cap_clamps_to_at_least_one():
    config.set_value("roadmap.cap", "0")
    assert roadmap.cap() == 1
    config.set_value("roadmap.cap", "-5")
    assert roadmap.cap() == 1


def test_a1_sync_merges_and_preserves_local_items_and_ranks():
    config.set_value("roadmap.sources.acme", {
        "notion_database": "db-123",
        "lanes": {"now": "Now", "next": ["Next", "Next 2"], "later": "Later"},
        "rank_prefix": True,
    })
    store.write("roadmap.json", {"items": [
        {"id": "acme-idea", "product": "acme", "title": "Local Idea", "lane": "next", "rank": 1, "source": "factory", "note": "Keep me"},
        {"id": "FEAT-2", "product": "acme", "title": "Feature 2", "lane": "next", "rank": 2, "source": "notion", "sourceId": "p2"},
        {"id": "FEAT-1", "product": "acme", "title": "Feature 1", "lane": "next", "rank": 3, "source": "notion", "sourceId": "p1"},
    ], "sources": {}})

    fake_pages = [
        {"id": "p1", "properties": {"Project name": {"title": [{"plain_text": "Feature 1"}]}, "Priority": {"select": {"name": "Next"}}, "ID": {"unique_id": {"prefix": "FEAT", "number": 1}}}},
        {"id": "p2", "properties": {"Project name": {"title": [{"plain_text": "Feature 2"}]}, "Priority": {"select": {"name": "Next"}}, "ID": {"unique_id": {"prefix": "FEAT", "number": 2}}}},
    ]
    with patch("factory.roadmap._notion_http", return_value={"results": fake_pages, "has_more": False}):
        roadmap.sync("acme")

    data = roadmap.load()
    items = {i["id"]: i for i in data["items"]}
    assert "acme-idea" in items
    assert items["acme-idea"]["note"] == "Keep me"
    assert items["FEAT-2"]["rank"] < items["FEAT-1"]["rank"]


def test_a2_empty_query_guard_and_local_note_restoration():
    config.set_value("roadmap.sources.acme", {
        "notion_database": "db-123",
        "lanes": {"now": "Now", "next": "Next"},
    })
    store.write("roadmap.json", {"items": [
        {"id": "FEAT-1", "product": "acme", "title": "F1", "lane": "next", "rank": 1, "source": "notion", "sourceId": "p1"},
    ], "sources": {}})
    roadmap.update("FEAT-1", note="Important note", project="proj-1")

    with patch("factory.roadmap._notion_http", return_value={"results": [], "has_more": False}):
        roadmap.sync()
    data = roadmap.load()
    assert len(data["items"]) == 1
    assert data["sources"]["acme"]["warning"].startswith("Notion returned no rows")
    assert not data["sources"]["acme"].get("error")  # a warning, not read-only
    with patch("factory.roadmap._notion_http", return_value={}) as http:
        roadmap.update("FEAT-1", title="Still writable")
    assert http.call_args[0][0] == "PATCH"

    p2 = {"id": "p2", "properties": {"Project name": {"title": [{"plain_text": "F2"}]}, "Priority": {"select": {"name": "Next"}}, "ID": {"unique_id": {"prefix": "FEAT", "number": 2}}}}
    with patch("factory.roadmap._notion_http", return_value={"results": [p2], "has_more": False}):
        roadmap.sync("acme")
    data = roadmap.load()
    assert [i["id"] for i in data["items"]] == ["FEAT-2"]

    p1 = {"id": "p1", "properties": {"Project name": {"title": [{"plain_text": "F1"}]}, "Priority": {"select": {"name": "Next"}}, "ID": {"unique_id": {"prefix": "FEAT", "number": 1}}}}
    with patch("factory.roadmap._notion_http", return_value={"results": [p1], "has_more": False}):
        roadmap.sync("acme")
    data = roadmap.load()
    it1 = next(i for i in data["items"] if i["id"] == "FEAT-1")
    assert it1["note"] == "Important note"
    assert it1["project"] == "proj-1"


def test_a3_concurrent_move_survives_sync():
    config.set_value("roadmap.sources.acme", {
        "notion_database": "db-123",
        "lanes": {"now": "Now", "next": "Next", "later": "Later"},
    })
    store.write("roadmap.json", {"items": [
        {"id": "FEAT-1", "product": "acme", "title": "F1", "lane": "next", "rank": 1, "source": "notion", "sourceId": "p1"},
    ], "sources": {}})

    fake_page = {"id": "p1", "properties": {"Project name": {"title": [{"plain_text": "F1"}]}, "Priority": {"select": {"name": "Next"}}, "ID": {"unique_id": {"prefix": "FEAT", "number": 1}}}}

    def fake_fetch(method, url, token, body=None):
        if "databases" in url and "query" in url:
            roadmap.move("FEAT-1", "later")
            return {"results": [fake_page], "has_more": False}
        return {}

    with patch("factory.roadmap._notion_http", side_effect=fake_fetch):
        roadmap.sync("acme")

    data = roadmap.load()
    it = next(i for i in data["items"] if i["id"] == "FEAT-1")
    assert it["lane"] == "later"


def test_a4_write_rules_patch_notion_item():
    config.set_value("roadmap.sources.acme", {
        "notion_database": "db-123",
        "lanes": {"now": "Now", "next": "Next", "later": "Later"},
        "done_status": ["Released", "Done"],
        "new_status": "Backlog",
    })
    store.write("roadmap.json", {"items": [
        {"id": "FEAT-1", "product": "acme", "title": "F1", "lane": "done", "rank": 1, "source": "notion", "sourceId": "p1", "done": "2026-11-01", "due": "2026-11-05"},
    ], "sources": {}})

    calls = []
    def fake_http(method, url, token, body=None):
        calls.append((method, url, body))
        return {}

    with patch("factory.roadmap._notion_http", side_effect=fake_http):
        roadmap.move("FEAT-1", "next")
        assert len(calls) == 1
        props = calls[-1][2]["properties"]
        assert props.get("Status") == {"status": {"name": "Backlog"}}
        assert "Target Launch" not in props

        calls.clear()
        roadmap.update("FEAT-1", due="none")
        assert len(calls) == 1
        assert calls[-1][2]["properties"].get("Target Launch") == {"date": None}

        calls.clear()
        config.set_value("roadmap.sources.acme", {
            "notion_database": "db-123",
            "lanes": {"now": "Now", "next": "Next"},
            "rank_prefix": False,
        })
        roadmap.update("FEAT-1", title="Renamed Feature")
        assert len(calls) == 1
        assert calls[-1][2]["properties"]["Project name"]["title"][0]["text"]["content"] == "Renamed Feature"


def test_a5_adds_that_stay_put():
    config.set_value("roadmap.sources.acme", {
        "notion_database": "db-123",
        "lanes": {"now": "Now", "next": "Next"},
        "done_status": ["Released", "Done"],
    })
    store.write("roadmap.json", {"items": [
        {"id": "FEAT-1", "product": "acme", "title": "Now 1", "lane": "now", "rank": 1, "source": "notion", "sourceId": "p1"},
    ], "sources": {}})

    with pytest.raises(roadmap.RoadmapError) as excinfo:
        roadmap.add("acme", "later", "Idea")
    assert excinfo.value.code == "bad_lane"

    calls = []
    def fake_http(method, url, token, body=None):
        calls.append((method, url, body))
        if method == "POST":
            return {"id": "page-new", "properties": {"ID": {"unique_id": {"prefix": "FEAT", "number": 99}}}, "url": "https://notion.so/p"}
        return {}

    with patch("factory.roadmap._notion_http", side_effect=fake_http):
        roadmap.add("acme", "done", "Shipped")
        assert calls[0][2]["properties"]["Status"] == {"status": {"name": "Released"}}

    calls.clear()
    with patch("factory.roadmap._notion_http", side_effect=fake_http):
        roadmap.add("acme", "now", "Top Idea")
        assert calls[0][2]["properties"]["Project name"]["title"][0]["text"]["content"] == "P1 — Top Idea"
        assert any(c[0] == "PATCH" and c[2].get("properties", {}).get("Project name", {}).get("title", [{}])[0].get("text", {}).get("content") == "P2 — Now 1" for c in calls)

    calls.clear()
    def fake_http_no_id(method, url, token, body=None):
        return {"id": "uuid-page-123", "properties": {"ID": {"unique_id": None}}, "url": "https://notion.so/uuid"}
    with patch("factory.roadmap._notion_http", side_effect=fake_http_no_id):
        new_it = roadmap.add("acme", "next", "Fallback ID")
        assert new_it["id"] == "uuid-page-123"

    config.set_value("roadmap.sources.acme", {"lanes": {"now": "Now"}})
    with pytest.raises(FactoryError) as excinfo:
        roadmap.add("acme", "now", "Boom")
    assert "notion_database" in str(excinfo.value)


def test_a6_config_removed_and_config_validation():
    config.set_value("roadmap.sources.acme", None)
    store.write("roadmap.json", {"items": [], "sources": {"acme": {"error": "Old error"}}})
    roadmap.sync()
    data = roadmap.load()
    assert "acme" not in data.get("sources", {})
    assert roadmap.source_error("acme") is None

    assert roadmap.get_notion_config("missing_prod") is None

    config.set_value("roadmap.sources.bad", "not-a-dict")
    store.write("products.json", {"bad": {"name": "Bad", "repos": []}, "good": {"name": "Good", "repos": []}})
    with pytest.raises(FactoryError):
        roadmap.get_notion_config("bad")

    config.set_value("roadmap.sources.good", None)
    roadmap.sync()
    data = roadmap.load()
    assert data["sources"]["bad"]["error"] == "roadmap.sources.bad must be an object with notion_database and lanes"


def test_a7_pagination_guard():
    config.set_value("roadmap.sources.acme", {
        "notion_database": "db-123",
        "lanes": {"now": "Now"},
    })
    store.write("roadmap.json", {"items": [], "sources": {}})
    with patch("factory.roadmap._notion_http", return_value={"results": [], "has_more": True, "next_cursor": None}):
        roadmap.sync()
    data = roadmap.load()
    assert "pagination error" in data["sources"]["acme"]["error"]


def test_a8_items_sort_malformed_done():
    store.write("roadmap.json", {"items": [
        {"id": "d1", "product": "acme", "title": "Done 1", "lane": "done", "done": "2026-10-15"},
        {"id": "d2", "product": "acme", "title": "Done 2", "lane": "done", "done": "not-an-iso-date"},
        {"id": "d3", "product": "acme", "title": "Done 3", "lane": "done", "done": "2026-11-09"},
    ], "sources": {}})
    res = roadmap.items("acme")
    idx3 = next(k for k, x in enumerate(res) if x["id"] == "d3")
    idx1 = next(k for k, x in enumerate(res) if x["id"] == "d1")
    assert idx3 < idx1


def test_b_lock_not_held_during_notion_calls():
    config.set_value("roadmap.sources.acme", {
        "notion_database": "db-123",
        "lanes": {"now": "Now", "next": "Next"},
    })
    store.write("roadmap.json", {"items": [
        {"id": "FEAT-1", "product": "acme", "title": "F1", "lane": "next", "rank": 1, "source": "notion", "sourceId": "p1"},
    ], "sources": {}})

    def fake_http(method, url, token, body=None):
        with open(store.path(".lock"), "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        return {}

    with patch("factory.roadmap._notion_http", side_effect=fake_http):
        res = roadmap.move("FEAT-1", "now")
        assert res["lane"] == "now"


def test_b_notion_partial_error():
    config.set_value("roadmap.sources.acme", {
        "notion_database": "db-123",
        "lanes": {"now": "Now", "next": "Next"},
        "rank_prefix": True,
    })
    store.write("roadmap.json", {"items": [
        {"id": "FEAT-1", "product": "acme", "title": "Now 1", "lane": "now", "rank": 1, "source": "notion", "sourceId": "p1"},
        {"id": "FEAT-2", "product": "acme", "title": "Next 2", "lane": "next", "rank": 1, "source": "notion", "sourceId": "p2"},
    ], "sources": {}})

    calls = 0
    def fake_http(method, url, token, body=None):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise urllib.error.URLError("connection dropped")
        return {}

    with patch("factory.roadmap._notion_http", side_effect=fake_http):
        with pytest.raises(roadmap.RoadmapError) as excinfo:
            roadmap.move("FEAT-2", "now", before="FEAT-1")
        assert excinfo.value.code == "notion_partial"

    with patch("factory.roadmap._notion_http", side_effect=fake_http):
        calls = 0
        code, body = serve.save_roadmap({"action": "move", "id": "FEAT-2", "lane": "now", "before": "FEAT-1"})
        assert code == 502
        assert body.get("partial") is True


def test_c3_add_unknown_product_and_update_invalid_project():
    with pytest.raises(FactoryError):
        roadmap.add("ghost", "next", "Ghost Idea")
    store.write("roadmap.json", {"items": [
        {"id": "n1", "product": "acme", "title": "N1", "lane": "now", "rank": 1, "source": "factory"},
    ], "sources": {}})
    with pytest.raises(FactoryError):
        roadmap.update("n1", project='" onmouseover=x')


def _notion_now_store(n_now, extra):
    config.set_value("roadmap.sources.acme", {"notion_database": "db", "lanes": {"now": "Now", "next": "Next", "later": "Later"}})
    items = [{"id": f"N{k}", "product": "acme", "title": f"N{k}", "lane": "now", "rank": k, "source": "notion", "sourceId": f"pn{k}"}
             for k in range(1, n_now + 1)]
    items += [{"id": x, "product": "acme", "title": x, "lane": "later", "rank": k + 1, "source": "notion", "sourceId": "p" + x}
              for k, x in enumerate(extra)]
    store.write("roadmap.json", {"items": items, "sources": {}})


def test_concurrent_moves_respect_the_cap_and_titles_stay_unique():
    import threading, time
    config.set_value("roadmap.cap", 2)
    _notion_now_store(1, ["X1", "X2"])
    titles = {}

    def slow_http(method, url, token, body=None):
        time.sleep(0.2)  # both moves are in flight at once
        t = body["properties"].get("Project name")
        if t:
            titles[url.rsplit("/", 1)[1]] = t["title"][0]["text"]["content"]
        return {}

    errors = []
    def run(x):
        try:
            roadmap.move(x, "now")
        except roadmap.RoadmapError as e:
            errors.append(e.code)
    with patch("factory.roadmap._notion_http", side_effect=slow_http):
        ts = [threading.Thread(target=run, args=(x,)) for x in ("X1", "X2")]
        [t.start() for t in ts]
        [t.join() for t in ts]
    now = [i for i in roadmap.load()["items"] if i["lane"] == "now"]
    assert len(now) == 2 and errors == ["now_full"]
    prefixes = [v.split(" — ")[0] for v in titles.values() if " — " in v]
    assert len(prefixes) == len(set(prefixes))


def test_add_to_now_keeps_the_item_when_renumbering_fails():
    _notion_now_store(1, [])
    posts = []

    def http(method, url, token, body=None):
        if method == "POST":
            posts.append(1)
            return {"id": "pnew", "properties": {"ID": {"unique_id": {"prefix": "FEAT", "number": 9}}}}
        raise roadmap.RoadmapError("Notion HTTP 502", code="notion_error")
    with patch("factory.roadmap._notion_http", side_effect=http):
        res = roadmap.add("acme", "now", "Hot")
    assert "warning" in res and len(posts) == 1
    assert [i["id"] for i in roadmap.items("acme") if i["lane"] == "now"] == ["FEAT-9", "N1"]


def test_sync_during_add_does_not_duplicate_the_item():
    import threading, time
    _notion_now_store(0, [])
    page = {"id": "pnew", "url": "u", "properties": {"Project name": {"title": [{"plain_text": "Idea"}]},
            "Priority": {"select": {"name": "Next"}}, "ID": {"unique_id": {"prefix": "FEAT", "number": 9}}}}
    created = threading.Event()

    def http(method, url, token, body=None):
        if url.endswith("/pages"):
            created.set()
            time.sleep(0.3)  # the sync below fetches while add still holds the roadmap
            return page
        return {"results": [page] if created.is_set() else [], "has_more": False}
    with patch("factory.roadmap._notion_http", side_effect=http):
        t = threading.Thread(target=roadmap.add, args=("acme", "next", "Idea"))
        t.start()
        created.wait(2)
        roadmap.sync("acme")
        t.join()
    ids = [i["id"] for i in roadmap.load()["items"]]
    assert ids.count("FEAT-9") == 1


def test_move_to_a_lane_without_notion_priority_is_refused():
    _notion_now_store(1, ["X1"])
    with patch("factory.roadmap._notion_http", return_value={}):
        with pytest.raises(roadmap.RoadmapError) as e:
            roadmap.move("X1", "icebox")
    assert e.value.code == "bad_lane"
    assert serve.save_roadmap({"action": "move", "id": "X1", "lane": "icebox"})[0] == 400


def test_invalid_local_map_is_refused():
    store.write("roadmap.json", {"items": [], "sources": {}, "local": {"x": "not a dict"}})
    with pytest.raises(FactoryError):
        roadmap.load()


def test_move_onto_itself_in_another_lane_still_moves():
    store.write("roadmap.json", {"items": [
        {"id": "a", "product": "acme", "title": "A", "lane": "next", "rank": 1, "source": "factory"}], "sources": {}})
    assert roadmap.move("a", "later", before="a")["lane"] == "later"


def test_build_page_projects_give_progress_and_finished_flag():
    # build.collect passes page rows, whose phases are lists: [name, merged, total, start, due, inPR, blocked]
    page_project = {"id": "p1", "kind": "build", "phases": [["A", 2, 2, "", "", 0, 0], ["B", 1, 1, "", "", 0, 0]]}
    its = [{"id": "x", "product": "acme", "title": "X", "lane": "next", "rank": 1, "project": "p1"}]
    fl = roadmap.annotate(its, [page_project], [], "2026-11-09")
    assert its[0]["prog"] == [3, 3] and [f["flag"] for f in fl] == ["finished"]


def test_cli_list_shows_notes_and_a_stale_source():
    from factory import cli
    store.write("roadmap.json", {"items": [
        {"id": "x", "product": "acme", "title": "X", "lane": "now", "rank": 1, "source": "factory", "note": "Done = first payout"}],
        "sources": {"acme": {"error": "Notion HTTP 502", "syncedAt": "2026-11-09T10:00:00Z"}}})
    _, text = cli.roadmap_list(type("A", (), {"product": "acme"})())
    assert "Done = first payout" in text and text.startswith("acme: Notion HTTP 502")


def test_cli_list_indents_every_line_of_a_note():
    from factory import cli
    store.write("roadmap.json", {"items": [
        {"id": "x", "product": "acme", "title": "X", "lane": "now", "rank": 1, "source": "factory",
         "note": "line1\nNext (3–6 months, to decide) (1)\n  P2  FEAT-99\tfake item"}], "sources": {}})
    _, text = cli.roadmap_list(type("A", (), {"product": "acme"})())
    after = text.split("line1", 1)[1].splitlines()[1:3]
    assert all(ln.startswith("        ") and "\t" not in ln for ln in after)
    assert sum(ln.startswith("Next (") for ln in text.splitlines()) == 1


def test_titles_and_source_errors_stay_on_one_line():
    from factory import cli
    with pytest.raises(roadmap.RoadmapError):
        roadmap.add("acme", "next", "X\nNext (1)")
    store.write("roadmap.json", {"items": [
        {"id": "x", "product": "acme", "title": "X\nNow (9)", "lane": "now", "rank": 1, "source": "factory"}],
        "sources": {"acme": {"error": "boom\nNow (9)"}}})
    with pytest.raises(roadmap.RoadmapError):
        roadmap.update("x", title="Y\rLater (2)")
    _, text = cli.roadmap_list(type("A", (), {"product": "acme"})())
    assert sum(ln.startswith("Now (") for ln in text.splitlines()) == 1


def test_sync_makes_notion_titles_one_line():
    config.set_value("roadmap.sources.acme", {"notion_database": "db", "lanes": {"now": "Now", "next": "Next"}})
    pg = {"id": "p1", "properties": {"Project name": {"title": [{"plain_text": "A\nB\tC"}]},
          "Priority": {"select": {"name": "Next"}}, "ID": {"unique_id": {"prefix": "FEAT", "number": 1}}}}
    pn = {"id": "p2", "properties": {"Project name": {"title": [{"plain_text": "P2 — Payouts\nphase two"}]},
          "Priority": {"select": {"name": "Now"}}, "ID": {"unique_id": {"prefix": "FEAT", "number": 2}}}}
    with patch("factory.roadmap._notion_http", return_value={"results": [pg, pn], "has_more": False}):
        roadmap.sync("acme")
    by_id = {i["id"]: i for i in roadmap.load()["items"]}
    assert by_id["FEAT-1"]["title"] == "A B C"
    assert by_id["FEAT-2"]["title"] == "Payouts phase two"  # prefix read and stripped
    with patch("factory.roadmap._notion_http", return_value={}):
        roadmap.update("FEAT-1", title="A B C", note="n")  # the drawer sends the title back
