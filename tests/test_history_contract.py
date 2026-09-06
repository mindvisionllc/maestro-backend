import importlib
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def history_client(monkeypatch, tmp_path):
    db = tmp_path / "history.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR", str(tmp_path / "artists"))
    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)
        yield TestClient(m.app), m, db


def test_history_response_is_artist_and_agent_scoped(history_client):
    client, _, db = history_client
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_id TEXT,
            agent_id TEXT,
            role TEXT,
            content TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO messages (artist_id,agent_id,role,content) VALUES (?,?,?,?)",
        [
            ("artist-1", "social-manager", "user", "hello"),
            ("artist-1", "social-manager", "assistant", "hi"),
            ("artist-2", "social-manager", "user", "private"),
            ("artist-1", "pr-agent", "user", "other agent"),
        ],
    )
    conn.commit()
    conn.close()

    response = client.get(
        "/api/history",
        params={"artist_id": "artist-1", "agent_id": "social-manager"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "history": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
    }


def test_missing_history_store_returns_empty_wrapper(history_client, tmp_path, monkeypatch):
    client, m, _ = history_client
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "missing.db")
    response = client.get(
        "/api/history",
        params={"artist_id": "artist-1", "agent_id": "social-manager"},
    )
    assert response.status_code == 200
    assert response.json() == {"history": []}


def test_history_database_failure_is_not_masked_as_empty(history_client, tmp_path, monkeypatch):
    client, m, _ = history_client
    broken_path = tmp_path / "directory-not-db"
    broken_path.mkdir()
    monkeypatch.setattr(m, "DB_PATH", broken_path)
    response = client.get(
        "/api/history",
        params={"artist_id": "artist-1", "agent_id": "social-manager"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Conversation history unavailable"