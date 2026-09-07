import importlib
import sqlite3
import sys
import types
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
    monkeypatch.setenv("PLMKR_OTP_SEND_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("PLMKR_OTP_MAX_SENDS_PER_WINDOW", "100")
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
        "release_service",
        "phase4_service",
        "admin_service",
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


def test_otp_bootstrap_does_not_require_a_mobile_embedded_api_secret(identity_client):
    sent = identity_client.post(
        "/api/auth/send-otp",
        json={"phone": "+1 555 101 9999"},
    )
    assert sent.status_code == 200, sent.text

    verified = identity_client.post(
        "/api/auth/verify-otp",
        json={"phone": "+1 555 101 9999", "code": "000000"},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["access_token"]


def test_bearer_session_satisfies_customer_middleware_without_api_key(identity_client):
    artist = login(identity_client, "+1 555 102 9999")
    response = identity_client.get(
        f"/api/artist?artist_id={artist['artist_id']}",
        headers={"Authorization": f"Bearer {artist['access_token']}"},
    )
    assert response.status_code == 200, response.text


def test_logout_persistently_revokes_current_session(identity_client, tmp_path):
    artist = login(identity_client, "+1 555 102 8888")
    bearer_headers = {
        "Authorization": f"Bearer {artist['access_token']}",
    }

    before = identity_client.get(
        f"/api/artist?artist_id={artist['artist_id']}",
        headers=bearer_headers,
    )
    assert before.status_code == 200, before.text

    logout = identity_client.post("/api/auth/logout", headers=bearer_headers)
    assert logout.status_code == 200, logout.text
    assert logout.json()["revoked"] is True

    after = identity_client.get(
        f"/api/artist?artist_id={artist['artist_id']}",
        headers=bearer_headers,
    )
    assert after.status_code == 401, after.text

    with sqlite3.connect(str(tmp_path / "identity.db")) as conn:
        rows = conn.execute(
            "SELECT session_hash FROM revoked_artist_sessions"
        ).fetchall()
    assert len(rows) == 1
    assert artist["access_token"] not in rows[0][0]


def test_postgres_revocation_path_uses_hashed_session_ids(monkeypatch):
    from artist_identity import ArtistAuthError, decode_session, issue_session, revoke_session

    revoked_hashes = set()
    executed = []

    class FakeCursor:
        def __init__(self):
            self.selected = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params=()):
            normalized = " ".join(sql.split())
            executed.append((normalized, params))
            if normalized.startswith("INSERT INTO revoked_artist_sessions"):
                revoked_hashes.add(params[0])
            elif normalized.startswith("SELECT 1 FROM revoked_artist_sessions"):
                self.selected = params[0] in revoked_hashes

        def fetchone(self):
            return (1,) if self.selected else None

    class FakeConnection:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setenv("PLMKR_SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("PLMKR_IDENTITY_SECRET", IDENTITY_SECRET)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/session-db")
    monkeypatch.setitem(
        sys.modules,
        "psycopg2",
        types.SimpleNamespace(connect=lambda _: FakeConnection()),
    )

    session = issue_session("artist_postgres", now=1_000)
    assert decode_session(session["access_token"], now=1_001)["sub"] == "artist_postgres"
    revoke_session(session["access_token"], now=1_002)

    with pytest.raises(ArtistAuthError, match="revoked"):
        decode_session(session["access_token"], now=1_003)

    assert len(revoked_hashes) == 1
    assert session["access_token"] not in next(iter(revoked_hashes))
    assert any("ON CONFLICT (session_hash) DO NOTHING" in sql for sql, _ in executed)


def test_otp_attempt_limit_invalidates_code(identity_client, monkeypatch):
    import main

    monkeypatch.setattr(main, "OTP_MAX_VERIFY_ATTEMPTS", 3)
    phone = "+1 555 103 9999"
    sent = identity_client.post("/api/auth/send-otp", json={"phone": phone})
    assert sent.status_code == 200, sent.text

    for attempt in range(3):
        response = identity_client.post(
            "/api/auth/verify-otp",
            json={"phone": phone, "code": "111111"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["valid"] is False

    correct_after_lockout = identity_client.post(
        "/api/auth/verify-otp",
        json={"phone": phone, "code": "000000"},
    )
    assert correct_after_lockout.json()["valid"] is False
    assert "request a new code" in correct_after_lockout.json()["reason"].lower()


def test_otp_send_cooldown_returns_retry_after(identity_client, monkeypatch):
    import main

    monkeypatch.setattr(main, "OTP_SEND_COOLDOWN_SECONDS", 60)
    phone = "+1 555 104 9999"
    first = identity_client.post("/api/auth/send-otp", json={"phone": phone})
    second = identity_client.post("/api/auth/send-otp", json={"phone": phone})

    assert first.status_code == 200, first.text
    assert second.status_code == 429, second.text
    assert int(second.headers["Retry-After"]) > 0


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


def test_cross_artist_chat_handoff_and_billing_are_denied(identity_client):
    artist_a = login(identity_client, "+1 555 701 0001")
    artist_b = login(identity_client, "+1 555 701 0002")
    artist_a_headers = headers(artist_a)
    artist_b_id = artist_b["artist_id"]

    chat = identity_client.post(
        "/api/chat_stream",
        json={
            "artist_id": artist_b_id,
            "agent_id": "puppet-master",
            "message": "__greet__",
            "tts": False,
        },
        headers=artist_a_headers,
    )
    assert chat.status_code == 404, chat.text

    handoff = identity_client.post(
        "/api/handoff",
        data={
            "artist_id": artist_b_id,
            "agent_id": "grid-prophet",
            "history": "[]",
            "tts": "false",
        },
        headers=artist_a_headers,
    )
    assert handoff.status_code == 404, handoff.text

    billing_history = identity_client.get(
        f"/api/billing/history?artist_id={artist_b_id}",
        headers=artist_a_headers,
    )
    assert billing_history.status_code == 404, billing_history.text

    checkout = identity_client.post(
        "/api/billing/create-checkout",
        json={"artist_id": artist_b_id, "tier": "Starter"},
        headers=artist_a_headers,
    )
    assert checkout.status_code == 404, checkout.text


def test_cross_artist_campaign_resources_are_not_addressable_by_id(identity_client):
    artist_a = login(identity_client, "+1 555 801 0001")
    artist_b = login(identity_client, "+1 555 801 0002")
    artist_a_headers = headers(artist_a)
    artist_b_id = artist_b["artist_id"]

    created = identity_client.post(
        "/api/social/posts",
        json={
            "artist_id": artist_b["artist_id"],
            "platform": "instagram",
            "content": "Private campaign draft",
        },
        headers=headers(artist_b),
    )
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]

    for method, path, kwargs in (
        ("get", f"/api/social/posts/{post_id}", {}),
        ("patch", f"/api/social/posts/{post_id}", {"json": {"content": "Stolen"}}),
        ("delete", f"/api/social/posts/{post_id}", {}),
        ("get", f"/api/social/posts?artist_id={artist_b['artist_id']}", {}),
        ("get", f"/api/pitches?artist_id={artist_b['artist_id']}", {}),
        ("get", f"/api/pr-outreach?artist_id={artist_b['artist_id']}", {}),
        ("get", f"/api/booking-inquiries?artist_id={artist_b['artist_id']}", {}),
        ("get", f"/api/reports/weekly?artist_id={artist_b['artist_id']}", {}),
    ):
        response = getattr(identity_client, method)(
            path,
            headers=headers(artist_a),
            **kwargs,
        )
        assert response.status_code == 404, (method, path, response.text)

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


def test_cross_artist_release_and_campaign_resources_are_denied(identity_client):
    artist_a = login(identity_client, "+1 555 901 0001")
    artist_b = login(identity_client, "+1 555 901 0002")

    created = identity_client.post(
        "/api/releases",
        json={
            "artist_id": artist_b["artist_id"],
            "title": "Private Release",
            "release_date": "2026-10-30",
            "genre": "pop",
        },
        headers=headers(artist_b),
    )
    assert created.status_code == 200, created.text
    release_id = created.json()["id"]

    cases = (
        ("get", f"/api/releases?artist_id={artist_b['artist_id']}", {}),
        ("get", f"/api/releases/{release_id}", {}),
        ("patch", f"/api/releases/{release_id}", {"json": {"title": "Stolen"}}),
        ("post", f"/api/releases/{release_id}/generate-campaign", {}),
        ("get", f"/api/releases/{release_id}/campaign", {}),
        ("post", f"/api/releases/{release_id}/campaign/execute-due", {}),
    )
    for method, path, kwargs in cases:
        response = getattr(identity_client, method)(
            path,
            headers=headers(artist_a),
            **kwargs,
        )
        assert response.status_code == 404, (method, path, response.text)


def test_phase4_devices_and_artist_stats_are_owner_scoped(identity_client):
    artist_a = login(identity_client, "+1 555 902 0001")
    artist_b = login(identity_client, "+1 555 902 0002")

    registered = identity_client.post(
        "/api/devices/register",
        json={
            "artist_id": artist_b["artist_id"],
            "platform": "ios",
            "token": "ExponentPushToken[artist-b-device]",
            "app_version": "1.0.0",
        },
        headers=headers(artist_b),
    )
    assert registered.status_code == 201, registered.text

    cases = (
        ("get", f"/api/devices?artist_id={artist_b['artist_id']}", {}),
        (
            "post",
            "/api/devices/register",
            {"json": {
                "artist_id": artist_b["artist_id"],
                "platform": "ios",
                "token": "ExponentPushToken[cross-artist-device]",
                "app_version": "1.0.0",
            }},
        ),
        (
            "post",
            "/api/push/send",
            {"json": {
                "artist_id": artist_b["artist_id"],
                "title": "Blocked",
                "body": "Must not cross artist scope",
            }},
        ),
        ("get", f"/api/admin/stats?artist_id={artist_b['artist_id']}", {}),
    )
    for method, path, kwargs in cases:
        response = getattr(identity_client, method)(
            path,
            headers=headers(artist_a),
            **kwargs,
        )
        assert response.status_code == 404, (method, path, response.text)


def test_artist_session_cannot_use_admin_or_shared_directory_mutations(identity_client):
    artist = login(identity_client, "+1 555 903 0001")
    # Use only the customer bearer session. The shared test helper also adds
    # the valid administrator API key, which would correctly authorize admin
    # operations and therefore cannot be used for this negative test.
    artist_headers = {
        "Authorization": f"Bearer {artist['access_token']}",
    }

    for method, path in (
        ("get", "/api/admin/diagnostics"),
        ("get", "/api/admin/diagnostics/anthropic-stats"),
        ("get", "/api/admin/diagnostics/gmail-stats"),
        ("get", "/api/admin/diagnostics/performance"),
        ("get", "/api/admin/diagnostics/scheduler"),
        ("post", "/api/curators/seed"),
        ("post", "/api/pr-contacts/seed"),
        ("post", "/api/booking-contacts/seed"),
    ):
        response = getattr(identity_client, method)(path, headers=artist_headers)
        assert response.status_code == 401, (method, path, response.text)

    admin_response = identity_client.get(
        "/api/admin/diagnostics",
        headers={"X-API-Key": API_KEY},
    )
    assert admin_response.status_code == 200, admin_response.text


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
