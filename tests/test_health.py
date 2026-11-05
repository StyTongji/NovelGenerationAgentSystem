"""Phase 0 acceptance: app boots and /health returns ok."""

from __future__ import annotations


async def test_health_ok(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_app_creation():
    from app.main import create_app

    app = create_app()
    assert app.title == "Novel Narrative Engine"
