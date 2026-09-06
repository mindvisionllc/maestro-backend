"""
Unit tests for B-06 — /api/transcribe upload size limit and extension allowlist.

Tests exercise _ALLOWED_AUDIO_EXTS and MAX_UPLOAD_BYTES without loading Whisper.
Run with:  python3 -m pytest tests/test_transcribe.py -v
"""

import io
import importlib
import os
import threading
import time
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    monkeypatch.setenv("MAX_UPLOAD_SIZE", str(10 * 1024 * 1024))  # 10 MB in tests
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR",     str(tmp_path / "artists"))

    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)
        yield TestClient(m.app)


def _upload(client, filename: str, content: bytes) -> object:
    return client.post(
        "/api/transcribe",
        files={"audio": (filename, io.BytesIO(content), "audio/mpeg")},
    )


# ── Extension allowlist ───────────────────────────────────────────────────────

def test_allowed_extension_mp3(client):
    mock_model = MagicMock()
    mock_model.transcribe.return_value = {"text": "hello"}
    with patch("main.get_whisper", return_value=mock_model):
        resp = _upload(client, "clip.mp3", b"fake-audio-data")
    assert resp.status_code == 200
    assert resp.json()["text"] == "hello"


@pytest.mark.parametrize("filename", ["clip.mp3", "clip.m4a", "clip.wav", "clip.ogg", "clip.webm"])
def test_allowed_extensions_pass(client, filename):
    mock_model = MagicMock()
    mock_model.transcribe.return_value = {"text": "ok"}
    with patch("main.get_whisper", return_value=mock_model):
        resp = _upload(client, filename, b"x")
    assert resp.status_code == 200


@pytest.mark.parametrize("filename", ["clip.exe", "clip.mp4", "clip.avi", "clip.txt"])
def test_disallowed_extensions_rejected(client, filename):
    resp = _upload(client, filename, b"x")
    assert resp.status_code == 400
    assert "Unsupported audio format" in resp.json()["detail"]


# ── Size limit ────────────────────────────────────────────────────────────────

def test_oversized_upload_rejected(client, monkeypatch):
    import main as m
    limit = m.MAX_UPLOAD_BYTES
    oversized = b"A" * (limit + 1)
    resp = _upload(client, "big.mp3", oversized)
    assert resp.status_code == 413
    assert "limit" in resp.json()["detail"]


def test_exactly_at_limit_accepted(client, monkeypatch):
    import main as m
    limit = m.MAX_UPLOAD_BYTES
    mock_model = MagicMock()
    mock_model.transcribe.return_value = {"text": "fine"}
    with patch("main.get_whisper", return_value=mock_model):
        resp = _upload(client, "ok.mp3", b"B" * limit)
    assert resp.status_code == 200


def test_env_override_max_upload_size(monkeypatch, tmp_path):
    """MAX_UPLOAD_SIZE env var controls the limit."""
    monkeypatch.setenv("MAX_UPLOAD_SIZE", str(1024))  # 1 KB
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR",     str(tmp_path / "artists"))

    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)
        assert m.MAX_UPLOAD_BYTES == 1024
        client = TestClient(m.app)

    resp = client.post(
        "/api/transcribe",
        files={"audio": ("x.mp3", io.BytesIO(b"X" * 1025), "audio/mpeg")},
    )
    assert resp.status_code == 413


def test_provider_failure_cleans_up_temp_file(client):
    mock_model = MagicMock()
    mock_model.transcribe.side_effect = RuntimeError("provider detail must not leak")
    with patch("main.get_whisper", return_value=mock_model), \
         patch("main.os.unlink", wraps=os.unlink) as unlink:
        resp = _upload(client, "clip.mp3", b"fake-audio-data")
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Transcription unavailable"
    unlink.assert_called_once()


def test_provider_timeout_is_bounded_and_cleans_up(client, monkeypatch):
    import main as m
    worker_done = threading.Event()

    def slow_transcribe(path):
        time.sleep(0.05)
        worker_done.set()
        return {"text": "late"}

    real_unlink = os.unlink

    def unlink_after_worker(path):
        assert worker_done.is_set()
        real_unlink(path)

    mock_model = MagicMock()
    mock_model.transcribe.side_effect = slow_transcribe
    monkeypatch.setattr(m, "TRANSCRIBE_TIMEOUT_SECONDS", 0.001)
    with patch("main.get_whisper", return_value=mock_model), \
         patch("main.os.unlink", side_effect=unlink_after_worker) as unlink:
        resp = _upload(client, "clip.mp3", b"fake-audio-data")
        assert resp.status_code == 504
        assert resp.json()["detail"] == "Transcription timed out"
        unlink.assert_called_once()
