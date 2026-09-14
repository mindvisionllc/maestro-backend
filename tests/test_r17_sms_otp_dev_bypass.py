"""
R-17 — Twilio auth token validation + SMS_OTP_DEV_BYPASS guard.

Tests:
- Malformed TWILIO_AUTH_TOKEN causes send-otp to return a sanitized 503 (not store OTP first)
- No pending verification is stored when provider config is malformed
- SMS_OTP_DEV_BYPASS=true alongside a Verify service SID → sys.exit(1) at boot
- SMS_OTP_DEV_BYPASS=true on Railway → sys.exit(1) at boot
- SMS_OTP_DEV_BYPASS=true in local dev → send-otp succeeds with code 000000
- Normal flow (valid or missing Twilio) is unaffected when bypass disabled
"""

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


_PLMKR_KEY = "test-plmkr-key"


def _build_client(monkeypatch, tmp_path, **extra_env):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH",           db_path)
    monkeypatch.setenv("DATABASE_URL",      "")
    monkeypatch.setenv("AUDIO_CACHE_DIR",   str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR",       str(tmp_path / "artists"))
    monkeypatch.setenv("PLMKR_API_KEY",     _PLMKR_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("APP_BASE_URL",      "https://test.example.com")
    monkeypatch.setenv("PLMKR_SESSION_SECRET",  "session-secret-" + ("s" * 48))
    monkeypatch.setenv("PLMKR_IDENTITY_SECRET", "identity-secret-" + ("i" * 48))
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("SMS_OTP_DEV_BYPASS",  raising=False)
    for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SERVICE_SID", "TWILIO_VERIFY_SID"):
        monkeypatch.delenv(k, raising=False)
    for k, v in extra_env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, str(v))

    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)

    from fastapi.testclient import TestClient
    return TestClient(m.app, raise_server_exceptions=False), m


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_malformed_twilio_token_returns_503(monkeypatch, tmp_path):
    """Malformed TWILIO_AUTH_TOKEN causes send-otp to return a sanitized 503 (names only, no values)."""
    client, _ = _build_client(
        monkeypatch, tmp_path,
        TWILIO_ACCOUNT_SID="AC" + "a" * 32,
        TWILIO_AUTH_TOKEN="not-32-hex",
        TWILIO_VERIFY_SERVICE_SID="VA" + "a" * 32,
    )
    resp = client.post(
        "/api/auth/send-otp",
        json={"phone": "+15559990000"},
        headers={"X-API-Key": _PLMKR_KEY},
    )
    assert resp.status_code == 503
    detail = resp.json().get("detail")
    assert detail["code"] == "auth_not_configured"
    assert "not-32-hex" not in resp.text
    assert "AC" + "a" * 32 not in resp.text


def test_malformed_token_does_not_store_otp(monkeypatch, tmp_path):
    """No pending verification is stored when provider config validation fails."""
    client, m = _build_client(
        monkeypatch, tmp_path,
        TWILIO_ACCOUNT_SID="AC" + "a" * 32,
        TWILIO_AUTH_TOKEN="bad",
        TWILIO_VERIFY_SERVICE_SID="VA" + "a" * 32,
    )
    phone = "+15559990001"
    # Clear any leftover state from previous test
    m._otp_store.clear()

    client.post("/api/auth/send-otp", json={"phone": phone}, headers={"X-API-Key": _PLMKR_KEY})

    assert m._normalize_phone(phone) not in m._otp_store, (
        "pending verification must not be stored when Twilio config validation fails"
    )


def test_sms_otp_dev_bypass_on_railway_calls_sys_exit(monkeypatch, tmp_path):
    """SMS_OTP_DEV_BYPASS=true + RAILWAY_ENVIRONMENT set → sys.exit(1)."""
    monkeypatch.setenv("SMS_OTP_DEV_BYPASS",  "true")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("APP_BASE_URL",        "https://test.railway.app")
    monkeypatch.setenv("DB_PATH",             str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL",        "")
    monkeypatch.setenv("AUDIO_CACHE_DIR",     str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR",         str(tmp_path / "artists"))
    monkeypatch.setenv("ANTHROPIC_API_KEY",   "sk-test")

    with pytest.raises(SystemExit) as exc_info:
        with patch("whisper.load_model", return_value=MagicMock()):
            import main as m
            importlib.reload(m)

    assert exc_info.value.code == 1


def test_sms_otp_dev_bypass_in_local_dev_returns_000000(monkeypatch, tmp_path):
    """SMS_OTP_DEV_BYPASS=true in local dev → send-otp returns 200 with dev code."""
    client, _ = _build_client(monkeypatch, tmp_path, SMS_OTP_DEV_BYPASS="true")
    resp = client.post(
        "/api/auth/send-otp",
        json={"phone": "+15559991234"},
        headers={"X-API-Key": _PLMKR_KEY},
    )
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


def test_verify_otp_succeeds_after_dev_bypass_with_000000(monkeypatch, tmp_path):
    """After bypass send-otp, verify-otp accepts code 000000."""
    client, _ = _build_client(monkeypatch, tmp_path, SMS_OTP_DEV_BYPASS="true")
    phone = "+15559992222"
    client.post("/api/auth/send-otp", json={"phone": phone}, headers={"X-API-Key": _PLMKR_KEY})

    resp = client.post(
        "/api/auth/verify-otp",
        json={"phone": phone, "code": "000000"},
        headers={"X-API-Key": _PLMKR_KEY},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("valid") is True
    assert data["artist_id"].startswith("artist_") and data["access_token"]


def test_bypass_disabled_normal_flow_returns_503_when_twilio_unconfigured(monkeypatch, tmp_path):
    """Without bypass and without valid Twilio config, send-otp returns 503."""
    client, _ = _build_client(
        monkeypatch, tmp_path,
        SMS_OTP_DEV_BYPASS=None,
        TWILIO_ACCOUNT_SID=None,
        TWILIO_AUTH_TOKEN=None,
        TWILIO_VERIFY_SERVICE_SID=None,
        TWILIO_VERIFY_SID=None,
    )
    resp = client.post(
        "/api/auth/send-otp",
        json={"phone": "+15559993333"},
        headers={"X-API-Key": _PLMKR_KEY},
    )
    assert resp.status_code == 503


def test_sms_otp_dev_bypass_with_verify_sid_calls_sys_exit(monkeypatch, tmp_path):
    """SMS_OTP_DEV_BYPASS=true + TWILIO_VERIFY_SERVICE_SID set → sys.exit(1): bypass never shadows Verify."""
    monkeypatch.setenv("SMS_OTP_DEV_BYPASS",         "true")
    monkeypatch.setenv("TWILIO_VERIFY_SERVICE_SID",  "VA" + "a" * 32)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.setenv("DB_PATH",             str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL",        "")
    monkeypatch.setenv("AUDIO_CACHE_DIR",     str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR",         str(tmp_path / "artists"))
    monkeypatch.setenv("ANTHROPIC_API_KEY",   "sk-test")

    with pytest.raises(SystemExit) as exc_info:
        with patch("whisper.load_model", return_value=MagicMock()):
            import main as m
            importlib.reload(m)

    assert exc_info.value.code == 1
