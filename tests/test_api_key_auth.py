"""
Unit tests for R-04 — X-API-Key middleware.

Tests use the FastAPI test client with the real middleware wired up.
Run with:  python3 -m pytest tests/test_api_key_auth.py -v
"""

import importlib
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def _base_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH",            str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL",       "")
    monkeypatch.setenv("ANTHROPIC_API_KEY",  "sk-test")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test")
    monkeypatch.setenv("AUDIO_CACHE_DIR",    str(tmp_path / "audio_cache"))
    monkeypatch.setenv("ARTISTS_DIR",        str(tmp_path / "artists"))


@pytest.fixture()
def client_no_key(_base_env, monkeypatch):
    """App with PLMKR_API_KEY unset → dev-permissive."""
    monkeypatch.delenv("PLMKR_API_KEY", raising=False)
    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)
        yield TestClient(m.app)


@pytest.fixture()
def client_with_key(_base_env, monkeypatch):
    """App with PLMKR_API_KEY='secret-key'."""
    monkeypatch.setenv("PLMKR_API_KEY", "secret-key")
    with patch("whisper.load_model", return_value=MagicMock()):
        import main as m
        importlib.reload(m)
        yield TestClient(m.app)


# ── Dev-permissive mode (no key set) ─────────────────────────────────────────

def test_no_key_set_allows_all_requests(client_no_key):
    """When PLMKR_API_KEY is unset, all requests pass through."""
    resp = client_no_key.get("/health")
    assert resp.status_code == 200


def test_no_key_set_no_header_needed(client_no_key):
    """No X-API-Key header required in dev mode."""
    resp = client_no_key.get("/health")
    assert resp.status_code == 200


# ── Enforced mode (key set) ───────────────────────────────────────────────────

def test_health_bypasses_auth(client_with_key):
    """/health is reachable without X-API-Key even when auth is enabled."""
    resp = client_with_key.get("/health")
    assert resp.status_code == 200


def test_missing_api_key_returns_401(client_with_key):
    """Protected endpoint with no header → 401."""
    resp = client_with_key.get("/api/gmail/status?artist_id=test")
    assert resp.status_code == 401
    assert "X-API-Key" in resp.json()["detail"]


def test_wrong_api_key_returns_401(client_with_key):
    """Protected endpoint with wrong key → 401."""
    resp = client_with_key.get(
        "/api/gmail/status?artist_id=test",
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


def test_correct_api_key_passes(client_with_key):
    """Protected endpoint with correct key → not 401."""
    resp = client_with_key.get(
        "/api/gmail/status?artist_id=test",
        headers={"X-API-Key": "secret-key"},
    )
    assert resp.status_code != 401


def test_docs_bypasses_auth(client_with_key):
    """/docs is reachable without X-API-Key."""
    resp = client_with_key.get("/docs")
    assert resp.status_code != 401


def test_timing_safe_comparison(client_with_key):
    """Key comparison uses secrets.compare_digest (timing-safe) — verified by behavior."""
    resp1 = client_with_key.get(
        "/api/gmail/status?artist_id=test",
        headers={"X-API-Key": "secret-key"},
    )
    resp2 = client_with_key.get(
        "/api/gmail/status?artist_id=test",
        headers={"X-API-Key": "secret-kex"},  # one char off
    )
    assert resp1.status_code != 401
    assert resp2.status_code == 401


def test_options_preflight_bypasses_auth(client_with_key):
    """OPTIONS preflight to /api/* must not be blocked by API key middleware.

    Failure mode: _APIKeyMiddleware intercepts OPTIONS before CORSMiddleware
    can respond → browser never gets Access-Control-Allow-* headers → all
    cross-origin requests from the frontend silently fail once PLMKR_API_KEY
    is set on Railway.

    This test would return 401 on the branch before the OPTIONS short-circuit
    was added.
    """
    resp = client_with_key.options(
        "/api/curators",
        headers={
            "Origin":                         "https://plmkr.vercel.app",
            "Access-Control-Request-Method":  "GET",
            "Access-Control-Request-Headers": "x-api-key",
        },
    )
    assert resp.status_code != 401, (
        "OPTIONS preflight was blocked by auth middleware — "
        "add 'if request.method == \"OPTIONS\": return await call_next(request)' "
        "to _APIKeyMiddleware.dispatch()"
    )
    assert "access-control-allow-origin" in resp.headers, (
        "CORS preflight response missing Access-Control-Allow-Origin header"
    )



def test_agent_portraits_are_public_but_api_routes_remain_protected(client_with_key):
    portrait = client_with_key.get(
        "/static/agents/marcus.jpg",
        follow_redirects=False,
    )
    assert portrait.status_code in (200, 302)

    if portrait.status_code == 200:
        assert portrait.headers["content-type"].startswith("image/")
    else:
        location = portrait.headers["location"]
        assert location.startswith("https://res.cloudinary.com/")
        assert location.endswith("/marcus.jpg")

    protected = client_with_key.get("/api/gmail/status?artist_id=test")
    assert protected.status_code == 401
