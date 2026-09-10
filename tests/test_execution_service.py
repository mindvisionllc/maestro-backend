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


def test_operation_input_limits_reject_before_persisting_ledger_state(services):
    svc, _, _, db = services

    with pytest.raises(HTTPException) as exc:
        svc._create_or_get_operation(**_gmail_request(key="k" * (svc.MAX_IDENTIFIER_LENGTH + 1)))
    assert exc.value.status_code == 422
    assert exc.value.detail == {
        "code": "field_too_long",
        "field": "idempotency_key",
        "max_length": svc.MAX_IDENTIFIER_LENGTH,
    }

    with pytest.raises(HTTPException) as exc:
        svc._create_or_get_operation(
            **_gmail_request(key="oversized-body", body="x" * (svc.MAX_EMAIL_BODY_LENGTH + 1))
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == {
        "code": "field_too_long",
        "field": "body",
        "max_length": svc.MAX_EMAIL_BODY_LENGTH,
    }

    conn = sqlite3.connect(str(db))
    assert conn.execute("SELECT COUNT(*) FROM execution_operations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM execution_operation_events").fetchone()[0] == 0
    conn.close()


def test_social_operation_limits_duplicate_and_excessive_profile_ids(services):
    svc, _, _, _ = services

    with pytest.raises(HTTPException) as exc:
        svc._create_or_get_operation(
            artist_id="artist-1",
            action_type=svc.SOCIAL_SCHEDULE,
            idempotency_key="social-too-many-profiles",
            payload={
                "post_id": "post-1",
                "buffer_profile_ids": [f"profile-{i}" for i in range(svc.MAX_BUFFER_PROFILES + 1)],
            },
        )
    assert exc.value.detail["code"] == "invalid_buffer_profiles"

    operation, created = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type=svc.SOCIAL_SCHEDULE,
        idempotency_key="social-dedup-profiles",
        payload={
            "post_id": "post-1",
            "platform": "Instagram",
            "buffer_profile_ids": ["profile-2", "profile-1", "profile-1"],
        },
    )
    assert created is True
    assert operation["profile_binding"] == ["profile-1", "profile-2"]
    assert operation["payload"]["platform"] == "instagram"

    with pytest.raises(HTTPException) as exc:
        svc._create_or_get_operation(
            artist_id="artist-1",
            action_type=svc.SOCIAL_SCHEDULE,
            idempotency_key="social-unsupported-platform",
            payload={
                "post_id": "post-2",
                "platform": "mastodon",
                "buffer_profile_ids": ["profile-1"],
            },
        )
    assert exc.value.detail["code"] == "unsupported_social_platform"


def test_idempotency_lookup_recovers_operation_without_history_scan(services):
    svc, _, _, _ = services
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="lookup-key"))

    recovered = svc._get_operation_by_idempotency(
        operation["artist_id"], operation["action_type"], operation["idempotency_key"],
    )
    assert recovered["id"] == operation["id"]
    assert svc._get_operation_by_idempotency(
        "other-artist", operation["action_type"], operation["idempotency_key"],
    ) == {}


def test_artist_can_cancel_queued_operation_and_history_is_atomic(services):
    svc, _, _, _ = services
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="cancel-queued"))
    approved = svc.approve_operation(operation["id"], artist_id=operation["artist_id"])
    ready = svc.mark_operation_ready(approved["id"], artist_id=approved["artist_id"])

    canceled = svc.cancel_operation(ready["id"], artist_id=ready["artist_id"])

    assert canceled["status"] == "canceled"
    assert canceled["completed_at"]
    assert [event["event_type"] for event in canceled["events"]][-1] == "artist_canceled"
    assert canceled["events"][-1]["from_status"] == "pending"
    assert asyncio.run(svc.execute_operation(canceled["id"], artist_id=canceled["artist_id"]))["status"] == "canceled"


def test_cancel_rejects_in_flight_or_unknown_operations(services):
    svc, _, _, db = services
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="cancel-guard"))
    for status in ("executing", "unknown", "succeeded"):
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE execution_operations SET status=? WHERE id=?", (status, operation["id"]))
        conn.commit()
        conn.close()
        with pytest.raises(HTTPException) as exc:
            svc.cancel_operation(operation["id"], artist_id=operation["artist_id"])
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "operation_not_cancellable"


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


def test_creation_rolls_back_when_its_history_event_cannot_be_recorded(services, monkeypatch):
    svc, _, _, db = services
    original = svc._insert_operation_event

    def fail_history(*args, **kwargs):
        raise sqlite3.OperationalError("history unavailable")

    monkeypatch.setattr(svc, "_insert_operation_event", fail_history)
    with pytest.raises(sqlite3.OperationalError):
        svc._create_or_get_operation(**_gmail_request(key="atomic-create"))

    conn = sqlite3.connect(str(db))
    assert conn.execute(
        "SELECT COUNT(*) FROM execution_operations WHERE idempotency_key=?",
        ("atomic-create",),
    ).fetchone()[0] == 0
    conn.close()
    monkeypatch.setattr(svc, "_insert_operation_event", original)


def test_approval_rolls_back_when_history_event_cannot_be_recorded(services, monkeypatch):
    svc, _, _, _ = services
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="atomic-approval"))

    def fail_history(*args, **kwargs):
        raise sqlite3.OperationalError("history unavailable")

    monkeypatch.setattr(svc, "_insert_operation_event", fail_history)
    with pytest.raises(sqlite3.OperationalError):
        svc.approve_operation(operation["id"], artist_id=operation["artist_id"])

    current = svc._get_operation(operation["id"])
    assert current["approved_at"] is None
    assert [event["event_type"] for event in current["events"]] == ["created"]


def test_readiness_rolls_back_when_history_event_cannot_be_recorded(services, monkeypatch):
    svc, _, _, _ = services
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="atomic-ready"))
    approved = svc.approve_operation(operation["id"], artist_id=operation["artist_id"])

    def fail_history(*args, **kwargs):
        raise sqlite3.OperationalError("history unavailable")

    monkeypatch.setattr(svc, "_insert_operation_event", fail_history)
    with pytest.raises(sqlite3.OperationalError):
        svc.mark_operation_ready(approved["id"], artist_id=approved["artist_id"])

    current = svc._get_operation(operation["id"])
    assert current["ready_at"] is None
    assert [event["event_type"] for event in current["events"]] == ["created", "artist_approved"]


@pytest.mark.parametrize("reconciled, initial_status, final_status", [
    (False, "executing", "succeeded"),
    (True, "unknown", "unknown"),
])
def test_finish_rolls_back_status_when_history_event_cannot_be_recorded(
    services, monkeypatch, reconciled, initial_status, final_status,
):
    svc, _, _, db = services
    operation, _ = svc._create_or_get_operation(
        **_gmail_request(key=f"atomic-finish-{reconciled}")
    )
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE execution_operations SET status=? WHERE id=?",
        (initial_status, operation["id"]),
    )
    conn.commit()
    conn.close()

    def fail_history(*args, **kwargs):
        raise sqlite3.OperationalError("history unavailable")

    monkeypatch.setattr(svc, "_insert_operation_event", fail_history)
    with pytest.raises(sqlite3.OperationalError):
        svc._finish(
            operation["id"],
            final_status,
            provider_reference="provider-id" if not reconciled else None,
            reconciled=reconciled,
            expected_status=initial_status,
        )

    current = svc._get_operation(operation["id"])
    assert current["status"] == initial_status
    assert current["provider_reference"] is None
    assert [event["event_type"] for event in current["events"]] == ["created"]


def test_social_post_completion_and_operation_history_share_a_transaction(services, monkeypatch):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-atomic-completion",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Atomic completion",
        "status": "scheduling",
    })
    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="atomic-social-completion",
        payload={"post_id": "post-atomic-completion", "buffer_profile_ids": ["profile-1"]},
    )
    conn = sqlite3.connect(str(svc._DB_PATH))
    conn.execute(
        "UPDATE execution_operations SET status='executing' WHERE id=?",
        (operation["id"],),
    )
    conn.commit()
    conn.close()

    def fail_history(*args, **kwargs):
        raise sqlite3.OperationalError("history unavailable")

    monkeypatch.setattr(svc, "_insert_operation_event", fail_history)
    with pytest.raises(sqlite3.OperationalError):
        svc._finish(
            operation["id"],
            "succeeded",
            provider_reference="buffer-atomic",
            expected_status="executing",
            social_post_id="post-atomic-completion",
            social_post_reference="buffer-atomic",
        )

    current = svc._get_operation(operation["id"])
    post = social._db_get_post("post-atomic-completion")
    assert current["status"] == "executing"
    assert current["provider_reference"] is None
    assert post["status"] == "scheduling"
    assert post["buffer_update_id"] == ""


def test_social_post_completion_rolls_back_when_operation_guard_is_stale(services):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-stale-completion",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Stale completion",
        "status": "scheduling",
    })
    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="stale-social-completion",
        payload={"post_id": "post-stale-completion", "buffer_profile_ids": ["profile-1"]},
    )
    conn = sqlite3.connect(str(svc._DB_PATH))
    conn.execute(
        "UPDATE execution_operations SET status='executing' WHERE id=?",
        (operation["id"],),
    )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError, match="changed before completion"):
        svc._finish(
            operation["id"],
            "succeeded",
            provider_reference="buffer-stale",
            expected_status="pending",
            social_post_id="post-stale-completion",
            social_post_reference="buffer-stale",
        )

    current = svc._get_operation(operation["id"])
    post = social._db_get_post("post-stale-completion")
    assert current["status"] == "executing"
    assert current["provider_reference"] is None
    assert post["status"] == "scheduling"
    assert post["buffer_update_id"] == ""


def test_approval_and_readiness_are_idempotent_without_duplicate_events(services):
    svc, _, _, _ = services
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="idempotent-gates"))

    first_approval = svc.approve_operation(operation["id"], artist_id=operation["artist_id"])
    second_approval = svc.approve_operation(operation["id"], artist_id=operation["artist_id"])
    first_ready = svc.mark_operation_ready(operation["id"], artist_id=operation["artist_id"])
    second_ready = svc.mark_operation_ready(operation["id"], artist_id=operation["artist_id"])

    assert second_approval["approved_at"] == first_approval["approved_at"]
    assert second_ready["ready_at"] == first_ready["ready_at"]
    assert [event["event_type"] for event in second_ready["events"]] == [
        "created", "artist_approved", "execution_ready",
    ]


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


def test_operation_lookup_route_is_scoped_and_rejects_unsupported_actions(services, monkeypatch):
    svc, _, _, _ = services
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="route-lookup"))

    def scoped_artist(_request, claimed_artist_id):
        if claimed_artist_id != "artist-1":
            from artist_identity import ArtistAuthError
            raise ArtistAuthError("Artist resource not found")
        return claimed_artist_id

    monkeypatch.setattr(svc, "require_artist_scope", scoped_artist)
    recovered = svc.lookup_operation(
        "artist-1", operation["action_type"], operation["idempotency_key"], request=object(),
    )
    assert recovered["id"] == operation["id"]

    with pytest.raises(HTTPException) as exc:
        svc.lookup_operation("artist-1", "pitch.send", "route-lookup", request=object())
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "unsupported_action"

    with pytest.raises(HTTPException) as exc:
        svc.lookup_operation("artist-1", operation["action_type"], "missing", request=object())
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        svc.lookup_operation("artist-2", operation["action_type"], operation["idempotency_key"], request=object())
    assert exc.value.status_code == 404


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


@pytest.mark.parametrize(
    ("error", "error_code"),
    [
        ("Gmail is not connected", "reconciliation_unavailable"),
        ("Gmail lookup failed", "reconciliation_failed"),
    ],
)
def test_failed_gmail_reconciliation_remains_required_for_safe_retry(
    services, monkeypatch, error, error_code,
):
    svc, pitch, _, _ = services

    async def timed_out(*args, **kwargs):
        raise TimeoutError("response lost")

    monkeypatch.setattr(pitch, "send_email", timed_out)
    operation, _ = svc._create_or_get_operation(**_gmail_request(key=f"reconcile-{error_code}"))
    _approve(svc, operation)
    unknown = asyncio.run(svc.execute_operation(operation["id"]))

    if error_code == "reconciliation_unavailable":
        monkeypatch.setattr(
            svc,
            "_find_gmail_message_by_rfc822_id",
            lambda *args: (_ for _ in ()).throw(pitch.GmailNotConnected(error)),
        )
    else:
        monkeypatch.setattr(
            svc,
            "_find_gmail_message_by_rfc822_id",
            lambda *args: (_ for _ in ()).throw(RuntimeError(error)),
        )

    failed = asyncio.run(svc.reconcile_operation(operation["id"]))

    assert unknown["status"] == "unknown"
    assert failed["status"] == "unknown"
    assert failed["error_code"] == error_code
    assert failed["reconciliation_required"] is True
    assert failed["reconciled_at"] is None
    assert failed["events"][-1]["event_type"] == "reconciliation_failed"
    assert failed["events"][-1]["from_status"] == "unknown"


def test_unknown_outcome_records_attempt_completion_without_collapsing_reconciliation_time(
    services, monkeypatch,
):
    svc, pitch, _, _ = services

    async def timed_out(*args, **kwargs):
        raise TimeoutError("response lost")

    monkeypatch.setattr(pitch, "send_email", timed_out)
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="unknown-timing"))
    _approve(svc, operation)

    unknown = asyncio.run(svc.execute_operation(operation["id"]))

    assert unknown["status"] == "unknown"
    assert unknown["completed_at"]
    assert unknown["reconciled_at"] is None

    # A completed provider lookup that finds no message is a finished
    # reconciliation with an unknown outcome, unlike an unavailable/failed
    # lookup which must remain retryable.
    monkeypatch.setattr(svc, "_find_gmail_message_by_rfc822_id", lambda *args: None)
    reconciled = asyncio.run(svc.reconcile_operation(operation["id"]))

    assert reconciled["status"] == "unknown"
    assert reconciled["completed_at"] == unknown["completed_at"]
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


def test_social_platform_binding_fails_closed_before_provider_dispatch(services, monkeypatch):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-platform-mismatch",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Instagram-only draft",
        "status": "draft",
    })
    schedule = AsyncMock()
    monkeypatch.setattr(social, "_buffer_schedule_post", schedule)
    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="platform-mismatch",
        payload={
            "post_id": "post-platform-mismatch",
            "platform": "Twitter",
            "buffer_profile_ids": ["profile-2"],
        },
    )
    _approve(svc, operation)

    result = asyncio.run(svc.execute_operation(operation["id"]))

    assert result["status"] == "failed"
    assert result["error_code"] == "social_platform_mismatch"
    assert social._db_get_post("post-platform-mismatch")["status"] == "draft"
    schedule.assert_not_awaited()


def test_live_social_execution_requires_selected_profiles_to_match_platform(services, monkeypatch):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-profile-platform-mismatch",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Profile must match platform",
        "status": "draft",
    })
    monkeypatch.setattr(social, "_BUFFER_LIVE", True)
    schedule = AsyncMock(return_value={"id": "must-not-send"})
    monkeypatch.setattr(social, "_buffer_schedule_post", schedule)
    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="profile-platform-mismatch",
        payload={
            "post_id": "post-profile-platform-mismatch",
            "platform": "instagram",
            "buffer_profile_ids": ["profile-2"],
        },
    )
    _approve(svc, operation)

    result = asyncio.run(svc.execute_operation(operation["id"]))

    assert result["status"] == "failed"
    assert result["error_code"] == "buffer_profile_platform_mismatch"
    assert social._db_get_post("post-profile-platform-mismatch")["status"] == "draft"
    schedule.assert_not_awaited()


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

    assert exc.value.status_code == 404
    assert exc.value.detail == "Operation not found"
    assert first["id"] not in str(exc.value.detail)
    assert svc._get_operation_by_idempotency(
        "artist-2", "social.buffer.schedule", "artist-two-key",
    ) == {}


def test_social_operation_creation_rejects_existing_post_owned_by_another_artist(services):
    svc, _, social, db = services
    social._db_create_post({
        "id": "post-owned-by-one",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Artist-owned draft",
        "status": "draft",
    })

    with pytest.raises(HTTPException) as exc:
        svc._create_or_get_operation(
            artist_id="artist-2",
            action_type="social.buffer.schedule",
            idempotency_key="cross-artist-create",
            payload={"post_id": "post-owned-by-one", "buffer_profile_ids": ["profile-1"]},
        )

    assert exc.value.status_code == 404
    conn = sqlite3.connect(str(db))
    assert conn.execute(
        "SELECT COUNT(*) FROM execution_operations WHERE artist_id=?",
        ("artist-2",),
    ).fetchone()[0] == 0
    conn.close()


def test_operation_owner_mismatch_is_hidden(services):
    svc, _, _, _ = services
    operation, _ = svc._create_or_get_operation(**_gmail_request())

    with pytest.raises(HTTPException) as exc:
        asyncio.run(svc.execute_operation(operation["id"], artist_id="other-artist"))
    assert exc.value.status_code == 404


def test_combined_route_never_enters_execution_for_new_operation(services, monkeypatch):
    svc, _, _, _ = services
    monkeypatch.setattr(svc, "require_artist_scope", lambda _request, artist_id: artist_id)

    async def unexpected_execution(*args, **kwargs):
        raise AssertionError("new operations must not enter the execution path")

    monkeypatch.setattr(svc, "execute_operation", unexpected_execution)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            svc.create_and_execute_operation(
                svc.OperationRequest(**_gmail_request(key="combined-new")),
                request=object(),
            )
        )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "operation_not_approved"
    operation = svc._list_operations("artist-1")[0]
    assert operation["status"] == "pending"
    assert operation["approved_at"] is None


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


def test_buffer_restore_and_retryable_history_are_atomic(services, monkeypatch):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-atomic-retry",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Atomic retry recovery",
        "status": "draft",
    })

    async def unavailable(*args, **kwargs):
        raise social.BufferNotConnected("not connected")

    monkeypatch.setattr(social, "_buffer_schedule_post", unavailable)
    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="atomic-retryable-social",
        payload={"post_id": "post-atomic-retry", "buffer_profile_ids": ["profile-1"]},
    )
    _approve(svc, operation)

    original = svc._insert_operation_event

    def fail_history(*args, **kwargs):
        if len(args) >= 4 and args[3] == "dispatch_finished":
            raise sqlite3.OperationalError("history unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(svc, "_insert_operation_event", fail_history)
    with pytest.raises(sqlite3.OperationalError):
        asyncio.run(svc.execute_operation(operation["id"]))

    current = svc._get_operation(operation["id"])
    assert current["status"] == "executing"
    assert social._db_get_post("post-atomic-retry")["status"] == "scheduling"
    monkeypatch.setattr(svc, "_insert_operation_event", original)


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


def test_live_buffer_auth_expiry_is_retryable_and_leaves_social_draft_intact(services, monkeypatch):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-auth-expired",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Reconnect Buffer",
        "status": "draft",
    })

    async def expired_profiles(artist_id):
        raise social.BufferAuthExpired("Buffer authorization expired; reconnect Buffer before continuing")

    schedule = MagicMock()
    monkeypatch.setattr(social, "_BUFFER_LIVE", True)
    monkeypatch.setattr(social, "_buffer_list_profiles", expired_profiles)
    monkeypatch.setattr(social, "_buffer_schedule_post", schedule)

    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="auth-expired",
        payload={
            "post_id": "post-auth-expired",
            "platform": "instagram",
            "buffer_profile_ids": ["profile-1"],
        },
    )
    _approve(svc, operation)

    failed = asyncio.run(svc.execute_operation(operation["id"]))

    assert failed["status"] == "failed_retryable"
    assert failed["error_code"] == "BufferAuthExpired"
    assert "reconnect Buffer" in failed["error_detail"]
    assert social._db_get_post("post-auth-expired")["status"] == "draft"
    schedule.assert_not_called()

def test_buffer_schedule_auth_expiry_is_retryable_and_restores_social_draft(services, monkeypatch):
    svc, _, social, _ = services
    social._db_create_post({
        "id": "post-schedule-auth-expired",
        "artist_id": "artist-1",
        "platform": "instagram",
        "content": "Retry after reconnect",
        "status": "draft",
    })

    async def available_profiles(artist_id):
        return [{"id": "profile-1", "service": "instagram"}]

    async def expired_schedule(*args, **kwargs):
        raise social.BufferAuthExpired(
            "Buffer authorization expired; reconnect Buffer before continuing"
        )

    monkeypatch.setattr(social, "_BUFFER_LIVE", True)
    monkeypatch.setattr(social, "_buffer_list_profiles", available_profiles)
    monkeypatch.setattr(social, "_buffer_schedule_post", expired_schedule)

    operation, _ = svc._create_or_get_operation(
        artist_id="artist-1",
        action_type="social.buffer.schedule",
        idempotency_key="schedule-auth-expired",
        payload={
            "post_id": "post-schedule-auth-expired",
            "platform": "instagram",
            "buffer_profile_ids": ["profile-1"],
        },
    )
    _approve(svc, operation)

    failed = asyncio.run(svc.execute_operation(operation["id"]))

    assert failed["status"] == "failed_retryable"
    assert failed["error_code"] == "BufferAuthExpired"
    assert social._db_get_post("post-schedule-auth-expired")["status"] == "draft"


def test_operation_history_cursor_continues_without_duplicates_or_cross_artist_leak(services):
    svc, _, _, db = services
    first, _ = svc._create_or_get_operation(**_gmail_request(key="cursor-first"))
    second, _ = svc._create_or_get_operation(**_gmail_request(key="cursor-second"))
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE execution_operations SET updated_at=? WHERE id=?", ("2026-01-01T00:00:01+00:00", first["id"]))
    conn.execute("UPDATE execution_operations SET updated_at=? WHERE id=?", ("2026-01-01T00:00:02+00:00", second["id"]))
    conn.commit()
    conn.close()

    page = svc._list_operations("artist-1", limit=1)
    assert page[0]["id"] == second["id"]
    cursor = svc._operation_cursor(page[0])
    older = svc._list_operations("artist-1", limit=1, before=cursor)
    assert [item["id"] for item in older] == [first["id"]]

    with pytest.raises(HTTPException) as exc:
        svc._list_operations("artist-1", before="not-a-cursor")
    assert exc.value.status_code == 422
    assert exc.value.detail == {"code": "invalid_operation_cursor"}


def test_provider_diagnostics_are_bounded_before_ledger_persistence():
    svc = __import__("execution_service")
    small = {"message_id": "msg-1", "status": "sent"}
    assert svc._bounded_provider_result(small) == small

    oversized = {"body": "x" * (svc.MAX_PROVIDER_RESULT_BYTES + 1)}
    bounded = svc._bounded_provider_result(oversized)
    assert bounded == {
        "truncated": True,
        "reason": "provider_result_too_large",
        "max_bytes": svc.MAX_PROVIDER_RESULT_BYTES,
    }


def test_error_details_are_bounded_without_changing_short_recovery_messages():
    svc = __import__("execution_service")
    assert svc._bounded_error_detail("Reconnect Buffer before retrying.") == "Reconnect Buffer before retrying."

    bounded = svc._bounded_error_detail("e" * (svc.MAX_ERROR_DETAIL_LENGTH + 100))
    assert len(bounded) == svc.MAX_ERROR_DETAIL_LENGTH
    assert bounded.endswith("… [detail truncated]")


def test_operation_event_responses_are_bounded_without_deleting_durable_history(services):
    svc, _, _, _ = services
    operation, _ = svc._create_or_get_operation(**_gmail_request(key="bounded-events"))
    conn = sqlite3.connect(str(svc._DB_PATH))
    for index in range(svc.MAX_OPERATION_EVENTS + 7):
        conn.execute(
            """INSERT INTO execution_operation_events
               (operation_id, artist_id, event_type, from_status, to_status, metadata, occurred_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                operation["id"], operation["artist_id"], f"event-{index}",
                "pending", "pending", "{}", f"2026-01-01T00:00:{index:02d}+00:00",
            ),
        )
    conn.commit()
    conn.close()

    recovered = svc._get_operation(operation["id"])

    assert len(recovered["events"]) == svc.MAX_OPERATION_EVENTS
    assert recovered["events"][0]["event_type"] == "event-7"
    assert recovered["events"][-1]["event_type"] == f"event-{svc.MAX_OPERATION_EVENTS + 6}"
    assert recovered["events_truncated"] is True
    assert svc._operation_event_count(operation["id"], operation["artist_id"]) == svc.MAX_OPERATION_EVENTS + 8
