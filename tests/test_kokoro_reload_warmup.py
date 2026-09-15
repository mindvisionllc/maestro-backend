"""Regression coverage for reload-safe native Kokoro warmup."""

from pathlib import Path


MAIN_SOURCE = Path(__file__).resolve().parents[1].joinpath("main.py").read_text()


def test_kokoro_state_survives_main_reload():
    assert '_kokoro = globals().get("_kokoro")' in MAIN_SOURCE
    assert '_kokoro_available = globals().get("_kokoro_available")' in MAIN_SOURCE
    assert '_kokoro_warmup_thread = globals().get("_kokoro_warmup_thread")' in MAIN_SOURCE


def test_main_reload_reuses_live_kokoro_warmup_thread():
    warmup_start = MAIN_SOURCE.index("if _kokoro_warmup_thread is None")
    warmup_block = MAIN_SOURCE[warmup_start:MAIN_SOURCE.index("_sync_sqlite_service_paths()", warmup_start)]
    assert "_kokoro_warmup_thread.is_alive()" in warmup_block
    # Pass 4 (VOICE_DIAGNOSIS.md §C): the warmup thread now warms two
    # subsystems — get_kokoro() (in-process availability/R-19-warning check)
    # AND _kokoro_worker_supervisor's subprocess (the one that actually
    # serves synthesis requests) — via _warmup_kokoro_and_worker(), which
    # calls get_kokoro() itself.
    assert "target=_warmup_kokoro_and_worker" in warmup_block
