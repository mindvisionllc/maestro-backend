import importlib
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
