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
