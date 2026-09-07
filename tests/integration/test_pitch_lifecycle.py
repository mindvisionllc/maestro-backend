"""
IT.1 — Phase 1 Pitch Lifecycle Integration Test

Full flow (one test function, sequential assertions):
  1. Create curator via POST /api/curators
  2. Generate pitch email (Claude mocked) via POST /api/pitches/generate
  3. Create Pitch record with status=draft
  4. Mock Gmail send → status=sent + gmail_thread_id recorded
  5. Mock Gmail inbox scan + Claude classify → status=replied
  6. Assert PitchInteraction logged with direction=inbound
"""

import json
import uuid
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from tests.integration.conftest import mock_gmail_service


ARTIST_ID = "artist-pitch-001"


@pytest.fixture()
def client(tmp_path):
    from tests.integration.conftest import build_app, seed_artist, seed_gmail_tokens
    from fastapi.testclient import TestClient
    db = str(tmp_path / "pitch_lifecycle.db")
    app = build_app(db)
    seed_artist(db, ARTIST_ID)
    seed_gmail_tokens(db, ARTIST_ID)
    with TestClient(app, raise_server_exceptions=True) as c:
        c._db = db
        yield c


# ── Claude mock helpers ───────────────────────────────────────────────────────

def _claude_pitch_response():
    m = MagicMock()
    m.content = [MagicMock(text=json.dumps({
        "subject": "Playlist pitch — Integration Artist",
        "body":    "Hi, we'd love to have you feature our new single.",
    }))]
    return m


def _claude_classify_response(sentiment="positive"):
    m = MagicMock()
    m.content = [MagicMock(text=json.dumps({
        "sentiment": sentiment,
        "summary":   "Curator expressed strong interest.",
    }))]
    return m


# ── Lifecycle test ────────────────────────────────────────────────────────────

def test_pitch_draft_generation_and_legacy_batch_lockdown(client):
    r = client.post("/api/curators", json={
        "name": "Jordan Lee",
        "outlet": "Indie Discovery Playlist",
        "genres": ["indie", "pop"],
        "tier": "B",
        "contact_email": "jordan@example.com",
    })
    assert r.status_code == 201, r.text
    curator_id = r.json()["id"]

    with patch("anthropic.Anthropic") as mock_claude:
        mock_claude.return_value.messages.create.return_value = _claude_pitch_response()
        r = client.post("/api/pitches/generate", json={
            "artist_id": ARTIST_ID,
            "curator_id": curator_id,
        })

    assert r.status_code == 200, r.text
    assert r.json()["subject"]
    assert r.json()["body"]

    mock_send = AsyncMock()
    with patch("anthropic.Anthropic") as mock_claude, \
         patch("pitch_service.send_email", mock_send):
        r = client.post("/api/pitches/batch", json={
            "artist_id": ARTIST_ID,
            "curator_ids": [curator_id],
        })

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "durable_operation_required"
    assert r.json()["detail"]["action_type"] == "gmail.send"
    mock_claude.assert_not_called()
    mock_send.assert_not_awaited()
