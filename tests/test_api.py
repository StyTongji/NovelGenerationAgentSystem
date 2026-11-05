"""API surface tests for Phase 1 (novels, bootstrap, state, characters)."""

from __future__ import annotations

from tests.helpers import create_novel

BOOTSTRAP_PAYLOAD = {
    "power_system": {
        "name": "青玄修炼体系",
        "rules": ["常态下跨两个大境界正面击杀几乎不可能"],
        "realms": [
            {"name": "炼体", "rank": 1},
            {"name": "练气", "rank": 2},
            {"name": "筑基", "rank": 3},
            {"name": "金丹", "rank": 4},
            {"name": "元婴", "rank": 5},
        ],
    },
    "characters": [
        {"name": "林凡", "is_protagonist": True, "realm_name": "筑基", "subrealm": "后期"},
        {"name": "苏青", "realm_name": "筑基", "subrealm": "中期"},
        {"name": "玄清真人", "realm_name": "金丹", "subrealm": "后期"},
    ],
    "locations": [{"name": "青玄大陆"}, {"name": "天剑城", "parent_name": "青玄大陆"}],
    "initial_facts": [
        {
            "statement": "玄清真人是魔教卧底",
            "secrecy": "top_secret",
            "known_by": ["玄清真人"],
        }
    ],
    "plot_threads": [
        {"title": "父亲失踪之谜", "status": "active", "priority": 0.9}
    ],
    "constraints": [{"type": "hard", "statement": "苏青不能死亡"}],
}


async def test_create_and_get_novel(client):
    response = await client.post(
        "/api/v1/novels",
        json={"title": "九霄问道", "genre": "玄幻", "premise": "少年追查父亲失踪之谜"},
    )
    assert response.status_code == 201
    novel = response.json()
    assert novel["title"] == "九霄问道"
    assert novel["current_chapter_number"] == 0

    got = await client.get(f"/api/v1/novels/{novel['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == novel["id"]


async def test_bootstrap_and_state_view(client):
    created = await client.post("/api/v1/novels", json={"title": "九霄问道"})
    novel_id = created.json()["id"]

    boot = await client.post(f"/api/v1/novels/{novel_id}/bootstrap", json=BOOTSTRAP_PAYLOAD)
    assert boot.status_code == 201

    # author view shows hidden facts
    state = await client.get(f"/api/v1/novels/{novel_id}/state", params={"view": "author"})
    assert state.status_code == 200
    body = state.json()
    assert "玄清真人是魔教卧底" in body["hidden_facts"]
    assert {c["name"] for c in body["characters"]} == {"林凡", "苏青", "玄清真人"}
    lin = next(c for c in body["characters"] if c["name"] == "林凡")
    assert lin["current_state"]["realm_name"] == "筑基"
    assert any(t["title"] == "父亲失踪之谜" for t in body["plot_threads"])

    # reader view hides hidden facts
    reader = (await client.get(f"/api/v1/novels/{novel_id}/state", params={"view": "reader"})).json()
    assert reader["hidden_facts"] == []


async def test_bootstrap_duplicate_character_rejected(client):
    created = await client.post("/api/v1/novels", json={"title": "X"})
    novel_id = created.json()["id"]
    first = await client.post(f"/api/v1/novels/{novel_id}/bootstrap", json=BOOTSTRAP_PAYLOAD)
    assert first.status_code == 201

    # bootstrapping again with same power system name must 422, not corrupt state
    second = await client.post(f"/api/v1/novels/{novel_id}/bootstrap", json=BOOTSTRAP_PAYLOAD)
    assert second.status_code == 422


async def test_add_constraint_and_fact(client):
    created = await client.post("/api/v1/novels", json={"title": "九霄问道"})
    novel_id = created.json()["id"]
    await client.post(f"/api/v1/novels/{novel_id}/bootstrap", json=BOOTSTRAP_PAYLOAD)

    constraint = await client.post(
        f"/api/v1/novels/{novel_id}/constraints",
        json={"type": "hard", "statement": "第150章以前不能揭示轮回古镜真相"},
    )
    assert constraint.status_code == 201
    assert constraint.json()["active"] is True

    fact = await client.post(
        f"/api/v1/novels/{novel_id}/facts",
        json={"statement": "林凡持有轮回古镜", "secrecy": "secret", "known_by": ["林凡"]},
    )
    assert fact.status_code == 201

    plot_threads = (await client.get(f"/api/v1/novels/{novel_id}/plot-threads")).json()
    assert len(plot_threads) == 1
