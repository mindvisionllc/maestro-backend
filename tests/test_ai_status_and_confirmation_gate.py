"""VOICE_DIAGNOSIS.md Phase 4.7 / 4.3 — focused, deterministic coverage.

Phase 4.7: /api/health must expose ai_available so the client can show a
degraded-mode indicator BEFORE the artist wastes a turn — the static
__greet__ branch (cdd0c8d) succeeds regardless of key state, so it must never
again be the only signal of LLM health. This is exactly the gap the confirmed
physical-device incident fell into: the call looked healthy at connect time
while every real turn silently 503'd.

Phase 4.3: a consequential, real-external-effect tool (send_pitch_email) must
never fire on an ordinary advisory turn without an explicit confirmation
exchange. See test_marcus_tool_use.py for the fuller tool-loop coverage of
this same gate; this file adds the isolated, minimal-surface version.

No real Anthropic/Gmail/network calls anywhere in this file.
"""
import importlib
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


def _load_main(monkeypatch, tmp_path, *, with_key: bool):
    if with_key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    else:
        # Pass 4 (VOICE_DIAGNOSIS.md §H): setenv("") not delenv — this reload
        # re-runs main.py's top-level load_dotenv(), whose override=False
        # only skips a key already *present* in os.environ. delenv makes it
        # look absent again, so the reload silently refills it from the real
        # .env (which does have a real ANTHROPIC_API_KEY), defeating the
        # "without a key" scenario this test exists to cover. An empty
        # string still reads as falsy everywhere that matters
        # (ANTHROPIC_AVAILABLE = bool(ANTHROPIC_API_KEY)).
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR", str(tmp_path / "artists"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)
    return m


# ── Phase 4.7: /api/health surfaces ai_available ─────────────────────────────

def test_api_health_reports_ai_unavailable_without_a_key(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path, with_key=False)
    client = TestClient(m.app)
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ai_available"] is False
    # The plain liveness check is unaffected — this is a distinct, richer endpoint.
    assert client.get("/health").json() == {"status": "ok"}


def test_api_health_reports_ai_available_with_a_key(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path, with_key=True)
    client = TestClient(m.app)
    body = client.get("/api/health").json()
    assert body["ai_available"] is True


def test_greeting_succeeds_regardless_of_ai_available_confirming_it_is_not_a_health_signal(monkeypatch, tmp_path):
    """Pins the exact defect this field exists to compensate for: the static
    greeting's own success/failure must never be read as proof the LLM path
    works — it succeeds either way."""
    for with_key in (False, True):
        m = _load_main(monkeypatch, tmp_path, with_key=with_key)
        client = TestClient(m.app)
        resp = client.post("/api/chat_stream", json={
            "agent_id": "puppet-master", "message": "__greet__",
            "artist_id": "artist-1", "history": "[]", "tts": False,
        })
        assert resp.status_code == 200, f"greeting failed with ai key present={with_key}"
        ai_available = client.get("/api/health").json()["ai_available"]
        assert ai_available is with_key


# ── Phase 4.3: send_pitch_email confirmation gate (minimal-surface) ─────────

def test_send_pitch_email_tool_schema_advertises_the_confirmed_field(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path, with_key=True)
    tool = next(t for t in m.MARCUS_TOOLS if t["name"] == "send_pitch_email")
    props = tool["input_schema"]["properties"]
    assert "confirmed" in props
    assert props["confirmed"]["type"] == "boolean"
    # curator_id/subject/body remain required; confirmed is not (first call omits it).
    assert set(tool["input_schema"]["required"]) == {"curator_id", "subject", "body"}
