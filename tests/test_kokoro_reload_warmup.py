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
    assert "target=get_kokoro" in warmup_block
