"""
IT-A2 — Scheduler Pipeline Integration Tests

Exercises the campaign scheduler sweep:
  - execute_all_due_campaign_actions() processes due actions up to SCHEDULER_BATCH_LIMIT
  - Actions with no matching contacts are marked done (skipped)
  - Pitch actions with real contacts call real code paths → observability stats increment
  - Stuck "running" actions are reset to pending by init_release_db()
  - Completed actions are not re-executed on subsequent sweeps
  - Campaign generation creates properly dated actions for past releases

Mocks at API boundaries only (not service layer):
  - anthropic.Anthropic  (Anthropic SDK client constructor)
  - pitch_service._get_gmail_service  (Google API client factory)
"""

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from tests.integration.conftest import (
    build_release_app,
    make_claude_response,
    make_send_gmail_svc,
    seed_artist,
    seed_gmail_tokens,
)
from fastapi.testclient import TestClient


ARTIST_ID = "artist-sched-001"

_PITCH_DRAFT = make_claude_response({
    "subject": "Scheduler Test Pitch",
    "body":    "Automated pitch from the scheduler pipeline test.",
})


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def sched_client(tmp_path, monkeypatch):
    """TestClient with all 5 routers + release_service. Scheduler batch limit left at default."""
    db = str(tmp_path / "scheduler.db")
    app = build_release_app(db)
    seed_artist(db, ARTIST_ID, artist_name="Scheduler Artist", genre="hip-hop")
    seed_gmail_tokens(db, ARTIST_ID)
    with TestClient(app, raise_server_exceptions=True) as c:
        c._db = db
        yield c


@pytest.fixture()
def release_svc(sched_client):
    """Return the freshly-reloaded release_service module for direct calls."""
    import release_service
    return release_service


def _insert_pending_action(db_path: str, release_id: str, artist_id: str,
                            action_type: str = "pitch_curators",
                            offset_hours: int = -1) -> str:
    """Insert a pending campaign action scheduled in the past (default 1 h ago)."""
    action_id   = str(uuid.uuid4())
    sched_at    = (datetime.now(timezone.utc) + timedelta(hours=offset_hours)).isoformat()
    payload_str = json.dumps({"artist_id": artist_id, "release_title": "Test EP",
                              "genre": "hip-hop", "mood": "energetic",
                              "tier_filter": ["A", "B"]})
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO campaign_actions "
        "(id, release_id, action_type, scheduled_for, status, payload_json) "
        "VALUES (?,?,?,?,?,?)",
        (action_id, release_id, action_type, sched_at, "pending", payload_str),
    )
    conn.commit()
    conn.close()
    return action_id


def _get_action_status(db_path: str, action_id: str) -> str:
    conn = sqlite3.connect(db_path)
    row  = conn.execute(
        "SELECT status FROM campaign_actions WHERE id=?", (action_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_sweep_no_due_actions_returns_zero(sched_client):
    """Release with only future actions → 0 executed on execute-due."""
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    r = sched_client.post("/api/releases", json={
        "artist_id":    ARTIST_ID,
        "title":        "Future EP",
        "release_date": future,
        "genre":        "hip-hop",
        "mood":         "energetic",
    })
    assert r.status_code == 200
    release_id = r.json()["id"]

    r = sched_client.post(f"/api/releases/{release_id}/campaign/execute-due")
    assert r.status_code == 200
    assert r.json()["executed"] == 0


def test_sweep_pitch_action_skips_with_no_curators(sched_client):
    """Pitch action with no curators seeded → _execute_action returns skipped → action marked done."""
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    r = sched_client.post("/api/releases", json={
        "artist_id":    ARTIST_ID,
        "title":        "Skip EP",
        "release_date": future,
        "genre":        "hip-hop",
        "mood":         "chill",
    })
    assert r.status_code == 200
    release_id = r.json()["id"]

    action_id = _insert_pending_action(sched_client._db, release_id, ARTIST_ID)

    r = sched_client.post(f"/api/releases/{release_id}/campaign/execute-due")
    assert r.status_code == 200
    data = r.json()
    assert data["executed"] == 1

    executed = data["actions"][0]
    assert executed["status"] == "done"
    assert executed["result"]["status"] == "skipped"

    assert _get_action_status(sched_client._db, action_id) == "done"


def test_sweep_batch_limit_caps_execution(tmp_path, monkeypatch):
    """Global sweep: 5 pending actions with limit=2 → only 2 processed per tick."""
    monkeypatch.setenv("SCHEDULER_BATCH_LIMIT", "2")
    db  = str(tmp_path / "batchlimit.db")
    app = build_release_app(db)
    seed_artist(db, ARTIST_ID, artist_name="Batch Artist", genre="pop")
    seed_gmail_tokens(db, ARTIST_ID)

    import release_service
    release_id = str(uuid.uuid4())
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO releases (id, artist_id, title, release_date, genre, mood) "
        "VALUES (?,?,?,?,?,?)",
        (release_id, ARTIST_ID, "Batch EP",
         (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"),
         "pop", "happy"),
    )
    conn.commit()
    conn.close()

    for _ in range(5):
        _insert_pending_action(db, release_id, ARTIST_ID)

    asyncio.run(release_service.execute_all_due_campaign_actions())

    conn = sqlite3.connect(db)
    done_count    = conn.execute(
        "SELECT COUNT(*) FROM campaign_actions WHERE status='done'"
    ).fetchone()[0]
    pending_count = conn.execute(
        "SELECT COUNT(*) FROM campaign_actions WHERE status='pending'"
    ).fetchone()[0]
    conn.close()

    assert done_count    == 2, f"Expected 2 done, got {done_count}"
    assert pending_count == 3, f"Expected 3 pending, got {pending_count}"


def test_scheduler_pitch_action_cannot_bypass_durable_operations(sched_client):
    from unittest.mock import AsyncMock
    """Scheduled pitch execution fails closed instead of directly sending Gmail."""
    import pitch_service
    from anthropic_utils import get_anthropic_stats

    r = sched_client.post("/api/curators", json={
        "name": "Test Curator",
        "outlet": "Scheduler Playlist",
        "genres": ["hip-hop"],
        "tier": "A",
        "contact_email": "sched@example.com",
    })
    assert r.status_code == 201

    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    r = sched_client.post("/api/releases", json={
        "artist_id": ARTIST_ID,
        "title": "Stats EP",
        "release_date": future,
        "genre": "hip-hop",
        "mood": "focused",
    })
    assert r.status_code == 200
    release_id = r.json()["id"]
    action_id = _insert_pending_action(sched_client._db, release_id, ARTIST_ID)

    before_anthropic = sum(v["total"] for v in get_anthropic_stats().values())
    before_gmail = sum(v["total"] for v in pitch_service.get_gmail_stats().values())

    mock_send = AsyncMock()
    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("pitch_service.send_email", mock_send):
        r = sched_client.post(f"/api/releases/{release_id}/campaign/execute-due")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["executed"] == 1
    assert data["actions"][0]["status"] == "failed"
    assert _get_action_status(sched_client._db, action_id) == "failed"
    mock_anthropic.assert_not_called()
    mock_send.assert_not_awaited()

    after_anthropic = sum(v["total"] for v in get_anthropic_stats().values())
    after_gmail = sum(v["total"] for v in pitch_service.get_gmail_stats().values())
    assert after_anthropic == before_anthropic
    assert after_gmail == before_gmail



def test_stuck_running_actions_reset_on_init(tmp_path):
    """Actions stuck in 'running' (from a crashed process) are reset to pending by init_release_db."""
    import importlib
    import os

    db = str(tmp_path / "stuck.db")
    os.environ["DB_PATH"]       = db
    os.environ["DATABASE_URL"]  = ""

    import release_service
    importlib.reload(release_service)
    release_service.init_release_db()

    release_id = str(uuid.uuid4())
    action_id  = str(uuid.uuid4())
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO releases (id, artist_id, title, release_date) VALUES (?,?,?,?)",
        (release_id, ARTIST_ID, "Stuck EP", "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO campaign_actions "
        "(id, release_id, action_type, scheduled_for, status) VALUES (?,?,?,?,?)",
        (action_id, release_id, "pitch_curators",
         (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(), "running"),
    )
    conn.commit()
    conn.close()

    importlib.reload(release_service)
    release_service.init_release_db()

    assert _get_action_status(db, action_id) == "pending", (
        "init_release_db must reset stuck 'running' actions to 'pending'"
    )


def test_completed_actions_not_re_executed(sched_client):
    """Actions already in status='done' are not picked up by the next sweep."""
    future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    r = sched_client.post("/api/releases", json={
        "artist_id":    ARTIST_ID,
        "title":        "Done EP",
        "release_date": future,
        "genre":        "hip-hop",
        "mood":         "reflective",
    })
    assert r.status_code == 200
    release_id = r.json()["id"]

    action_id = _insert_pending_action(sched_client._db, release_id, ARTIST_ID)

    # First sweep: executes the action (skipped — no curators), marks done
    r = sched_client.post(f"/api/releases/{release_id}/campaign/execute-due")
    assert r.status_code == 200
    assert r.json()["executed"] == 1
    assert _get_action_status(sched_client._db, action_id) == "done"

    # Second sweep: action already done → 0 executed
    r = sched_client.post(f"/api/releases/{release_id}/campaign/execute-due")
    assert r.status_code == 200
    assert r.json()["executed"] == 0


def test_past_release_generates_due_actions(sched_client):
    """Release with a past date generates campaign actions with scheduled_for already elapsed."""
    from anthropic_utils import get_anthropic_stats

    past = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
    r = sched_client.post("/api/releases", json={
        "artist_id":    ARTIST_ID,
        "title":        "Past EP",
        "release_date": past,
        "genre":        "hip-hop",
        "mood":         "nostalgic",
    })
    assert r.status_code == 200
    release_id = r.json()["id"]

    r = sched_client.post(f"/api/releases/{release_id}/generate-campaign")
    assert r.status_code == 200
    data = r.json()
    assert data["actions_created"] > 0

    r = sched_client.get(f"/api/releases/{release_id}/campaign")
    assert r.status_code == 200
    camp      = r.json()
    now_iso   = datetime.now(timezone.utc).isoformat()

    import release_service as rs
    conn = sqlite3.connect(sched_client._db)
    due_count = conn.execute(
        "SELECT COUNT(*) FROM campaign_actions "
        "WHERE release_id=? AND status='pending' AND scheduled_for <= ?",
        (release_id, now_iso),
    ).fetchone()[0]
    conn.close()

    assert due_count > 0, (
        f"Past release should have at least 1 due action; got {due_count}"
    )
    assert camp["counts"]["pending"] > 0
