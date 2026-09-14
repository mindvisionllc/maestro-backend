"""
PLMKR Admin Service — Stats, deep health, and diagnostics endpoints.

GET /api/admin/stats?artist_id=...&since=ISO_DATE
GET /api/admin/health/deep
GET /api/admin/diagnostics
"""

import os
import shutil
import sqlite3
import sys
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse

from artist_identity import ArtistAuthError, require_admin_api_key, require_artist_scope

from logging_config import get_ring_buffer

log = logging.getLogger("admin_service")

router = APIRouter()

_DB_PATH     = Path(os.environ.get("DB_PATH", "/data/memory.db"))
_STATIC_DIR  = Path(__file__).parent / "static"


def _conn():
    return sqlite3.connect(str(_DB_PATH))


def _require_admin(request: Request) -> None:
    try:
        require_admin_api_key(request)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _count(cur, table: str, where: str, params: tuple) -> int:
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params)
        return cur.fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _max_date(cur, table: str, col: str, where: str, params: tuple) -> Optional[str]:
    try:
        cur.execute(f"SELECT MAX({col}) FROM {table} WHERE {where}", params)
        row = cur.fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


# ── Stats endpoint ────────────────────────────────────────────────────────────

@router.get("/api/admin/stats", tags=["admin"])
def admin_stats(artist_id: str, request: Request, since: Optional[str] = None):
    """
    Return activity stats for an artist.
    since: ISO datetime string (optional). If omitted, returns all-time stats.
    """
    try:
        artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    since_val = since or "1970-01-01T00:00:00"

    conn = _conn()
    cur  = conn.cursor()

    pitches_sent    = _count(cur, "pitches",          "artist_id=? AND status!='draft' AND created_at>=?", (artist_id, since_val))
    pitches_replied = _count(cur, "pitches",          "artist_id=? AND status='replied' AND created_at>=?", (artist_id, since_val))

    pr_sent         = _count(cur, "pr_outreach",      "artist_id=? AND status!='draft' AND created_at>=?", (artist_id, since_val))
    pr_replied      = _count(cur, "pr_outreach",      "artist_id=? AND status='replied' AND created_at>=?", (artist_id, since_val))

    booking_sent    = _count(cur, "booking_inquiries", "artist_id=? AND status!='draft' AND created_at>=?", (artist_id, since_val))
    booking_replied = _count(cur, "booking_inquiries", "artist_id=? AND status='replied' AND created_at>=?", (artist_id, since_val))

    social_published = _count(cur, "social_posts",    "artist_id=? AND status='posted' AND created_at>=?", (artist_id, since_val))

    last_report_date = _max_date(cur, "weekly_reports", "generated_at", "artist_id=?", (artist_id,))

    conn.close()

    def _rate(replied: int, sent: int) -> float:
        return round(replied / sent, 2) if sent > 0 else 0.0

    return {
        "artist_id":             artist_id,
        "since":                 since_val,
        "pitches_sent":          pitches_sent,
        "pitches_replied":       pitches_replied,
        "reply_rate":            _rate(pitches_replied, pitches_sent),
        "pr_sent":               pr_sent,
        "pr_replied":            pr_replied,
        "pr_reply_rate":         _rate(pr_replied, pr_sent),
        "booking_sent":          booking_sent,
        "booking_replied":       booking_replied,
        "booking_reply_rate":    _rate(booking_replied, booking_sent),
        "social_posts_published": social_published,
        "last_report_date":      last_report_date,
    }


# ── Deep health endpoint ──────────────────────────────────────────────────────

def _check_db_connected() -> bool:
    try:
        conn = _conn()
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


def _check_scheduler_running() -> bool:
    try:
        from pitch_service import _scheduler
        return _scheduler is not None and _scheduler.running
    except Exception:
        return False


def _count_gmail_connected() -> int:
    """Count artists who have stored Gmail access tokens in their profile."""
    try:
        conn = _conn()
        cur  = conn.cursor()
        cur.execute("SELECT data FROM artists")
        rows = cur.fetchall()
        conn.close()
        import json
        count = 0
        for (data_str,) in rows:
            try:
                data = json.loads(data_str) if data_str else {}
                if data.get("gmail_tokens", {}).get("access_token"):
                    count += 1
            except Exception:
                pass
        return count
    except Exception:
        return 0


def _count_buffer_connected() -> int:
    """Count artists who have stored Buffer tokens."""
    try:
        conn = _conn()
        cur  = conn.cursor()
        cur.execute("SELECT data FROM artists")
        rows = cur.fetchall()
        conn.close()
        import json
        count = 0
        for (data_str,) in rows:
            try:
                data = json.loads(data_str) if data_str else {}
                if data.get("buffer_tokens", {}).get("access_token"):
                    count += 1
            except Exception:
                pass
        return count
    except Exception:
        return 0


def _disk_usage_pct() -> float:
    try:
        usage = shutil.disk_usage("/")
        return round(usage.used / usage.total * 100, 1)
    except Exception:
        return -1.0


def _security_posture() -> dict:
    """Snapshot of security-relevant env-var state at request time."""
    plmkr_key      = os.environ.get("PLMKR_API_KEY", "")
    anthropic_key  = os.environ.get("ANTHROPIC_API_KEY", "")
    webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    dev_unsigned   = os.environ.get("STRIPE_DEV_ALLOW_UNSIGNED", "").lower() == "true"
    allowed_origins = os.environ.get("ALLOWED_ORIGINS", "")

    auth_enabled = bool(plmkr_key)
    return {
        "auth_enabled":                  auth_enabled,
        "auth_mode":                     "enforced" if auth_enabled else "dev-permissive",
        "anthropic_available":           bool(anthropic_key),
        "stripe_signed_webhooks_required": bool(webhook_secret) or not dev_unsigned,
        "stripe_dev_allow_unsigned":     dev_unsigned,
        "cors_origins":                  allowed_origins or "*",
    }


# ── Diagnostics endpoint ──────────────────────────────────────────────────────

# All env var names from the codebase. Values are never exposed — only SET/MISSING.
_KNOWN_ENV_VARS = [
    "ALLOWED_ORIGINS", "ANTHROPIC_API_KEY", "APP_BASE_URL",
    "ARTISTS_DIR", "AUDIO_CACHE_DIR",
    "BUFFER_CLIENT_ID", "BUFFER_CLIENT_SECRET", "BUFFER_REDIRECT_URI",
    "CLOUDINARY_CLOUD_NAME", "DAILY_PITCH_QUOTA", "DATABASE_URL",
    "DATA_DIR", "DB_FAILOVER_TO_SQLITE", "DB_PATH", "D_ID_API_KEY",
    "ELEVENLABS_API_KEY",
    "GMAIL_OAUTH_CLIENT_ID", "GMAIL_OAUTH_CLIENT_SECRET", "GMAIL_OAUTH_REDIRECT_URI",
    "KNOWLEDGE_BASE", "MAX_UPLOAD_SIZE", "PLMKR_API_KEY",
    "RAILWAY_ENVIRONMENT", "REPLY_POLL_HOURS",
    "SCHEDULER_BATCH_LIMIT", "SCHEDULER_ENABLED", "SENTRY_DSN",
    "SKILLS_DIR",
    "STRIPE_DEV_ALLOW_UNSIGNED", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
    "TEMP_AUDIO_DIR", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER",
    "TWILIO_VERIFY_SERVICE_SID",
]

_SERVICE_ENV_MAP = {
    "anthropic":   "ANTHROPIC_API_KEY",
    "gmail":       "GMAIL_OAUTH_CLIENT_ID",
    "stripe":      "STRIPE_SECRET_KEY",
    "twilio":      "TWILIO_ACCOUNT_SID",
    "buffer":      "BUFFER_CLIENT_ID",
    "elevenlabs":  "ELEVENLABS_API_KEY",
    "d_id":        "D_ID_API_KEY",
    "cloudinary":  "CLOUDINARY_CLOUD_NAME",
}


def _env_snapshot() -> dict:
    """Return SET/MISSING for every known env var. Never exposes values."""
    return {
        var: ("SET" if os.environ.get(var) else "MISSING")
        for var in _KNOWN_ENV_VARS
    }


def _service_status() -> dict:
    return {
        svc: bool(os.environ.get(env_var))
        for svc, env_var in _SERVICE_ENV_MAP.items()
    }


def _runtime_versions() -> dict:
    versions: dict = {"python": sys.version.split()[0], "sqlite": "unknown"}
    try:
        import sqlite3 as _sq
        versions["sqlite"] = _sq.sqlite_version
    except Exception:
        pass
    try:
        import uvicorn
        versions["uvicorn"] = uvicorn.__version__
    except Exception:
        pass
    try:
        import fastapi
        versions["fastapi"] = fastapi.__version__
    except Exception:
        pass
    return versions


def _volume_info() -> dict:
    data_path = Path("/data")
    writable = False
    try:
        test_file = data_path / ".write_test"
        test_file.write_text("ok")
        test_file.unlink()
        writable = True
    except Exception:
        pass
    info: dict = {"writable": writable}
    try:
        usage = shutil.disk_usage("/data")
        info["total_mb"]  = round(usage.total / 1_048_576)
        info["free_mb"]   = round(usage.free  / 1_048_576)
        info["used_pct"]  = round(usage.used  / usage.total * 100, 1)
    except Exception:
        info["total_mb"] = info["free_mb"] = info["used_pct"] = None
    return info


def _scheduler_info() -> dict:
    try:
        from pitch_service import _scheduler
        if _scheduler is None or not _scheduler.running:
            return {"running": False, "jobs": 0, "next_run_time": None}
        jobs = _scheduler.get_jobs()
        next_times = [j.next_run_time for j in jobs if j.next_run_time]
        return {
            "running":       True,
            "jobs":          len(jobs),
            "next_run_time": min(next_times).isoformat() if next_times else None,
        }
    except Exception:
        return {"running": False, "jobs": 0, "next_run_time": None}


@router.get("/api/admin/diagnostics/anthropic-stats", tags=["admin"])
def admin_anthropic_stats(request: Request):
    """Per-model Anthropic call counters (total, success, retry, fail). Auth required."""
    _require_admin(request)
    from anthropic_utils import get_anthropic_stats
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "models":    get_anthropic_stats(),
    }


@router.get("/api/admin/diagnostics/gmail-stats", tags=["admin"])
def admin_gmail_stats(request: Request):
    """Per-artist Gmail call counters (total, success, retry, fail). Auth required."""
    _require_admin(request)
    from pitch_service import get_gmail_stats
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "artists":   get_gmail_stats(),
    }


@router.get("/api/admin/diagnostics/performance", tags=["admin"])
def admin_diagnostics_performance(request: Request):
    """
    Per-route p50/p95/p99 latency percentiles. Auth required.
    Rolling window of last 1000 requests per route.
    """
    _require_admin(request)
    from performance_metrics import get_all_percentiles
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "routes":    get_all_percentiles(),
    }


def _scheduler_queue_diagnostics() -> dict:
    """Query campaign_actions for scheduler queue state. Returns empty lists on OperationalError."""
    import json as _json
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    try:
        conn = _conn()
        cur = conn.cursor()

        cur.execute(
            "SELECT id, release_id, action_type, scheduled_for "
            "FROM campaign_actions WHERE status='pending' "
            "ORDER BY scheduled_for ASC LIMIT 10"
        )
        next_pending = [
            {"id": r[0], "release_id": r[1], "action_type": r[2], "scheduled_for": r[3]}
            for r in cur.fetchall()
        ]

        cur.execute(
            "SELECT id, release_id, action_type, executed_at, status, result_json "
            "FROM campaign_actions WHERE status IN ('done', 'failed') "
            "ORDER BY executed_at DESC LIMIT 20"
        )
        last_completed = [
            {
                "id": r[0], "release_id": r[1], "action_type": r[2],
                "executed_at": r[3], "status": r[4],
                "result": _json.loads(r[5] or "{}"),
            }
            for r in cur.fetchall()
        ]

        cur.execute(
            "SELECT status, COUNT(*) FROM campaign_actions "
            "WHERE created_at >= ? GROUP BY status",
            (cutoff_24h,)
        )
        counts_24h = {row[0]: row[1] for row in cur.fetchall()}

        conn.close()
        return {
            "next_pending":   next_pending,
            "last_completed": last_completed,
            "counts_24h":     counts_24h,
        }
    except sqlite3.OperationalError:
        return {"next_pending": [], "last_completed": [], "counts_24h": {}}


@router.get("/api/admin/diagnostics/scheduler", tags=["admin"])
def admin_diagnostics_scheduler(request: Request):
    """Scheduler queue state: next 10 pending, last 20 completed, 24h status counts. Auth required."""
    _require_admin(request)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **_scheduler_queue_diagnostics(),
    }


@router.get("/api/admin/diagnostics", tags=["admin"])
def admin_diagnostics(request: Request):
    """
    Full runtime diagnostics. Requires X-API-Key. Never exposes env var values.
    """
    _require_admin(request)
    rb = get_ring_buffer()
    recent_errors = rb.get_entries()[-20:] if rb else []
    return {
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "env_snapshot":   _env_snapshot(),
        "service_status": _service_status(),
        "runtime":        _runtime_versions(),
        "volume":         _volume_info(),
        "scheduler":      _scheduler_info(),
        "recent_errors":  recent_errors,
    }


@router.get("/api/admin/health/deep", tags=["admin"])
def admin_health_deep(response: Response):
    """
    Readiness check: DB, scheduler, OAuth token counts, disk usage, and
    security posture of the running deploy.

    Returns 503 when db_connected=False so Railway restarts on DB failure.
    """
    db_ok = _check_db_connected()
    if not db_ok:
        response.status_code = 503
    return {
        "timestamp":                     datetime.now(timezone.utc).isoformat(),
        "db_connected":                  db_ok,
        "scheduler_running":             _check_scheduler_running(),
        "gmail_token_valid_for_artists": _count_gmail_connected(),
        "buffer_token_valid_for_artists": _count_buffer_connected(),
        "disk_usage_pct":                _disk_usage_pct(),
        **_security_posture(),
    }


@router.get("/admin/dashboard", tags=["admin"], response_class=HTMLResponse)
def admin_dashboard():
    """
    In-app admin monitoring dashboard — server-rendered HTML.

    Requires X-API-Key (same middleware as all protected routes).
    The page JS stores the key in sessionStorage for in-page API fetches.

    Decision: static HTML file at static/admin_dashboard.html (no Jinja2 — existing
    codebase has no templates dir; static/ is already copied in Dockerfile).
    """
    html_path = _STATIC_DIR / "admin_dashboard.html"
    if not html_path.exists():
        raise HTTPException(status_code=503, detail="Dashboard HTML not found in static/")
    log.info("dashboard_served", extra={"event": "dashboard_served", "svc": "admin_service"})
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
