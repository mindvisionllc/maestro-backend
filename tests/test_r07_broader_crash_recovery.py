"""R-07 — intermediate-state sweep: campaign_actions 'running' rows reset on init."""
import sqlite3
import os
import pytest


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "release.db")


@pytest.fixture
def patched_env(monkeypatch, db_path):
    monkeypatch.setenv("RELEASE_DB_PATH", db_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_rs(monkeypatch, db_path):
    monkeypatch.setenv("DB_PATH", db_path)
    import importlib
    import release_service as rs
    importlib.reload(rs)
    return rs


def _seed_stuck_row(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS campaign_actions (
            id            TEXT PRIMARY KEY,
            release_id    TEXT NOT NULL,
            action_type   TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            status        TEXT DEFAULT 'pending',
            payload_json  TEXT DEFAULT '{}',
            executed_at   TEXT,
            result_json   TEXT DEFAULT '{}',
            created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        )
    """)
    conn.execute(
        "INSERT INTO campaign_actions (id, release_id, action_type, scheduled_for, status)"
        " VALUES ('act-1', 'rel-1', 'social', '2026-01-01T00:00:00', 'running')"
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Test 1 — stuck 'running' row is reset to 'pending' by init_release_db()
# ---------------------------------------------------------------------------

def test_stuck_running_row_reset_to_ready(monkeypatch, tmp_path):
    db_path = str(tmp_path / "release.db")
    _seed_stuck_row(db_path)

    rs = _import_rs(monkeypatch, db_path)
    rs.init_release_db()

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT status FROM campaign_actions WHERE id='act-1'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row[0] == "ready", f"Expected 'ready', got {row[0]!r}"


# ---------------------------------------------------------------------------
# Test 2 — init_release_db() logs the reset count when rows are fixed
# ---------------------------------------------------------------------------

def test_stuck_row_reset_logged(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "release.db")
    _seed_stuck_row(db_path)

    rs = _import_rs(monkeypatch, db_path)
    capsys.readouterr()  # flush module-load noise
    rs.init_release_db()
    captured = capsys.readouterr()

    assert "Reset 1 stuck" in captured.out


# ---------------------------------------------------------------------------
# Test 3 — no stuck rows → no reset log line (clean startup is silent)
# ---------------------------------------------------------------------------

def test_clean_startup_no_reset_log(monkeypatch, tmp_path, capsys):
    db_path = str(tmp_path / "release.db")

    rs = _import_rs(monkeypatch, db_path)
    capsys.readouterr()
    rs.init_release_db()
    captured = capsys.readouterr()

    assert "Reset" not in captured.out


# ---------------------------------------------------------------------------
# Test 4 — campaign_actions is the only table with a recoverable intermediate
#           state: pitch/pr/booking/social use terminal states only
# ---------------------------------------------------------------------------

def test_campaign_actions_only_intermediate_state_table():
    """
    Verifies our sweep finding: only campaign_actions uses 'running' as an
    intermediate pre-await state. Other services go draft→sent/failed directly.
    This is a documentation test — it will fail if a future service adds a
    new intermediate state without a corresponding init reset.
    """
    import ast, pathlib

    repo = pathlib.Path(__file__).parent.parent
    service_files = [
        repo / "pitch_service.py",
        repo / "pr_service.py",
        repo / "booking_service.py",
        repo / "social_service.py",
        repo / "release_service.py",
    ]

    # We expect 'running' only in release_service (campaign_actions table)
    running_hits = {}
    for f in service_files:
        src = f.read_text()
        lines_with_running = [
            i + 1
            for i, line in enumerate(src.splitlines())
            if '"running"' in line or "'running'" in line
        ]
        if lines_with_running:
            running_hits[f.name] = lines_with_running

    # Only release_service.py should have 'running'
    unexpected = {k: v for k, v in running_hits.items() if k != "release_service.py"}
    assert not unexpected, (
        f"Unexpected intermediate 'running' state found in: {unexpected}. "
        "Add a startup reset in the corresponding init_*_db() function."
    )
    # And release_service must have it (sanity check we're actually searching)
    assert "release_service.py" in running_hits, (
        "Expected 'running' in release_service.py but not found — did the fix change the literal?"
    )
