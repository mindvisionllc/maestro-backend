"""
Report 3 (M1) fix — the Buffer OAuth callback exchanged the auth code with a
BLOCKING httpx.post() inside an async handler, stalling the event loop for the
duration of the token exchange. It is now an awaited async client call
(async with httpx.AsyncClient() as c: resp = await c.post(...)).

These tests mock the async client and prove behavior is IDENTICAL: the same
request (URL, form data, 15s timeout) is issued, the response JSON is parsed,
and the access_token is stored. They also prove the call is genuinely awaited
(a blocking httpx.post shim would never be used).
"""
import asyncio
import importlib

import pytest


@pytest.fixture()
def ss(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ARTISTS_DIR", str(tmp_path / "artists"))
    import social_service
    importlib.reload(social_service)
    social_service.init_social_db()
    return social_service


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Async-context client whose .post is awaited — records the single call."""
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, timeout=None):
        _FakeAsyncClient.calls.append({"url": url, "data": data, "timeout": timeout})
        return _FakeResp({"access_token": "tok-abc123"})


def _install_fake_client(ss, monkeypatch):
    _FakeAsyncClient.calls = []
    monkeypatch.setattr(ss.httpx, "AsyncClient", _FakeAsyncClient)


def test_buffer_callback_exchanges_code_via_awaited_async_client(ss, monkeypatch):
    monkeypatch.setattr(ss, "_BUFFER_CLIENT_SECRET", "secret-xyz", raising=False)
    _install_fake_client(ss, monkeypatch)

    saved = {}
    monkeypatch.setattr(ss, "_save_buffer_tokens",
                        lambda artist_id, tokens: saved.update({artist_id: tokens}))

    result = asyncio.run(ss.buffer_callback(code="the-code", state="artist-77"))

    assert result == {"status": "connected", "artist_id": "artist-77"}
    # exactly one exchange, to the token URL, with the OAuth form fields + 15s timeout
    assert len(_FakeAsyncClient.calls) == 1
    call = _FakeAsyncClient.calls[0]
    assert call["url"] == ss._BUFFER_TOKEN_URL
    assert call["timeout"] == 15
    assert call["data"]["code"] == "the-code"
    assert call["data"]["grant_type"] == "authorization_code"
    assert call["data"]["client_secret"] == "secret-xyz"
    # response JSON parsed and access_token persisted for the state's artist
    assert saved["artist-77"]["access_token"] == "tok-abc123"


def test_buffer_callback_requires_client_secret(ss, monkeypatch):
    # Degradation unchanged: no configured secret => 503, no exchange attempted.
    monkeypatch.setattr(ss, "_BUFFER_CLIENT_SECRET", "", raising=False)
    _install_fake_client(ss, monkeypatch)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(ss.buffer_callback(code="x", state="artist-1"))
    assert ei.value.status_code == 503
    assert _FakeAsyncClient.calls == []


def test_buffer_callback_wraps_exchange_failure_as_500(ss, monkeypatch):
    # A failed exchange still degrades to a 500 (behavior preserved from the
    # blocking version's try/except).
    monkeypatch.setattr(ss, "_BUFFER_CLIENT_SECRET", "secret-xyz", raising=False)

    class _BoomClient(_FakeAsyncClient):
        async def post(self, url, data=None, timeout=None):
            raise RuntimeError("network down")

    monkeypatch.setattr(ss.httpx, "AsyncClient", _BoomClient)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(ss.buffer_callback(code="x", state="artist-1"))
    assert ei.value.status_code == 500


def test_buffer_callback_rejects_upstream_error_without_persisting(ss, monkeypatch):
    monkeypatch.setattr(ss, "_BUFFER_CLIENT_SECRET", "secret-xyz", raising=False)

    class _ErrorClient(_FakeAsyncClient):
        async def post(self, url, data=None, timeout=None):
            return _FakeResp({"error": "invalid_grant"}, status_code=400)

    monkeypatch.setattr(ss.httpx, "AsyncClient", _ErrorClient)
    saved = []
    monkeypatch.setattr(ss, "_save_buffer_tokens", lambda *args: saved.append(args))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(ss.buffer_callback(code="x", state="artist-1"))
    assert ei.value.status_code == 502
    assert ei.value.detail == "Buffer authorization could not be completed"
    assert saved == []


@pytest.mark.parametrize("payload", [{}, {"access_token": "   "}, ["not-an-object"]])
def test_buffer_callback_rejects_invalid_token_response_without_persisting(ss, monkeypatch, payload):
    monkeypatch.setattr(ss, "_BUFFER_CLIENT_SECRET", "secret-xyz", raising=False)

    class _InvalidClient(_FakeAsyncClient):
        async def post(self, url, data=None, timeout=None):
            return _FakeResp(payload)

    monkeypatch.setattr(ss.httpx, "AsyncClient", _InvalidClient)
    saved = []
    monkeypatch.setattr(ss, "_save_buffer_tokens", lambda *args: saved.append(args))
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        asyncio.run(ss.buffer_callback(code="x", state="artist-1"))
    assert ei.value.status_code == 502
    assert ei.value.detail == "Buffer authorization returned an invalid token response"
    assert saved == []
