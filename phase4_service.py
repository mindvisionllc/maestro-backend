"""
PLMKR Phase 4 — iOS / App Backend Foundation

Push notification infrastructure (APNs + FCM stubs behind feature flags),
app configuration endpoint, version compatibility check, and IAP receipt
validation stub.

All live clients are behind APNS_LIVE / FCM_LIVE / IAP_LIVE flags defaulting
false — parallel to BUFFER_LIVE pattern from S3. No real APNs/FCM/Apple calls
are made until the flags are enabled.
"""

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from artist_identity import ArtistAuthError, require_artist_scope

log = logging.getLogger("phase4_service")

# ── Config ────────────────────────────────────────────────────────────────────
_DB_PATH = Path(os.environ.get("DB_PATH", "/data/memory.db"))

_APNS_LIVE       = os.environ.get("APNS_LIVE", "false").lower() == "true"
_APNS_CERT_PATH  = os.environ.get("APNS_CERT_PATH", "")

_FCM_LIVE        = os.environ.get("FCM_LIVE", "false").lower() == "true"
_FCM_SERVER_KEY  = os.environ.get("FCM_SERVER_KEY", "")

_IAP_LIVE        = os.environ.get("IAP_LIVE", "false").lower() == "true"

_APP_MIN_VERSION_IOS     = os.environ.get("APP_MIN_VERSION_IOS",     "1.0.0")
_APP_MIN_VERSION_ANDROID = os.environ.get("APP_MIN_VERSION_ANDROID", "1.0.0")
_APP_CURRENT_VERSION     = os.environ.get("APP_CURRENT_VERSION",     "1.0.0")

router = APIRouter()


# ═══════════════════════════════════════════════════════════════════════════════
# DB — device_tokens table
# ═══════════════════════════════════════════════════════════════════════════════

def init_phase4_db():
    """Create device_tokens table. Idempotent."""
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS device_tokens (
            id          TEXT PRIMARY KEY,
            artist_id   TEXT NOT NULL,
            platform    TEXT NOT NULL,
            token       TEXT NOT NULL,
            app_version TEXT DEFAULT '',
            registered_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
            UNIQUE(artist_id, platform, token)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_tokens_artist "
        "ON device_tokens (artist_id)"
    )
    conn.commit()
    conn.close()
    log.info("db_ready", extra={"event": "db_ready", "svc": "phase4_service"})


def _db_register_device(artist_id: str, platform: str, token: str, app_version: str = "") -> dict:
    record_id = str(uuid.uuid4())
    conn = sqlite3.connect(str(_DB_PATH))
    try:
        conn.execute(
            """INSERT INTO device_tokens (id, artist_id, platform, token, app_version)
               VALUES (?,?,?,?,?)
               ON CONFLICT(artist_id, platform, token) DO UPDATE SET
                 app_version=excluded.app_version,
                 registered_at=strftime('%Y-%m-%dT%H:%M:%S','now')""",
            (record_id, artist_id, platform, token, app_version),
        )
        persisted = conn.execute(
            "SELECT id FROM device_tokens WHERE artist_id=? AND platform=? AND token=?",
            (artist_id, platform, token),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    return {"id": persisted[0] if persisted else record_id,
            "artist_id": artist_id, "platform": platform,
            "token": token, "app_version": app_version}


def _db_list_device_tokens(artist_id: str) -> list[dict]:
    conn = sqlite3.connect(str(_DB_PATH))
    cur  = conn.cursor()
    cur.execute(
        "SELECT id,artist_id,platform,token,app_version,registered_at "
        "FROM device_tokens WHERE artist_id=? ORDER BY registered_at DESC",
        (artist_id,),
    )
    rows = cur.fetchall()
    conn.close()
    cols = ["id", "artist_id", "platform", "token", "app_version", "registered_at"]
    return [dict(zip(cols, r)) for r in rows]


def _public_device_record(device: dict) -> dict:
    """Return device metadata without exposing the reusable push token."""
    return {
        key: value
        for key, value in device.items()
        if key != "token"
    } | {"token_registered": bool(device.get("token"))}


# ═══════════════════════════════════════════════════════════════════════════════
# Push notification stubs
# ═══════════════════════════════════════════════════════════════════════════════

async def _send_apns(token: str, title: str, body: str, data: dict) -> dict:
    """APNs stub — only active when APNS_LIVE=true and APNS_CERT_PATH is set."""
    if not _APNS_LIVE or not _APNS_CERT_PATH:
        log.info("would_have_sent_apns", extra={
            "event": "would_have_sent_apns",
            "title": title, "reason": "APNS_LIVE not enabled",
        })
        return {"mocked": True, "platform": "ios"}

    # Real APNs implementation goes here when APNS_LIVE=true.
    # Use apns2 or httpx with APNs HTTP/2 API.
    # Placeholder returns a mock — replace when APNS_CERT_PATH is configured.
    log.warning("apns_live_not_implemented", extra={
        "event": "apns_live_not_implemented",
        "note": "APNS_LIVE=true but real APNs client not yet wired",
    })
    return {"mocked": True, "platform": "ios", "note": "live_stub"}


async def _send_fcm(token: str, title: str, body: str, data: dict) -> dict:
    """FCM stub — only active when FCM_LIVE=true and FCM_SERVER_KEY is set."""
    if not _FCM_LIVE or not _FCM_SERVER_KEY:
        log.info("would_have_sent_fcm", extra={
            "event": "would_have_sent_fcm",
            "title": title, "reason": "FCM_LIVE not enabled",
        })
        return {"mocked": True, "platform": "android"}

    # Real FCM implementation goes here when FCM_LIVE=true.
    # Use httpx to POST to https://fcm.googleapis.com/fcm/send with Authorization header.
    log.warning("fcm_live_not_implemented", extra={
        "event": "fcm_live_not_implemented",
        "note": "FCM_LIVE=true but real FCM client not yet wired",
    })
    return {"mocked": True, "platform": "android", "note": "live_stub"}


# ═══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

class DeviceRegisterRequest(BaseModel):
    artist_id:   str
    platform:    str          # "ios" or "android"
    token:       str
    app_version: str = ""


class DeviceUnregisterRequest(BaseModel):
    artist_id: str
    platform: str
    token: str


@router.post("/api/devices/register", status_code=201, tags=["phase4"])
def register_device(req: DeviceRegisterRequest, request: Request = None):
    """Register an iOS or Android device token for push notifications."""
    try:
        req.artist_id = require_artist_scope(request, req.artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    platform = req.platform.lower()
    if platform not in ("ios", "android"):
        raise HTTPException(status_code=400, detail="platform must be 'ios' or 'android'")
    if not req.token or len(req.token) < 8:
        raise HTTPException(status_code=400, detail="Invalid device token")
    record = _db_register_device(req.artist_id, platform, req.token, req.app_version)
    log.info("device_registered", extra={
        "event": "device_registered", "artist_id": req.artist_id,
        "platform": platform, "app_version": req.app_version,
    })
    return _public_device_record(record)


@router.get("/api/devices", tags=["phase4"])
def list_devices(artist_id: str, request: Request = None):
    """List registered device tokens for an artist."""
    try:
        artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"devices": [_public_device_record(device) for device in _db_list_device_tokens(artist_id)]}


@router.post("/api/devices/unregister", tags=["phase4"])
def unregister_device(req: DeviceUnregisterRequest, request: Request = None):
    """Remove one artist-scoped device registration during logout or rotation."""
    try:
        artist_id = require_artist_scope(request, req.artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    platform = req.platform.lower()
    if platform not in ("ios", "android"):
        raise HTTPException(status_code=400, detail="platform must be 'ios' or 'android'")
    if not req.token or len(req.token) < 8:
        raise HTTPException(status_code=400, detail="Invalid device token")
    conn = sqlite3.connect(str(_DB_PATH))
    try:
        cursor = conn.execute(
            "DELETE FROM device_tokens WHERE artist_id=? AND platform=? AND token=?",
            (artist_id, platform, req.token),
        )
        conn.commit()
        removed = cursor.rowcount
    finally:
        conn.close()
    log.info("device_unregistered", extra={
        "event": "device_unregistered", "artist_id": artist_id,
        "platform": platform, "removed": removed,
    })
    return {"removed": removed == 1}


class NotificationSendRequest(BaseModel):
    artist_id: str
    title:     str
    body:      str
    data:      dict = {}


@router.post("/api/push/send", tags=["phase4"])
async def push_send(req: NotificationSendRequest, request: Request = None):
    """
    Send push notification to all registered devices for an artist.
    APNs and FCM clients are stubs behind APNS_LIVE / FCM_LIVE flags (default false).
    """
    try:
        req.artist_id = require_artist_scope(request, req.artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    devices = _db_list_device_tokens(req.artist_id)
    if not devices:
        return {"sent": 0, "errors": [], "note": "no registered devices"}

    results = {"sent": 0, "errors": [], "results": []}
    for device in devices:
        try:
            if device["platform"] == "ios":
                r = await _send_apns(device["token"], req.title, req.body, req.data)
            else:
                r = await _send_fcm(device["token"], req.title, req.body, req.data)
            results["results"].append(r)
            results["sent"] += 1
        except Exception:
            # Provider error text can echo request details. Keep token material
            # and provider-specific secrets out of the artist-facing response.
            results["errors"].append(f"{device['platform']}: delivery failed")

    log.info("notification_sent", extra={
        "event": "notification_sent", "artist_id": req.artist_id,
        "sent": results["sent"], "errors": len(results["errors"]),
    })
    return results


# ── App config endpoint ───────────────────────────────────────────────────────

_APP_FEATURE_FLAGS = {
    "pitch_send":        True,
    "pr_send":           True,
    "booking_send":      True,
    "social_scheduling": True,
    "weekly_reports":    True,
    "push_notifications": _APNS_LIVE or _FCM_LIVE,
    "iap":               _IAP_LIVE,
}

_SUPPORT_URLS = {
    "help":    "https://playmaker.app/help",
    "terms":   "https://playmaker.app/terms",
    "privacy": "https://playmaker.app/privacy",
    "contact": "mailto:support@playmaker.app",
}


@router.get("/api/app/config", tags=["phase4"])
def get_app_config():
    """
    Return app version requirements, feature flags, kill-switches, and support URLs.
    Versioned so old app builds still function in degraded mode.
    """
    return {
        "schema_version":        1,
        "current_version": {
            "ios":     _APP_CURRENT_VERSION,
            "android": _APP_CURRENT_VERSION,
        },
        "min_version": {
            "ios":     _APP_MIN_VERSION_IOS,
            "android": _APP_MIN_VERSION_ANDROID,
        },
        "feature_flags": _APP_FEATURE_FLAGS,
        "support_urls":  _SUPPORT_URLS,
        "kill_switches": {},
    }


# ── Version compatibility check ───────────────────────────────────────────────

def _compare_semver(v1: str, v2: str) -> int:
    """Return -1 if v1 < v2, 0 if equal, 1 if v1 > v2."""
    def parts(v):
        try:
            return [int(x) for x in v.strip().split(".")]
        except ValueError:
            return [0, 0, 0]
    a, b = parts(v1), parts(v2)
    for x, y in zip(a, b):
        if x < y:
            return -1
        if x > y:
            return 1
    return 0


class VersionCheckRequest(BaseModel):
    platform:        str    # "ios" or "android"
    current_version: str    # semver string from the app build


@router.post("/api/app/version-check", tags=["phase4"])
def version_check(req: VersionCheckRequest):
    """
    Frontend sends current app version; backend responds:
      ok                   — version is current, no action needed
      soft_update          — newer version available, update recommended
      hard_update_required — version below minimum, app must update before use
    """
    platform = req.platform.lower()
    if platform == "ios":
        min_ver = _APP_MIN_VERSION_IOS
    elif platform == "android":
        min_ver = _APP_MIN_VERSION_ANDROID
    else:
        raise HTTPException(status_code=400, detail="platform must be 'ios' or 'android'")

    current = req.current_version
    below_min     = _compare_semver(current, min_ver) < 0
    below_latest  = _compare_semver(current, _APP_CURRENT_VERSION) < 0

    if below_min:
        status = "hard_update_required"
        message = f"This version ({current}) is no longer supported. Please update to continue."
    elif below_latest:
        status = "soft_update"
        message = f"A new version ({_APP_CURRENT_VERSION}) is available."
    else:
        status = "ok"
        message = "App is up to date."

    log.info("version_check", extra={
        "event": "version_check", "platform": platform,
        "current": current, "status": status,
    })
    return {
        "status":          status,
        "message":         message,
        "current_version": current,
        "latest_version":  _APP_CURRENT_VERSION,
        "min_version":     min_ver,
    }


# ── IAP receipt validation stub ───────────────────────────────────────────────

class IAPValidateRequest(BaseModel):
    artist_id:       str
    receipt_data:    str     # base64-encoded Apple receipt
    product_id:      str
    transaction_id:  str


@router.post("/api/iap/validate-receipt", tags=["phase4"])
async def validate_iap_receipt(req: IAPValidateRequest):
    """
    In-app purchase receipt validation stub.
    Apple receipt validation client is behind IAP_LIVE flag (default false).
    Stripe remains the primary billing rail; this is for App Store compliance.
    """
    if not _IAP_LIVE:
        log.info("would_have_validated_iap", extra={
            "event": "would_have_validated_iap",
            "artist_id": req.artist_id, "product_id": req.product_id,
            "reason": "IAP_LIVE not enabled",
        })
        return {
            "valid":          True,
            "mocked":         True,
            "artist_id":      req.artist_id,
            "product_id":     req.product_id,
            "transaction_id": req.transaction_id,
            "note":           "IAP_LIVE=false — receipt not validated against Apple servers",
        }

    # Real Apple receipt validation goes here when IAP_LIVE=true.
    # POST to https://buy.itunes.apple.com/verifyReceipt with shared secret.
    log.warning("iap_live_not_implemented", extra={
        "event": "iap_live_not_implemented",
        "note": "IAP_LIVE=true but Apple receipt validation not yet wired",
    })
    return {
        "valid":          True,
        "mocked":         True,
        "artist_id":      req.artist_id,
        "product_id":     req.product_id,
        "transaction_id": req.transaction_id,
        "note":           "IAP_LIVE=true — live_stub (Apple client not yet wired)",
    }
