"""
IT.3 — Phase 2 Booking Lifecycle Integration Test

Full flow (one test function, sequential assertions):
  1. Create booking contact via POST /api/booking-contacts
  2. Generate booking email (Claude mocked) via POST /api/booking-inquiries/generate
  3. Batch send → inquiry saved with status=sent + gmail_thread_id recorded
  4. Mock Gmail inbox scan + Claude classify → status=replied
  5. Assert BookingInteraction logged with direction=inbound
"""

import json
import uuid
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from tests.integration.conftest import mock_gmail_service


ARTIST_ID = "artist-booking-001"


@pytest.fixture()
def client(tmp_path):
    from tests.integration.conftest import build_app, seed_artist, seed_gmail_tokens
    from fastapi.testclient import TestClient
    db = str(tmp_path / "booking_lifecycle.db")
    app = build_app(db)
    seed_artist(db, ARTIST_ID)
    seed_gmail_tokens(db, ARTIST_ID)
    with TestClient(app, raise_server_exceptions=True) as c:
        c._db = db
        yield c


# ── Claude mock helpers ───────────────────────────────────────────────────────

def _claude_booking_response():
    m = MagicMock()
    m.content = [MagicMock(text=json.dumps({
        "subject": "Booking inquiry — Integration Artist at The Test Venue",
        "body":    "Hi, we'd love to discuss a performance opportunity at your venue.",
    }))]
    return m


def _claude_classify_response(sentiment="positive"):
    m = MagicMock()
    m.content = [MagicMock(text=json.dumps({
        "sentiment": sentiment,
        "summary":   "Venue expressed interest in booking the artist.",
    }))]
    return m


# ── Lifecycle test ────────────────────────────────────────────────────────────

def test_booking_draft_generation_and_legacy_batch_lockdown(client):
    r = client.post("/api/booking-contacts", json={
        "name": "Sam Torres",
        "venue_name": "The Test Venue",
        "venue_type": "club",
        "city": "Brooklyn",
        "capacity": 500,
        "genres": ["indie", "alternative"],
        "tier": "B",
        "contact_email": "sam@testvenue.example.com",
    })
    assert r.status_code == 201, r.text
    contact_id = r.json()["id"]

    with patch("anthropic.Anthropic") as mock_claude:
        mock_claude.return_value.messages.create.return_value = _claude_booking_response()
        r = client.post("/api/booking-inquiries/generate", json={
            "artist_id": ARTIST_ID,
            "contact_id": contact_id,
        })

    assert r.status_code == 200, r.text
    assert r.json()["subject"]
    assert r.json()["body"]

    mock_send = AsyncMock()
    with patch("anthropic.Anthropic") as mock_claude, \
         patch("pitch_service.send_email", mock_send):
        r = client.post("/api/booking-inquiries/batch", json={
            "artist_id": ARTIST_ID,
            "contact_ids": [contact_id],
        })

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "durable_operation_required"
    assert r.json()["detail"]["action_type"] == "gmail.send"
    mock_claude.assert_not_called()
    mock_send.assert_not_awaited()
