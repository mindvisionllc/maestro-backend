"""
R-26 — Buffer real HTTP client behind BUFFER_LIVE feature flag.

Tests (all mock httpx.AsyncClient — no real Buffer calls):
1. BUFFER_LIVE=false (default) → mock response returned, no HTTP call
2. BUFFER_LIVE=true + SCHEDULER_ENABLED=dry_run → would_have_posted logged, mock returned
3. BUFFER_LIVE=true → httpx POST called with artist OAuth access token
5. 200 response → result returned correctly
6. 429 first then 200 → retried once, 200 returned
7. 429 on all attempts → RuntimeError raised
8. 500 response → RuntimeError raised
9. Malformed JSON response → RuntimeError raised
"""

import asyncio
import importlib
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── fixture helpers ───────────────────────────────────────────────────────────

_FAKE_ARTIST = "artist-buf-test"
_FAKE_TOKEN  = "buf-tok-xyz"
_FAKE_PROFILE = ["prof-1", "prof-2"]
_FAKE_CONTENT = "New single out now — link in bio 🎵"


def _reload_social(monkeypatch, *, buffer_live: bool, scheduler_enabled: str = ""):
    if buffer_live:
        monkeypatch.setenv("BUFFER_LIVE", "true")
    else:
        monkeypatch.setenv("BUFFER_LIVE", "false")

    if scheduler_enabled:
        monkeypatch.setenv("SCHEDULER_ENABLED", scheduler_enabled)
    else:
        monkeypatch.delenv("SCHEDULER_ENABLED", raising=False)

    import social_service
    importlib.reload(social_service)
    return social_service


def _with_tokens(svc, artist_id: str, token: str):
    """Patch _load_buffer_tokens to return a fake access token."""
    svc._load_buffer_tokens = lambda aid: {"access_token": token} if aid == artist_id else {}


def _mock_response(*, status_code: int, body: dict | str | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    if isinstance(body, dict):
        resp.json = MagicMock(return_value=body)
        resp.text = json.dumps(body)
    elif isinstance(body, str):
        resp.json = MagicMock(side_effect=ValueError("invalid json"))
        resp.text = body
    else:
        resp.json = MagicMock(return_value={})
        resp.text = ""
    return resp


# ── Test 1: BUFFER_LIVE=false → mock, no HTTP ────────────────────────────────

def test_buffer_live_false_returns_mock(monkeypatch):
    """BUFFER_LIVE=false → mock response; httpx never called."""
    svc = _reload_social(monkeypatch, buffer_live=False)
    _with_tokens(svc, _FAKE_ARTIST, _FAKE_TOKEN)

    with patch("httpx.AsyncClient") as mock_client:
        result = asyncio.run(svc._buffer_schedule_post(_FAKE_ARTIST, _FAKE_CONTENT, _FAKE_PROFILE))

    assert result.get("mocked") is True
    assert result.get("status") == "buffer_queued"
    mock_client.assert_not_called()


# ── Test 2: BUFFER_LIVE=true but no API key → mock ───────────────────────────

# ── Test 3: dry_run → would_have_posted logged, mock returned ────────────────

def test_buffer_dry_run_logs_would_have_posted(monkeypatch):
    """SCHEDULER_ENABLED=dry_run → would_have_posted event logged; httpx not called."""
    svc = _reload_social(monkeypatch, buffer_live=True, scheduler_enabled="dry_run")
    _with_tokens(svc, _FAKE_ARTIST, _FAKE_TOKEN)

    info_calls = []
    original_info = svc.log.info
    svc.log.info = lambda msg, *a, **kw: info_calls.append(kw.get("extra", {}))

    try:
        with patch("httpx.AsyncClient") as mock_client:
            result = asyncio.run(svc._buffer_schedule_post(_FAKE_ARTIST, _FAKE_CONTENT, _FAKE_PROFILE))
    finally:
        svc.log.info = original_info

    assert result.get("mocked") is True
    assert result.get("dry_run") is True
    would_have = [c for c in info_calls if c.get("event") == "would_have_posted"]
    assert would_have, f"No would_have_posted event logged. Got: {info_calls}"
    mock_client.assert_not_called()


# ── Test 4: live → httpx POST called with correct payload ────────────────────

def test_buffer_live_calls_httpx_post(monkeypatch):
    """BUFFER_LIVE=true + key set → httpx.AsyncClient.post() called with access_token in payload."""
    svc = _reload_social(monkeypatch, buffer_live=True)
    _with_tokens(svc, _FAKE_ARTIST, _FAKE_TOKEN)

    ok_body = {"id": "upd-123", "status": "buffer_sent"}
    mock_resp = _mock_response(status_code=200, body=ok_body)
    mock_post = AsyncMock(return_value=mock_resp)

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=mock_post))
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("social_service.httpx.AsyncClient", return_value=mock_ctx):
        result = asyncio.run(svc._buffer_schedule_post(_FAKE_ARTIST, _FAKE_CONTENT, _FAKE_PROFILE))

    assert mock_post.called, "httpx AsyncClient.post() must be called in live mode"
    call_kwargs = mock_post.call_args
    data = call_kwargs.kwargs.get("data") or (call_kwargs.args[1] if len(call_kwargs.args) > 1 else {})
    assert data.get("access_token") == _FAKE_TOKEN
    assert data.get("text") == _FAKE_CONTENT
    assert result.get("id") == "upd-123"


# ── Test 5: 200 response parsed correctly ────────────────────────────────────

def test_buffer_200_response_parsed(monkeypatch):
    """200 OK response is returned verbatim."""
    svc = _reload_social(monkeypatch, buffer_live=True)
    _with_tokens(svc, _FAKE_ARTIST, _FAKE_TOKEN)

    ok_body = {"id": "upd-abc", "status": "buffer_queued", "extra_field": True}
    mock_resp = _mock_response(status_code=200, body=ok_body)
    mock_post = AsyncMock(return_value=mock_resp)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=mock_post))
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("social_service.httpx.AsyncClient", return_value=mock_ctx):
        result = asyncio.run(svc._buffer_schedule_post(_FAKE_ARTIST, _FAKE_CONTENT, _FAKE_PROFILE))

    assert result["id"] == "upd-abc"
    assert result["extra_field"] is True


# ── Test 6: 429 once then 200 → retried, success ─────────────────────────────

def test_buffer_429_then_200_retries(monkeypatch):
    """First call returns 429; second call returns 200 → success after retry."""
    svc = _reload_social(monkeypatch, buffer_live=True)
    _with_tokens(svc, _FAKE_ARTIST, _FAKE_TOKEN)

    rate_resp = _mock_response(status_code=429)
    ok_resp   = _mock_response(status_code=200, body={"id": "retried-ok"})
    responses = [rate_resp, ok_resp]
    call_count = {"n": 0}

    async def fake_post(*a, **kw):
        r = responses[call_count["n"]]
        call_count["n"] += 1
        return r

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=fake_post))
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("social_service.httpx.AsyncClient", return_value=mock_ctx), \
         patch("social_service.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(svc._buffer_schedule_post(_FAKE_ARTIST, _FAKE_CONTENT, _FAKE_PROFILE))

    assert result["id"] == "retried-ok"
    assert call_count["n"] == 2


# ── Test 7: 429 all attempts → RuntimeError ──────────────────────────────────

def test_buffer_429_all_attempts_raises(monkeypatch):
    """All attempts return 429 → RuntimeError raised."""
    svc = _reload_social(monkeypatch, buffer_live=True)
    _with_tokens(svc, _FAKE_ARTIST, _FAKE_TOKEN)

    rate_resp = _mock_response(status_code=429)

    async def always_429(*a, **kw):
        return rate_resp

    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=always_429))
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("social_service.httpx.AsyncClient", return_value=mock_ctx), \
         patch("social_service.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError, match="rate limit"):
            asyncio.run(svc._buffer_schedule_post(_FAKE_ARTIST, _FAKE_CONTENT, _FAKE_PROFILE))


# ── Test 8: 500 response → RuntimeError ──────────────────────────────────────

def test_buffer_500_raises(monkeypatch):
    """500 from Buffer → RuntimeError raised immediately."""
    svc = _reload_social(monkeypatch, buffer_live=True)
    _with_tokens(svc, _FAKE_ARTIST, _FAKE_TOKEN)

    err_resp = _mock_response(status_code=500, body={"error": "server error"})
    mock_post = AsyncMock(return_value=err_resp)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=mock_post))
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("social_service.httpx.AsyncClient", return_value=mock_ctx):
        with pytest.raises(RuntimeError, match="500"):
            asyncio.run(svc._buffer_schedule_post(_FAKE_ARTIST, _FAKE_CONTENT, _FAKE_PROFILE))


# ── Test 9: malformed JSON → RuntimeError ────────────────────────────────────

def test_buffer_malformed_json_raises(monkeypatch):
    """200 OK but body is not valid JSON → RuntimeError raised."""
    svc = _reload_social(monkeypatch, buffer_live=True)
    _with_tokens(svc, _FAKE_ARTIST, _FAKE_TOKEN)

    bad_resp = _mock_response(status_code=200, body="not-json-at-all")
    mock_post = AsyncMock(return_value=bad_resp)
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=mock_post))
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("social_service.httpx.AsyncClient", return_value=mock_ctx):
        with pytest.raises(RuntimeError, match="non-JSON"):
            asyncio.run(svc._buffer_schedule_post(_FAKE_ARTIST, _FAKE_CONTENT, _FAKE_PROFILE))
