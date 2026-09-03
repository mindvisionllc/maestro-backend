import asyncio
import importlib
import sqlite3
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException


@pytest.fixture()
def services(tmp_path, monkeypatch):
    db = tmp_path / "operations.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("DATABASE_URL", "")

    import pitch_service
    import social_service
    import execution_service

    pitch_service = importlib.reload(pitch_service)
    social_service = importlib.reload(social_service)
    execution_service = importlib.reload(execution_service)
    pitch_service.init_pitch_db()
    social_service.init_social_db()
    execution_service.init_execution_db()
    return execution_service, pitch_service, social_service, db


def _gmail_request(key="gmail-key", body="Hello"):
    return {
        "artist_id": "artist-1",
        "action_type": "gmail.send",
        "idempotency_key": key,
        "payload": {
            "to": "listener@example.com",
            "subject": "A subject",
            "body": body,
        },
    }


def test_create_is_idempotent_and_conflicting_payload_is_rejected(services):
    svc, _, _, _ = services
    request = _gmail_request()
    first, created = svc._create_or_get_operation(**request)
    second, created_again = svc._create_or_get_operation(**request)

    assert created is True
    assert created_again is False
    assert second["id"] == first["id"]
    assert second["status"] == "pending"

    with pytest.raises(HTTPException) as exc:
        svc._create_or_get_operation(**_gmail_request(body="Different"))
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "idempotency_conflict"


@pytest.mark.parametrize(
    "action_type",
    ["pitch.send", "pr.send", "booking.send", "social.batch", "release.execute"],
)
def test_ambiguous_and_batch_actions_remain_unsupported(services, action_type):
    svc, _, _, _ = services
    with pytest.raises(HTTPException) as exc:
        svc._create_or_get_operation(
            artist_id="artist-1",
            action_type=action_type,
            idempotency_key="blocked",
            payload={},
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "unsupported_action"


def test_gmail_success_records_provider_result_and_replay_does_not_resend(services, monkeypatch):
    svc, pitch, _, _ = services
    calls = []

    async def fake_send(artist_id, to, subject, body, *, message_id_header=None):
        calls.append((artist_id, to, subject, body, message_id_header))
        return {"message_id": "msg-1", "thread_id": "thread-1", "status": "sent"}

    monkeypatch.setattr(pitch, "send_email", fake_send)
    operation, _ = svc._create_or_get_operation(**_gmail_request())
    result = asyncio.run(svc.execute_operation(operation["id"]))
    replay = asyncio.run(svc.execute_operation(operation["id"]))

    assert result["status"] == "succeeded"
    assert result["provider_reference"] == "msg-1"
    assert result["provider_result"]["thread_id"] == "thread-1"
    assert calls[0][4] == operation["correlation_key"]
    assert replay["id"] == result["id"]
    assert len(calls) == 1


def test_unknown_gmail_outcome_reconciles_by_message_id(services, monkeypatch):
    svc, pitch, _, _ = services

    async def timed_out(*args, **kwargs):
        raise TimeoutError("response lost")

    monkeypatch.setattr(pitch, "send_email", timed_out)
    operation, _ = svc._create_or_get_operation(**_gmail_request())
    unknown = asyncio.run(svc.execute_operation(operation["id"]))
    assert unknown["status"] == "unknown"
    assert unknown["reconciliation_required"] is True

    monkeypatch.setattr(
        svc,
        "_find_gmail_message_by_rfc822_id",
        lambda artist_id, message_id: {"id": "msg-found", "threadId": "thread-found"},
    )
    reconciled = asyncio.run(svc.reconcile_operation(operation["id"]))
    assert reconciled["status"] == "succeeded"
    assert reconciled["provider_reference"] == "msg-found"
    assert reconciled["provider_result"]["reconciled"] is True
    assert reconciled["reconciled_at"]


def test_startup_moves_interrupted_execution_to_unknown(services):
    svc, _, _, db = services
    operation, _ = svc._create_or_get_operation(**_gmail_request())
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE execution_operations SET status='executing' WHERE id=?",
        (operation["id"],),
    )
    conn.commit()
    conn.close()

    svc.init_execution_db()
    recovered = svc._get_operation(operation["id"])
    assert recovered["status"] == "unknown"
    assert recovered["error_code"] == "process_interrupted"


def test_single_social_post_success_updates_post_and_records_buffer_result(services, monkeypatch):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-1",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "A scheduled post",
        "status": "draft",
        "scheduled_at": "2026-09-04T12:00:00+00:00",
    })
    calls = []

    async def fake_schedule(*args, **kwargs):
        calls.append((args, kwargs))
        return {"id": "buffer-1", "status": "buffer_queued", "mocked": True}

    monkeypatch.setattr(social, "_buffer_schedule_post", fake_schedule)
    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="social-key",
        payload={"post_id": "post-1", "buffer_profile_ids": ["profile-1"]},
    )
    result = asyncio.run(svc.execute_operation(operation["id"]))

    assert result["status"] == "succeeded"
    assert result["provider_reference"] == "buffer-1"
    assert result["provider_result"]["mocked"] is True
    assert social._db_get_post("post-1")["status"] == "scheduled"
    assert social._db_get_post("post-1")["buffer_update_id"] == "buffer-1"
    assert len(calls) == 1


def test_social_post_can_only_be_bound_to_one_operation(services):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-unique",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Only once",
        "status": "draft",
    })
    payload = {"post_id": "post-unique", "buffer_profile_ids": ["profile-1"]}
    first, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="first-key",
        payload=payload,
    )

    with pytest.raises(HTTPException) as exc:
        svc._create_or_get_operation(
            artist_id="artist-1",
            action_type="social.buffer.schedule",
            idempotency_key="second-key",
            payload=payload,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "resource_already_has_operation"
    assert exc.value.detail["operation_id"] == first["id"]


def test_operation_owner_mismatch_is_hidden(services):
    svc, _, _, _ = services
    operation, _ = svc._create_or_get_operation(**_gmail_request())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.execute_operation(operation["id"], artist_id="other-artist"))
    assert exc.value.status_code == 404


def test_reconcile_does_not_race_an_inflight_execution(services, monkeypatch):
    svc, _, _, db = services
    operation, _ = svc._create_or_get_operation(**_gmail_request())
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE execution_operations SET status='executing' WHERE id=?",
        (operation["id"],),
    )
    conn.commit()
    conn.close()
    called = {"value": False}

    def unexpected_lookup(*args):
        called["value"] = True
        return {"id": "should-not-be-used"}

    monkeypatch.setattr(svc, "_find_gmail_message_by_rfc822_id", unexpected_lookup)
    result = asyncio.run(svc.reconcile_operation(operation["id"]))
    assert result["status"] == "executing"
    assert called["value"] is False


def test_unknown_social_outcome_requires_manual_reconciliation_and_never_retries(services, monkeypatch):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-2",
        "artist_id": "artist-1",
        "platform": "twitter",
        "content": "Potentially accepted",
        "status": "draft",
    })
    calls = {"count": 0}

    async def timed_out(*args, **kwargs):
        calls["count"] += 1
        raise TimeoutError("response lost")

    monkeypatch.setattr(social, "_buffer_schedule_post", timed_out)
    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="social-unknown",
        payload={"post_id": "post-2", "buffer_profile_ids": ["profile-2"]},
    )
    unknown = asyncio.run(svc.execute_operation(operation["id"]))
    replay = asyncio.run(svc.execute_operation(operation["id"]))
    reconciled = asyncio.run(svc.reconcile_operation(operation["id"]))

    assert unknown["status"] == "unknown"
    assert replay["status"] == "unknown"
    assert reconciled["status"] == "unknown"
    assert reconciled["error_code"] == "manual_reconciliation_required"
    assert calls["count"] == 1


def test_buffer_not_connected_can_retry_same_operation_safely(services, monkeypatch):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-retry",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Retry after connecting",
        "status": "draft",
    })
    attempts = {"count": 0}

    async def connect_then_succeed(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise social.BufferNotConnected("not connected")
        return {"id": "buffer-retried", "status": "buffer_queued", "mocked": True}

    monkeypatch.setattr(social, "_buffer_schedule_post", connect_then_succeed)
    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="retryable-social",
        payload={"post_id": "post-retry", "buffer_profile_ids": ["profile-1"]},
    )

    first = asyncio.run(svc.execute_operation(operation["id"]))
    assert first["status"] == "failed_retryable"
    assert social._db_get_post("post-retry")["status"] == "draft"

    second = asyncio.run(svc.execute_operation(operation["id"]))
    assert second["status"] == "succeeded"
    assert second["provider_reference"] == "buffer-retried"
    assert second["attempt_count"] == 2
    assert attempts["count"] == 2