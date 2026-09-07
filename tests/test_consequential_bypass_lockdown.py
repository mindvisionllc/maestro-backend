import asyncio
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import booking_service
import pitch_service
import pr_service
import social_service


def _client(router):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_direct_gmail_send_is_a_tripwire_and_never_calls_provider(monkeypatch):
    called = {"value": False}

    async def forbidden(*args, **kwargs):
        called["value"] = True
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(pitch_service, "send_email", forbidden)
    response = _client(pitch_service.router).post(
        "/api/gmail/send",
        json={"artist_id": "artist-a", "to": "a@example.com", "subject": "S", "body": "B"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "durable_operation_required"
    assert called["value"] is False


def test_pitch_batch_is_blocked_before_generation_or_send(monkeypatch):
    monkeypatch.setattr(pitch_service, "generate_pitch_email", AsyncMock(side_effect=AssertionError("generation reached")))
    monkeypatch.setattr(pitch_service, "send_email", AsyncMock(side_effect=AssertionError("send reached")))
    response = _client(pitch_service.router).post(
        "/api/pitches/batch",
        json={"artist_id": "artist-a", "curator_ids": ["c1"], "track_metadata": {}},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "durable_operation_required"


def test_pr_batch_is_blocked_before_generation_or_send(monkeypatch):
    monkeypatch.setattr(pr_service, "generate_pr_email", AsyncMock(side_effect=AssertionError("generation reached")))
    response = _client(pr_service.router).post(
        "/api/pr-outreach/batch",
        json={"artist_id": "artist-a", "contact_ids": ["c1"], "release_context": {}},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "durable_operation_required"


def test_booking_batch_is_blocked_before_generation_or_send(monkeypatch):
    monkeypatch.setattr(booking_service, "generate_booking_email", AsyncMock(side_effect=AssertionError("generation reached")))
    response = _client(booking_service.router).post(
        "/api/booking-inquiries/batch",
        json={"artist_id": "artist-a", "contact_ids": ["c1"], "show_context": {}},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "durable_operation_required"


def test_social_batch_draft_mode_still_works(monkeypatch, tmp_path):
    monkeypatch.setattr(social_service, "_DB_PATH", tmp_path / "social.db")
    social_service.init_social_db()
    monkeypatch.setattr(social_service, "_load_artist_data", lambda artist_id: {"artist_name": "Test"})
    monkeypatch.setattr(
        social_service,
        "generate_social_post",
        AsyncMock(return_value={
            "content": "draft",
            "suggested_media_prompt": "",
            "optimal_posting_window": "",
        }),
    )
    req = social_service.BatchPostRequest(
        artist_id="artist-a", platforms=["instagram"], posts_per_platform=1, schedule_buffer=False
    )
    result = asyncio.run(social_service.schedule_posts(req))
    assert result["generated"] == 1
    assert result["scheduled_via_buffer"] == 0


def test_social_batch_buffer_mode_is_blocked_before_generation(monkeypatch):
    monkeypatch.setattr(
        social_service,
        "generate_social_post",
        AsyncMock(side_effect=AssertionError("generation reached")),
    )
    req = social_service.BatchPostRequest(
        artist_id="artist-a",
        platforms=["instagram"],
        posts_per_platform=1,
        schedule_buffer=True,
        buffer_profile_ids=["profile-1"],
    )
    try:
        asyncio.run(social_service.schedule_posts(req))
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 409
        assert exc.detail["code"] == "durable_operation_required"
    else:
        raise AssertionError("Buffer bypass should be blocked")


def test_legacy_followup_routes_cannot_send_provider_messages(monkeypatch):
    forbidden = AsyncMock(side_effect=AssertionError("provider path reached"))
    monkeypatch.setattr(pitch_service, "send_email", forbidden)

    cases = (
        (pitch_service.router, "/api/pitches/followups/queue"),
        (pr_service.router, "/api/pr-outreach/followups/queue"),
        (booking_service.router, "/api/booking-inquiries/followups/queue"),
    )
    for router, path in cases:
        response = _client(router).post(path, params={"artist_id": "artist-a"})
        assert response.status_code == 409, (path, response.text)
        assert response.json()["detail"]["code"] == "durable_operation_required"

    forbidden.assert_not_awaited()
