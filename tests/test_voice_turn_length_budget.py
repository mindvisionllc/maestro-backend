"""Targeted coverage for VOICE_DIAGNOSIS.md Pass 3 (voice reply length + turn
budget).

Covers:
  - `voice: true` on /api/chat_stream is the real "this is a voice call"
    signal (not `tts`, which only means "stream audio inline over SSE" and is
    always false from both CallScreen.js and voice_probe.py in practice) —
    it flips the system prompt to _VOICE_RULES and caps max_tokens.
  - The server-side VOICE_CHAR_CEILING truncation: a long fake reply is cut
    at a sentence boundary, never mid-sentence, and `done`'s full_text matches
    what was actually streamed as `text` events.
  - A text-mode (voice: false) turn of the same length is NOT truncated —
    the ceiling is voice-only.
  - `_truncate_at_sentence` unit behavior in isolation.

Everything here is in-process and deterministic: the Anthropic client is
faked (no real network/LLM call), matching tests/test_chat_stream_timeout.py's
pattern.
"""
import importlib
import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


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


# A long, multi-sentence fake reply — well over any sane voice-turn ceiling,
# so it exercises truncation regardless of the exact configured number.
_LONG_REPLY_SENTENCES = [
    "Here is the first sentence of a much longer answer than any voice reply should ever be.",
    "Here is a second sentence adding more detail than a live phone call needs.",
    "And a third sentence that should never reach the artist's ear in voice mode.",
    "A fourth sentence, for good measure, well past any reasonable spoken-turn budget.",
]
_LONG_REPLY = " ".join(_LONG_REPLY_SENTENCES)


class _SteadyStream:
    """Fake async_client.messages.stream(...) that yields a fixed reply word
    by word — same shape as test_chat_stream_timeout.py's fake, just longer."""

    def __init__(self, text):
        self._words = [w + " " for w in text.split(" ")]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    @property
    def text_stream(self):
        async def _gen():
            for w in self._words:
                yield w
        return _gen()


def _post_chat(client, *, voice, message="give me a full breakdown"):
    return client.post("/api/chat_stream", json={
        "agent_id":  "music-edu",   # non-Marcus agent takes the shared _claude() path
        "message":   message,
        "artist_id": "artist-voice-budget",
        "history":   "[]",
        "tts":       False,
        "voice":     voice,
    })


def test_voice_turn_reply_is_truncated_at_sentence_boundary(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path)
    monkeypatch.setattr(m.async_client.messages, "stream",
                        lambda **kw: _SteadyStream(_LONG_REPLY))

    client = TestClient(m.app)
    resp = _post_chat(client, voice=True)
    assert resp.status_code == 200

    events    = _parse_sse(resp.text)
    types     = [e["type"] for e in events]
    full_text = "".join(e["text"] for e in events if e["type"] == "text")
    done_evt  = next(e for e in events if e["type"] == "done")

    assert "error" not in types, types
    assert len(full_text) <= m.VOICE_CHAR_CEILING, full_text
    # never mid-sentence: must end on a sentence-ending punctuation mark
    assert full_text.rstrip()[-1] in ".!?", repr(full_text)
    # the full reply text was genuinely longer — this is truncation, not a
    # naturally short reply
    assert len(full_text) < len(_LONG_REPLY)
    # done's full_text must match what text events actually delivered — the
    # client accumulates its spoken transcript from `text` events, so a
    # mismatch here would mean the persisted/logged reply lies about what was
    # actually said on the call
    assert done_evt["full_text"] == full_text


def test_voice_turn_ceiling_does_not_apply_to_text_mode(monkeypatch, tmp_path):
    m = _load_main(monkeypatch, tmp_path)
    monkeypatch.setattr(m.async_client.messages, "stream",
                        lambda **kw: _SteadyStream(_LONG_REPLY))

    client = TestClient(m.app)
    resp = _post_chat(client, voice=False)
    assert resp.status_code == 200

    events    = _parse_sse(resp.text)
    full_text = "".join(e["text"] for e in events if e["type"] == "text")
    # Sentence-chunked streaming (split_sentence, pre-existing/unrelated to this
    # pass) doesn't preserve inter-sentence whitespace byte-for-byte — compare
    # on content, not exact spacing; the point of this test is that nothing was
    # dropped by a voice-only ceiling that must not apply here.
    assert full_text.replace(" ", "") == _LONG_REPLY.replace(" ", "")
    assert len(full_text) > m.VOICE_CHAR_CEILING


def test_voice_turn_sets_voice_system_prompt_and_max_tokens(monkeypatch, tmp_path):
    """voice:true must flip build_system_blocks(voice_mode=...) and max_tokens,
    independent of `tts` — the exact gap VOICE_DIAGNOSIS.md Pass 3 §2 found
    (do_tts alone, driven by `tts`, was always False for a real call)."""
    m = _load_main(monkeypatch, tmp_path)
    captured = {}

    # Route through generate_marcus, which calls messages.create (not .stream)
    # and shares the exact same system_blocks/max_tokens computed once in
    # chat_stream() — a convenient single call site to inspect both values.
    class _FakeResp:
        content = []
        stop_reason = "end_turn"

    async def fake_create(**kwargs):
        captured["max_tokens"] = kwargs.get("max_tokens")
        captured["system"]     = kwargs.get("system")
        return _FakeResp()

    monkeypatch.setattr(m.async_client.messages, "create", fake_create)

    client = TestClient(m.app)
    resp = client.post("/api/chat_stream", json={
        "agent_id":  "puppet-master",
        "message":   "what's my next move",
        "artist_id": "artist-voice-budget",
        "history":   "[]",
        "tts":       False,
        "voice":     True,
    })
    assert resp.status_code == 200
    assert captured["max_tokens"] == m.VOICE_MAX_TOKENS
    system_text = json.dumps(captured["system"])
    assert "VOICE MODE" in system_text
    assert "TEXT MODE" not in system_text


@pytest.mark.parametrize("text,ceiling,expected", [
    ("Short reply.", 130, "Short reply."),                       # under ceiling: unchanged
    ("A. B. C. D. E. F. G. H. I. J. K. L. M. N.", 10, "A. B. C."),  # cuts at last full sentence <= ceiling
    ("No punctuation at all here just words", 10, "No"),  # no sentence boundary anywhere: word-boundary fallback, never mid-word
])
def test_truncate_at_sentence(text, ceiling, expected):
    import main as m
    assert m._truncate_at_sentence(text, ceiling) == expected


def test_truncate_at_sentence_extends_bounded_for_colon_led_clause():
    """Observed in practice: a colon-led compound question has no sentence
    end within the ceiling itself. Must still land on the real sentence end
    (bounded +60 chars) rather than cut mid-thought or mid-word."""
    import main as m
    text = "A" * 20 + ": " + "B" * 20 + "."   # no '.'/'!'/'?' in the first 20 chars
    assert len(text[:20].rstrip(".!?")) == 20  # sanity: ceiling window has no boundary
    result = m._truncate_at_sentence(text, 20)
    assert result == text  # the only sentence end is at the very end, within +60
    assert result.endswith(".")


def test_truncate_at_sentence_never_cuts_mid_word_beyond_extended_window():
    import main as m
    text = "word " * 10 + "B" * 200   # spaces in the window; no sentence end anywhere
    result = m._truncate_at_sentence(text, 22)
    assert result == "word word word word"  # last full word at/before the ceiling
    assert not result.endswith("wor")        # never a partial word
    assert len(result) <= 22
    assert text.startswith(result)
