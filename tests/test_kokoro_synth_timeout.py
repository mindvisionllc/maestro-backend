"""Targeted coverage for the bounded Kokoro synthesis path (voice-reliability fix).

Covers the confirmed defect: a Kokoro synth call with no bound could hold
_tts_lock/_kokoro_native_lock indefinitely, leaving every later /api/tts/synth
request (this one and any queued behind it) pending forever instead of
reaching a terminal (success/failure) state. These tests exercise
synthesize_speech() directly against a fake Kokoro model — no real Kokoro or
ElevenLabs calls are made.

Note: every "stuck" fake call below uses a bounded delay, not a wait that
would never return on its own. asyncio.run() automatically calls the loop's
shutdown_default_executor() on exit, which blocks until every submitted
executor job actually finishes — so a genuinely-unbounded fake call would
hang the *test process* itself (not just the caller under test) waiting for
that orphaned thread, independent of whether synthesize_speech()'s own
caller-side bound is working correctly.
"""

import asyncio
import importlib
import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


class _FakeKokoro:
    """Stand-in for the native Kokoro model with controllable timing/failure."""

    def __init__(self, *, delay=0.0, raise_exc=None):
        self.calls = 0
        self.max_concurrent = 0
        self._concurrent = 0
        self._guard = threading.Lock()
        self.delay = delay
        self.raise_exc = raise_exc

    def create(self, text, voice, speed, lang):
        with self._guard:
            self._concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self._concurrent)
        try:
            self.calls += 1
            if self.delay:
                time.sleep(self.delay)
            if self.raise_exc:
                raise self.raise_exc
            return np.zeros(2400, dtype=np.float32), 24000
        finally:
            with self._guard:
                self._concurrent -= 1


@pytest.fixture()
def kokoro_module(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR", str(tmp_path / "artists"))
    # main.py's warmup thread (started at reload) loads the real, local
    # ~325MB Kokoro ONNX model when the model files are present in this repo
    # checkout, which can hold the GIL for minutes during graph construction —
    # unrelated to (and far slower than) anything under test here. Blocking
    # the kokoro_onnx import for the life of this fixture makes that warmup
    # fail fast (caught, _kokoro_available=False) instead of racing a real
    # model load against this file's fake-Kokoro, bounded-timeout assertions.
    with patch("whisper.load_model", return_value=MagicMock()), \
         patch.dict("sys.modules", {"kokoro_onnx": None}):
        import main as m
        importlib.reload(m)
        if m._kokoro_warmup_thread is not None:
            m._kokoro_warmup_thread.join(timeout=30)
        yield m


# 1. Successful Kokoro synthesis.
def test_successful_synthesis_returns_wav_bytes(kokoro_module, monkeypatch):
    m = kokoro_module
    fake = _FakeKokoro()
    monkeypatch.setattr(m, "get_kokoro", lambda: fake)

    audio = asyncio.run(m.synthesize_speech("hello there", "am_onyx", "call-1"))

    assert audio is not None and len(audio) > 0
    assert fake.calls == 1


# 2. A later synthesis request after earlier successful requests.
def test_later_request_succeeds_after_earlier_success(kokoro_module, monkeypatch):
    m = kokoro_module
    fake = _FakeKokoro()
    monkeypatch.setattr(m, "get_kokoro", lambda: fake)

    first = asyncio.run(m.synthesize_speech("first turn", "am_onyx", "call-1"))
    second = asyncio.run(m.synthesize_speech("second turn", "am_onyx", "call-1"))

    assert first is not None
    assert second is not None
    assert fake.calls == 2


# 3. Serialized access — the model is not known to be concurrency-safe.
def test_concurrent_calls_never_enter_kokoro_create_simultaneously(kokoro_module, monkeypatch):
    m = kokoro_module
    fake = _FakeKokoro(delay=0.05)
    monkeypatch.setattr(m, "get_kokoro", lambda: fake)

    async def _run_both():
        return await asyncio.gather(
            m.synthesize_speech("overlapping greeting", "am_onyx", "call-a"),
            m.synthesize_speech("overlapping reply", "am_onyx", "call-b"),
        )

    results = asyncio.run(_run_both())

    assert all(r is not None for r in results)
    assert fake.max_concurrent == 1, "two synth calls ran kokoro.create() concurrently"


# 4. Backend synthesis timeout.
def test_stuck_synthesis_times_out_instead_of_hanging(kokoro_module, monkeypatch):
    m = kokoro_module
    monkeypatch.setattr(m, "KOKORO_SYNTH_TIMEOUT_SECONDS", 0.1)
    # Slower than the patched bound but still bounded (see module docstring).
    fake = _FakeKokoro(delay=0.3)
    monkeypatch.setattr(m, "get_kokoro", lambda: fake)

    async def _run_with_deadline():
        t0 = time.monotonic()
        result = await asyncio.wait_for(
            m.synthesize_speech("a response that takes too long", "am_onyx", "call-stuck"),
            timeout=2.0,  # generous outer bound — the real bound under test is 0.1s
        )
        return result, time.monotonic() - t0

    result, elapsed = asyncio.run(_run_with_deadline())

    assert result is None  # bounded failure, not a hang
    assert elapsed < 0.25  # caller released by the 0.1s bound, not the 0.3s native call


# 5. Lock/resource release after success.
def test_tts_lock_released_after_success(kokoro_module, monkeypatch):
    m = kokoro_module
    fake = _FakeKokoro()
    monkeypatch.setattr(m, "get_kokoro", lambda: fake)

    asyncio.run(m.synthesize_speech("release after success", "am_onyx", "call-1"))

    assert not m._tts_lock.locked()


# 6. Lock/resource release after exception.
def test_tts_lock_released_after_synth_exception(kokoro_module, monkeypatch):
    m = kokoro_module
    fake = _FakeKokoro(raise_exc=RuntimeError("native synth failure"))
    monkeypatch.setattr(m, "get_kokoro", lambda: fake)

    result = asyncio.run(m.synthesize_speech("release after exception", "am_onyx", "call-1"))

    assert result is None
    assert not m._tts_lock.locked()


# 7. Lock/resource release after timeout — a later request is not permanently
# blocked by an earlier stuck one.
def test_later_request_not_blocked_by_earlier_timed_out_request(kokoro_module, monkeypatch):
    m = kokoro_module
    # Slower than the patched bound but still bounded (see module docstring).
    stuck = _FakeKokoro(delay=0.3)

    monkeypatch.setattr(m, "get_kokoro", lambda: stuck)
    monkeypatch.setattr(m, "KOKORO_SYNTH_TIMEOUT_SECONDS", 0.1)

    async def _scenario():
        # First call gets stuck and times out from the caller's perspective;
        # its worker thread is still running, holding _kokoro_native_lock.
        first = await asyncio.wait_for(
            m.synthesize_speech("stuck turn", "am_onyx", "call-1"), timeout=2.0,
        )
        assert first is None

        # A second, healthy request must still be able to make progress — it
        # waits out the first call's remaining native-lock hold, but is not
        # blocked forever behind it.
        healthy = _FakeKokoro()
        monkeypatch.setattr(m, "get_kokoro", lambda: healthy)
        monkeypatch.setattr(m, "KOKORO_SYNTH_TIMEOUT_SECONDS", 5.0)
        t0 = time.monotonic()
        second = await m.synthesize_speech("second, healthy turn", "am_onyx", "call-2")
        elapsed = time.monotonic() - t0

        assert second is not None
        assert elapsed < 1.0  # bounded wait for the native lock, not indefinite

    asyncio.run(_scenario())
    assert not m._tts_lock.locked()
