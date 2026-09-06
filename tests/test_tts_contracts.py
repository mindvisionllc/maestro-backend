import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def tts_client(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR", str(tmp_path / "artists"))
    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)
        m._cancelled_calls.clear()
        yield TestClient(m.app), m


def test_tts_status_shape(tts_client):
    client, _ = tts_client
    response = client.get("/api/tts/status")
    assert response.status_code == 200
    assert set(response.json()) == {"ready", "engine"}


def test_tts_synth_success_shape(tts_client, monkeypatch):
    client, m = tts_client
    monkeypatch.setattr(m, "tts", AsyncMock(return_value=b"wav"))
    response = client.post(
        "/api/tts/synth",
        json={"text": "hello", "voice": "am_onyx", "call_id": "call-1"},
    )
    assert response.status_code == 200
    assert response.json() == {"audio": "d2F2"}


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "", "voice": "am_onyx", "call_id": "call-1"},
        {"text": "hello", "voice": "", "call_id": "call-1"},
        {"text": "hello", "voice": "am_onyx", "call_id": "x" * 129},
    ],
)
def test_tts_synth_rejects_invalid_inputs_without_provider_call(tts_client, monkeypatch, payload):
    client, m = tts_client
    provider = AsyncMock(return_value=b"must-not-run")
    monkeypatch.setattr(m, "tts", provider)
    response = client.post("/api/tts/synth", json=payload)
    assert response.status_code == 422
    provider.assert_not_called()


def test_tts_cancel_requires_bounded_call_id(tts_client):
    client, _ = tts_client
    assert client.post("/api/tts/cancel", json={"call_id": ""}).status_code == 422
    assert client.post("/api/tts/cancel", json={"call_id": "x" * 129}).status_code == 422


def test_tts_pre_cancel_returns_cancelled_without_provider_call(tts_client, monkeypatch):
    client, m = tts_client
    provider = AsyncMock(return_value=b"must-not-run")
    monkeypatch.setattr(m, "tts", provider)
    assert client.post("/api/tts/cancel", json={"call_id": "call-2"}).status_code == 200
    response = client.post(
        "/api/tts/synth",
        json={"text": "hello", "voice": "am_onyx", "call_id": "call-2"},
    )
    assert response.json() == {"audio": None, "cancelled": True}
    provider.assert_not_called()


def test_expired_cancellation_is_purged(tts_client, monkeypatch):
    client, m = tts_client
    m._cancelled_calls["expired"] = 0
    monkeypatch.setattr(m, "tts", AsyncMock(return_value=b"wav"))
    response = client.post(
        "/api/tts/synth",
        json={"text": "hello", "voice": "am_onyx", "call_id": "active"},
    )
    assert response.status_code == 200
    assert "expired" not in m._cancelled_calls


def test_tts_cancellation_registry_has_hard_cap(tts_client, monkeypatch):
    client, m = tts_client
    monkeypatch.setattr(m, "_TTS_MAX_CANCELLED_CALLS", 2)
    for call_id in ("oldest", "middle", "newest"):
        response = client.post("/api/tts/cancel", json={"call_id": call_id})
        assert response.status_code == 200
    assert len(m._cancelled_calls) == 2
    assert "oldest" not in m._cancelled_calls