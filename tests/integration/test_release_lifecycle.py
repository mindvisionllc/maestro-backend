"""
IT.6 — Full release campaign lifecycle integration test.

Artist creates release → generates campaign → executes due actions.
All Phase 1/2/3 calls are mocked (no Gmail, no Anthropic).
"""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture()
def app_and_db(tmp_path, monkeypatch):
    db = tmp_path / "it6.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    import importlib
    import pitch_service, pr_service, booking_service, social_service, release_service

    for mod in (pitch_service, pr_service, booking_service, social_service, release_service):
        importlib.reload(mod)

    pitch_service.init_pitch_db()
    pr_service.init_pr_db()
    booking_service.init_booking_db()
    social_service.init_social_db()
    release_service.init_release_db()

    app = FastAPI()
    app.include_router(pitch_service.router)
    app.include_router(pr_service.router)
    app.include_router(booking_service.router)
    app.include_router(social_service.router)
    app.include_router(release_service.router)

    return TestClient(app), db, release_service


def test_release_lifecycle_full(app_and_db):
    client, db, rs = app_and_db

    future_date = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    past_date   = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")

    # 1. Create release
    resp = client.post("/api/releases", json={
        "artist_id":    "artist-it6",
        "title":        "Echoes",
        "release_date": future_date,
        "genre":        "indie",
        "mood":         "melancholic",
    })
    assert resp.status_code == 200
    release_id = resp.json()["id"]

    # 2. Generate campaign
    resp = client.post(f"/api/releases/{release_id}/generate-campaign")
    assert resp.status_code == 200
    data = resp.json()
    assert data["actions_created"] == len(rs._CAMPAIGN_SCHEDULE)
    assert data["status"] == "active"

    # 3. Verify campaign view
    resp = client.get(f"/api/releases/{release_id}/campaign")
    assert resp.status_code == 200
    camp = resp.json()
    assert camp["counts"]["awaiting_approval"] == len(rs._CAMPAIGN_SCHEDULE)
    assert camp["counts"]["done"]    == 0

    # 4. No actions due yet (release is 30 days away)
    resp = client.post(f"/api/releases/{release_id}/campaign/execute-due")
    assert resp.status_code == 200
    assert resp.json()["executed"] == 0

    # 5. Shift release date to past so some actions are due
    resp = client.patch(f"/api/releases/{release_id}", json={"release_date": past_date})
    assert resp.status_code == 200

    # Regenerate campaign with past date — some actions will be in the past
    resp = client.post(f"/api/releases/{release_id}/generate-campaign")
    assert resp.status_code == 200

    # 6. Artist approval and readiness are separate from execution.
    resp = client.post(f"/api/releases/{release_id}/campaign/approve")
    assert resp.status_code == 200
    assert resp.json()["approved"] == len(rs._CAMPAIGN_SCHEDULE)
    resp = client.post(f"/api/releases/{release_id}/campaign/ready")
    assert resp.status_code == 200
    assert resp.json()["ready"] == len(rs._CAMPAIGN_SCHEDULE)

    # 7. Execute due actions (mocked — no real email sends)
    mock_result = {"status": "skipped", "reason": "no contacts seeded in IT env"}
    with patch("release_service._execute_action", new=AsyncMock(return_value=mock_result)):
        resp = client.post(f"/api/releases/{release_id}/campaign/execute-due")

    assert resp.status_code == 200
    executed_data = resp.json()
    assert executed_data["executed"] > 0  # some actions were past due

    # 8. Verify those actions are now marked done
    resp = client.get(f"/api/releases/{release_id}/campaign")
    counts = resp.json()["counts"]
    assert counts["done"] == executed_data["executed"]

    # 9. Verify release is still readable
    resp = client.get(f"/api/releases/{release_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Echoes"
