import asyncio
import importlib
import sqlite3
from unittest.mock import AsyncMock, MagicMock

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
    async def fake_profiles(artist_id):
        return [
            {"id": "profile-1", "service": "instagram"},
            {"id": "profile-2", "service": "twitter"},
        ]
    monkeypatch.setattr(social_service, "_buffer_list_profiles", fake_profiles)
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


def _approve(svc, operation):
    approved = svc.approve_operation(operation["id"], artist_id=operation["artist_id"])
    return svc.mark_operation_ready(approved["id"], artist_id=operation["artist_id"])


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


def test_operation_events_preserve_artist_approval_and_dispatch_history(services, monkeypatch):
    svc, pitch, _, _ = services

    async def fake_send(*args, **kwargs):
        return {"message_id": "event-msg", "status": "sent"}

    monkeypatch.setattr(pitch, "send_email", fake_send)
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="event-history"))
    approved = svc.approve_operation(operation["id"], artist_id=operation["artist_id"])
    ready = svc.mark_operation_ready(approved["id"], artist_id=operation["artist_id"])
    result = asyncio.run(svc.execute_operation(ready["id"], artist_id=operation["artist_id"]))

    assert [event["event_type"] for event in result["events"]] == [
        "created",
        "artist_approved",
        "execution_ready",
        "dispatch_started",
        "dispatch_finished",
    ]
    assert result["events"][3]["from_status"] == "pending"
    assert result["events"][3]["to_status"] == "executing"
    assert result["events"][-1]["to_status"] == "succeeded"
    assert result["events"][-1]["metadata"]["provider_reference"] == "event-msg"


def test_operation_events_are_artist_scoped(services):
    svc, _, _, _ = services
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="event-scope"))
    other_request = _gmail_request(key="event-scope-other")
    other_request["artist_id"] = "artist-2"
    other, _ = svc._create_or_get_operation(**other_request)

    assert operation["events"][0]["event_type"] == "created"
    assert other["events"][0]["event_type"] == "created"
    assert svc._list_operation_events(operation["id"], "artist-2") == []


def test_operation_history_is_artist_scoped_bounded_and_filterable(services):
    svc, _, _, _ = services
    first, _ = svc._create_or_get_operation(**_gmail_request(key="history-old"))
    newest_request = _gmail_request(key="history-newest")
    newest, _ = svc._create_or_get_operation(**newest_request)
    second_request = _gmail_request(key="history-new")
    second_request["artist_id"] = "artist-2"
    second, _ = svc._create_or_get_operation(**second_request)

    _approve(svc, first)
    conn = sqlite3.connect(str(svc._DB_PATH))
    conn.execute(
        "UPDATE execution_operations SET status='succeeded', updated_at=? WHERE id=?",
        ("2099-01-01T00:00:00+00:00", newest["id"]),
    )
    conn.execute(
        "UPDATE execution_operations SET updated_at=? WHERE id=?",
        ("2026-01-01T00:00:00+00:00", first["id"]),
    )
    conn.commit()
    conn.close()

    history = svc._list_operations("artist-1")
    assert [item["id"] for item in history] == [newest["id"], first["id"]]
    assert [item["id"] for item in svc._list_operations("artist-1", limit=1)] == [newest["id"]]
    assert svc._list_operations("artist-2")[0]["id"] == second["id"]
    assert svc._list_operations("artist-1", status="succeeded")[0]["status"] == "succeeded"

    with pytest.raises(HTTPException) as exc:
        svc._list_operations("artist-1", limit=101)
    assert exc.value.status_code == 422


def test_operation_history_route_is_scoped_and_exposes_bounded_contract(services, monkeypatch):
    svc, _, _, _ = services
    owned, _ = svc._create_or_get_operation(**_gmail_request(key="route-owned"))
    other_request = _gmail_request(key="route-other")
    other_request["artist_id"] = "artist-2"
    other, _ = svc._create_or_get_operation(**other_request)

    def scoped_artist(_request, claimed_artist_id):
        if claimed_artist_id != "artist-1":
            from artist_identity import ArtistAuthError
            raise ArtistAuthError("Artist resource not found")
        return claimed_artist_id

    monkeypatch.setattr(svc, "require_artist_scope", scoped_artist)
    response = svc.list_operations("artist-1", limit=50, request=object())
    assert [item["id"] for item in response["operations"]] == [owned["id"]]
    assert response["limit"] == 50

    with pytest.raises(HTTPException) as exc:
        svc.list_operations("artist-1", limit=101, request=object())
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        svc.list_operations("artist-1", limit=50, status="not-a-status", request=object())
    assert exc.value.status_code == 422

    with pytest.raises(HTTPException) as exc:
        svc.get_operation(other["id"], "artist-1", request=object())
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        svc._list_operations("artist-1", status="not-a-status")
    assert exc.value.status_code == 422


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
    _approve(svc, operation)
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
    _approve(svc, operation)
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


def test_gmail_result_without_message_id_is_unknown_and_not_retryable(services, monkeypatch):
    svc, pitch, _, _ = services

    async def missing_reference(*args, **kwargs):
        return {"status": "sent"}

    monkeypatch.setattr(pitch, "send_email", missing_reference)
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="gmail-missing-reference"))
    _approve(svc, operation)

    result = asyncio.run(svc.execute_operation(operation["id"]))

    assert result["status"] == "unknown"
    assert result["reconciliation_required"] is True
    assert result["error_code"] == "provider_reference_missing"
    assert result["provider_result"] == {"status": "sent"}


def test_startup_moves_interrupted_execution_to_unknown(services):
    svc, _, _, db = services
    operation, _ = svc._create_or_get_operation(**_gmail_request())
    _approve(svc, operation)
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
    assert recovered["events"][-1]["event_type"] == "process_interrupted"
    assert recovered["events"][-1]["from_status"] == "executing"


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
    discovery = AsyncMock()

    async def fake_schedule(*args, **kwargs):
        calls.append((args, kwargs))
        return {"id": "buffer-1", "status": "buffer_queued", "mocked": True}

    monkeypatch.setattr(social, "_buffer_schedule_post", fake_schedule)
    monkeypatch.setattr(social, "_buffer_list_profiles", discovery)
    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="social-key",
        payload={"post_id": "post-1", "buffer_profile_ids": ["profile-1"]},
    )
    _approve(svc, operation)
    result = asyncio.run(svc.execute_operation(operation["id"]))

    assert result["status"] == "succeeded"
    assert result["provider_reference"] == "buffer-1"
    assert result["provider_result"]["mocked"] is True
    assert social._db_get_post("post-1")["status"] == "scheduled"
    assert social._db_get_post("post-1")["buffer_update_id"] == "buffer-1"
    assert len(calls) == 1
    discovery.assert_not_called()


def test_social_result_without_update_id_is_unknown_and_blocks_retry(services, monkeypatch):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-missing-reference",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Potentially scheduled",
        "status": "draft",
    })

    async def missing_reference(*args, **kwargs):
        return {"status": "buffer_queued"}

    monkeypatch.setattr(social, "_buffer_schedule_post", missing_reference)
    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="social-missing-reference",
        payload={"post_id": "post-missing-reference", "buffer_profile_ids": ["profile-1"]},
    )
    _approve(svc, operation)

    result = asyncio.run(svc.execute_operation(operation["id"]))

    assert result["status"] == "unknown"
    assert result["reconciliation_required"] is True
    assert result["error_code"] == "provider_reference_missing"
    assert result["provider_result"] == {"status": "buffer_queued"}
    assert social._db_get_post("post-missing-reference")["status"] == "scheduling"


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


def test_social_resource_conflict_does_not_disclose_other_artist_operation(services):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-cross-artist",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Artist-owned draft",
        "status": "draft",
    })
    first, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="artist-one-key",
        payload={"post_id": "post-cross-artist", "buffer_profile_ids": ["profile-1"]},
    )

    with pytest.raises(HTTPException) as exc:
        svc._create_or_get_operation(
            artist_id="artist-2",
            action_type="social.buffer.schedule",
            idempotency_key="artist-two-key",
            payload={"post_id": "post-cross-artist", "buffer_profile_ids": ["profile-1"]},
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == {
        "code": "resource_already_has_operation",
        "message": "This social post is already bound to an execution operation.",
    }
    assert first["id"] not in str(exc.value.detail)


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
    _approve(svc, operation)
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
    _approve(svc, operation)

    first = asyncio.run(svc.execute_operation(operation["id"]))
    assert first["status"] == "failed_retryable"
    assert social._db_get_post("post-retry")["status"] == "draft"

    second = asyncio.run(svc.execute_operation(operation["id"]))
    assert second["status"] == "succeeded"
    assert second["provider_reference"] == "buffer-retried"
    assert second["attempt_count"] == 2
    assert attempts["count"] == 2


def test_unapproved_operation_never_calls_provider(services, monkeypatch):
    svc, pitch, _, _ = services
    send = MagicMock()
    monkeypatch.setattr(pitch, "send_email", send)
    operation, _ = svc._create_or_get_operation(**_gmail_request())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.execute_operation(operation["id"]))
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "operation_not_approved"
    send.assert_not_called()
    assert svc._get_operation(operation["id"])["status"] == "pending"


def test_approved_operation_requires_final_readiness_before_provider(services, monkeypatch):
    svc, pitch, _, _ = services
    send = MagicMock()
    monkeypatch.setattr(pitch, "send_email", send)
    operation, _ = svc._create_or_get_operation(**_gmail_request())
    svc.approve_operation(operation["id"], artist_id=operation["artist_id"])

    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.execute_operation(operation["id"]))
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "operation_not_ready"
    send.assert_not_called()
    assert svc._get_operation(operation["id"])["approved_at"]
    assert svc._get_operation(operation["id"])["ready_at"] is None


@pytest.mark.parametrize(
    "recipient",
    ["", "not-an-email", "a@example.com,b@example.com", "Name <a@example.com>", "a@example.com\nBcc:x@example.com"],
)
def test_invalid_gmail_recipient_rejected_before_ledger_insert(services, recipient):
    svc, _, _, db = services
    request = _gmail_request()
    request["payload"]["to"] = recipient
    with pytest.raises(HTTPException) as exc:
        svc._create_or_get_operation(**request)
    assert exc.value.status_code == 422
    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM execution_operations").fetchone()[0] == 0
    conn.close()


def test_social_profile_binding_is_canonical_and_validated(services, monkeypatch):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-binding",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Bound profiles",
        "status": "draft",
    })
    request = {
        "artist_id": "artist-1",
        "action_type": "social.buffer.schedule",
        "idempotency_key": "binding-key",
        "payload": {
            "post_id": "post-binding",
            "buffer_profile_ids": ["profile-2", "profile-1", "profile-2"],
        },
    }
    operation, _ = svc._create_or_get_operation(**request)
    replay, created = svc._create_or_get_operation(
        **{
            **request,
            "payload": {
                "post_id": "post-binding",
                "buffer_profile_ids": ["profile-1", "profile-2"],
            },
        }
    )
    assert created is False
    assert replay["id"] == operation["id"]
    assert operation["profile_binding"] == ["profile-1", "profile-2"]

    async def only_one_profile(artist_id):
        return [{"id": "profile-1", "service": "instagram"}]

    schedule = MagicMock()
    monkeypatch.setattr(social, "_BUFFER_LIVE", True)
    monkeypatch.setattr(social, "_buffer_list_profiles", only_one_profile)
    monkeypatch.setattr(social, "_buffer_schedule_post", schedule)
    _approve(svc, operation)
    failed = asyncio.run(svc.execute_operation(operation["id"]))
    assert failed["status"] == "failed"
    assert failed["error_code"] == "invalid_buffer_profile"
    schedule.assert_not_called()
