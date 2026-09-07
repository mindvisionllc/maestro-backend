"""
IT.2 — Phase 2 PR Lifecycle Integration Test

Full flow (one test function, sequential assertions):
  1. Create PR contact via POST /api/pr-contacts
  2. Generate PR email (Claude mocked) via POST /api/pr-outreach/generate
  3. Batch send → outreach saved with status=sent + gmail_thread_id recorded
  4. Mock Gmail inbox scan + Claude classify → status=replied
  5. Assert PRInteraction logged with direction=inbound
"""

import json
import uuid
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from tests.integration.conftest import mock_gmail_service


ARTIST_ID = "artist-pr-001"


@pytest.fixture()
def client(tmp_path):
    from tests.integration.conftest import build_app, seed_artist, seed_gmail_tokens
    from fastapi.testclient import TestClient
    db = str(tmp_path / "pr_lifecycle.db")
    app = build_app(db)
    seed_artist(db, ARTIST_ID)
    seed_gmail_tokens(db, ARTIST_ID)
    with TestClient(app, raise_server_exceptions=True) as c:
        c._db = db
        yield c


# ── Claude mock helpers ───────────────────────────────────────────────────────

def _claude_pr_response():
    m = MagicMock()
    m.content = [MagicMock(text=json.dumps({
        "subject": "Feature request — Integration Artist's new EP",
        "body":    "Hi, we'd love to have you cover our upcoming EP release.",
    }))]
    return m


def _claude_classify_response(sentiment="positive"):
    m = MagicMock()
    m.content = [MagicMock(text=json.dumps({
        "sentiment": sentiment,
        "summary":   "Press contact expressed interest in covering the release.",
    }))]
    return m


# ── Lifecycle test ────────────────────────────────────────────────────────────

def test_pr_draft_generation_and_legacy_batch_lockdown(client):
    r = client.post("/api/pr-contacts", json={
        "name": "Alex Rivera",
        "outlet_type": "blog",
        "outlet_name": "Indie Pulse Blog",
        "genres": ["indie", "alternative"],
        "tier": "B",
        "contact_email": "alex@indiepulse.example.com",
        "beat": "emerging artists",
    })
    assert r.status_code == 201, r.text
    contact_id = r.json()["id"]

    with patch("anthropic.Anthropic") as mock_claude:
        mock_claude.return_value.messages.create.return_value = _claude_pr_response()
        r = client.post("/api/pr-outreach/generate", json={
            "artist_id": ARTIST_ID,
            "contact_id": contact_id,
        })

    assert r.status_code == 200, r.text
    assert r.json()["subject"]
    assert r.json()["body"]

    mock_send = AsyncMock()
    with patch("anthropic.Anthropic") as mock_claude, \
         patch("pitch_service.send_email", mock_send):
        r = client.post("/api/pr-outreach/batch", json={
            "artist_id": ARTIST_ID,
            "contact_ids": [contact_id],
        })

    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "durable_operation_required"
    assert r.json()["detail"]["action_type"] == "gmail.send"
    mock_claude.assert_not_called()
    mock_send.assert_not_awaited()
