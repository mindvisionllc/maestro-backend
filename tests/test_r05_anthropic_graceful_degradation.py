"""
R-05 — ANTHROPIC_API_KEY graceful degradation.

When the key is absent, app must:
  - boot without crashing
  - return HTTP 503 (not 500) on AI-dependent routes
  - report anthropic_available: false in /api/admin/health/deep

Run with:  python3 -m pytest tests/test_r05_anthropic_graceful_degradation.py -v
"""

import importlib
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _load_app(monkeypatch, *, with_key: bool, tmp_path):
    if with_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DB_PATH",         str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL",    "")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR",     str(tmp_path / "artists"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)
        return TestClient(m.app)


def test_app_boots_without_anthropic_key(monkeypatch, tmp_path):
    """App must not crash at startup when ANTHROPIC_API_KEY is absent."""
    client = _load_app(monkeypatch, with_key=False, tmp_path=tmp_path)
    resp = client.get("/health")
    assert resp.status_code == 200


def test_chat_stream_returns_503_without_key(monkeypatch, tmp_path):
    """POST /api/chat_stream must return 503, not crash, when key is absent."""
    client = _load_app(monkeypatch, with_key=False, tmp_path=tmp_path)
    resp = client.post("/api/chat_stream", json={
        "agent_id":  "puppet-master",
        "message":   "Hello",
        "artist_id": "test-artist",
        "history":   "[]",
        "tts":       False,
    })
    assert resp.status_code == 503, (
        f"Expected 503 when key absent, got {resp.status_code}. "
        "Add ANTHROPIC_AVAILABLE guard to /api/chat_stream."
    )
    assert "ANTHROPIC_API_KEY" in resp.json().get("detail", ""), (
        "503 response should mention ANTHROPIC_API_KEY in detail"
    )


def test_chat_stream_greet_succeeds_without_key(monkeypatch, tmp_path):
    """The static __greet__ branch makes no LLM call and must succeed (200,
    with the greeting text streamed) even when ANTHROPIC_API_KEY is absent —
    the greeting is not AI-dependent, so it must not share the guard above."""
    client = _load_app(monkeypatch, with_key=False, tmp_path=tmp_path)
    resp = client.post("/api/chat_stream", json={
        "agent_id":  "puppet-master",
        "message":   "__greet__",
        "artist_id": "test-artist",
        "history":   "[]",
        "tts":       False,
    })
    assert resp.status_code == 200, (
        f"Expected 200 for the static greeting even without a key, got {resp.status_code}: {resp.text}"
    )
    assert '"type": "text"' in resp.text
    assert '"type": "done"' in resp.text


def test_handoff_returns_503_without_key(monkeypatch, tmp_path):
    """POST /api/handoff must return 503, not crash, when key is absent."""
    client = _load_app(monkeypatch, with_key=False, tmp_path=tmp_path)
    resp = client.post("/api/handoff", data={
        "agent_id": "lex-cipher",
        "history":  "[]",
        "tts":      "false",
    })
    assert resp.status_code == 503, (
        f"Expected 503 when key absent, got {resp.status_code}. "
        "Add ANTHROPIC_AVAILABLE guard to /api/handoff."
    )


def test_health_deep_reports_anthropic_unavailable(monkeypatch, tmp_path):
    """Deep health must report anthropic_available: false when key is absent."""
    client = _load_app(monkeypatch, with_key=False, tmp_path=tmp_path)
    resp = client.get("/api/admin/health/deep")
    assert resp.status_code == 200
    data = resp.json()
    assert "anthropic_available" in data, (
        "anthropic_available field missing from /api/admin/health/deep"
    )
    assert data["anthropic_available"] is False


def test_health_deep_reports_anthropic_available_when_key_set(monkeypatch, tmp_path):
    """Deep health must report anthropic_available: true when key is present."""
    client = _load_app(monkeypatch, with_key=True, tmp_path=tmp_path)
    resp = client.get("/api/admin/health/deep")
    assert resp.status_code == 200
    assert resp.json()["anthropic_available"] is True
