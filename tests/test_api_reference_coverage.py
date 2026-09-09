"""
B1 — API reference coverage smoke test.

Verifies that docs/API_REFERENCE.md contains every route currently
registered in the FastAPI app. Regenerates at test time by calling
app.openapi() directly.
"""

import importlib
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_api_reference_covers_all_routes(monkeypatch, tmp_path):
    """API_REFERENCE.md must contain every path from app.openapi()."""
    monkeypatch.setenv("DB_PATH",           str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL",      "")
    monkeypatch.setenv("AUDIO_CACHE_DIR",   str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR",       str(tmp_path / "artists"))
    monkeypatch.setenv("PLMKR_API_KEY",     "")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)

    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)

    schema = m.app.openapi()
    all_paths = set(schema.get("paths", {}).keys())

    ref_path = Path(__file__).parent.parent / "docs" / "API_REFERENCE.md"
    assert ref_path.exists(), f"docs/API_REFERENCE.md not found at {ref_path}"
    content = ref_path.read_text()

    missing = [p for p in all_paths if p not in content]
    assert not missing, (
        f"The following {len(missing)} routes are missing from API_REFERENCE.md:\n"
        + "\n".join(f"  {p}" for p in sorted(missing))
    )


def test_committed_openapi_snapshot_covers_all_routes(monkeypatch, tmp_path):
    """The checked-in machine-readable contract must match registered paths."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test-openapi.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("AUDIO_CACHE_DIR", str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR", str(tmp_path / "artists"))
    monkeypatch.setenv("PLMKR_API_KEY", "")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)

    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)

    snapshot_path = Path(__file__).parent.parent / "docs" / "openapi.json"
    assert snapshot_path.exists(), f"OpenAPI snapshot not found at {snapshot_path}"
    snapshot = json.loads(snapshot_path.read_text())
    registered_paths = set(m.app.openapi().get("paths", {}))
    snapshot_paths = set(snapshot.get("paths", {}))
    assert registered_paths == snapshot_paths, (
        "docs/openapi.json is stale; regenerate it with scripts/export_openapi.py.\n"
        f"Missing: {sorted(registered_paths - snapshot_paths)}\n"
        f"Unexpected: {sorted(snapshot_paths - registered_paths)}"
    )
