"""
Unit tests for phase4_service.py — Phase 4 iOS backend foundation.

Tests cover:
  - Device registration (happy path, invalid platform, invalid token, duplicate)
  - Notification send (mocked APNs/FCM with LIVE flags, no devices)
  - App config endpoint (versioned config structure)
  - Version check (ok / soft_update / hard_update_required)
  - IAP receipt validation stub

Run with:  python3 -m pytest tests/test_phase4_service.py -v
"""

import importlib
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("APNS_LIVE", "false")
    monkeypatch.setenv("FCM_LIVE", "false")
    monkeypatch.setenv("IAP_LIVE", "false")
    monkeypatch.setenv("APP_MIN_VERSION_IOS", "1.0.0")
    monkeypatch.setenv("APP_MIN_VERSION_ANDROID", "1.0.0")
    monkeypatch.setenv("APP_CURRENT_VERSION", "1.2.0")
    import phase4_service
    importlib.reload(phase4_service)
    phase4_service.init_phase4_db()
    yield db


@pytest.fixture()
def p4():
    import phase4_service
    return phase4_service


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "client.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("PLMKR_API_KEY", "test-key")
    monkeypatch.setenv("APP_MIN_VERSION_IOS", "1.0.0")
    monkeypatch.setenv("APP_MIN_VERSION_ANDROID", "1.0.0")
    monkeypatch.setenv("APP_CURRENT_VERSION", "1.2.0")
    monkeypatch.setenv("APNS_LIVE", "false")
    monkeypatch.setenv("FCM_LIVE", "false")
    monkeypatch.setenv("IAP_LIVE", "false")
    import phase4_service
    importlib.reload(phase4_service)
    phase4_service.init_phase4_db()
    # Minimal FastAPI app — avoids main.py's /data directory creation
    from fastapi import FastAPI
    app = FastAPI()
    # Wrap router behind API-key middleware matching main.py pattern
    from fastapi import Request
    from fastapi.responses import JSONResponse
    api_key = "test-key"

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        if request.headers.get("X-API-Key") != api_key:
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        return await call_next(request)

    app.include_router(phase4_service.router)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


_HEADERS = {"X-API-Key": "test-key"}
_VALID_TOKEN = "abc1234567890abc"


# ═══════════════════════════════════════════════════════════════════════════════
# Device registration
# ═══════════════════════════════════════════════════════════════════════════════

def test_register_device_ios_happy_path(client):
    r = client.post("/api/devices/register", json={
        "artist_id": "artist-ios-001",
        "platform": "ios",
        "token": _VALID_TOKEN,
        "app_version": "1.2.0",
    }, headers=_HEADERS)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["platform"] == "ios"
    assert d["artist_id"] == "artist-ios-001"
    assert d["token_registered"] is True
    assert "token" not in d


def test_list_devices_redacts_push_tokens(client):
    client.post("/api/devices/register", json={
        "artist_id": "artist-redaction-001",
        "platform": "ios",
        "token": _VALID_TOKEN,
    }, headers=_HEADERS)
    response = client.get("/api/devices?artist_id=artist-redaction-001", headers=_HEADERS)
    assert response.status_code == 200, response.text
    device = response.json()["devices"][0]
    assert device["token_registered"] is True
    assert "token" not in device


def test_unregister_device_removes_only_matching_artist_registration(client):
    for artist_id in ("artist-unregister", "artist-other"):
        response = client.post("/api/devices/register", json={
            "artist_id": artist_id,
            "platform": "ios",
            "token": _VALID_TOKEN,
        }, headers=_HEADERS)
        assert response.status_code == 201

    response = client.post("/api/devices/unregister", json={
        "artist_id": "artist-unregister",
        "platform": "ios",
        "token": _VALID_TOKEN,
    }, headers=_HEADERS)
    assert response.status_code == 200
    assert response.json() == {"removed": True}
    assert client.get("/api/devices?artist_id=artist-unregister", headers=_HEADERS).json()["devices"] == []
    assert len(client.get("/api/devices?artist_id=artist-other", headers=_HEADERS).json()["devices"]) == 1


def test_unregister_device_is_idempotent(client):
    payload = {"artist_id": "artist-unregister-idempotent", "platform": "android", "token": _VALID_TOKEN}
    assert client.post("/api/devices/unregister", json=payload, headers=_HEADERS).json() == {"removed": False}


def test_register_device_android_happy_path(client):
    r = client.post("/api/devices/register", json={
        "artist_id": "artist-android-001",
        "platform": "android",
        "token": _VALID_TOKEN,
        "app_version": "1.2.0",
    }, headers=_HEADERS)
    assert r.status_code == 201, r.text
    assert r.json()["platform"] == "android"


def test_register_device_invalid_platform(client):
    r = client.post("/api/devices/register", json={
        "artist_id": "artist-001",
        "platform": "windows",
        "token": _VALID_TOKEN,
    }, headers=_HEADERS)
    assert r.status_code == 400


def test_register_device_invalid_token(client):
    r = client.post("/api/devices/register", json={
        "artist_id": "artist-001",
        "platform": "ios",
        "token": "short",
    }, headers=_HEADERS)
    assert r.status_code == 400


def test_register_device_duplicate_upserts(p4):
    """Registering the same token twice upserts — no duplicate rows."""
    p4._db_register_device("artist-dup", "ios", _VALID_TOKEN, "1.0.0")
    p4._db_register_device("artist-dup", "ios", _VALID_TOKEN, "1.2.0")
    devices = p4._db_list_device_tokens("artist-dup")
    assert len(devices) == 1
    assert devices[0]["app_version"] == "1.2.0"


# ═══════════════════════════════════════════════════════════════════════════════
# Notification send
# ═══════════════════════════════════════════════════════════════════════════════

def test_send_notification_no_devices(client):
    """No registered devices → sent=0, no error."""
    r = client.post("/api/push/send", json={
        "artist_id": "artist-nodevices",
        "title": "New pitch reply!",
        "body": "A curator replied to your pitch.",
    }, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["sent"] == 0


def test_send_notification_mocked_ios(p4):
    """With APNS_LIVE=false, _send_apns returns mocked response without real call."""
    import asyncio
    result = asyncio.run(p4._send_apns(_VALID_TOKEN, "Test", "Body", {}))
    assert result["mocked"] is True
    assert result["platform"] == "ios"


def test_send_notification_mocked_android(p4):
    """With FCM_LIVE=false, _send_fcm returns mocked response without real call."""
    import asyncio
    result = asyncio.run(p4._send_fcm(_VALID_TOKEN, "Test", "Body", {}))
    assert result["mocked"] is True
    assert result["platform"] == "android"


def test_send_notification_dispatches_to_registered_devices(p4):
    """Notification send calls the right stub per platform."""
    import asyncio
    p4._db_register_device("artist-push", "ios", _VALID_TOKEN, "1.2.0")
    p4._db_register_device("artist-push", "android", "androidtoken1234", "1.2.0")

    req = p4.NotificationSendRequest(
        artist_id="artist-push", title="Test", body="Push body", data={}
    )
    result = asyncio.run(p4.push_send(req))
    assert result["sent"] == 2
    platforms = {r["platform"] for r in result["results"]}
    assert "ios" in platforms
    assert "android" in platforms


# ═══════════════════════════════════════════════════════════════════════════════
# App config
# ═══════════════════════════════════════════════════════════════════════════════

def test_app_config_returns_versioned_structure(client):
    r = client.get("/api/app/config", headers=_HEADERS)
    assert r.status_code == 200
    cfg = r.json()
    assert "schema_version" in cfg
    assert "min_version" in cfg
    assert "current_version" in cfg
    assert "feature_flags" in cfg
    assert "support_urls" in cfg
    assert "kill_switches" in cfg


def test_app_config_min_version_ios(client):
    r = client.get("/api/app/config", headers=_HEADERS)
    cfg = r.json()
    assert cfg["min_version"]["ios"] == "1.0.0"
    assert cfg["min_version"]["android"] == "1.0.0"


# ═══════════════════════════════════════════════════════════════════════════════
# Version check
# ═══════════════════════════════════════════════════════════════════════════════

def test_version_check_ok(client):
    r = client.post("/api/app/version-check", json={
        "platform": "ios", "current_version": "1.2.0"
    }, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_version_check_soft_update(client):
    r = client.post("/api/app/version-check", json={
        "platform": "ios", "current_version": "1.1.0"
    }, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "soft_update"


def test_version_check_hard_update_required(client):
    r = client.post("/api/app/version-check", json={
        "platform": "ios", "current_version": "0.9.0"
    }, headers=_HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "hard_update_required"


def test_version_check_invalid_platform(client):
    r = client.post("/api/app/version-check", json={
        "platform": "linux", "current_version": "1.0.0"
    }, headers=_HEADERS)
    assert r.status_code == 400


def test_compare_semver(p4):
    assert p4._compare_semver("1.0.0", "1.0.0") == 0
    assert p4._compare_semver("1.1.0", "1.0.0") == 1
    assert p4._compare_semver("0.9.0", "1.0.0") == -1
    assert p4._compare_semver("2.0.0", "1.9.9") == 1


# ═══════════════════════════════════════════════════════════════════════════════
# IAP receipt validation stub
# ═══════════════════════════════════════════════════════════════════════════════

def test_iap_validate_receipt_mocked(client):
    """With IAP_LIVE=false, returns mocked valid response."""
    r = client.post("/api/iap/validate-receipt", json={
        "artist_id": "artist-iap-001",
        "receipt_data": "base64datahere==",
        "product_id": "com.playmaker.pro.monthly",
        "transaction_id": "txn-abc-123",
    }, headers=_HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is True
    assert data["mocked"] is True
    assert data["product_id"] == "com.playmaker.pro.monthly"
