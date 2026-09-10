from fastapi import Request
"""
PLMKR Social Service — Phase 3
Handles social post scheduling (Buffer API), Riley persona post generation,
weekly report aggregation, and Claude Sonnet-powered weekly synthesis.

Same architecture as pr_service.py — self-contained, no circular imports.
Tables always live in SQLite. Buffer tokens stored in artist profile.
"""

import asyncio
from artist_identity import (
    ArtistAuthError,
    decode_oauth_state,
    identity_configured,
    issue_oauth_state,
    require_artist_scope,
)
import os
import re
import json
import uuid
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import httpx

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
import anthropic
from anthropic_utils import _anthropic_call_with_retry

log = logging.getLogger("social_service")

# ── Config ────────────────────────────────────────────────────────────────────
_DB_PATH       = Path(os.environ.get("DB_PATH", "/data/memory.db"))
_DATABASE_URL  = os.environ.get("DATABASE_URL", "")
_ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
_MODEL_HAIKU   = "claude-haiku-4-5-20251001"
_MODEL_SONNET  = "claude-sonnet-4-6"           # used for weekly reports (synthesis quality)

_BUFFER_CLIENT_ID     = os.environ.get("BUFFER_CLIENT_ID", "")
_BUFFER_CLIENT_SECRET = os.environ.get("BUFFER_CLIENT_SECRET", "")
_BUFFER_REDIRECT_URI  = os.environ.get("BUFFER_REDIRECT_URI", "")
_BUFFER_AUTH_URL      = "https://bufferapp.com/oauth2/authorize"
_BUFFER_TOKEN_URL     = "https://api.bufferapp.com/1/oauth2/token.json"
_BUFFER_POST_URL      = "https://api.bufferapp.com/1/updates/create.json"
_BUFFER_PROFILES_URL  = "https://api.bufferapp.com/1/profiles.json"
# Keep provider-controlled discovery data bounded before it reaches the
# artist-facing selector or becomes part of an approved execution binding.
_MAX_BUFFER_PROFILES     = 100
_MAX_BUFFER_PROFILE_ID   = 256
_MAX_BUFFER_PROFILE_TEXT = 512
_MAX_BUFFER_AVATAR_URL   = 2048
# R-26: feature flag for real Buffer HTTP client (BUFFER_LIVE=false default — safe)
_BUFFER_LIVE          = os.environ.get("BUFFER_LIVE", "false").lower() == "true"

_SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "").lower() == "true"
_SCHEDULER_DRY_RUN = os.environ.get("SCHEDULER_ENABLED", "").lower() == "dry_run"

# R-28: configurable weekly report schedule (defaults: Sunday 18:00 UTC)
_WEEKLY_REPORT_DAY    = os.environ.get("WEEKLY_REPORT_DAY",      "sun").strip().lower()
_WEEKLY_REPORT_HOUR   = int(os.environ.get("WEEKLY_REPORT_HOUR_UTC", "18"))
_WEEKLY_REPORT_MINUTE = int(os.environ.get("WEEKLY_REPORT_MINUTE",   "0"))

router = APIRouter()


# ═══════════════════════════════════════════════════════════════════════════════
# DB: Social + Report tables (always SQLite)
# ═══════════════════════════════════════════════════════════════════════════════

def init_social_db():
    """Create social_posts and weekly_reports tables. Idempotent."""
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS social_posts (
            id               TEXT PRIMARY KEY,
            artist_id        TEXT NOT NULL,
            platform         TEXT NOT NULL,
            content          TEXT NOT NULL,
            media_url        TEXT DEFAULT '',
            status           TEXT DEFAULT 'draft',
            scheduled_at     TEXT,
            posted_at        TEXT,
            post_url         TEXT DEFAULT '',
            engagement_stats TEXT DEFAULT '{}',
            buffer_update_id TEXT DEFAULT '',
            created_at       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_social_posts_artist "
        "ON social_posts (artist_id)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weekly_reports (
            id              TEXT PRIMARY KEY,
            artist_id       TEXT NOT NULL,
            week_start      TEXT NOT NULL,
            week_end        TEXT NOT NULL,
            summary         TEXT DEFAULT '{}',
            insights        TEXT DEFAULT '',
            recommendations TEXT DEFAULT '',
            momentum_score  INTEGER DEFAULT 5,
            headline        TEXT DEFAULT '',
            highlights      TEXT DEFAULT '[]',
            generated_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_weekly_reports_artist "
        "ON weekly_reports (artist_id)"
    )
    # Schema migration for existing DBs — weekly_reports table
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(weekly_reports)").fetchall()}
    for col, ddl in [
        ("momentum_score", "INTEGER DEFAULT 5"),
        ("headline",       "TEXT DEFAULT ''"),
        ("highlights",     "TEXT DEFAULT '[]'"),
    ]:
        if col not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE weekly_reports ADD COLUMN {col} {ddl}")
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "duplicate column name" not in msg and "no such table" not in msg:
                    raise RuntimeError(f"Migration failure on table weekly_reports ({col}): {e}") from e
    # Schema migration — artists table: per-artist timezone support
    existing_artist_cols = {row[1] for row in conn.execute("PRAGMA table_info(artists)").fetchall()}
    if "timezone" not in existing_artist_cols:
        try:
            conn.execute("ALTER TABLE artists ADD COLUMN timezone TEXT DEFAULT 'UTC'")
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "duplicate column name" not in msg and "no such table" not in msg:
                raise RuntimeError(f"Migration failure on table artists (timezone): {e}") from e
    conn.commit()
    conn.close()
    log.info("db_ready", extra={"event": "db_ready", "svc": "social_service"})


# ── Artist timezone helper ────────────────────────────────────────────────────

def _get_artist_timezone(artist_id: str) -> str:
    """Return the artist's stored IANA timezone string, defaulting to 'UTC'."""
    conn = sqlite3.connect(str(_DB_PATH))
    cur  = conn.cursor()
    try:
        cur.execute("SELECT timezone FROM artists WHERE artist_id=?", (artist_id,))
        row = cur.fetchone()
        return (row[0] or "UTC") if row else "UTC"
    except sqlite3.OperationalError:
        return "UTC"  # timezone column not yet migrated
    finally:
        conn.close()


def _week_boundaries_in_tz(tz_name: str) -> tuple[str, str]:
    """Return (week_start, week_end) ISO strings for the most recent Sunday week
    in the given IANA timezone.  Falls back to UTC on invalid tz.

    The global scheduler fires at Sunday 18:00 UTC; this function computes the
    correct week boundary using the artist's local timezone so the 'week' covers
    the right 7-day span for them.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        tz = ZoneInfo("UTC")

    now_local = datetime.now(tz)
    # Walk back to most recent Sunday (weekday 6)
    days_since_sunday = (now_local.weekday() + 1) % 7
    last_sunday = now_local - timedelta(days=days_since_sunday)

    week_end   = last_sunday.replace(hour=23, minute=59, second=59, microsecond=0)
    week_start = (week_end - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)

    return (week_start.strftime("%Y-%m-%dT%H:%M:%S"),
            week_end.strftime("%Y-%m-%dT%H:%M:%S"))


# ── Artist data helpers (same Postgres/SQLite routing pattern) ────────────────

def _load_artist_data(artist_id: str) -> dict:
    if _DATABASE_URL:
        try:
            import psycopg2
            with psycopg2.connect(_DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT data FROM artists WHERE artist_id = %s", (artist_id,)
                    )
                    row = cur.fetchone()
            return dict(row[0]) if row else {}
        except Exception:
            pass
    conn = sqlite3.connect(str(_DB_PATH))
    cur  = conn.cursor()
    cur.execute("SELECT data FROM artists WHERE artist_id=?", (artist_id,))
    row  = cur.fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}


def _save_artist_data(artist_id: str, data: dict):
    serialized = json.dumps(data)
    if _DATABASE_URL:
        try:
            import psycopg2
            with psycopg2.connect(_DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO artists (artist_id, data) VALUES (%s, %s) "
                        "ON CONFLICT (artist_id) DO UPDATE SET data = EXCLUDED.data",
                        (artist_id, json.dumps(data)),
                    )
            return
        except Exception:
            pass
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        "INSERT OR REPLACE INTO artists (artist_id, data) VALUES (?,?)",
        (artist_id, serialized),
    )
    conn.commit()
    conn.close()


# ── JSON parse helper ─────────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    return json.loads(text)


# ═══════════════════════════════════════════════════════════════════════════════
# Unit 3.1 — SocialPost CRUD
# ═══════════════════════════════════════════════════════════════════════════════

_SP_COLS = [
    "id", "artist_id", "platform", "content", "media_url", "status",
    "scheduled_at", "posted_at", "post_url", "engagement_stats",
    "buffer_update_id", "created_at",
]


def _sp_row_to_dict(row, cols) -> dict:
    d = dict(zip(cols, row))
    try:
        d["engagement_stats"] = json.loads(d["engagement_stats"]) if d["engagement_stats"] else {}
    except Exception:
        d["engagement_stats"] = {}
    return d


def _db_list_posts(
    artist_id: str, platform: str = "", status: str = ""
) -> list[dict]:
    conn   = sqlite3.connect(str(_DB_PATH))
    cur    = conn.cursor()
    q      = f"SELECT {','.join(_SP_COLS)} FROM social_posts WHERE artist_id=?"
    params: list = [artist_id]
    if platform:
        q += " AND platform=?"; params.append(platform)
    if status:
        q += " AND status=?"; params.append(status)
    q += " ORDER BY scheduled_at ASC, created_at DESC"
    cur.execute(q, params)
    rows = cur.fetchall()
    conn.close()
    return [_sp_row_to_dict(r, _SP_COLS) for r in rows]


def _db_get_post(post_id: str) -> dict:
    conn = sqlite3.connect(str(_DB_PATH))
    cur  = conn.cursor()
    cur.execute(
        f"SELECT {','.join(_SP_COLS)} FROM social_posts WHERE id=?", (post_id,)
    )
    row = cur.fetchone()
    conn.close()
    return _sp_row_to_dict(row, _SP_COLS) if row else {}


def _db_create_post(p: dict) -> dict:
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """INSERT INTO social_posts
           (id,artist_id,platform,content,media_url,status,scheduled_at,
            buffer_update_id)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            p["id"], p["artist_id"], p["platform"], p["content"],
            p.get("media_url", ""), p.get("status", "draft"),
            p.get("scheduled_at"), p.get("buffer_update_id", ""),
        ),
    )
    conn.commit()
    conn.close()
    return p


def _db_update_post(post_id: str, updates: dict):
    if "engagement_stats" in updates and isinstance(updates["engagement_stats"], dict):
        updates = {**updates, "engagement_stats": json.dumps(updates["engagement_stats"])}
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [post_id]
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(f"UPDATE social_posts SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()


def _db_delete_post(post_id: str):
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("DELETE FROM social_posts WHERE id=?", (post_id,))
    conn.commit()
    conn.close()


# ── SocialPost endpoints ──────────────────────────────────────────────────────

class SocialPostIn(BaseModel):
    artist_id: str
    platform: str
    content: str
    media_url: str = ""
    scheduled_at: Optional[str] = None
    status: str = "draft"


class SocialPostPatch(BaseModel):
    content: Optional[str] = None
    media_url: Optional[str] = None
    status: Optional[str] = None
    scheduled_at: Optional[str] = None
    posted_at: Optional[str] = None
    post_url: Optional[str] = None
    engagement_stats: Optional[dict] = None


@router.get("/api/social/posts", tags=["social"])
def list_posts(artist_id: str, platform: str = "", status: str = "", request: Request = None):
    try:
        artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"posts": _db_list_posts(artist_id, platform=platform, status=status)}


@router.get("/api/social/posts/{post_id}", tags=["social"])
def get_post(post_id: str, request: Request = None):
    p = _db_get_post(post_id)
    if not p:
        raise HTTPException(status_code=404, detail="Post not found")
    try:
        require_artist_scope(request, p["artist_id"])
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return p


@router.post("/api/social/posts", status_code=201, tags=["social"])
def create_post(p: SocialPostIn, request: Request = None):
    try:
        scoped_artist_id = require_artist_scope(request, p.artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    new_id = str(uuid.uuid4())
    row    = {**p.model_dump(), "artist_id": scoped_artist_id, "id": new_id}
    _db_create_post(row)
    return row


@router.patch("/api/social/posts/{post_id}", tags=["social"])
def patch_post(post_id: str, patch: SocialPostPatch, request: Request = None):
    existing = _db_get_post(post_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Post not found")
    try:
        require_artist_scope(request, existing["artist_id"])
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    updates = {k: v for k, v in patch.model_dump().items() if v is not None}
    if updates:
        _db_update_post(post_id, updates)
    return {**existing, **updates}


@router.delete("/api/social/posts/{post_id}", status_code=204, tags=["social"])
def delete_post(post_id: str, request: Request = None):
    p = _db_get_post(post_id)
    if not p:
        raise HTTPException(status_code=404, detail="Post not found")
    try:
        require_artist_scope(request, p["artist_id"])
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    _db_delete_post(post_id)


# ═══════════════════════════════════════════════════════════════════════════════
# Unit 3.2 — Buffer API integration (mocked send, real OAuth flow stubs)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_buffer_tokens(artist_id: str) -> dict:
    profile = _load_artist_data(artist_id)
    # Treat legacy/corrupt artist data as disconnected. Buffer credentials
    # are only usable when the stored value has the exact shape this service
    # owns; callers must never crash or attempt a provider request because a
    # profile file contains an unexpected JSON value.
    tokens = profile.get("buffer_tokens", {})
    if not isinstance(tokens, dict):
        return {}
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return {}
    return {"access_token": access_token.strip()}


def _save_buffer_tokens(artist_id: str, tokens: dict):
    profile = _load_artist_data(artist_id)
    profile["buffer_tokens"] = tokens
    _save_artist_data(artist_id, profile)


class BufferNotConnected(Exception):
    pass


class BufferAuthExpired(Exception):
    """The artist's Buffer authorization is no longer accepted."""


@router.get("/api/buffer/auth", tags=["buffer"])
def buffer_auth(artist_id: str, request: Request = None):
    """Redirect the authenticated artist to Buffer OAuth."""
    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if not _BUFFER_CLIENT_ID:
        raise HTTPException(
            status_code=503,
            detail="BUFFER_CLIENT_ID not configured",
        )

    state = (
        issue_oauth_state(scoped_artist_id, "buffer")
        if identity_configured()
        else scoped_artist_id
    )
    params = urlencode({
        "client_id": _BUFFER_CLIENT_ID,
        "redirect_uri": _BUFFER_REDIRECT_URI,
        "response_type": "code",
        "state": state,
    })
    return RedirectResponse(url=f"{_BUFFER_AUTH_URL}?{params}")



@router.get("/api/buffer/callback", tags=["buffer"])
async def buffer_callback(code: str, state: str):
    """Exchange Buffer code using signed artist-bound state."""
    if not _BUFFER_CLIENT_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Buffer OAuth not configured",
        )

    try:
        artist_id = (
            decode_oauth_state(state, "buffer")
            if identity_configured()
            else state
        )
    except ArtistAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _BUFFER_TOKEN_URL,
                data={
                    "client_id": _BUFFER_CLIENT_ID,
                    "client_secret": _BUFFER_CLIENT_SECRET,
                    "redirect_uri": _BUFFER_REDIRECT_URI,
                    "code": code,
                    "grant_type": "authorization_code",
                },
                timeout=15,
            )
        if response.status_code < 200 or response.status_code >= 300:
            log.error("buffer_token_exchange_error", extra={
                "event": "buffer_token_exchange_error",
                "status": response.status_code,
            })
            raise HTTPException(
                status_code=502,
                detail="Buffer authorization could not be completed",
            )
        tokens = response.json()
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=500,
            detail="Buffer token exchange failed",
        )

    access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(access_token, str) or not access_token.strip():
        log.error("buffer_token_exchange_invalid_response", extra={
            "event": "buffer_token_exchange_invalid_response",
        })
        raise HTTPException(
            status_code=502,
            detail="Buffer authorization returned an invalid token response",
        )

    _save_buffer_tokens(artist_id, {
        "access_token": access_token.strip(),
        "stored_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"status": "connected", "artist_id": artist_id}



@router.get("/api/buffer/status", tags=["buffer"])
def buffer_status(artist_id: str, request: Request = None):
    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    tokens = _load_buffer_tokens(scoped_artist_id)
    return {
        "connected": bool(tokens.get("access_token")),
        "artist_id": scoped_artist_id,
    }



@router.delete("/api/buffer/connection", tags=["buffer"])
def buffer_disconnect(artist_id: str, request: Request = None):
    """Revoke the artist's stored Buffer connection without changing other profile data."""
    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    profile = _load_artist_data(scoped_artist_id)
    if not isinstance(profile, dict):
        profile = {"artist_id": scoped_artist_id}
    else:
        profile = dict(profile)
    # Keep the profile record and all unrelated provider connections intact.
    profile["buffer_tokens"] = {}
    _save_artist_data(scoped_artist_id, profile)
    log.info("buffer_connection_revoked", extra={
        "event": "buffer_connection_revoked",
        "artist_id": scoped_artist_id,
    })
    return {"status": "disconnected", "connected": False, "artist_id": scoped_artist_id}


async def _buffer_list_profiles(artist_id: str) -> list[dict]:
    """Return Buffer profiles available to the artist's connected account."""
    tokens = _load_buffer_tokens(artist_id)
    access_token = tokens.get("access_token")
    if not access_token:
        raise BufferNotConnected(f"Artist {artist_id} has not connected Buffer")

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.get(
                _BUFFER_PROFILES_URL,
                params={"access_token": access_token},
            )
    except httpx.HTTPError as exc:
        log.error("buffer_profiles_transport_error", extra={
            "event": "buffer_profiles_transport_error",
            "error": type(exc).__name__,
        })
        raise RuntimeError("Buffer profile discovery is temporarily unavailable") from exc

    if resp.status_code in (401, 403):
        log.warning("buffer_profiles_auth_expired", extra={
            "event": "buffer_profiles_auth_expired",
            "status": resp.status_code,
        })
        raise BufferAuthExpired(
            "Buffer authorization expired; reconnect Buffer before continuing"
        )
    if resp.status_code != 200:
        log.error("buffer_profiles_error", extra={
            "event": "buffer_profiles_error",
            "status": resp.status_code,
            "body": resp.text[:200],
        })
        raise RuntimeError(f"Buffer API returned {resp.status_code}")

    try:
        data = resp.json()
    except Exception:
        raise RuntimeError("Buffer API returned non-JSON profile response")

    if (
        not isinstance(data, list)
        or len(data) > _MAX_BUFFER_PROFILES
        or any(not isinstance(profile, dict) for profile in data)
    ):
        raise RuntimeError("Buffer API returned invalid profile response")

    profiles = []
    seen_profile_ids = set()
    for profile in data:
        profile_id = profile.get("id")
        if (
            not isinstance(profile_id, str)
            or not profile_id.strip()
            or len(profile_id.strip()) > _MAX_BUFFER_PROFILE_ID
        ):
            raise RuntimeError("Buffer API returned invalid profile response")
        normalized_id = profile_id.strip()
        if normalized_id in seen_profile_ids:
            raise RuntimeError("Buffer API returned duplicate profile IDs")
        seen_profile_ids.add(normalized_id)
        normalized = {"id": normalized_id}
        for field in (
            "service", "formatted_username", "service_username", "username",
            "name", "avatar_url",
        ):
            value = profile.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            normalized_value = value.strip()
            maximum = (
                _MAX_BUFFER_AVATAR_URL
                if field == "avatar_url"
                else _MAX_BUFFER_PROFILE_TEXT
            )
            if len(normalized_value) > maximum:
                raise RuntimeError("Buffer API returned invalid profile response")
            normalized[field] = normalized_value
        profiles.append(normalized)

    return profiles


@router.get("/api/buffer/profiles", tags=["buffer"])
async def buffer_profiles(artist_id: str, request: Request = None):
    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        profiles = await _buffer_list_profiles(scoped_artist_id)
    except BufferNotConnected:
        raise HTTPException(
            status_code=409,
            detail="Buffer account not connected",
        )
    except BufferAuthExpired as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return {"artist_id": scoped_artist_id, "profiles": profiles}



async def _buffer_post_real(
    access_token: str,
    content: str,
    profile_ids: list[str],
    media_url: str = "",
    scheduled_at: Optional[str] = None,
) -> dict:
    """POST to Buffer API with 429 retry (max 2 attempts) and 10s timeout."""
    payload: dict = {
        "text":           content,
        "profile_ids[]":  profile_ids,
        "access_token":   access_token,
    }
    if scheduled_at:
        dt = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))
        payload["scheduled_at"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if media_url:
        payload["media[link]"] = media_url

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.post(_BUFFER_POST_URL, data=payload)

        if resp.status_code == 429:
            if attempt < max_attempts:
                wait = 2 ** attempt
                log.warning("buffer_rate_limited", extra={
                    "event": "buffer_rate_limited", "attempt": attempt, "wait_s": wait,
                })
                await asyncio.sleep(wait)
                continue
            log.error("buffer_rate_limit_exhausted", extra={
                "event": "buffer_rate_limit_exhausted", "attempts": attempt,
            })
            raise RuntimeError("Buffer API rate limit exceeded after retries")

        if resp.status_code in (401, 403):
            log.warning("buffer_post_auth_expired", extra={
                "event": "buffer_post_auth_expired",
                "status": resp.status_code,
            })
            raise BufferAuthExpired(
                "Buffer authorization expired; reconnect Buffer before continuing"
            )

        if resp.status_code != 200:
            log.error("buffer_post_error", extra={
                "event": "buffer_post_error", "status": resp.status_code, "body": resp.text[:200],
            })
            raise RuntimeError(f"Buffer API returned {resp.status_code}")

        try:
            data = resp.json()
        except Exception:
            log.error("buffer_json_error", extra={
                "event": "buffer_json_error", "body": resp.text[:200],
            })
            raise RuntimeError("Buffer API returned non-JSON response")

        log.info("buffer_post_queued", extra={
            "event": "buffer_post_queued", "update_id": data.get("id"),
        })
        return data

    raise RuntimeError("Buffer post failed after max attempts")  # unreachable but satisfies type checker


async def _buffer_schedule_post(
    artist_id: str,
    content: str,
    profile_ids: list[str],
    media_url: str = "",
    scheduled_at: Optional[str] = None,
) -> dict:
    """Schedule a post via Buffer API.

    Routing logic (R-26):
      BUFFER_LIVE=false (default) → mock response (safe)
      SCHEDULER_ENABLED=dry_run  → log would_have_posted, mock response
      BUFFER_LIVE=true           → real Buffer HTTP call using artist OAuth token
    """
    tokens = _load_buffer_tokens(artist_id)
    if not tokens.get("access_token"):
        raise BufferNotConnected(f"Artist {artist_id} has not connected Buffer")

    if not _BUFFER_LIVE:
        log.info("buffer_post_mocked", extra={
            "event": "buffer_post_mocked", "artist_id": artist_id, "reason": "BUFFER_LIVE not enabled",
        })
        return {
            "id":     str(uuid.uuid4()),
            "status": "buffer_queued",
            "mocked": True,
            "text":   content[:60] + ("…" if len(content) > 60 else ""),
        }

    if _SCHEDULER_DRY_RUN:
        log.info("would_have_posted", extra={
            "event": "would_have_posted", "artist_id": artist_id, "dry_run": True,
        })
        return {
            "id":     str(uuid.uuid4()),
            "status": "buffer_queued",
            "mocked": True,
            "dry_run": True,
            "text":   content[:60] + ("…" if len(content) > 60 else ""),
        }

    return await _buffer_post_real(
        access_token=tokens["access_token"],
        content=content,
        profile_ids=profile_ids,
        media_url=media_url,
        scheduled_at=scheduled_at,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Unit 3.4 — generateSocialPost() — Riley persona
# ═══════════════════════════════════════════════════════════════════════════════

_PLATFORM_LIMITS = {
    "twitter":   280,
    "instagram": 2200,
    "tiktok":    2200,
    "facebook":  1000,
    "linkedin":  3000,
}

_PLATFORM_STYLE = {
    "twitter":   "punchy, direct, hook in first 8 words, no hashtag overload (max 2), conversational",
    "instagram": "warm and visual, lead with emotion, use 3-5 relevant hashtags at end, emojis ok",
    "tiktok":    "energetic, trend-aware, speak to Gen Z/Millennial, hook is everything, casual",
    "facebook":  "slightly longer form, storytelling tone, community-focused, less hashtags",
    "linkedin":  "professional but human, music industry angle, milestone-focused, no hashtag overload (max 3), first-person narrative",
}

_RILEY_SYSTEM = (
    "You are Riley, Social Media Manager at Playmaker. You write platform-specific social posts "
    "for artists that sound authentic — not like marketing copy.\n\n"
    "Rules:\n"
    "- Match the platform's native voice exactly (Twitter = punchy, Instagram = visual + warm, "
    "TikTok = energetic + trend-aware, Facebook = community-focused)\n"
    "- Write IN the artist's voice — use their genre, personality, and bio as character reference\n"
    "- Never sound like a press release. Sound like the artist typed it themselves\n"
    "- optimal_posting_window: best day + time window as a string (e.g. 'Tuesday 6-9pm ET')\n"
    "- suggested_media_prompt: a one-sentence image/video description for AI generation or photographer brief\n\n"
    "Return ONLY valid JSON:\n"
    '{"content":"...","suggested_media_prompt":"...","optimal_posting_window":"..."}'
)


async def generate_social_post(
    artist_profile: dict,
    platform: str,
    context: dict,
    tone: str = "authentic",
) -> dict:
    """
    Draft one platform-specific social post for Riley.
    Returns {content, suggested_media_prompt, optimal_posting_window}.
    """
    platform   = platform.lower()
    char_limit = _PLATFORM_LIMITS.get(platform, 280)
    style_note = _PLATFORM_STYLE.get(platform, "")

    artist_name = artist_profile.get("artist_name", "The artist")
    genre       = artist_profile.get("genre", "")
    bio         = (artist_profile.get("bio", "") or "")[:200]

    release = context.get("release", "")
    show    = context.get("show", "")
    news    = context.get("news", "")
    custom  = context.get("custom", "")

    prompt = (
        f"Artist: {artist_name}\n"
        f"Genre: {genre}\n"
        f"Bio snippet: {bio}\n"
        f"Tone: {tone}\n"
        f"Platform: {platform} (character limit: {char_limit})\n"
        f"Platform style: {style_note}\n"
    )
    if release:
        prompt += f"Context — Release: {release}\n"
    if show:
        prompt += f"Context — Show: {show}\n"
    if news:
        prompt += f"Context — News: {news}\n"
    if custom:
        prompt += f"Context — Custom: {custom}\n"
    prompt += "\nWrite the post. Return JSON only."

    _client = anthropic.Anthropic(api_key=_ANTHROPIC_KEY)
    resp    = await _anthropic_call_with_retry(
        _client,
        model=_MODEL_HAIKU,
        max_tokens=512,
        system=_RILEY_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
    )
    result = _parse_json(resp.content[0].text)
    # Enforce character limit on content
    content = result.get("content", "")
    if len(content) > char_limit:
        result["content"] = content[:char_limit - 1] + "…"
    return result


class GeneratePostRequest(BaseModel):
    artist_id: str
    platform: str
    context: dict = {}
    tone: str = "authentic"


@router.post("/api/social/posts/generate", tags=["social"])
async def api_generate_post(req: GeneratePostRequest, request: Request = None):
    try:
        artist_id = require_artist_scope(request, req.artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    artist = _load_artist_data(artist_id)
    try:
        draft = await generate_social_post(artist, req.platform, req.context, req.tone)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Post generation failed: {e}")
    return {**draft, "artist_id": artist_id, "platform": req.platform}


# ═══════════════════════════════════════════════════════════════════════════════
# Unit 3.5 — schedulePosts() batch orchestration
# ═══════════════════════════════════════════════════════════════════════════════

class BatchPostRequest(BaseModel):
    artist_id: str
    platforms: list[str]
    context: dict = {}
    tone: str = "authentic"
    posts_per_platform: int = 3
    schedule_buffer: bool = False  # True = send to Buffer, False = save as draft
    buffer_profile_ids: list[str] = []  # Buffer profile IDs per platform (artist configures)
    start_date: Optional[str] = None    # ISO date string — first post date, defaults to tomorrow


@router.post("/api/social/posts/batch", tags=["social"])
async def schedule_posts(req: BatchPostRequest, request: Request = None):
    """Generate draft posts; consequential Buffer scheduling uses operations only."""
    try:
        artist_id = require_artist_scope(request, req.artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    if req.schedule_buffer:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "durable_operation_required",
                "action_type": "social.buffer.schedule",
                "message": "Direct Buffer scheduling is disabled. Save drafts, then create/approve durable operations.",
            },
        )
    artist  = _load_artist_data(artist_id)
    results: dict = {
        "generated": 0,
        "scheduled_via_buffer": 0,
        "errors": [],
        "post_ids": [],
    }

    start = (
        datetime.fromisoformat(req.start_date)
        if req.start_date
        else datetime.now(timezone.utc) + timedelta(days=1)
    )
    # Spread posts evenly across 7 days
    total_posts = len(req.platforms) * req.posts_per_platform
    if total_posts > 0:
        spacing = timedelta(days=7) / total_posts
    else:
        spacing = timedelta(days=1)

    post_index = 0
    for platform in req.platforms:
        for i in range(req.posts_per_platform):
            scheduled_at = (start + spacing * post_index).isoformat()
            post_index  += 1

            try:
                draft = await generate_social_post(
                    artist, platform, req.context, req.tone
                )
            except Exception as e:
                results["errors"].append(f"Generation failed for {platform} post {i+1}: {e}")
                continue

            post_id = str(uuid.uuid4())
            post    = {
                "id":           post_id,
                "artist_id":    artist_id,
                "platform":     platform,
                "content":      draft["content"],
                "media_url":    "",
                "status":       "draft",
                "scheduled_at": scheduled_at,
            }
            _db_create_post(post)
            results["generated"] += 1
            results["post_ids"].append(post_id)

            if req.schedule_buffer and req.buffer_profile_ids:
                try:
                    buf = await _buffer_schedule_post(
                        artist_id, draft["content"],
                        req.buffer_profile_ids, scheduled_at=scheduled_at,
                    )
                    _db_update_post(post_id, {
                        "status":           "scheduled",
                        "buffer_update_id": buf.get("id", ""),
                    })
                    results["scheduled_via_buffer"] += 1
                except BufferNotConnected:
                    results["errors"].append(
                        f"Buffer not connected — {platform} post saved as draft"
                    )
                except Exception as e:
                    results["errors"].append(f"Buffer scheduling failed for {platform}: {e}")

    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Unit 3.6 — WeeklyReport schema + endpoints
# ═══════════════════════════════════════════════════════════════════════════════

_WR_COLS = [
    "id", "artist_id", "week_start", "week_end",
    "summary", "insights", "recommendations",
    "momentum_score", "headline", "highlights", "generated_at",
]


def _wr_row_to_dict(row, cols) -> dict:
    d = dict(zip(cols, row))
    try:
        d["summary"] = json.loads(d["summary"]) if d["summary"] else {}
    except Exception:
        d["summary"] = {}
    try:
        d["highlights"] = json.loads(d["highlights"]) if d["highlights"] else []
    except Exception:
        d["highlights"] = []
    return d


def _db_list_reports(artist_id: str, limit: int = 12) -> list[dict]:
    conn = sqlite3.connect(str(_DB_PATH))
    cur  = conn.cursor()
    cur.execute(
        f"SELECT {','.join(_WR_COLS)} FROM weekly_reports "
        "WHERE artist_id=? ORDER BY week_start DESC LIMIT ?",
        (artist_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [_wr_row_to_dict(r, _WR_COLS) for r in rows]


def _db_get_report(report_id: str) -> dict:
    conn = sqlite3.connect(str(_DB_PATH))
    cur  = conn.cursor()
    cur.execute(
        f"SELECT {','.join(_WR_COLS)} FROM weekly_reports WHERE id=?", (report_id,)
    )
    row = cur.fetchone()
    conn.close()
    return _wr_row_to_dict(row, _WR_COLS) if row else {}


def _db_save_report(r: dict):
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """INSERT OR REPLACE INTO weekly_reports
           (id,artist_id,week_start,week_end,summary,insights,recommendations,
            momentum_score,headline,highlights)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            r["id"], r["artist_id"], r["week_start"], r["week_end"],
            json.dumps(r.get("summary", {})),
            r.get("insights", ""), r.get("recommendations", ""),
            r.get("momentum_score", 5),
            r.get("headline", ""),
            json.dumps(r.get("highlights", [])),
        ),
    )
    conn.commit()
    conn.close()


@router.get("/api/reports/weekly", tags=["reports"])
def list_weekly_reports(artist_id: str, limit: int = 12, request: Request = None):
    try:
        artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"reports": _db_list_reports(artist_id, limit=limit)}


@router.get("/api/reports/weekly/{report_id}", tags=["reports"])
def get_weekly_report(report_id: str, request: Request = None):
    r = _db_get_report(report_id)
    if not r:
        raise HTTPException(status_code=404, detail="Report not found")
    try:
        require_artist_scope(request, r["artist_id"])
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return r


class GenerateReportRequest(BaseModel):
    artist_id: str
    week_start: Optional[str] = None  # ISO date string, defaults to last Monday
    week_end: Optional[str] = None    # ISO date string, defaults to last Sunday


@router.post("/api/reports/weekly/generate", tags=["reports"])
async def api_generate_weekly_report(req: GenerateReportRequest, request: Request = None):
    try:
        artist_id = require_artist_scope(request, req.artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        report = await generate_weekly_report(artist_id, req.week_start, req.week_end)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Report generation failed: {e}")
    return report


# ═══════════════════════════════════════════════════════════════════════════════
# Unit 3.7 — generateWeeklyReport() — Claude Sonnet synthesis
# ═══════════════════════════════════════════════════════════════════════════════

_REPORT_SYSTEM = (
    "You are Marcus, Artist Manager at Playmaker. You write weekly management reports "
    "for artists — clear, actionable, no fluff.\n\n"
    "Structure your response as valid JSON:\n"
    "{\n"
    '  "headline": "one sentence summary of the week",\n'
    '  "highlights": ["bullet 1", "bullet 2", "bullet 3"],\n'
    '  "insights": "2-3 paragraph narrative analysis — what the data means for the artist\'s trajectory",\n'
    '  "recommendations": "3-5 concrete actions for next week, numbered, specific",\n'
    '  "momentum_score": 1-10\n'
    "}\n\n"
    "momentum_score: 1=stalled, 5=steady, 10=breakthrough week. "
    "Be honest — a 3/10 week needs honest feedback, not spin. "
    "Base analysis on the actual metrics provided."
)


def _aggregate_week_data(artist_id: str, week_start: str, week_end: str) -> dict:
    """Query all service tables for the given artist + week window."""
    conn = sqlite3.connect(str(_DB_PATH))
    cur  = conn.cursor()

    def count_where(table, extra_where="", params=None):
        base   = f"SELECT COUNT(*) FROM {table} WHERE artist_id=? AND created_at>=? AND created_at<=?"
        qparams = [artist_id, week_start, week_end]
        if extra_where:
            base   += f" AND {extra_where}"
            qparams += (params or [])
        cur.execute(base, qparams)
        row = cur.fetchone()
        return row[0] if row else 0

    def count_status(table, status):
        cur.execute(
            f"SELECT COUNT(*) FROM {table} WHERE artist_id=? AND status=? "
            f"AND created_at>=? AND created_at<=?",
            (artist_id, status, week_start, week_end),
        )
        row = cur.fetchone()
        return row[0] if row else 0

    # All queries wrapped — tables may not exist in test DBs or fresh deploys
    pitches_sent = pitches_replied = 0
    try:
        pitches_sent    = count_status("pitches", "sent")
        pitches_replied = count_status("pitches", "replied")
    except Exception:
        pass

    pr_sent = pr_replied = pr_featured = 0
    try:
        pr_sent     = count_status("pr_outreach", "sent")
        pr_replied  = count_status("pr_outreach", "replied")
        pr_featured = count_status("pr_outreach", "featured")
    except Exception:
        pass

    bk_sent = bk_replied = bk_booked = 0
    try:
        bk_sent    = count_status("booking_inquiries", "sent")
        bk_replied = count_status("booking_inquiries", "replied")
        bk_booked  = count_status("booking_inquiries", "booked")
    except Exception:
        pass

    social_posted = social_scheduled = 0
    try:
        social_posted    = count_status("social_posts", "posted")
        social_scheduled = count_status("social_posts", "scheduled")
    except Exception:
        pass

    conn.close()

    return {
        "week_start":   week_start,
        "week_end":     week_end,
        "pitches": {
            "sent":    pitches_sent,
            "replied": pitches_replied,
            "reply_rate": round(pitches_replied / pitches_sent, 2) if pitches_sent else 0.0,
        },
        "pr_outreach": {
            "sent":     pr_sent,
            "replied":  pr_replied,
            "featured": pr_featured,
        },
        "booking": {
            "sent":    bk_sent,
            "replied": bk_replied,
            "booked":  bk_booked,
        },
        "social": {
            "posted":    social_posted,
            "scheduled": social_scheduled,
        },
    }


async def generate_weekly_report(
    artist_id: str,
    week_start: Optional[str] = None,
    week_end: Optional[str] = None,
) -> dict:
    """
    Aggregate the artist's week data and synthesize with Claude Sonnet.
    Saves report to DB. Returns the full report dict.
    """
    now = datetime.now(timezone.utc)
    if not week_start or not week_end:
        # Use artist's timezone for week boundary so the 'week' covers the right
        # 7-day span. Scheduler fires globally at Sunday 18:00 UTC; each artist
        # gets their own local week.
        tz_name = _get_artist_timezone(artist_id)
        _ws, _we = _week_boundaries_in_tz(tz_name)
        if not week_end:
            week_end = _we
        if not week_start:
            week_start = _ws

    artist  = _load_artist_data(artist_id)
    metrics = _aggregate_week_data(artist_id, week_start, week_end)

    artist_name = artist.get("artist_name", "The artist")
    genre       = artist.get("genre", "")

    prompt = (
        f"Artist: {artist_name} | Genre: {genre}\n"
        f"Week: {week_start[:10]} → {week_end[:10]}\n\n"
        f"METRICS:\n{json.dumps(metrics, indent=2)}\n\n"
        "Write the weekly management report. Return JSON only."
    )

    _client = anthropic.Anthropic(api_key=_ANTHROPIC_KEY)
    resp    = await _anthropic_call_with_retry(
        _client,
        model=_MODEL_SONNET,
        max_tokens=1200,
        system=_REPORT_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    analysis = _parse_json(resp.content[0].text)

    report_id = str(uuid.uuid4())
    report    = {
        "id":              report_id,
        "artist_id":       artist_id,
        "week_start":      week_start,
        "week_end":        week_end,
        "summary":         metrics,
        "insights":        analysis.get("insights", ""),
        "recommendations": analysis.get("recommendations", ""),
        "generated_at":    now.isoformat(),
        "headline":        analysis.get("headline", ""),
        "highlights":      analysis.get("highlights", []),
        "momentum_score":  analysis.get("momentum_score", 5),
    }
    _db_save_report(report)
    log.info("weekly_report_generated", extra={"artist_id": artist_id,
             "action": "weekly_report", "result": "ok",
             "momentum_score": report["momentum_score"]})
    # Attempt email delivery — GmailNotConnected is non-fatal (report already saved)
    try:
        await _email_weekly_report(artist_id, report, artist)
    except Exception as e:
        log.warning("report_email_skipped", extra={
            "event": "report_email_skipped", "artist_id": artist_id, "reason": str(e),
        })
    return report


# ═══════════════════════════════════════════════════════════════════════════════
# Unit 3.8.1 — Weekly report email delivery
# ═══════════════════════════════════════════════════════════════════════════════

def _build_report_html(report: dict, artist_name: str) -> str:
    """Build an inline HTML email body for the weekly report."""
    week_start = report.get("week_start", "")[:10]
    week_end   = report.get("week_end",   "")[:10]
    headline   = report.get("headline",   "Weekly update")
    score      = report.get("momentum_score", 5)
    insights   = report.get("insights",   "").replace("\n", "<br>")
    recs       = report.get("recommendations", "").replace("\n", "<br>")
    highlights = report.get("highlights", [])
    summary    = report.get("summary", {})

    pitches = summary.get("pitches", {})
    pr      = summary.get("pr_outreach", {})
    booking = summary.get("booking", {})
    social  = summary.get("social", {})

    highlights_html = "".join(f"<li>{h}</li>" for h in highlights) if highlights else "<li>No highlights this week</li>"

    empty_state = (
        pitches.get("sent", 0) + pr.get("sent", 0) +
        booking.get("sent", 0) + social.get("posted", 0) == 0
    )

    if empty_state:
        insights = (
            "This is your first week with Playmaker — no outreach activity yet, but that's OK! "
            "Your agents are ready. Connect Gmail, add curators, and schedule your first pitch batch."
        )
        recs = (
            "1. Complete Gmail OAuth setup so your agents can send on your behalf.\n"
            "2. Ask Marcus to pitch 5-10 curators to get your first batch out.\n"
            "3. Set a weekly cadence — 3 pitches, 1 PR outreach, 4 social posts is a solid start."
        )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#f9fafb;margin:0;padding:24px}}
  .card{{background:#fff;border-radius:12px;padding:32px;max-width:600px;margin:0 auto;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  h1{{font-size:20px;color:#111;margin:0 0 4px}}
  .week{{font-size:13px;color:#6b7280;margin:0 0 24px}}
  .score{{display:inline-block;background:#f3f4f6;border-radius:8px;padding:4px 12px;font-size:14px;font-weight:600;color:#374151}}
  .metric-row{{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}}
  .metric{{background:#f9fafb;border-radius:8px;padding:12px 16px;flex:1;min-width:100px}}
  .metric-label{{font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:.5px}}
  .metric-value{{font-size:22px;font-weight:700;color:#111;margin-top:2px}}
  h2{{font-size:14px;font-weight:600;color:#374151;margin:24px 0 8px;text-transform:uppercase;letter-spacing:.5px}}
  ul{{margin:0;padding-left:20px}}
  li{{color:#374151;margin:4px 0;font-size:14px}}
  p{{color:#374151;font-size:14px;line-height:1.6;margin:0 0 12px}}
  .footer{{font-size:12px;color:#9ca3af;text-align:center;margin-top:32px}}
</style></head>
<body><div class="card">
  <h1>{headline}</h1>
  <div class="week">{week_start} → {week_end} &nbsp;|&nbsp; <span class="score">Momentum {score}/10</span></div>
  <div class="metric-row">
    <div class="metric"><div class="metric-label">Pitches sent</div><div class="metric-value">{pitches.get('sent',0)}</div></div>
    <div class="metric"><div class="metric-label">Replies</div><div class="metric-value">{pitches.get('replied',0)}</div></div>
    <div class="metric"><div class="metric-label">PR outreach</div><div class="metric-value">{pr.get('sent',0)}</div></div>
    <div class="metric"><div class="metric-label">Bookings</div><div class="metric-value">{booking.get('sent',0)}</div></div>
    <div class="metric"><div class="metric-label">Posts live</div><div class="metric-value">{social.get('posted',0)}</div></div>
  </div>
  <h2>Highlights</h2><ul>{highlights_html}</ul>
  <h2>Analysis</h2><p>{insights}</p>
  <h2>Next week</h2><p>{recs}</p>
  <div class="footer">Playmaker · {artist_name} · Week of {week_start}</div>
</div></body></html>"""


def _build_report_plain(report: dict, artist_name: str) -> str:
    """Plain-text fallback for the weekly report email."""
    week_start = report.get("week_start", "")[:10]
    week_end   = report.get("week_end",   "")[:10]
    headline   = report.get("headline",   "Weekly update")
    score      = report.get("momentum_score", 5)
    insights   = report.get("insights",   "")
    recs       = report.get("recommendations", "")
    summary    = report.get("summary", {})
    pitches    = summary.get("pitches", {})
    pr         = summary.get("pr_outreach", {})
    booking    = summary.get("booking", {})
    social     = summary.get("social", {})

    return (
        f"PLAYMAKER — Weekly Report\n"
        f"{artist_name} | {week_start} → {week_end} | Momentum: {score}/10\n\n"
        f"{headline}\n\n"
        f"METRICS\n"
        f"  Pitches sent:  {pitches.get('sent', 0)}\n"
        f"  Replies:       {pitches.get('replied', 0)}\n"
        f"  PR outreach:   {pr.get('sent', 0)}\n"
        f"  Bookings:      {booking.get('sent', 0)}\n"
        f"  Posts live:    {social.get('posted', 0)}\n\n"
        f"ANALYSIS\n{insights}\n\n"
        f"NEXT WEEK\n{recs}\n"
    )


async def _email_weekly_report(artist_id: str, report: dict, artist: dict):
    """
    Send the weekly report to the artist's email via Gmail.
    Raises GmailNotConnected if Gmail not linked — caller handles gracefully.
    """
    from pitch_service import send_email, GmailNotConnected
    artist_email = artist.get("email", "")
    if not artist_email:
        raise ValueError(f"No email address on artist profile for {artist_id}")

    artist_name = artist.get("artist_name", "Artist")
    week_start  = report.get("week_start", "")[:10]
    subject     = f"Your Playmaker Weekly Report — {week_start}"
    html_body   = _build_report_html(report, artist_name)
    plain_body  = _build_report_plain(report, artist_name)

    await send_email(
        artist_id=artist_id,
        to=artist_email,
        subject=subject,
        body=html_body,
    )
    log.info("report_email_sent", extra={
        "event": "report_email_sent", "artist_id": artist_id,
        "to": artist_email, "week_start": week_start,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Unit 3.8 — Scheduled report generation (extends pitch_service's APScheduler)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_artists_with_any_activity() -> list[str]:
    """Return all artist_ids that have at least one record in any service table."""
    conn    = sqlite3.connect(str(_DB_PATH))
    cur     = conn.cursor()
    ids: set = set()
    for table in ("pitches", "pr_outreach", "booking_inquiries", "social_posts"):
        try:
            cur.execute(f"SELECT DISTINCT artist_id FROM {table}")
            ids.update(r[0] for r in cur.fetchall())
        except Exception:
            pass
    conn.close()
    return list(ids)


async def _generate_all_weekly_reports():
    """Scheduler job: generate weekly report for every active artist. Logs 'would_have_fired' in dry_run mode."""
    if _SCHEDULER_DRY_RUN:
        log.info("would_have_fired", extra={"event": "would_have_fired", "job_id": "weekly_reports", "dry_run": True})
        return
    artists = _get_artists_with_any_activity()
    log.info("report_scheduler_start", extra={"event": "report_scheduler_start", "artist_count": len(artists)})
    for aid in artists:
        try:
            report = await generate_weekly_report(aid)
            score  = report.get("momentum_score", "?")
            log.info("report_scheduler_result", extra={"event": "report_scheduler_result", "artist_id": aid, "momentum_score": score})
        except Exception as e:
            log.error("report_scheduler_error", extra={"event": "report_scheduler_error", "artist_id": aid, "error": str(e)})


def init_report_scheduler():
    """
    Add weekly report job to the existing pitch_service APScheduler instance.
    Called from main.py after init_scheduler(). No-op unless SCHEDULER_ENABLED=true.
    """
    if not (_SCHEDULER_ENABLED or _SCHEDULER_DRY_RUN):
        log.info("report_scheduler_disabled", extra={"event": "report_scheduler_disabled", "reason": "SCHEDULER_ENABLED not set"})
        return
    try:
        from pitch_service import _scheduler
        if _scheduler is None:
            log.warning("report_scheduler_disabled", extra={"event": "report_scheduler_disabled", "reason": "pitch_service scheduler not running"})
            return
        # Schedule configurable via WEEKLY_REPORT_DAY / WEEKLY_REPORT_HOUR_UTC / WEEKLY_REPORT_MINUTE
        _scheduler.add_job(
            _generate_all_weekly_reports,
            "cron",
            day_of_week=_WEEKLY_REPORT_DAY,
            hour=_WEEKLY_REPORT_HOUR,
            minute=_WEEKLY_REPORT_MINUTE,
            id="weekly_reports",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=120,
        )
        log.info("report_scheduler_started", extra={
            "event":   "report_scheduler_started",
            "schedule": f"{_WEEKLY_REPORT_DAY}_{_WEEKLY_REPORT_HOUR:02d}:{_WEEKLY_REPORT_MINUTE:02d}_UTC",
        })
    except ImportError:
        log.error("report_scheduler_disabled", extra={"event": "report_scheduler_disabled", "reason": "pitch_service not importable"})
    except Exception as e:
        log.error("report_scheduler_error", extra={"event": "report_scheduler_error", "error": str(e)})
