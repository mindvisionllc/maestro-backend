"""Signed artist sessions and privacy-preserving phone identity binding."""

import base64
import hashlib
import hmac
import json
import os
import time
import uuid


SESSION_TTL_SECONDS = int(os.environ.get("PLMKR_SESSION_TTL_SECONDS", "2592000"))


class ArtistAuthError(Exception):
    pass


def identity_configured() -> bool:
    return bool(
        os.environ.get("PLMKR_SESSION_SECRET", "").strip()
        and os.environ.get("PLMKR_IDENTITY_SECRET", "").strip()
    )


def _required_secret(name: str) -> bytes:
    value = os.environ.get(name, "").strip()
    if len(value) < 32:
        raise ArtistAuthError(f"{name} must contain at least 32 characters")
    return value.encode()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def phone_fingerprint(normalized_phone: str) -> str:
    return hmac.new(
        _required_secret("PLMKR_IDENTITY_SECRET"),
        normalized_phone.encode(),
        hashlib.sha256,
    ).hexdigest()


def new_artist_id() -> str:
    return f"artist_{uuid.uuid4().hex}"


def issue_session(artist_id: str, now: int | None = None) -> dict:
    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + SESSION_TTL_SECONDS
    payload = {
        "sub": artist_id,
        "iat": issued_at,
        "exp": expires_at,
        "ver": 1,
    }
    encoded = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signature = _b64encode(
        hmac.new(
            _required_secret("PLMKR_SESSION_SECRET"),
            encoded.encode(),
            hashlib.sha256,
        ).digest()
    )
    return {
        "access_token": f"{encoded}.{signature}",
        "token_type": "Bearer",
        "expires_in": SESSION_TTL_SECONDS,
        "expires_at": expires_at,
    }


def decode_session(token: str, now: int | None = None) -> dict:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(
                _required_secret("PLMKR_SESSION_SECRET"),
                encoded.encode(),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ArtistAuthError("Invalid artist session")

        payload = json.loads(_b64decode(encoded))
        current_time = int(time.time() if now is None else now)

        if payload.get("ver") != 1:
            raise ArtistAuthError("Unsupported artist session")
        if not isinstance(payload.get("sub"), str) or not payload["sub"]:
            raise ArtistAuthError("Invalid artist session")
        if not isinstance(payload.get("exp"), int) or payload["exp"] <= current_time:
            raise ArtistAuthError("Artist session expired")

        return payload
    except ArtistAuthError:
        raise
    except Exception as exc:
        raise ArtistAuthError("Invalid artist session") from exc


def bearer_token(authorization: str) -> str:
    scheme, separator, token = (authorization or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise ArtistAuthError("Missing artist session")
    return token.strip()


def authenticated_artist_id(request) -> str:
    """Resolve the signed artist principal from an HTTP request."""
    if not identity_configured():
        return ""
    if request is None:
        raise ArtistAuthError("Missing artist session")
    token = bearer_token(request.headers.get("Authorization", ""))
    return decode_session(token)["sub"]


def require_artist_scope(request, claimed_artist_id: str) -> str:
    """Require the signed principal to own the claimed artist scope."""
    if not identity_configured():
        return claimed_artist_id

    principal = authenticated_artist_id(request)
    if (
        not claimed_artist_id
        or not hmac.compare_digest(principal, claimed_artist_id)
    ):
        raise ArtistAuthError("Artist resource not found")
    return principal


def require_admin_api_key(request) -> None:
    """Require the server-side API key; artist bearer sessions are not admin auth."""
    configured_key = os.environ.get("PLMKR_API_KEY", "").strip()
    if not configured_key:
        if identity_configured():
            raise ArtistAuthError("Admin API key is not configured")
        return
    if request is None:
        raise ArtistAuthError("Missing admin authorization")
    supplied_key = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(supplied_key, configured_key):
        raise ArtistAuthError("Invalid admin authorization")


def issue_oauth_state(artist_id: str, provider: str) -> str:
    """Create a short-lived signed OAuth state bound to artist and provider."""
    issued_at = int(time.time())
    payload = {
        "sub": artist_id,
        "provider": provider,
        "iat": issued_at,
        "exp": issued_at + 600,
        "ver": 1,
    }
    encoded = _b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    signature = _b64encode(
        hmac.new(
            _required_secret("PLMKR_SESSION_SECRET"),
            encoded.encode(),
            hashlib.sha256,
        ).digest()
    )
    return f"{encoded}.{signature}"


def decode_oauth_state(state: str, provider: str) -> str:
    """Validate signed OAuth state and return its bound artist."""
    try:
        encoded, supplied_signature = state.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(
                _required_secret("PLMKR_SESSION_SECRET"),
                encoded.encode(),
                hashlib.sha256,
            ).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ArtistAuthError("Invalid OAuth state")

        payload = json.loads(_b64decode(encoded))
        if payload.get("ver") != 1:
            raise ArtistAuthError("Invalid OAuth state")
        if payload.get("provider") != provider:
            raise ArtistAuthError("Invalid OAuth provider state")
        if not isinstance(payload.get("sub"), str) or not payload["sub"]:
            raise ArtistAuthError("Invalid OAuth state")
        if not isinstance(payload.get("exp"), int) or payload["exp"] <= int(time.time()):
            raise ArtistAuthError("OAuth state expired")
        return payload["sub"]
    except ArtistAuthError:
        raise
    except Exception as exc:
        raise ArtistAuthError("Invalid OAuth state") from exc
