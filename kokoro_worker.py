"""Standalone Kokoro synthesis worker process entry point.

VOICE_DIAGNOSIS.md §C: a stalled `kokoro.create()` call previously held
`_kokoro_native_lock` (a plain `threading.Lock`) forever — Python cannot
forcibly interrupt a running native call on a thread, so every later synth
request queued behind that lock and timed out at exactly
KOKORO_SYNTH_TIMEOUT_SECONDS until the whole backend was restarted.

The fix runs the native call in its own OS *process* instead of a thread.
A process, unlike a thread, can be killed outright — `Process.terminate()`
then `.kill()` — and killing it destroys every lock/mutex/native state it
held with no possibility of leaking into the next call. See
main.py's `_SupervisedSubprocessWorker` for the supervisor that spawns this,
detects a stall via a real (OS-level, not `threading.Lock`-based) queue
timeout, and kills + respawns on stall.

Deliberately NOT part of main.py / does not import it: importing main.py in
the child would re-run all of its top-level side effects (skill preload,
every service's DB init, the old in-process warmup) for no benefit and would
slow every respawn down. This module's only dependency is kokoro_onnx.
"""
from __future__ import annotations


def run(onnx_path: str, voices_path: str, req_q, resp_q) -> None:
    """Blocking worker loop. Runs in the child process.

    Protocol: `req_q` yields `(job_id, text, voice, speed, lang)` tuples, or
    `None` as a shutdown sentinel. For each job, `resp_q` receives exactly one
    `(job_id, ok, payload)` — `payload` is `(samples, sample_rate)` on
    success (ok=True) or a plain error string on failure (ok=False).
    Immediately after the model loads, puts `("__ready__", True, None)` so
    the supervisor can tell "still loading" apart from "dead".
    """
    try:
        from kokoro_onnx import Kokoro
        kokoro = Kokoro(onnx_path, voices_path)
    except Exception as e:  # model load itself failed — report and exit
        resp_q.put(("__ready__", False, str(e)))
        return

    resp_q.put(("__ready__", True, None))

    while True:
        item = req_q.get()
        if item is None:
            return
        job_id, text, voice, speed, lang = item
        try:
            samples, sr = kokoro.create(text, voice=voice, speed=speed, lang=lang)
            resp_q.put((job_id, True, (samples, sr)))
        except Exception as e:
            resp_q.put((job_id, False, str(e)))
