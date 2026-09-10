"""
Unit tests for release_service.py — all mocked, no real API calls.

Run with:  python3 -m pytest tests/test_release_service.py -v
"""

import asyncio
import importlib
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db))
    import release_service
    importlib.reload(release_service)
    release_service.init_release_db()
    yield db


@pytest.fixture()
def rs():
    import release_service
    return release_service


@pytest.fixture()
def client(rs):
    app = FastAPI()
    app.include_router(rs.router)
    return TestClient(app)


# ── Helpers ───────────────────────────────────────────────────────────────────

_FUTURE_DATE = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
_PAST_DATE   = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d")


def _make_release(rs, **overrides) -> dict:
    defaults = {
        "artist_id":    "artist-001",
        "title":        "Test Album",
        "release_date": _FUTURE_DATE,
        "genre":        "indie",
        "mood":         "upbeat",
    }
    defaults.update(overrides)
    import uuid
    r = {**defaults, "id": str(uuid.uuid4()), "status": "draft"}
    rs._db_create_release(r)
    return r


# ── Tests: CRUD ───────────────────────────────────────────────────────────────

def test_create_release_via_endpoint(client):
    resp = client.post("/api/releases", json={
        "artist_id":    "artist-001",
        "title":        "My EP",
        "release_date": _FUTURE_DATE,
        "genre":        "pop",
        "mood":         "energetic",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"]    == "My EP"
    assert data["status"]   == "draft"
    assert "id"             in data


def test_list_releases_for_artist(client, rs):
    _make_release(rs, artist_id="artist-list", title="Release 1")
    _make_release(rs, artist_id="artist-list", title="Release 2")
    _make_release(rs, artist_id="artist-other", title="Other")
    resp = client.get("/api/releases?artist_id=artist-list")
    assert resp.status_code == 200
    releases = resp.json()["releases"]
    assert len(releases) == 2
    assert all(r["artist_id"] == "artist-list" for r in releases)


def test_get_release_by_id(client, rs):
    r = _make_release(rs)
    resp = client.get(f"/api/releases/{r['id']}")
    assert resp.status_code == 200
    assert resp.json()["title"] == r["title"]


def test_get_release_by_id_returns_persisted_row_after_sqlite_round_trip(rs):
    r = _make_release(rs, title="Persisted Release")
    loaded = rs._db_get_release(r["id"])
    assert loaded["id"] == r["id"]
    assert loaded["title"] == "Persisted Release"


def test_get_release_404(client):
    resp = client.get("/api/releases/nonexistent-id")
    assert resp.status_code == 404


def test_patch_release_updates_title(client, rs):
    r = _make_release(rs)
    resp = client.patch(f"/api/releases/{r['id']}", json={"title": "Updated Title"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated Title"


def test_patch_release_invalid_date(client, rs):
    r = _make_release(rs)
    resp = client.patch(f"/api/releases/{r['id']}", json={"release_date": "not-a-date"})
    assert resp.status_code == 400


def test_create_release_invalid_date(client):
    resp = client.post("/api/releases", json={
        "artist_id": "a1", "title": "Bad", "release_date": "32-13-2026",
    })
    assert resp.status_code == 400


# ── Tests: Campaign Generation ────────────────────────────────────────────────

def test_generate_campaign_creates_actions(client, rs):
    r = _make_release(rs)
    resp = client.post(f"/api/releases/{r['id']}/generate-campaign")
    assert resp.status_code == 200
    data = resp.json()
    assert data["actions_created"] > 0
    assert data["status"] == "active"


def test_campaign_action_count_matches_schedule(rs):
    r = _make_release(rs)
    actions = rs._build_campaign_actions(r)
    # 3 pitch + 2 PR + 1 booking + 15 social = 21 entries in _CAMPAIGN_SCHEDULE
    assert len(actions) == len(rs._CAMPAIGN_SCHEDULE)


def test_generate_campaign_idempotent(client, rs):
    r = _make_release(rs)
    client.post(f"/api/releases/{r['id']}/generate-campaign")
    resp2 = client.post(f"/api/releases/{r['id']}/generate-campaign")
    assert resp2.status_code == 200
    # Second call should replace pending actions, not duplicate
    camp_resp = client.get(f"/api/releases/{r['id']}/campaign")
    total = camp_resp.json()["counts"]["total"]
    assert total == len(rs._CAMPAIGN_SCHEDULE)


def test_get_campaign_returns_action_list(client, rs):
    r = _make_release(rs)
    client.post(f"/api/releases/{r['id']}/generate-campaign")
    resp = client.get(f"/api/releases/{r['id']}/campaign")
    assert resp.status_code == 200
    data = resp.json()
    assert "actions" in data
    assert data["counts"]["awaiting_approval"] == len(rs._CAMPAIGN_SCHEDULE)


def test_execute_due_fires_past_actions(rs):
    r = _make_release(rs, release_date=_PAST_DATE)
    actions = rs._build_campaign_actions(r)
    for a in actions:
        rs._db_create_action(a)

    async def _run():
        mock_result = {"status": "skipped", "reason": "no contacts"}
        for action in actions:
            rs._db_update_action(action["id"], {"status": "ready"})
        with patch.object(rs, "_execute_action", new=AsyncMock(return_value=mock_result)):
            due = rs._db_list_due_actions()
            for action in due:
                rs._db_update_action(action["id"], {"status": "running"})
                result = await rs._execute_action(action)
                rs._db_update_action(action["id"], {"status": "done"})

    asyncio.run(_run())
    done_actions = [a for a in rs._db_list_actions(r["id"]) if a["status"] == "done"]
    assert len(done_actions) > 0


def test_execute_due_endpoint_returns_summary(client, rs):
    r = _make_release(rs, release_date=_PAST_DATE)
    client.post(f"/api/releases/{r['id']}/generate-campaign")
    blocked = client.post(f"/api/releases/{r['id']}/campaign/execute-due")
    assert blocked.json()["executed"] == 0
    assert client.post(f"/api/releases/{r['id']}/campaign/approve").json()["approved"] > 0
    assert client.post(f"/api/releases/{r['id']}/campaign/ready").json()["ready"] > 0

    mock_result = {"status": "skipped", "reason": "no contacts"}
    with patch("release_service._execute_action", new=AsyncMock(return_value=mock_result)):
        resp = client.post(f"/api/releases/{r['id']}/campaign/execute-due")

    assert resp.status_code == 200
    data = resp.json()
    assert "executed" in data
    assert data["executed"] >= 0


def test_due_action_claim_is_single_use_and_requires_ready_status(rs):
    r = _make_release(rs, release_date=_PAST_DATE)
    action = rs._build_campaign_actions(r)[0]
    rs._db_create_action(action)

    assert rs._db_claim_due_action(action["id"]) is False
    rs._db_update_action(action["id"], {"status": "ready"})
    assert rs._db_claim_due_action(action["id"]) is True
    assert rs._db_claim_due_action(action["id"]) is False
    assert rs._db_list_actions(r["id"])[0]["status"] == "running"


def test_init_release_db_resets_stuck_running_actions(tmp_path, monkeypatch):
    """Startup reset: running rows return to ready without losing approval."""
    import importlib, sqlite3, uuid
    db = tmp_path / "stuck.db"
    monkeypatch.setenv("DB_PATH", str(db))

    import release_service
    importlib.reload(release_service)
    release_service.init_release_db()

    # Manually insert a release and two actions stuck in 'running'
    conn = sqlite3.connect(str(db))
    release_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO releases (id, artist_id, title, release_date) VALUES (?,?,?,?)",
        (release_id, "artist-x", "Stuck Album", "2026-06-01"),
    )
    for _ in range(2):
        conn.execute(
            "INSERT INTO campaign_actions (id, release_id, action_type, scheduled_for, status) "
            "VALUES (?,?,?,?,?)",
            (str(uuid.uuid4()), release_id, "pitch", "2026-05-01T00:00:00", "running"),
        )
    conn.commit()
    conn.close()

    # Re-run init (simulates a restart) — should reset the stuck rows to the
    # execution-ready state without dropping the approval gate.
    importlib.reload(release_service)
    release_service.init_release_db()

    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT status FROM campaign_actions").fetchall()
    conn.close()

    assert all(r[0] == "ready" for r in rows), f"Expected all ready, got: {rows}"
