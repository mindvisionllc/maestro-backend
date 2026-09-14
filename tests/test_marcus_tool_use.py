"""
Unit 1.7 PROOF tests — Marcus (puppet-master) Anthropic tool_use loop.

Prove that, in /api/chat_stream:

  (a) Marcus emits search_curators then send_pitch_email then a final message;
      both internal pitch_service functions are invoked with the correct args and
      the stream surfaces a populated `actions` event (actions_taken);
  (b) a NON-Marcus agent never receives `tools` — messages.create is never called,
      it takes the unchanged streaming path, and emits NO `actions` event;
  (c) the gmail_not_connected path is handled gracefully (no crash; the actions
      event carries gmail_not_connected=True).

Everything is in-process and deterministic. NO network / LLM / Gmail calls — the
Anthropic client is faked and every pitch_service boundary is monkeypatched.
"""
import asyncio
import importlib
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Fake Anthropic SDK shapes ────────────────────────────────────────────────

class _Block:
    """Stand-in for an Anthropic content block (text or tool_use)."""
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type  = type
        self.text  = text
        self.name  = name
        self.input = input
        self.id    = id


class _Resp:
    """Stand-in for a messages.create(...) response."""
    def __init__(self, content, stop_reason):
        self.content     = content
        self.stop_reason = stop_reason


class _FakeStream:
    """Stand-in for async_client.messages.stream(...) — used by the non-Marcus path."""
    def __init__(self, text):
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def _gen():
            yield self._text
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


# ── (a) Marcus runs the tool loop and surfaces actions_taken ─────────────────
# send_pitch_email is gated (VOICE_DIAGNOSIS.md Phase 4.3 — a consequential,
# real-external-effect tool must never fire on an ordinary advisory turn
# without an explicit confirmation exchange): the first call (no `confirmed`)
# only drafts; a second call, unchanged apart from confirmed=true, actually
# sends. This test scripts both round-trips to prove the full, intended
# conversational flow end-to-end. See test_marcus_send_pitch_email_requires_
# explicit_confirmation below for the narrower, single-behavior guarantee.

def test_marcus_tool_loop_invokes_functions_and_emits_actions(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path)

    # Record calls into the existing pitch_service functions.
    list_calls, get_calls, send_calls = [], [], []

    def fake_list_curators(genre="", tier=""):
        list_calls.append({"genre": genre, "tier": tier})
        return [{
            "id": "cur-1", "name": "Test Curator", "outlet": "PlaylistX",
            "genres": ["indie", "pop"], "tier": "A", "contact_email": "c@example.com",
        }]

    def fake_get_curator(curator_id):
        get_calls.append(curator_id)
        return {"id": "cur-1", "name": "Test Curator", "contact_email": "c@example.com"}

    async def fake_send_email(artist_id, to, subject, body):
        send_calls.append({"artist_id": artist_id, "to": to, "subject": subject, "body": body})
        return {"message_id": "msg-123", "thread_id": "thr-1", "status": "sent"}

    monkeypatch.setattr(m.pitch_service, "_db_list_curators", fake_list_curators)
    monkeypatch.setattr(m.pitch_service, "_db_get_curator",   fake_get_curator)
    monkeypatch.setattr(m.pitch_service, "send_email",        fake_send_email)

    # Scripted Anthropic responses: search_curators → send_pitch_email (draft,
    # unconfirmed) → send_pitch_email (confirmed=true) → final text.
    responses = [
        _Resp([_Block("tool_use", name="search_curators",
                      input={"genre": "indie pop"}, id="t1")], "tool_use"),
        _Resp([_Block("tool_use", name="send_pitch_email",
                      input={"curator_id": "cur-1", "subject": "Sub", "body": "Body"},
                      id="t2")], "tool_use"),
        _Resp([_Block("tool_use", name="send_pitch_email",
                      input={"curator_id": "cur-1", "subject": "Sub", "body": "Body", "confirmed": True},
                      id="t3")], "tool_use"),
        _Resp([_Block("text", text="Done — I found a curator and sent your pitch.")],
              "end_turn"),
    ]
    create_calls = []

    async def fake_create(**kwargs):
        create_calls.append(kwargs)
        return responses[len(create_calls) - 1]

    monkeypatch.setattr(m.async_client.messages, "create", fake_create)
    # Guard: the streaming path must NOT be used by Marcus.
    def _no_stream(**kw):
        raise AssertionError("Marcus must not use messages.stream")
    monkeypatch.setattr(m.async_client.messages, "stream", _no_stream)

    client = TestClient(m.app)
    resp = client.post("/api/chat_stream", json={
        "agent_id":  "puppet-master",
        "message":   "find indie pop curators and pitch my single",
        "artist_id": "artist-9",
        "history":   "[]",
        "tts":       False,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types  = [e["type"] for e in events]

    # Both internal functions invoked with correct args — send_email exactly
    # once, only on the confirmed call.
    assert list_calls == [{"genre": "indie pop", "tier": ""}], list_calls
    assert get_calls  == ["cur-1", "cur-1"], get_calls
    assert send_calls == [{
        "artist_id": "artist-9", "to": "c@example.com",
        "subject": "Sub", "body": "Body",
    }], send_calls

    # actions event present, populated, before done.
    assert "actions" in types, types
    assert "done" in types
    assert types.index("actions") < types.index("done")
    actions_evt = next(e for e in events if e["type"] == "actions")
    tools_used  = [a["tool"] for a in actions_evt["actions_taken"]]
    assert tools_used == ["search_curators", "send_pitch_email", "send_pitch_email"], tools_used
    assert actions_evt["actions_taken"][1]["result"] == "draft_ready (unconfirmed)"
    assert actions_evt["actions_taken"][2]["result"] == "email sent"
    assert actions_evt["gmail_not_connected"] is False
    assert "Done" in next(e for e in events if e["type"] == "done")["full_text"]
    # Four create() round-trips: search, draft, confirmed send, final.
    assert len(create_calls) == 4
    # Tools were passed on every Marcus create call.
    assert all(kw.get("tools") == m.MARCUS_TOOLS for kw in create_calls)


# ── (a2) send_pitch_email is gated behind explicit confirmation ─────────────

def test_marcus_send_pitch_email_requires_explicit_confirmation(monkeypatch, tmp_path):
    """The narrow guarantee behind Phase 4.3: calling the tool WITHOUT
    confirmed=true must never reach pitch_service.send_email — no real email
    can leave on an ordinary advisory turn just because the model decided to.
    """
    m = _load_main(monkeypatch, tmp_path)
    monkeypatch.setattr(m.pitch_service, "_db_get_curator",
                        lambda cid: {"id": cid, "name": "Test Curator", "contact_email": "c@example.com"})

    send_calls = []
    async def fake_send_email(artist_id, to, subject, body):
        send_calls.append(1)
        return {"message_id": "should-not-happen"}
    monkeypatch.setattr(m.pitch_service, "send_email", fake_send_email)

    result, summary, gmail_not_connected = asyncio.run(
        m._execute_marcus_tool(
            "send_pitch_email",
            {"curator_id": "cur-1", "subject": "S", "body": "B"},  # no `confirmed` at all
            "artist-1",
        )
    )

    assert send_calls == [], "send_email must not be called without confirmed=true"
    assert result["status"] == "draft_ready"
    assert result["subject"] == "S" and result["body"] == "B"
    assert summary["result"] == "draft_ready (unconfirmed)"
    assert gmail_not_connected is False

    # confirmed=False is explicitly the same as omitting it.
    send_calls.clear()
    result2, _, _ = asyncio.run(
        m._execute_marcus_tool(
            "send_pitch_email",
            {"curator_id": "cur-1", "subject": "S", "body": "B", "confirmed": False},
            "artist-1",
        )
    )
    assert send_calls == []
    assert result2["status"] == "draft_ready"

    # Only confirmed=true (the literal boolean, not a truthy string) sends.
    result3, summary3, _ = asyncio.run(
        m._execute_marcus_tool(
            "send_pitch_email",
            {"curator_id": "cur-1", "subject": "S", "body": "B", "confirmed": True},
            "artist-1",
        )
    )
    assert send_calls == [1]
    assert result3["status"] == "sent"
    assert summary3["result"] == "email sent"


# ── (b) Non-Marcus agent never gets tools, takes the unchanged path ──────────

def test_non_marcus_agent_never_receives_tools(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path)

    create_calls = []

    async def fake_create(**kwargs):
        create_calls.append(kwargs)
        return _Resp([_Block("text", text="x")], "end_turn")

    monkeypatch.setattr(m.async_client.messages, "create", fake_create)
    monkeypatch.setattr(m.async_client.messages, "stream",
                        lambda **kw: _FakeStream("Here is some general guidance for you."))

    client = TestClient(m.app)
    resp = client.post("/api/chat_stream", json={
        "agent_id":  "music-edu",   # NOT puppet-master
        "message":   "give me a general check-in",
        "artist_id": "artist-9",
        "history":   "[]",
        "tts":       False,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types  = [e["type"] for e in events]

    # Unchanged path: messages.create (the tool loop) is never touched.
    assert create_calls == [], "non-Marcus agent must not invoke the tool_use create loop"
    # No actions event for non-Marcus agents.
    assert "actions" not in types, types
    assert "done" in types


# ── (c) gmail_not_connected handled gracefully ───────────────────────────────

def test_marcus_gmail_not_connected_is_handled(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path)

    monkeypatch.setattr(m.pitch_service, "_db_get_curator",
                        lambda cid: {"id": cid, "name": "Test Curator",
                                     "contact_email": "c@example.com"})

    async def fake_send_email(artist_id, to, subject, body):
        raise m.pitch_service.GmailNotConnected("no tokens")

    monkeypatch.setattr(m.pitch_service, "send_email", fake_send_email)

    responses = [
        # confirmed=True — this test is about GmailNotConnected handling, not
        # the confirmation gate (see test_marcus_send_pitch_email_requires_
        # explicit_confirmation for that); scripting an already-confirmed call
        # reaches send_email directly, same as before this gate existed.
        _Resp([_Block("tool_use", name="send_pitch_email",
                      input={"curator_id": "cur-1", "subject": "S", "body": "B", "confirmed": True},
                      id="t1")], "tool_use"),
        _Resp([_Block("text", text="You need to connect Gmail first.")], "end_turn"),
    ]
    create_calls = []

    async def fake_create(**kwargs):
        create_calls.append(kwargs)
        return responses[len(create_calls) - 1]

    monkeypatch.setattr(m.async_client.messages, "create", fake_create)

    client = TestClient(m.app)
    resp = client.post("/api/chat_stream", json={
        "agent_id":  "puppet-master",
        "message":   "pitch curator cur-1",
        "artist_id": "artist-no-gmail",
        "history":   "[]",
        "tts":       False,
    })
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    types  = [e["type"] for e in events]

    # Stream completes without crashing.
    assert "done" in types
    assert "error" not in types, types
    actions_evt = next(e for e in events if e["type"] == "actions")
    assert actions_evt["gmail_not_connected"] is True
    assert actions_evt["actions_taken"][0]["result"] == "gmail_not_connected"
