"""
Twilio Verify OTP migration — focused tests with a mocked Twilio client only.

No real provider call is ever made: main._build_twilio_client is replaced with a
fake whose verify.v2.services(<sid>) returns a scripted service double.
"""

import importlib
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from twilio.base.exceptions import TwilioRestException


_PLMKR_KEY = "verify-test-api-key"
_SESSION_SECRET = "session-secret-" + ("s" * 48)
_IDENTITY_SECRET = "identity-secret-" + ("i" * 48)
_ACCOUNT_SID = "AC" + "b" * 32
_AUTH_TOKEN = "c" * 32
_VERIFY_SID = "VA" + "d" * 32
_TWILIO_KEYS = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_VERIFY_SERVICE_SID", "TWILIO_VERIFY_SID")

# Shape of the leaked text seen on-device before the migration (ANSI + request path + docs URL).
_RAW_SDK_TEXT = (
    "\x1b[31m\x1b[49mHTTP Error\x1b[0m \x1b[37mYour request was:\x1b[0m\n"
    "POST /Accounts/" + _ACCOUNT_SID + "/Messages.json\n"
    "Unable to create record: The 'From' number +15550001234 is not a valid phone number\n"
    "More information may be available here:\nhttps://www.twilio.com/docs/errors/21212"
)


class FakeService:
    """Scripted stand-in for client.verify.v2.services(<sid>)."""

    def __init__(self, send_status="pending", check_status="approved", send_exc=None, check_exc=None):
        self.send_status = send_status
        self.check_status = check_status
        self.send_exc = send_exc
        self.check_exc = check_exc
        self.verifications = MagicMock()
        self.verification_checks = MagicMock()
        self.verifications.create.side_effect = self._send
        self.verification_checks.create.side_effect = self._check

    def _send(self, **kwargs):
        if self.send_exc:
            raise self.send_exc
        return MagicMock(status=self.send_status, sid="VE" + "0" * 32)

    def _check(self, **kwargs):
        if self.check_exc:
            raise self.check_exc
        return MagicMock(status=self.check_status)


class FakeTwilio:
    """Records constructor args and service SID lookups; never touches the network."""

    def __init__(self, service):
        self.service = service
        self.constructed_with = []
        self.services_requested = []

    def build(self, account_sid, auth_token):
        self.constructed_with.append((account_sid, auth_token))
        client = MagicMock()
        client.verify.v2.services.side_effect = self._services
        return client

    def _services(self, sid):
        self.services_requested.append(sid)
        return self.service


def _rest_error(status, code, msg=_RAW_SDK_TEXT):
    return TwilioRestException(status, "https://verify.twilio.com/v2/Services/" + _VERIFY_SID, msg=msg, code=code)


@pytest.fixture()
def verify_app(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "verify.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio"))
    monkeypatch.setenv("ARTISTS_DIR", str(tmp_path / "artists"))
    monkeypatch.setenv("PLMKR_API_KEY", _PLMKR_KEY)
    monkeypatch.setenv("PLMKR_SESSION_SECRET", _SESSION_SECRET)
    monkeypatch.setenv("PLMKR_IDENTITY_SECRET", _IDENTITY_SECRET)
    monkeypatch.setenv("PLMKR_OTP_SEND_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("PLMKR_OTP_MAX_SENDS_PER_WINDOW", "100")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", _ACCOUNT_SID)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", _AUTH_TOKEN)
    monkeypatch.setenv("TWILIO_VERIFY_SERVICE_SID", _VERIFY_SID)
    monkeypatch.delenv("TWILIO_VERIFY_SID", raising=False)
    monkeypatch.delenv("SMS_OTP_DEV_BYPASS", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)

    with patch("whisper.load_model", return_value=MagicMock()):
        import main
        importlib.reload(main)

    main._otp_store.clear()
    main._otp_send_history.clear()
    yield main, monkeypatch


def _client(main):
    return TestClient(main.app, raise_server_exceptions=False)


def _install(main, monkeypatch, service):
    fake = FakeTwilio(service)
    monkeypatch.setattr(main, "_build_twilio_client", fake.build)
    return fake


def _send(client, phone):
    return client.post("/api/auth/send-otp", json={"phone": phone}, headers={"X-API-Key": _PLMKR_KEY})


def _verify(client, phone, code):
    return client.post("/api/auth/verify-otp", json={"phone": phone, "code": code}, headers={"X-API-Key": _PLMKR_KEY})


def _assert_sanitized(resp, phone_digits="4165551234"):
    body = resp.text
    assert "\x1b" not in body
    assert "/Accounts/" not in body and "Messages.json" not in body
    assert _ACCOUNT_SID not in body and _AUTH_TOKEN not in body and _VERIFY_SID not in body
    assert "twilio" not in body.lower()
    assert "+1" + phone_digits not in body and phone_digits not in body
    assert "15550001234" not in body
    assert "Traceback" not in body


# ── Phone normalization ────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("4165551234", "+14165551234"),              # 10-digit NANP → +1
    (" (416) 555-1234 ", "+14165551234"),        # user formatting
    ("14165551234", "+14165551234"),             # 11-digit NANP beginning with 1
    ("1 416 555 1234", "+14165551234"),
    ("+14165551234", "+14165551234"),            # already valid E.164
    ("+1 (416) 555-1234", "+14165551234"),
    ("+44 20 7946 0958", "+442079460958"),       # non-NANP E.164
])
def test_normalize_phone_canonical_forms(verify_app, raw, expected):
    main, _ = verify_app
    assert main._normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "abc", "416-555-ABCD", "555-1234", "41655512", "0165551234", "1165551234",
    "24165551234", "+0441234567", "+1234", "+" + "9" * 16, "4165551234x1", "++14165551234",
    "416.555.1234 ext 2",
])
def test_normalize_phone_rejects_malformed(verify_app, raw):
    main, _ = verify_app
    with pytest.raises(main.PhoneNormalizationError):
        main._normalize_phone(raw)


def test_send_otp_rejects_malformed_number_without_provider_call(verify_app):
    main, monkeypatch = verify_app
    fake = _install(main, monkeypatch, FakeService())
    resp = _send(_client(main), "555-12AB")
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_phone"
    assert fake.service.verifications.create.call_count == 0


# ── Verify send / check wiring ─────────────────────────────────────────────────

def test_send_uses_configured_service_and_normalized_number(verify_app):
    main, monkeypatch = verify_app
    fake = _install(main, monkeypatch, FakeService(send_status="pending"))
    resp = _send(_client(main), "(416) 555-1234")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "message": "Code sent"}
    assert fake.constructed_with == [(_ACCOUNT_SID, _AUTH_TOKEN)]
    assert fake.services_requested == [_VERIFY_SID]
    fake.service.verifications.create.assert_called_once_with(to="+14165551234", channel="sms")
    assert "+14165551234" in main._otp_store
    assert main._otp_store["+14165551234"]["otp"] is None      # no locally generated code
    assert "+14165551234" in main._otp_send_history          # limits keyed by canonical number


def test_compat_alias_twilio_verify_sid_is_accepted(verify_app):
    main, monkeypatch = verify_app
    monkeypatch.delenv("TWILIO_VERIFY_SERVICE_SID")
    monkeypatch.setenv("TWILIO_VERIFY_SID", _VERIFY_SID)
    fake = _install(main, monkeypatch, FakeService())
    assert _send(_client(main), "4165551234").status_code == 200
    assert fake.services_requested == [_VERIFY_SID]


def test_check_uses_service_normalized_number_and_code_then_issues_signed_session(verify_app):
    main, monkeypatch = verify_app
    fake = _install(main, monkeypatch, FakeService(check_status="approved"))
    client = _client(main)
    assert _send(client, "4165551234").status_code == 200
    resp = _verify(client, "+1 416-555-1234", " 123456 ")
    assert resp.status_code == 200, resp.text
    fake.service.verification_checks.create.assert_called_once_with(to="+14165551234", code="123456")
    data = resp.json()
    assert data["valid"] is True
    assert data["artist_id"].startswith("artist_")
    assert data["access_token"] and data["token_type"] == "Bearer"
    assert main.decode_session(data["access_token"])["sub"] == data["artist_id"]
    assert "+14165551234" not in main._otp_store               # consumed on approval
    assert "4165551234" not in resp.text                       # full number never returned


def test_same_canonical_number_across_entry_styles_shares_pending_state(verify_app):
    main, monkeypatch = verify_app
    _install(main, monkeypatch, FakeService(check_status="approved"))
    client = _client(main)
    assert _send(client, "14165551234").status_code == 200
    first = _verify(client, "+14165551234", "123456").json()
    assert first["valid"] is True
    assert _send(client, "+1 (416) 555 1234").status_code == 200
    second = _verify(client, "416 555 1234", "123456").json()
    assert second["valid"] is True
    assert second["artist_id"] == first["artist_id"]           # canonical artist identity is stable


@pytest.mark.parametrize("status", ["pending", "canceled", "expired", "", None, "APPROVED "])
def test_unapproved_check_results_do_not_authenticate(verify_app, status):
    main, monkeypatch = verify_app
    _install(main, monkeypatch, FakeService(check_status=status))
    client = _client(main)
    assert _send(client, "4165551234").status_code == 200
    resp = _verify(client, "4165551234", "123456")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["error_code"] == "invalid_code"
    assert "access_token" not in data and "artist_id" not in data


def test_verify_without_pending_send_never_calls_provider(verify_app):
    main, monkeypatch = verify_app
    fake = _install(main, monkeypatch, FakeService(check_status="approved"))
    resp = _verify(_client(main), "4165551234", "123456")
    assert resp.json()["valid"] is False
    assert resp.json()["error_code"] == "expired_code"
    assert fake.service.verification_checks.create.call_count == 0


def test_malformed_code_counts_as_attempt_without_provider_call(verify_app):
    main, monkeypatch = verify_app
    fake = _install(main, monkeypatch, FakeService(check_status="approved"))
    client = _client(main)
    _send(client, "4165551234")
    for bad in ("", "12", "abcdef", "12345678901"):
        data = _verify(client, "4165551234", bad).json()
        assert data["valid"] is False and data["error_code"] == "invalid_code"
    assert fake.service.verification_checks.create.call_count == 0
    assert main._otp_store["+14165551234"]["attempts"] == 4


# ── Protections ────────────────────────────────────────────────────────────────

def test_send_cooldown_and_window_limit_remain_enforced(verify_app):
    main, monkeypatch = verify_app
    fake = _install(main, monkeypatch, FakeService())
    monkeypatch.setattr(main, "OTP_SEND_COOLDOWN_SECONDS", 60)
    client = _client(main)
    assert _send(client, "4165551234").status_code == 200
    second = _send(client, "+1 416 555 1234")                  # same canonical key
    assert second.status_code == 429
    assert second.json()["detail"]["code"] == "send_cooldown"
    assert int(second.headers["Retry-After"]) > 0
    assert second.json()["detail"]["retry_after"] == int(second.headers["Retry-After"])
    assert fake.service.verifications.create.call_count == 1  # cooldown checked before provider

    monkeypatch.setattr(main, "OTP_SEND_COOLDOWN_SECONDS", 0)
    monkeypatch.setattr(main, "OTP_MAX_SENDS_PER_WINDOW", 2)
    assert _send(client, "4165551234").status_code == 200
    third = _send(client, "4165551234")
    assert third.status_code == 429
    assert third.json()["detail"]["code"] == "send_limit"
    assert fake.service.verifications.create.call_count == 2


def test_verification_attempt_limit_invalidates_pending_code(verify_app):
    main, monkeypatch = verify_app
    service = FakeService(check_status="pending")
    fake = _install(main, monkeypatch, service)
    monkeypatch.setattr(main, "OTP_MAX_VERIFY_ATTEMPTS", 3)
    client = _client(main)
    _send(client, "4165551234")
    for i in range(2):
        assert _verify(client, "4165551234", "111111").json()["error_code"] == "invalid_code"
    locked = _verify(client, "4165551234", "111111").json()
    assert locked["valid"] is False and locked["error_code"] == "too_many_attempts"

    service.check_status = "approved"                            # provider would now approve …
    after = _verify(client, "4165551234", "123456").json()
    assert after["valid"] is False                               # … but the local lockout holds
    assert after["error_code"] == "expired_code"
    assert fake.service.verification_checks.create.call_count == 3


def test_duplicate_submission_after_approval_does_not_reissue_session(verify_app):
    main, monkeypatch = verify_app
    fake = _install(main, monkeypatch, FakeService(check_status="approved"))
    client = _client(main)
    _send(client, "4165551234")
    first = _verify(client, "4165551234", "123456").json()
    assert first["valid"] is True
    replay = _verify(client, "4165551234", "123456").json()
    assert replay["valid"] is False and "access_token" not in replay
    assert fake.service.verification_checks.create.call_count == 1


def test_expired_pending_verification_is_rejected(verify_app):
    main, monkeypatch = verify_app
    fake = _install(main, monkeypatch, FakeService(check_status="approved"))
    client = _client(main)
    _send(client, "4165551234")
    main._otp_store["+14165551234"]["expires"] = time.time() - 1
    data = _verify(client, "4165551234", "123456").json()
    assert data["valid"] is False and data["error_code"] == "expired_code"
    assert fake.service.verification_checks.create.call_count == 0


def test_locally_generated_code_is_never_accepted_when_verify_is_configured(verify_app):
    main, monkeypatch = verify_app
    fake = _install(main, monkeypatch, FakeService(check_status="approved"))
    client = _client(main)
    main._otp_store["+14165551234"] = {"otp": "000000", "expires": time.time() + 600, "attempts": 0, "provider": "dev"}
    data = _verify(client, "4165551234", "000000").json()
    assert data["valid"] is False and "access_token" not in data
    assert fake.service.verification_checks.create.call_count == 0
    assert "+14165551234" not in main._otp_store


# ── Fail-closed configuration ──────────────────────────────────────────────────

@pytest.mark.parametrize("key,value", [
    ("TWILIO_ACCOUNT_SID", None), ("TWILIO_ACCOUNT_SID", "SK" + "b" * 32), ("TWILIO_ACCOUNT_SID", "AC123"),
    ("TWILIO_AUTH_TOKEN", None), ("TWILIO_AUTH_TOKEN", "short"), ("TWILIO_AUTH_TOKEN", "g" * 32),
    ("TWILIO_VERIFY_SERVICE_SID", None), ("TWILIO_VERIFY_SERVICE_SID", "MG" + "d" * 32), ("TWILIO_VERIFY_SERVICE_SID", "VA12"),
])
def test_missing_or_malformed_provider_config_fails_closed(verify_app, key, value):
    main, monkeypatch = verify_app
    fake = _install(main, monkeypatch, FakeService(check_status="approved"))
    if value is None:
        monkeypatch.delenv(key)
    else:
        monkeypatch.setenv(key, value)
    client = _client(main)
    resp = _send(client, "4165551234")
    assert resp.status_code == 503
    assert resp.json()["detail"] == {"code": "auth_not_configured", "message": main._OTP_ERR_MESSAGES["auth_not_configured"]}
    _assert_sanitized(resp)
    assert fake.constructed_with == []
    assert "+14165551234" not in main._otp_store

    # A stale pending entry must not become an acceptance path either.
    main._otp_store["+14165551234"] = {"otp": None, "expires": time.time() + 600, "attempts": 0, "provider": "verify"}
    check = _verify(client, "4165551234", "123456")
    assert check.status_code == 503
    assert check.json()["detail"]["code"] == "auth_not_configured"
    assert fake.constructed_with == []


def test_from_number_is_not_required(verify_app):
    main, monkeypatch = verify_app
    monkeypatch.delenv("TWILIO_PHONE_NUMBER", raising=False)
    monkeypatch.delenv("TWILIO_FROM_NUMBER", raising=False)
    fake = _install(main, monkeypatch, FakeService())
    assert _send(_client(main), "4165551234").status_code == 200
    assert "from_" not in fake.service.verifications.create.call_args.kwargs


# ── Provider failure sanitization ──────────────────────────────────────────────

@pytest.mark.parametrize("exc,expected_code,expected_status", [
    (_rest_error(400, 60200), "invalid_phone", 400),
    (_rest_error(429, 60203), "send_limit", 429),
    (_rest_error(401, 20003), "auth_not_configured", 503),
    (_rest_error(404, 20404), "auth_not_configured", 503),
    (_rest_error(500, 20500), "provider_unavailable", 503),
    (ConnectionError(_RAW_SDK_TEXT), "provider_unavailable", 503),
    (TimeoutError("timed out contacting " + _ACCOUNT_SID), "provider_unavailable", 503),
])
def test_send_provider_failures_return_sanitized_errors(verify_app, exc, expected_code, expected_status):
    main, monkeypatch = verify_app
    _install(main, monkeypatch, FakeService(send_exc=exc))
    resp = _send(_client(main), "4165551234")
    assert resp.status_code == expected_status
    detail = resp.json()["detail"]
    assert detail["code"] == expected_code
    assert detail["message"] == main._OTP_ERR_MESSAGES[expected_code]
    _assert_sanitized(resp)
    assert "+14165551234" not in main._otp_store               # a failed send is never a "sent" state
    assert "+14165551234" not in main._otp_send_history


def test_unexpected_send_status_is_not_reported_as_sent(verify_app):
    main, monkeypatch = verify_app
    _install(main, monkeypatch, FakeService(send_status="canceled"))
    resp = _send(_client(main), "4165551234")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "provider_unavailable"
    assert "+14165551234" not in main._otp_store


@pytest.mark.parametrize("exc,expected_code,expected_http,keeps_pending", [
    (_rest_error(404, 20404), "expired_code", 200, False),
    (_rest_error(429, 60202), "too_many_attempts", 200, False),
    (_rest_error(400, 60200), "invalid_code", 200, True),
    (_rest_error(500, 20500), "provider_unavailable", 503, True),
    (ConnectionError(_RAW_SDK_TEXT), "provider_unavailable", 503, True),
])
def test_check_provider_failures_return_sanitized_errors(verify_app, exc, expected_code, expected_http, keeps_pending):
    main, monkeypatch = verify_app
    _install(main, monkeypatch, FakeService(check_exc=exc))
    client = _client(main)
    _send(client, "4165551234")
    resp = _verify(client, "4165551234", "123456")
    assert resp.status_code == expected_http
    body = resp.json()
    if expected_http == 200:
        assert body["valid"] is False and body["error_code"] == expected_code
    else:
        assert body["detail"]["code"] == expected_code
    assert "access_token" not in body
    _assert_sanitized(resp)
    assert ("+14165551234" in main._otp_store) is keeps_pending


def test_provider_failure_logs_carry_no_secrets_or_raw_text(verify_app, caplog):
    main, monkeypatch = verify_app
    _install(main, monkeypatch, FakeService(send_exc=_rest_error(500, 20500)))
    with caplog.at_level("DEBUG"):
        _send(_client(main), "4165551234")
    joined = "\n".join(f"{r.getMessage()} {getattr(r, 'category', '')} {getattr(r, 'phone', '')}" for r in caplog.records)
    assert "otp_provider_failure" in joined
    for forbidden in ("\x1b", "/Accounts/", _ACCOUNT_SID, _AUTH_TOKEN, _VERIFY_SID, "+14165551234", "15550001234", "twilio.com"):
        assert forbidden not in joined


# ── Approved-response contract: signed session or fail closed ──────────────────

def test_approved_response_carries_complete_signed_session_contract(verify_app):
    main, monkeypatch = verify_app
    _install(main, monkeypatch, FakeService(check_status="approved"))
    client = _client(main)
    _send(client, "4165551234")
    data = _verify(client, "4165551234", "123456").json()
    assert data["valid"] is True
    assert isinstance(data["artist_id"], str) and data["artist_id"].startswith("artist_")
    assert isinstance(data["access_token"], str) and data["access_token"]
    assert data["token_type"] == "Bearer"
    assert isinstance(data["expires_in"], int) and isinstance(data["expires_at"], int)
    assert data["profile"]["artist_id"] == data["artist_id"]
    assert data["returning_artist"] is False and data["profile"]["onboarded"] is False
    # The session is immediately usable through the existing bearer authorization path.
    saved = client.post(
        "/api/artist/save",
        json={"artist_id": data["artist_id"], "name": "Test Artist", "onboarded": True},
        headers={"Authorization": "Bearer " + data["access_token"]},
    )
    assert saved.status_code == 200, saved.status_code


@pytest.mark.parametrize("missing", ["PLMKR_SESSION_SECRET", "PLMKR_IDENTITY_SECRET"])
def test_missing_identity_secret_never_yields_bare_valid_true(verify_app, missing):
    main, monkeypatch = verify_app
    fake = _install(main, monkeypatch, FakeService(check_status="approved"))
    client = _client(main)
    _send(client, "4165551234")                                   # configured at send time
    monkeypatch.delenv(missing)
    resp = _verify(client, "4165551234", "123456")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "auth_not_configured"
    assert "valid" not in resp.json() and "access_token" not in resp.text
    _assert_sanitized(resp)
    assert fake.service.verification_checks.create.call_count == 0   # no provider check spent
    assert "+14165551234" in main._otp_store                        # pending state kept for retry

    # Send is also refused before any provider call when the session secrets are absent.
    main._otp_send_history.clear()
    send = _send(client, "4165551234")
    assert send.status_code == 503 and send.json()["detail"]["code"] == "auth_not_configured"
    assert fake.service.verifications.create.call_count == 1


def test_short_identity_secret_fails_closed_after_approval(verify_app):
    main, monkeypatch = verify_app
    _install(main, monkeypatch, FakeService(check_status="approved"))
    client = _client(main)
    _send(client, "4165551234")
    monkeypatch.setenv("PLMKR_SESSION_SECRET", "too-short")     # present but unusable
    resp = _verify(client, "4165551234", "123456")
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "auth_not_configured"
    assert "access_token" not in resp.text and "too-short" not in resp.text
