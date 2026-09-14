"""Targeted coverage for bounded Anthropic calls inside /api/chat_stream.

Reproduces the exact confirmed physical-device defect — "Call Marcus" stuck
showing "Live" with only the empty-transcript "…" placeholder for 1m43s+,
twice, with no reply text and no audio — at the SSE layer, and proves it now
resolves within the configured deadline instead of hanging.

Two independent Anthropic call sites are covered, both previously unbounded:
  (a) generate_marcus()'s per-iteration messages.create() in the puppet-master
      tool_use loop (MARCUS_CREATE_TIMEOUT_SECONDS) — the loop that calls
      pitch_service.send_email, whose own missing bound is the root cause
      covered by tests/test_gmail_send_timeout.py; this file covers a stalled
      Anthropic call itself, a separate unbounded stage in the same loop.
  (b) generate()'s _claude() streaming call, shared by every non-tool agent
      (ANTHROPIC_STREAM_TIMEOUT_SECONDS) — the literal "chat_stream" path.

Everything here is in-process and deterministic: the Anthropic client is
faked with a cooperative asyncio.sleep (never a real network/LLM call), so a
"stuck" call is bounded and yields the event loop instead of blocking it.
"""
import asyncio
import importlib
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


class _Block:
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type  = type
        self.text  = text
        self.name  = name
        self.input = input
        self.id    = id


class _Resp:
    def __init__(self, content, stop_reason):
        self.content     = content
        self.stop_reason = stop_reason


class _HangingStream:
    """Stand-in for async_client.messages.stream(...) whose text_stream never
    yields anything within the test's patched bound — models a stalled
    Anthropic streaming call before any token arrives (the confirmed defect:
    no text ever reached the client)."""

    def __init__(self, delay):
        self._delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def _gen():
            await asyncio.sleep(self._delay)  # cooperative — never blocks the loop
            yield "this text must never be reached"
        return _gen()


def _load_main(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY",      "sk-ant-test")
    monkeypatch.setenv("BANK_CONSULT_MOCK_MODE", "true")
    monkeypatch.setenv("DB_PATH",                str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL",           "")
    monkeypatch.setenv("AUDIO_CACHE_DIR",        str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR",            str(tmp_path / "artists"))
    monkeypatch.setenv("ELEVENLABS_API_KEY",     "")
    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)
    return m


def _parse_sse(body: str) -> list:
    events = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            events.append(json.loads(line[len("data:"):].strip()))
    return events


# ── (a) Marcus: a stalled messages.create() resolves within the deadline ────

def test_marcus_stalled_create_resolves_with_safe_error_not_a_hang(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "MARCUS_CREATE_TIMEOUT_SECONDS", 0.1)

    async def fake_create(**kwargs):
        await asyncio.sleep(2.0)  # bounded fake stall, cooperative
        raise AssertionError("must never resolve — the wait_for bound should fire first")

    monkeypatch.setattr(m.async_client.messages, "create", fake_create)

    client = TestClient(m.app)
    t0 = time.monotonic()
    resp = client.post("/api/chat_stream", json={
        "agent_id":  "puppet-master",
        "message":   "find indie pop curators and pitch my single",
        "artist_id": "artist-9",
        "history":   "[]",
        "tts":       False,
    })
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types  = [e["type"] for e in events]

    # Terminates predictably — well under the fake call's 2s stall — instead
    # of hanging the request (and, in the real defect, the whole call UI).
    assert elapsed < 1.0, f"chat_stream did not bound the stalled create() call (took {elapsed:.2f}s)"
    assert "error" in types, types
    # No reply text ever arrived — exactly the reported "no answer text" state.
    assert "text" not in types, types
    error_evt = next(e for e in events if e["type"] == "error")
    assert "try again" in error_evt["message"].lower()
    assert "timeout" not in error_evt["message"].lower()  # safe message, no internals


# ── (b) Generic path: a stalled Anthropic stream resolves within deadline ───

def test_generic_agent_stalled_stream_resolves_with_safe_error_not_a_hang(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "ANTHROPIC_STREAM_TIMEOUT_SECONDS", 0.1)

    monkeypatch.setattr(m.async_client.messages, "stream",
                        lambda **kw: _HangingStream(delay=2.0))

    client = TestClient(m.app)
    t0 = time.monotonic()
    resp = client.post("/api/chat_stream", json={
        "agent_id":  "music-edu",   # any non-Marcus agent takes the shared _claude() path
        "message":   "give me a general check-in",
        "artist_id": "artist-9",
        "history":   "[]",
        "tts":       False,
    })
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types  = [e["type"] for e in events]

    assert elapsed < 1.0, f"chat_stream did not bound the stalled stream (took {elapsed:.2f}s)"
    assert "error" in types, types
    assert "text" not in types, types  # reproduces the exact "Live … forever, no text" state
    error_evt = next(e for e in events if e["type"] == "error")
    assert "didn't respond in time" in error_evt["message"]


# ── (c) A healthy stream of any length is unaffected by the inactivity bound ─

def test_stream_that_keeps_producing_chunks_is_not_cut_off(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path)
    monkeypatch.setattr(m, "ANTHROPIC_STREAM_TIMEOUT_SECONDS", 0.1)

    class _SteadyStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        @property
        def text_stream(self):
            async def _gen():
                for word in ["Here ", "is ", "a ", "steady ", "reply."]:
                    await asyncio.sleep(0.02)  # well under the 0.1s inactivity bound
                    yield word
            return _gen()

    monkeypatch.setattr(m.async_client.messages, "stream", lambda **kw: _SteadyStream())

    client = TestClient(m.app)
    resp = client.post("/api/chat_stream", json={
        "agent_id":  "music-edu",
        "message":   "give me a general check-in",
        "artist_id": "artist-9",
        "history":   "[]",
        "tts":       False,
    })

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types  = [e["type"] for e in events]
    assert "error" not in types, types
    assert "done" in types
    full_text = "".join(e["text"] for e in events if e["type"] == "text")
    assert "steady reply" in full_text
