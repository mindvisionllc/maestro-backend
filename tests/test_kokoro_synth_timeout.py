"""Targeted coverage for the bounded, self-healing Kokoro synthesis path.

VOICE_DIAGNOSIS.md §C (Pass 4): the previous fix (a thread-pool worker plus a
threading.Lock, _kokoro_native_lock) bounded how long an *asyncio caller*
waited, but could not stop the underlying OS thread when a native
kokoro.create() call stalled — Python cannot safely interrupt a running
native call on a thread. The stalled thread kept the lock forever, and every
later call queued behind it, each timing out at exactly
KOKORO_SYNTH_TIMEOUT_SECONDS until the whole backend was restarted.

The fix (main.py's _SupervisedSubprocessWorker) runs the native call in its
own OS *process* instead. A process can be killed outright on a real,
OS-level queue-get timeout, and killing it destroys anything it held — there
is nothing left to leak into the next call.

These tests exercise synthesize_speech() and _SupervisedSubprocessWorker
directly against real (but fake/instrumented) subprocess targets — real
multiprocessing spawn, no mocked-out process/queue objects — so a picklability
or IPC-protocol regression would actually be caught here, not just the
control-flow logic. No real Kokoro or ElevenLabs calls are made.
"""

import asyncio
import importlib
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ── Fake worker-process targets ──────────────────────────────────────────────
# Top-level (picklable under multiprocessing's "spawn" context) stand-ins for
# kokoro_worker.run — same wire protocol, controllable behavior instead of a
# real ONNX model.

def _fake_target_ok(delay, req_q, resp_q):
    resp_q.put(("__ready__", True, None))
    while True:
        item = req_q.get()
        if item is None:
            return
        job_id, text, voice, speed, lang = item
        if delay:
            time.sleep(delay)
        resp_q.put((job_id, True, (np.zeros(2400, dtype=np.float32), 24000)))


def _fake_target_raises(req_q, resp_q):
    resp_q.put(("__ready__", True, None))
    while True:
        item = req_q.get()
        if item is None:
            return
        job_id, *_rest = item
        resp_q.put((job_id, False, "native synth failure"))


def _fake_target_stuck(req_q, resp_q):
    resp_q.put(("__ready__", True, None))
    while True:
        item = req_q.get()
        if item is None:
            return
        time.sleep(3600)  # never responds — the exact defect being fixed


@pytest.fixture()
def kokoro_module(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR", str(tmp_path / "artists"))
    # main.py's warmup thread (started at reload) would otherwise load the
    # real, local ~325MB Kokoro ONNX model when the model files are present
    # in this repo checkout (get_kokoro(), the in-process availability/R-19
    # check — unchanged by Pass 4) and, transitively, spawn a real worker
    # subprocess to warm it too. Neither is relevant to (or as fast as)
    # anything under test here. Blocking the kokoro_onnx import for the life
    # of this fixture makes get_kokoro() fail fast (_kokoro_available=False),
    # which also short-circuits _warmup_kokoro_and_worker()'s subprocess warm.
    with patch("whisper.load_model", return_value=MagicMock()), \
         patch.dict("sys.modules", {"kokoro_onnx": None}):
        import main as m
        importlib.reload(m)
        if m._kokoro_warmup_thread is not None:
            m._kokoro_warmup_thread.join(timeout=30)
        yield m


@pytest.fixture()
def fake_worker(kokoro_module, monkeypatch):
    """Installs a fresh, isolated supervisor (no shared restarts counter or
    live process across tests) pointed at an in-test fake target, and makes
    get_kokoro() report available so synthesize_speech() takes the Kokoro
    path instead of falling back to ElevenLabs. Always torn down, even on
    test failure, so no child process outlives its test."""
    m = kokoro_module
    monkeypatch.setattr(m, "get_kokoro", lambda: object())  # any truthy value
    installed = []

    def _factory(target, init_args=()):
        worker = m._SupervisedSubprocessWorker(target=target, init_args=init_args)
        monkeypatch.setattr(m, "_kokoro_worker_supervisor", worker)
        installed.append(worker)
        return worker

    yield _factory

    for w in installed:
        w._kill_locked()


# 1. Successful Kokoro synthesis — real subprocess spawn, real IPC round-trip.
def test_successful_synthesis_returns_wav_bytes(kokoro_module, fake_worker):
    m = kokoro_module
    fake_worker(_fake_target_ok, init_args=(0.0,))

    audio = asyncio.run(m.synthesize_speech("hello there", "am_onyx", "call-1"))

    assert audio is not None and len(audio) > 0


# 2. A later request after an earlier success reuses the same live process
# (no restart — restarts only happen on an actual stall).
def test_later_request_succeeds_after_earlier_success(kokoro_module, fake_worker):
    m = kokoro_module
    worker = fake_worker(_fake_target_ok, init_args=(0.0,))

    first = asyncio.run(m.synthesize_speech("first turn", "am_onyx", "call-1"))
    # Distinct text — the cache_file.exists() short-circuit in
    # synthesize_speech() would otherwise return before ever calling the
    # worker for an identical second call.
    second = asyncio.run(m.synthesize_speech("second turn, not cached", "am_onyx", "call-1"))

    assert first is not None
    assert second is not None
    assert worker.restarts == 0


# 3. Serialized access — synth_blocking() holds its lock for the whole call
# (see its docstring), so two overlapping requests must not run concurrently.
def test_concurrent_calls_are_serialized_not_run_together(kokoro_module, fake_worker):
    m = kokoro_module
    fake_worker(_fake_target_ok, init_args=(0.3,))

    async def _run_both():
        t0 = time.monotonic()
        results = await asyncio.gather(
            m.synthesize_speech("overlapping greeting", "am_onyx", "call-a"),
            m.synthesize_speech("overlapping reply, distinct text", "am_onyx", "call-b"),
        )
        return results, time.monotonic() - t0

    results, elapsed = asyncio.run(_run_both())

    assert all(r is not None for r in results)
    # If they ran concurrently this would be ~0.3s; serialized, ~0.6s+.
    assert elapsed > 0.5, "two synth calls appear to have run concurrently"


# 4. A stalled synth times out instead of hanging, and the worker is killed
# (not left running) so it cannot poison a later call.
def test_stuck_synthesis_times_out_and_kills_the_worker(kokoro_module, fake_worker, monkeypatch):
    m = kokoro_module
    monkeypatch.setattr(m, "KOKORO_SYNTH_TIMEOUT_SECONDS", 0.3)
    worker = fake_worker(_fake_target_stuck)

    async def _run_with_deadline():
        t0 = time.monotonic()
        result = await asyncio.wait_for(
            m.synthesize_speech("a response that never returns", "am_onyx", "call-stuck"),
            timeout=10.0,  # generous outer bound — the real bound under test is 0.3s
        )
        return result, time.monotonic() - t0

    result, elapsed = asyncio.run(_run_with_deadline())

    assert result is None  # bounded failure, not a hang
    assert elapsed < 5.0, "caller was not released anywhere near the configured bound"
    assert worker.restarts == 1, "stalled worker was not killed"
    assert worker._proc is None, "no live process should remain after a kill"


# 5. THE regression this pass exists to prove (C3): after a stall, the VERY
# NEXT call succeeds — it gets a fresh worker, it does not queue behind the
# dead one, and it does not need to "wait out" anything.
def test_next_call_succeeds_after_a_stall(kokoro_module, fake_worker, monkeypatch):
    m = kokoro_module
    monkeypatch.setattr(m, "KOKORO_SYNTH_TIMEOUT_SECONDS", 0.3)
    worker = fake_worker(_fake_target_stuck)

    first = asyncio.run(asyncio.wait_for(
        m.synthesize_speech("stuck turn", "am_onyx", "call-1"), timeout=10.0,
    ))
    assert first is None
    assert worker.restarts == 1

    # Swap in a healthy target — a real deployment would just retry against
    # the same (real) target; here we make the *next* spawn healthy the same
    # way a transient real stall would recover once whatever caused it
    # (e.g. a bad input) doesn't recur.
    worker._target = _fake_target_ok
    worker._init_args = (0.0,)
    monkeypatch.setattr(m, "KOKORO_SYNTH_TIMEOUT_SECONDS", 5.0)

    t0 = time.monotonic()
    second = asyncio.run(m.synthesize_speech("second, healthy turn", "am_onyx", "call-2"))
    elapsed = time.monotonic() - t0

    assert second is not None
    assert elapsed < 5.0  # bounded by a fresh spawn, not blocked on the dead one


# 6. Lock/resource release after success.
def test_tts_lock_released_after_success(kokoro_module, fake_worker):
    m = kokoro_module
    fake_worker(_fake_target_ok, init_args=(0.0,))

    asyncio.run(m.synthesize_speech("release after success", "am_onyx", "call-1"))

    assert not m._tts_lock.locked()


# 7. Lock/resource release after a worker-reported exception (not a stall).
def test_tts_lock_released_after_synth_exception(kokoro_module, fake_worker):
    m = kokoro_module
    fake_worker(_fake_target_raises)

    result = asyncio.run(m.synthesize_speech("release after exception", "am_onyx", "call-1"))

    assert result is None
    assert not m._tts_lock.locked()


# 8. C4: /api/tts/status and /api/health's wedge signal must stay responsive
# while a synth is in flight — they must never block behind synth_blocking's
# lock (see _SupervisedSubprocessWorker.status()'s docstring for why it
# deliberately does not take that lock).
def test_status_does_not_block_while_worker_is_busy(kokoro_module, fake_worker):
    m = kokoro_module
    worker = fake_worker(_fake_target_ok, init_args=(1.0,))

    import threading
    started = threading.Event()

    def _run_synth():
        started.set()
        asyncio.run(m.synthesize_speech("a slow but real reply", "am_onyx", "call-1"))

    t = threading.Thread(target=_run_synth)
    t.start()
    started.wait(timeout=2.0)
    time.sleep(0.2)  # let the synth actually get into its busy window

    t0 = time.monotonic()
    status = worker.status()
    status_elapsed = time.monotonic() - t0

    t.join(timeout=5.0)

    assert status_elapsed < 0.2, "status() blocked behind the in-flight synth's lock"
    assert status["busy_seconds"] is not None and status["busy_seconds"] > 0
