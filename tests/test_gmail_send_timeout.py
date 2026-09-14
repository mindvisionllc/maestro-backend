"""Targeted coverage for the bounded Gmail send path (Marcus voice-call defect).

Confirmed physical-device defect: "Call Marcus" hung at "Live … " indefinitely
(observed >1m43s, twice, after the TTS-only fixes in db1da91/5709f87 — neither
touched this code path). Root cause traced to pitch_service.send_email: every
step of a Gmail send (_get_gmail_service's token refresh, and
_gmail_execute_with_retry's request.execute()) is synchronous, blocking network
I/O, called directly inside an `async def` with no executor and no timeout —
the same class of bug already fixed for Kokoro TTS synthesis
(KOKORO_SYNTH_TIMEOUT_SECONDS), just not this call site. A stalled call froze
the single asyncio event loop thread indefinitely, which is why nothing —
not even the frontend's independent 60s SSE watchdog — could unstick a call
that had reached this point: the whole process, including the coroutine that
would otherwise write bytes to the SSE response, was blocked.

These tests exercise pitch_service.send_email() directly against a fake Gmail
service — no real Gmail/OAuth calls are made. Mirrors
tests/test_kokoro_synth_timeout.py's structure and its note on bounded fake
delays (asyncio.run()'s shutdown_default_executor() blocks on a genuinely-
unbounded fake call, hanging the test process itself, not just the caller).
"""

import asyncio
import threading
import time

import pytest


@pytest.fixture()
def ps(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    import importlib
    import pitch_service
    importlib.reload(pitch_service)
    return pitch_service


class _StuckGmailService:
    """Stand-in for the googleapiclient Gmail service: request.execute() blocks
    synchronously for `delay` seconds, exactly like a stalled real HTTP call."""

    def __init__(self, delay):
        self.delay = delay
        self.calls = 0
        self.max_concurrent = 0
        self._concurrent = 0
        self._guard = threading.Lock()

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        return self

    def execute(self):
        with self._guard:
            self._concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self._concurrent)
        try:
            self.calls += 1
            if self.delay:
                time.sleep(self.delay)
            return {"id": "msg-should-not-be-reached", "threadId": "thr-x"}
        finally:
            with self._guard:
                self._concurrent -= 1


# 1. A stuck Gmail call times out instead of hanging the caller forever.
def test_stuck_gmail_send_times_out_instead_of_hanging(ps, monkeypatch):
    monkeypatch.setattr(ps, "GMAIL_SEND_TIMEOUT_SECONDS", 0.1)
    # Slower than the patched bound but still bounded (see module docstring —
    # asyncio.run()'s shutdown_default_executor() blocks on the orphaned
    # worker thread regardless of the caller-side bound, so this also has to
    # stay short; the deadline proof below measures inside the coroutine,
    # before that unrelated cleanup wait, exactly like test_kokoro_synth_
    # timeout.py's test_stuck_synthesis_times_out_instead_of_hanging).
    stuck = _StuckGmailService(delay=0.3)
    monkeypatch.setattr(ps, "_get_gmail_service", lambda artist_id: stuck)

    async def _run_with_deadline():
        t0 = time.monotonic()
        with pytest.raises(ps.GmailSendTimeout):
            await ps.send_email("artist-1", "curator@example.com", "Sub", "Body")
        return time.monotonic() - t0

    elapsed = asyncio.run(_run_with_deadline())

    # Resolves close to the configured deadline, nowhere near the fake call's
    # actual 0.3s delay — this is the "resolves within the configured
    # deadline" proof for the exact hang the physical-device test hit.
    assert elapsed < 0.25, f"send_email did not bound its wait (took {elapsed:.2f}s)"


# 2. The event loop stays responsive while a Gmail call is stuck — proves the
#    blocking work actually moved to a thread (run_in_executor), not just that
#    the *caller* gives up while the process itself stays frozen.
def test_event_loop_stays_responsive_during_stuck_send(ps, monkeypatch):
    monkeypatch.setattr(ps, "GMAIL_SEND_TIMEOUT_SECONDS", 0.1)
    stuck = _StuckGmailService(delay=0.3)  # bounded — see module docstring
    monkeypatch.setattr(ps, "_get_gmail_service", lambda artist_id: stuck)

    async def scenario():
        t0 = time.monotonic()
        ticks = []

        async def ticker():
            for _ in range(5):
                await asyncio.sleep(0.02)
                ticks.append(time.monotonic() - t0)

        ticker_task = asyncio.create_task(ticker())
        with pytest.raises(ps.GmailSendTimeout):
            await ps.send_email("artist-1", "curator@example.com", "Sub", "Body")
        await ticker_task
        return ticks

    ticks = asyncio.run(scenario())
    # If the blocking Gmail call were still running directly on the loop
    # thread (the pre-fix bug), these ticks could not interleave with it at
    # all — the ticker coroutine would never get a turn until the 0.3s sleep
    # inside the (would-be) synchronous call finally returned.
    assert len(ticks) == 5
    assert ticks[-1] < 0.25


# 3. A successful send still returns normally through the bounded path.
def test_bounded_path_still_succeeds_on_a_normal_send(ps, monkeypatch):
    monkeypatch.setattr(ps, "GMAIL_SEND_TIMEOUT_SECONDS", 5.0)
    fast = _StuckGmailService(delay=0.0)
    monkeypatch.setattr(ps, "_get_gmail_service", lambda artist_id: fast)

    result = asyncio.run(ps.send_email("artist-1", "curator@example.com", "Sub", "Body"))

    assert result["status"] == "sent"
    assert fast.calls == 1


# 4. GmailNotConnected / GmailAuthExpired still propagate through the now-
#    executor-bound path (they're raised by _get_gmail_service, which now runs
#    inside the worker thread rather than directly on the event loop).
def test_gmail_not_connected_still_propagates(ps, monkeypatch):
    monkeypatch.setattr(ps, "GMAIL_SEND_TIMEOUT_SECONDS", 5.0)

    def _raise(artist_id):
        raise ps.GmailNotConnected("no tokens")

    monkeypatch.setattr(ps, "_get_gmail_service", _raise)

    with pytest.raises(ps.GmailNotConnected):
        asyncio.run(ps.send_email("artist-1", "curator@example.com", "Sub", "Body"))


# 5. _execute_marcus_tool (main.py) turns a Gmail send timeout into a graceful,
#    non-fatal tool_result — Marcus's tool_use loop must continue and finish
#    the turn normally, not surface a raw exception or hang the SSE stream.
def test_marcus_tool_handles_gmail_send_timeout_gracefully(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("BANK_CONSULT_MOCK_MODE", "true")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR", str(tmp_path / "artists"))
    monkeypatch.setenv("ELEVENLABS_API_KEY", "")
    from unittest.mock import MagicMock, patch
    with patch("whisper.load_model", return_value=MagicMock()):
        import importlib
        import main as m
        importlib.reload(m)

    async def fake_get_curator(curator_id):
        return None

    def fake_get_curator_sync(curator_id):
        return {"id": curator_id, "name": "Test Curator", "contact_email": "c@example.com"}

    async def fake_send_email(artist_id, to, subject, body):
        raise m.pitch_service.GmailSendTimeout("timed out")

    monkeypatch.setattr(m.pitch_service, "_db_get_curator", fake_get_curator_sync)
    monkeypatch.setattr(m.pitch_service, "send_email", fake_send_email)

    result, summary, gmail_not_connected = asyncio.run(
        m._execute_marcus_tool(
            "send_pitch_email",
            {"curator_id": "cur-1", "subject": "S", "body": "B"},
            "artist-1",
        )
    )

    assert result["error"] == "gmail_send_timeout"
    assert summary["result"] == "send_timeout"
    # Not an auth problem — the artist should not be told to reconnect Gmail.
    assert gmail_not_connected is False
