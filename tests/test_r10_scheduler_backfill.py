"""
R-10 — Scheduler coalesce + per-tick batch limit.

Tests:
  1. 50 past-due actions → only SCHEDULER_BATCH_LIMIT process per tick
  2. Second tick → next SCHEDULER_BATCH_LIMIT process
  3. Coalesce flag is set on registered scheduler jobs
  4. misfire_grace_time is set on registered jobs

Run with:  python3 -m pytest tests/test_r10_scheduler_backfill.py -v
"""

import asyncio
import importlib
import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    import release_service
    importlib.reload(release_service)
    release_service.init_release_db()
    yield tmp_path / "test.db"


@pytest.fixture()
def rs():
    import release_service
    return release_service


def _seed_pending_actions(rs, count: int, release_id: str = "rel-001") -> list:
    """Seed `count` past-due readiness-released campaign_actions directly in the DB."""
    import sqlite3
    from pathlib import Path
    import os
    db = Path(os.environ["DB_PATH"])
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    ids = []
    conn = sqlite3.connect(str(db))
    for i in range(count):
        action_id = f"act-{i:04d}"
        conn.execute(
            "INSERT INTO campaign_actions "
            "(id, release_id, action_type, scheduled_for, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (action_id, release_id, "pitch_curators", past, "ready"),
        )
        ids.append(action_id)
    conn.commit()
    conn.close()
    return ids


# ── 1. Batch limit: only SCHEDULER_BATCH_LIMIT actions per tick ───────────────

def test_batch_limit_processes_only_N_per_tick(tmp_path, monkeypatch):
    """With 50 past-due actions and limit=5, exactly 5 should be processed per tick."""
    monkeypatch.setenv("SCHEDULER_BATCH_LIMIT", "5")
    import release_service
    importlib.reload(release_service)
    release_service.init_release_db()

    _seed_pending_actions(release_service, 50)

    mock_execute = AsyncMock(return_value={"status": "ok"})
    with patch.object(release_service, "_execute_action", mock_execute):
        asyncio.run(release_service.execute_all_due_campaign_actions())

    assert mock_execute.call_count == 5, (
        f"Expected exactly 5 actions processed (SCHEDULER_BATCH_LIMIT=5), "
        f"got {mock_execute.call_count}. "
        "Add per-tick batch cap to execute_all_due_campaign_actions()."
    )


def test_second_tick_processes_next_batch(tmp_path, monkeypatch):
    """Running the executor twice processes actions 0-4 then 5-9."""
    monkeypatch.setenv("SCHEDULER_BATCH_LIMIT", "5")
    import release_service
    importlib.reload(release_service)
    release_service.init_release_db()

    _seed_pending_actions(release_service, 12)

    mock_execute = AsyncMock(return_value={"status": "ok"})
    with patch.object(release_service, "_execute_action", mock_execute):
        asyncio.run(release_service.execute_all_due_campaign_actions())
        first_count = mock_execute.call_count
        asyncio.run(release_service.execute_all_due_campaign_actions())
        second_count = mock_execute.call_count - first_count

    assert first_count == 5, f"First tick: expected 5, got {first_count}"
    assert second_count == 5, f"Second tick: expected 5, got {second_count}"

    # Third tick: only 2 remain
    with patch.object(release_service, "_execute_action", mock_execute):
        before = mock_execute.call_count
        asyncio.run(release_service.execute_all_due_campaign_actions())
        third_count = mock_execute.call_count - before
    assert third_count == 2, f"Third tick: expected 2 remaining, got {third_count}"


# ── 2. Coalesce and misfire_grace_time are set ────────────────────────────────

def test_campaign_executor_job_has_coalesce_and_grace(monkeypatch):
    """campaign_executor job must have coalesce=True and misfire_grace_time set."""
    recorded = {}

    class _FakeSched:
        running = True
        def add_job(self, fn, trigger, **kwargs):
            if kwargs.get("id") == "campaign_executor":
                recorded.update(kwargs)

    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("DB_PATH", "/tmp/test_coalesce.db")
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("AUDIO_CACHE_DIR", "/tmp/audio_test")
    monkeypatch.setenv("ARTISTS_DIR", "/tmp/artists_test")

    fake_sched = _FakeSched()

    with patch("pitch_service._scheduler", fake_sched), \
         patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)

    assert recorded.get("coalesce") is True, (
        "campaign_executor job missing coalesce=True. "
        "Add coalesce=True to _pitch_sched.add_job(...) in main.py."
    )
    assert recorded.get("misfire_grace_time") is not None, (
        "campaign_executor job missing misfire_grace_time. "
        "Add misfire_grace_time=<seconds> to _pitch_sched.add_job(...) in main.py."
    )


def test_inbox_poll_job_has_coalesce(monkeypatch, tmp_path):
    """inbox_poll APScheduler job must declare coalesce=True."""
    import sys

    monkeypatch.setenv("DB_PATH",           str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL",      "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")

    recorded = {}

    class _FakeAsyncSched:
        running = False
        def add_job(self, fn, trigger, **kwargs):
            recorded[kwargs.get("id", "?")] = kwargs
        def start(self):
            self.running = True

    fake_sched_instance = _FakeAsyncSched()

    # Inject a fake apscheduler.schedulers.asyncio module so the import
    # inside init_scheduler() succeeds without the real package installed.
    fake_asyncio_mod = MagicMock()
    fake_asyncio_mod.AsyncIOScheduler = MagicMock(return_value=fake_sched_instance)
    fake_pkg = MagicMock()
    fake_schedulers_pkg = MagicMock()
    fake_schedulers_pkg.asyncio = fake_asyncio_mod

    with patch.dict(sys.modules, {
        "apscheduler": fake_pkg,
        "apscheduler.schedulers": fake_schedulers_pkg,
        "apscheduler.schedulers.asyncio": fake_asyncio_mod,
    }):
        import pitch_service
        importlib.reload(pitch_service)
        pitch_service.init_pitch_db()
        pitch_service.init_scheduler()

    job_kwargs = recorded.get("inbox_poll", {})
    assert job_kwargs.get("coalesce") is True, (
        "inbox_poll job missing coalesce=True in pitch_service.py"
    )
    assert job_kwargs.get("misfire_grace_time") is not None, (
        "inbox_poll job missing misfire_grace_time in pitch_service.py"
    )
