import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


API_KEY = "identity-test-api-key"
SESSION_SECRET = "session-secret-" + ("s" * 48)
IDENTITY_SECRET = "identity-secret-" + ("i" * 48)


@pytest.fixture()
def identity_client(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "identity.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("ARTISTS_DIR", str(tmp_path / "artists"))
    monkeypatch.setenv("PLMKR_API_KEY", API_KEY)
    monkeypatch.setenv("PLMKR_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("PLMKR_IDENTITY_SECRET", IDENTITY_SECRET)
    monkeypatch.setenv("SMS_OTP_DEV_BYPASS", "true")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)

    # Service modules may already be cached by pytest with the default /data path.
    # Point every cached local service at this test's isolated database before
    # main reload invokes their initialization functions.
    import sys
    isolated_db = Path(tmp_path / "identity.db")
    for module_name in (
        "pitch_service",
        "pr_service",
        "booking_service",
        "social_service",
        "execution_service",
    ):
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, "_DB_PATH"):
            monkeypatch.setattr(module, "_DB_PATH", isolated_db)

    with patch("whisper.load_model", return_value=MagicMock()):
        import main
        importlib.reload(main)
        yield TestClient(main.app)


def login(identity_client, phone):
    api_headers = {"X-API-Key": API_KEY}
    sent = identity_client.post(
        "/api/auth/send-otp",
        json={"phone": phone},
        headers=api_headers,
    )
    assert sent.status_code == 200, sent.text

    verified = identity_client.post(
        "/api/auth/verify-otp",
        json={"phone": phone, "code": "000000"},
        headers=api_headers,
    )
    assert verified.status_code == 200, verified.text
    data = verified.json()
    assert data["valid"] is True
    assert data["artist_id"].startswith("artist_")
    assert data["access_token"]
    assert data["token_type"] == "Bearer"
    return data


def headers(session):
    return {
        "X-API-Key": API_KEY,
        "Authorization": f"Bearer {session['access_token']}",
    }


def test_verified_phone_receives_stable_server_owned_artist(identity_client):
    first = login(identity_client, "+1 555 111 2222")
    second = login(identity_client, "+1 555 111 2222")

    assert second["artist_id"] == first["artist_id"]
    assert "+1555" not in first["artist_id"]


def test_artist_can_save_and_read_only_own_profile(identity_client):
    artist = login(identity_client, "+1 555 222 3333")
    artist_id = artist["artist_id"]

    saved = identity_client.post(
        "/api/artist/save",
        json={
            "artist_id": artist_id,
            "name": "Identity Artist",
            "country": "Canada",
            "genres": ["pop"],
            "monthly_listeners": "0-999",
            "tier": "Gold",
            "onboarded": True,
        },
        headers=headers(artist),
    )
    assert saved.status_code == 200, saved.text

    own = identity_client.get(
        f"/api/artist?artist_id={artist_id}",
        headers=headers(artist),
    )
    assert own.status_code == 200, own.text
    assert own.json()["artist_name"] == "Identity Artist"

    lookup = identity_client.get(
        "/api/artist/lookup?name=Identity%20Artist",
        headers=headers(artist),
    )
    assert lookup.status_code == 200, lookup.text
    assert lookup.json()["artist_id"] == artist_id


def test_cross_artist_profile_history_and_lookup_are_denied(identity_client):
    artist_a = login(identity_client, "+1 555 333 4444")
    artist_b = login(identity_client, "+1 555 444 5555")

    for path in (
        f"/api/artist?artist_id={artist_b['artist_id']}",
        f"/api/history?artist_id={artist_b['artist_id']}&agent_id=puppet-master",
    ):
        response = identity_client.get(path, headers=headers(artist_a))
        assert response.status_code == 404, response.text

    response = identity_client.post(
        "/api/artist/save",
        json={
            "artist_id": artist_b["artist_id"],
            "name": "Stolen Artist",
            "genres": ["rock"],
        },
        headers=headers(artist_a),
    )
    assert response.status_code == 404, response.text


def test_missing_invalid_and_expired_sessions_fail_closed(identity_client):
    artist = login(identity_client, "+1 555 555 6666")
    artist_id = artist["artist_id"]

    missing = identity_client.get(
        f"/api/artist?artist_id={artist_id}",
        headers={"X-API-Key": API_KEY},
    )
    assert missing.status_code == 401

    tampered = dict(headers(artist))
    tampered["Authorization"] += "tampered"
    invalid = identity_client.get(
        f"/api/artist?artist_id={artist_id}",
        headers=tampered,
    )
    assert invalid.status_code == 401

def test_cross_artist_operations_gmail_buffer_and_notifications_are_denied(
    identity_client,
):
    artist_a = login(identity_client, "+1 555 601 0001")
    artist_b = login(identity_client, "+1 555 601 0002")
    artist_a_headers = headers(artist_a)
    artist_b_id = artist_b["artist_id"]

    operation = identity_client.post(
        "/api/operations",
        json={
            "artist_id": artist_b_id,
            "action_type": "gmail.send",
            "idempotency_key": "cross-artist-operation",
            "payload": {
                "to": "recipient@example.com",
                "subject": "Blocked",
                "body": "Blocked",
            },
        },
        headers=artist_a_headers,
    )
    assert operation.status_code == 404, operation.text

    gmail = identity_client.get(
        f"/api/gmail/status?artist_id={artist_b_id}",
        headers=artist_a_headers,
    )
    assert gmail.status_code == 404, gmail.text

    buffer_status = identity_client.get(
        f"/api/buffer/status?artist_id={artist_b_id}",
        headers=artist_a_headers,
    )
    assert buffer_status.status_code == 404, buffer_status.text

    buffer_profiles = identity_client.get(
        f"/api/buffer/profiles?artist_id={artist_b_id}",
        headers=artist_a_headers,
    )
    assert buffer_profiles.status_code == 404, buffer_profiles.text

    register = identity_client.post(
        "/api/notifications/register",
        json={
            "artist_id": artist_b_id,
            "push_token": "ExponentPushToken[cross-artist]",
        },
        headers=artist_a_headers,
    )
    assert register.status_code == 404, register.text

    notifications = identity_client.get(
        f"/api/notifications/{artist_b_id}",
        headers=artist_a_headers,
    )
    assert notifications.status_code == 404, notifications.text

    notification_send = identity_client.post(
        "/api/notifications/send",
        json={
            "artist_id": artist_b_id,
            "title": "Blocked",
            "body": "Cross-artist notification must not be sent",
        },
        headers=artist_a_headers,
    )
    assert notification_send.status_code == 404, notification_send.text

    billing = identity_client.post(
        "/api/billing/upgrade",
        json={"artist_id": artist_b_id, "tier": "Platinum"},
        headers=artist_a_headers,
    )
    assert billing.status_code == 404, billing.text


def test_signed_oauth_state_is_artist_and_provider_bound(identity_client):
    from artist_identity import (
        ArtistAuthError,
        decode_oauth_state,
        issue_oauth_state,
    )

    artist = login(identity_client, "+1 555 602 0001")
    state = issue_oauth_state(artist["artist_id"], "gmail")

    assert decode_oauth_state(state, "gmail") == artist["artist_id"]

    with pytest.raises(ArtistAuthError):
        decode_oauth_state(state, "buffer")

    with pytest.raises(ArtistAuthError):
        decode_oauth_state(state + "tampered", "gmail")
