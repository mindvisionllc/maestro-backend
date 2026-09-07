"""
IT-A1 — Artist Onboarding Flow Integration Tests

Covers the end-to-end path from artist seed through first pitch send and debrief:
  1. Seeded artist profile is present and correct in the DB
  2. Curator creation via POST /api/curators
  3. Pitch generation calls real _anthropic_call_with_retry → anthropic stats increment
  4. Batch send runs real send_email + _gmail_execute_with_retry → both stats increment
  5. Sent pitch is retrievable with status=sent and gmail_thread_id set
  6. Inbox scan with a curator reply updates pitch status to replied

Mocks at API boundaries only (not service layer):
  - anthropic.Anthropic  (Anthropic SDK client constructor)
  - pitch_service._get_gmail_service  (Google API client factory)
"""

import json
import sqlite3
import uuid
from unittest.mock import MagicMock, patch

import pytest

from tests.integration.conftest import (
    build_app,
    make_claude_response,
    make_send_gmail_svc,
    mock_gmail_service,
    seed_artist,
    seed_gmail_tokens,
)


ARTIST_ID = "artist-onboard-001"

_PITCH_DRAFT = make_claude_response({
    "subject": "First Pitch — Onboarding Artist",
    "body":    "Hi! We'd love to be featured on your playlist.",
})
_CLASSIFY_POS = make_claude_response({
    "sentiment": "positive",
    "summary":   "Curator is enthusiastic and wants to add the track.",
})


@pytest.fixture()
def client(tmp_path):
    db = str(tmp_path / "onboarding.db")
    app = build_app(db)
    seed_artist(db, ARTIST_ID, artist_name="Onboarding Artist", genre="indie pop")
    seed_gmail_tokens(db, ARTIST_ID)
    from fastapi.testclient import TestClient
    with TestClient(app, raise_server_exceptions=True) as c:
        c._db = db
        yield c


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_seeded_artist_has_correct_profile(client):
    """Artist profile inserted by seed_artist is present with correct fields."""
    conn = sqlite3.connect(client._db)
    row  = conn.execute(
        "SELECT data FROM artists WHERE artist_id=?", (ARTIST_ID,)
    ).fetchone()
    conn.close()
    assert row is not None, "Artist not found in DB"
    profile = json.loads(row[0])
    assert profile["artist_id"] == ARTIST_ID
    assert profile["genre"] == "indie pop"
    assert profile["artist_name"] == "Onboarding Artist"


def test_create_curator_returns_id_and_tier(client):
    """POST /api/curators creates a record and returns a usable ID."""
    r = client.post("/api/curators", json={
        "name":          "Jordan Lee",
        "outlet":        "Indie Discovery",
        "genres":        ["indie", "pop"],
        "tier":          "B",
        "contact_email": "jordan@example.com",
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert "id" in body
    assert body["tier"] == "B"
    assert body["outlet"] == "Indie Discovery"


def test_pitch_generation_increments_anthropic_stats(client):
    """POST /api/pitches/generate calls _anthropic_call_with_retry → stats.total increases."""
    from anthropic_utils import get_anthropic_stats

    r = client.post("/api/curators", json={
        "name": "Casey B.", "outlet": "Chill Vibes",
        "genres": ["indie"], "tier": "B",
        "contact_email": "casey@example.com",
    })
    assert r.status_code == 201
    curator_id = r.json()["id"]

    before = sum(v["total"] for v in get_anthropic_stats().values())

    with patch("anthropic.Anthropic") as mc:
        mc.return_value.messages.create.return_value = _PITCH_DRAFT
        r = client.post("/api/pitches/generate", json={
            "artist_id":  ARTIST_ID,
            "curator_id": curator_id,
        })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "subject" in body and "body" in body

    after = sum(v["total"] for v in get_anthropic_stats().values())
    assert after > before, "anthropic_stats.total must increase after pitch generation"


def test_legacy_pitch_batch_is_blocked_without_provider_or_model_usage(client):
    from unittest.mock import AsyncMock
    """Legacy pitch batch cannot bypass the durable operation ledger."""
    import pitch_service
    from anthropic_utils import get_anthropic_stats

    r = client.post("/api/curators", json={
        "name": "River P.",
        "outlet": "New Wave Radio",
        "genres": ["pop"],
        "tier": "A",
        "contact_email": "river@example.com",
    })
    assert r.status_code == 201
    curator_id = r.json()["id"]

    before_anthropic = sum(v["total"] for v in get_anthropic_stats().values())
    before_gmail = sum(v["total"] for v in pitch_service.get_gmail_stats().values())

    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("pitch_service.send_email", new=AsyncMock()) as mock_send:
        r = client.post("/api/pitches/batch", json={
            "artist_id": ARTIST_ID,
            "curator_ids": [curator_id],
        })

    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "durable_operation_required"
    assert detail["action_type"] == "gmail.send"
    mock_anthropic.assert_not_called()
    mock_send.assert_not_awaited()

    after_anthropic = sum(v["total"] for v in get_anthropic_stats().values())
    after_gmail = sum(v["total"] for v in pitch_service.get_gmail_stats().values())
    assert after_anthropic == before_anthropic
    assert after_gmail == before_gmail



def test_pitch_draft_generation_remains_available(client):
    """Draft generation remains safe while direct batch execution is blocked."""
    r = client.post("/api/curators", json={
        "name": "Skye M.",
        "outlet": "Morning Mood",
        "genres": ["acoustic"],
        "tier": "B",
        "contact_email": "skye@example.com",
    })
    assert r.status_code == 201
    curator_id = r.json()["id"]

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = _PITCH_DRAFT
        r = client.post("/api/pitches/generate", json={
            "artist_id": ARTIST_ID,
            "curator_id": curator_id,
        })

    assert r.status_code == 200, r.text
    draft = r.json()
    assert draft["subject"]
    assert draft["body"]



def test_inbox_scan_without_sent_pitch_does_not_create_a_match(client):
    """Read-only inbox scanning remains available without manufacturing sent records."""
    thread_id = f"thread-{uuid.uuid4().hex[:8]}"
    inbox_svc = mock_gmail_service(
        thread_id,
        "Unknown pitch",
        "Love it! Adding to the playlist.",
    )

    with patch("pitch_service._get_gmail_service", return_value=inbox_svc), \
         patch("anthropic.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = _CLASSIFY_POS
        r = client.post(f"/api/inbox/scan?artist_id={ARTIST_ID}")

    assert r.status_code == 200, r.text
    assert r.json()["matched"] == 0
