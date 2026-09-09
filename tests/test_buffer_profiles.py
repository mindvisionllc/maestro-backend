
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import social_service as svc


def _mock_response(status_code=200, body=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json = MagicMock(return_value=body)
    return resp


def test_buffer_list_profiles_requires_connection(monkeypatch):
    monkeypatch.setattr(svc, "_load_buffer_tokens", lambda artist_id: {})

    with pytest.raises(svc.BufferNotConnected):
        asyncio.run(svc._buffer_list_profiles("artist-1"))


@pytest.mark.parametrize("stored_tokens", [None, [], "token", {}, {"access_token": "   "}, {"access_token": 123}])
def test_buffer_token_loader_treats_malformed_saved_data_as_disconnected(monkeypatch, stored_tokens):
    monkeypatch.setattr(svc, "_load_artist_data", lambda artist_id: {"buffer_tokens": stored_tokens})

    assert svc._load_buffer_tokens("artist-1") == {}


def test_buffer_token_loader_strips_and_does_not_expose_extra_saved_fields(monkeypatch):
    monkeypatch.setattr(
        svc,
        "_load_artist_data",
        lambda artist_id: {"buffer_tokens": {
            "access_token": " token-123 ",
            "refresh_token": "must-not-be-used",
            "stored_at": "2026-09-09T12:00:00+00:00",
        }},
    )

    assert svc._load_buffer_tokens("artist-1") == {"access_token": "token-123"}


def test_buffer_list_profiles_fetches_profiles(monkeypatch):
    monkeypatch.setattr(
        svc,
        "_load_buffer_tokens",
        lambda artist_id: {"access_token": "token-123"},
    )

    profiles = [
        {"id": "prof-1", "service": "instagram", "formatted_username": "@artist"},
        {"id": "prof-2", "service": "facebook", "formatted_username": "Artist"},
    ]
    resp = _mock_response(200, profiles)

    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("social_service.httpx.AsyncClient", return_value=cm):
        result = asyncio.run(svc._buffer_list_profiles("artist-1"))

    assert result == profiles
    client.get.assert_awaited_once_with(
        svc._BUFFER_PROFILES_URL,
        params={"access_token": "token-123"},
    )


def test_buffer_list_profiles_rejects_non_200(monkeypatch):
    monkeypatch.setattr(
        svc,
        "_load_buffer_tokens",
        lambda artist_id: {"access_token": "token-123"},
    )

    resp = _mock_response(500, {}, "server error")

    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("social_service.httpx.AsyncClient", return_value=cm):
        with pytest.raises(RuntimeError, match="Buffer API returned 500"):
            asyncio.run(svc._buffer_list_profiles("artist-1"))


def test_buffer_list_profiles_rejects_invalid_shape(monkeypatch):
    monkeypatch.setattr(
        svc,
        "_load_buffer_tokens",
        lambda artist_id: {"access_token": "token-123"},
    )

    resp = _mock_response(200, {"unexpected": "object"})

    client = MagicMock()
    client.get = AsyncMock(return_value=resp)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("social_service.httpx.AsyncClient", return_value=cm):
        with pytest.raises(RuntimeError, match="invalid profile response"):
            asyncio.run(svc._buffer_list_profiles("artist-1"))


def test_buffer_list_profiles_rejects_profile_without_id(monkeypatch):
    monkeypatch.setattr(
        svc,
        "_load_buffer_tokens",
        lambda artist_id: {"access_token": "token-123"},
    )

    resp = _mock_response(200, [{"service": "instagram"}])
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("social_service.httpx.AsyncClient", return_value=cm):
        with pytest.raises(RuntimeError, match="invalid profile response"):
            asyncio.run(svc._buffer_list_profiles("artist-1"))


def test_buffer_list_profiles_returns_only_safe_profile_fields(monkeypatch):
    monkeypatch.setattr(
        svc,
        "_load_buffer_tokens",
        lambda artist_id: {"access_token": "token-123"},
    )

    resp = _mock_response(200, [{
        "id": " prof-1 ",
        "service": " instagram ",
        "formatted_username": " @artist ",
        "access_token": "should-not-leak",
        "metadata": {"private": True},
    }])
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("social_service.httpx.AsyncClient", return_value=cm):
        assert asyncio.run(svc._buffer_list_profiles("artist-1")) == [{
            "id": "prof-1",
            "service": "instagram",
            "formatted_username": "@artist",
        }]


def test_buffer_list_profiles_maps_transport_failure_to_runtime_error(monkeypatch):
    monkeypatch.setattr(
        svc,
        "_load_buffer_tokens",
        lambda artist_id: {"access_token": "token-123"},
    )

    client = MagicMock()
    client.get = AsyncMock(side_effect=svc.httpx.ConnectTimeout("timed out"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch("social_service.httpx.AsyncClient", return_value=cm):
        with pytest.raises(RuntimeError, match="temporarily unavailable"):
            asyncio.run(svc._buffer_list_profiles("artist-1"))
