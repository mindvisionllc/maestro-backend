"""
IT.5 — Cross-Phase Full Artist Journey Integration Test

Simulates a complete week for one artist across all four phases:
  Phase 1: 2 curator pitches sent, 1 reply classified
  Phase 2a: 2 PR contacts pitched, 1 feature reply
  Phase 2b: 2 venue inquiries sent, 1 booking reply
  Phase 3:  3 social posts generated + batch scheduled
  Report:   Weekly report generated, aggregates all activity
"""

import json
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from tests.integration.conftest import mock_gmail_service


ARTIST_ID = "artist-journey-001"


@pytest.fixture()
def client(tmp_path):
    from tests.integration.conftest import build_app, seed_artist, seed_gmail_tokens
    from fastapi.testclient import TestClient
    db = str(tmp_path / "full_journey.db")
    app = build_app(db)
    seed_artist(db, ARTIST_ID, artist_name="Journey Artist", genre="indie rock")
    seed_gmail_tokens(db, ARTIST_ID)
    with TestClient(app, raise_server_exceptions=True) as c:
        c._db = db
        yield c


# ── Generic Claude mock builders ──────────────────────────────────────────────

def _claude_json_response(payload: dict) -> MagicMock:
    m = MagicMock()
    m.content = [MagicMock(text=json.dumps(payload))]
    return m

PITCH_DRAFT    = {"subject": "Playlist pitch — Journey Artist", "body": "Love your taste — featuring us?"}
PR_DRAFT       = {"subject": "Feature request — Journey Artist EP", "body": "We'd love press coverage."}
BOOKING_DRAFT  = {"subject": "Booking inquiry — Journey Artist", "body": "We'd love to perform at your venue."}
CLASSIFY_POS   = {"sentiment": "positive", "summary": "Enthusiastic reply received."}
SOCIAL_POST    = {"content": "New music dropping soon. Stay tuned! #indierock", "hashtags": ["indierock"], "best_time": "18:00"}
REPORT_ANALYSIS = {
    "headline":        "Full week of outreach across all channels",
    "highlights":      ["2 pitches sent", "1 PR replied", "1 booking reply", "3 social posts"],
    "insights":        "Strong cross-platform activity. All channels engaged this week.",
    "recommendations": "1. Follow up on open pitches. 2. Convert venue reply to confirmed booking.",
    "momentum_score":  8,
}


# ── Cross-phase journey test ──────────────────────────────────────────────────

def test_full_artist_journey_preserves_drafts_and_blocks_direct_execution(client):
    curator_ids = []
    for i, name in enumerate(["Alex (Curator)", "Blake (Curator)"]):
        r = client.post("/api/curators", json={
            "name": name,
            "outlet": f"Cool Playlist {i}",
            "genres": ["indie", "rock"],
            "tier": "B",
            "contact_email": f"curator{i}@example.com",
        })
        assert r.status_code == 201, r.text
        curator_ids.append(r.json()["id"])

    pitch_send = AsyncMock()
    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("pitch_service.send_email", pitch_send):
        r = client.post("/api/pitches/batch", json={
            "artist_id": ARTIST_ID,
            "curator_ids": curator_ids,
        })
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "durable_operation_required"
    mock_anthropic.assert_not_called()
    pitch_send.assert_not_awaited()

    pr_contact_ids = []
    for i, name in enumerate(["Jamie (Press)", "Morgan (Blog)"]):
        r = client.post("/api/pr-contacts", json={
            "name": name,
            "outlet_type": "blog" if i else "magazine",
            "outlet_name": f"Outlet {i}",
            "genres": ["indie"],
            "tier": "B",
            "contact_email": f"press{i}@example.com",
            "beat": "emerging artists",
        })
        assert r.status_code == 201, r.text
        pr_contact_ids.append(r.json()["id"])

    pr_send = AsyncMock()
    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("pitch_service.send_email", pr_send):
        r = client.post("/api/pr-outreach/batch", json={
            "artist_id": ARTIST_ID,
            "contact_ids": pr_contact_ids,
        })
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "durable_operation_required"
    mock_anthropic.assert_not_called()
    pr_send.assert_not_awaited()

    booking_contact_ids = []
    for i, venue in enumerate(["The Local Spot", "City Music Hall"]):
        r = client.post("/api/booking-contacts", json={
            "name": f"Sam {i} (Booker)",
            "venue_name": venue,
            "venue_type": "club",
            "city": "Brooklyn",
            "capacity": 300 + i * 200,
            "genres": ["indie"],
            "tier": "B",
            "contact_email": f"booking{i}@example.com",
        })
        assert r.status_code == 201, r.text
        booking_contact_ids.append(r.json()["id"])

    booking_send = AsyncMock()
    with patch("anthropic.Anthropic") as mock_anthropic, \
         patch("pitch_service.send_email", booking_send):
        r = client.post("/api/booking-inquiries/batch", json={
            "artist_id": ARTIST_ID,
            "contact_ids": booking_contact_ids,
        })
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "durable_operation_required"
    mock_anthropic.assert_not_called()
    booking_send.assert_not_awaited()

    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_anthropic.return_value.messages.create.return_value = (
            _claude_json_response(SOCIAL_POST)
        )
        r = client.post("/api/social/posts/batch", json={
            "artist_id": ARTIST_ID,
            "platforms": ["twitter", "instagram"],
            "context": {"release": "new EP"},
            "tone": "authentic",
            "posts_per_platform": 2,
            "schedule_buffer": False,
        })

    assert r.status_code == 200, r.text
    assert r.json()["generated"] == 4
    assert r.json()["scheduled_via_buffer"] == 0
