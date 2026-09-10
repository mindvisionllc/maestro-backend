"""
PLMKR Release Service — Phase 4
Release Campaign Orchestration: coordinates Phases 1-3 outreach around a release date.

Tables: releases, campaign_actions (always SQLite at DB_PATH).
No new external APIs — delegates to pitch_service, pr_service, booking_service, social_service.
"""

import os
from artist_identity import ArtistAuthError, require_artist_scope
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

log = logging.getLogger("release_service")

router = APIRouter()

_DB_PATH = Path(os.environ.get("DB_PATH", "/data/memory.db"))

# Max actions processed per scheduler tick. Conservative default prevents a
# flood of past-due actions (e.g. after downtime) from all firing at once.
# If aggressive catchup is later wanted (e.g. post-downtime), a separate
# /api/admin/scheduler/catchup endpoint should be added; default scheduler
# behavior is conservative.
SCHEDULER_BATCH_LIMIT = int(os.environ.get("SCHEDULER_BATCH_LIMIT", "5"))
_SCHEDULER_DRY_RUN = os.environ.get("SCHEDULER_ENABLED", "").lower() == "dry_run"

# ── Action types ──────────────────────────────────────────────────────────────

ACTION_PITCH          = "pitch_curators"
ACTION_PR             = "pr_outreach"
ACTION_BOOKING        = "booking_inquiry"
ACTION_SOCIAL         = "social_post_schedule"

# Days relative to release_date → list of (action_type, wave_label)
_CAMPAIGN_SCHEDULE = [
    (-21, ACTION_BOOKING,  "venue_advance"),
    (-14, ACTION_PITCH,    "wave_1"),
    (-10, ACTION_PR,       "wave_1"),
    (-7,  ACTION_PITCH,    "wave_2"),
    (-7,  ACTION_SOCIAL,   "day_minus_7"),
    (-6,  ACTION_SOCIAL,   "day_minus_6"),
    (-5,  ACTION_SOCIAL,   "day_minus_5"),
    (-4,  ACTION_SOCIAL,   "day_minus_4"),
    (-3,  ACTION_PR,       "wave_2"),
    (-3,  ACTION_SOCIAL,   "day_minus_3"),
    (-2,  ACTION_SOCIAL,   "day_minus_2"),
    (-1,  ACTION_SOCIAL,   "day_minus_1"),
    (0,   ACTION_PITCH,    "release_day"),
    (0,   ACTION_SOCIAL,   "release_day"),
    (1,   ACTION_SOCIAL,   "day_plus_1"),
    (2,   ACTION_SOCIAL,   "day_plus_2"),
    (3,   ACTION_SOCIAL,   "day_plus_3"),
    (4,   ACTION_SOCIAL,   "day_plus_4"),
    (5,   ACTION_SOCIAL,   "day_plus_5"),
    (6,   ACTION_SOCIAL,   "day_plus_6"),
    (7,   ACTION_SOCIAL,   "day_plus_7"),
]


# ═══════════════════════════════════════════════════════════════════════════════
# DB Init
# ═══════════════════════════════════════════════════════════════════════════════

def init_release_db():
    """Create releases and campaign_actions tables. Idempotent."""
    import sqlite3
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS releases (
            id           TEXT PRIMARY KEY,
            artist_id    TEXT NOT NULL,
            title        TEXT NOT NULL,
            release_date TEXT NOT NULL,
            genre        TEXT DEFAULT '',
            mood         TEXT DEFAULT '',
            status       TEXT DEFAULT 'draft',
            created_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_releases_artist ON releases (artist_id)"
    )
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
        "CREATE INDEX IF NOT EXISTS idx_campaign_release ON campaign_actions (release_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_campaign_due "
        "ON campaign_actions (status, scheduled_for)"
    )

    # Reset any actions stuck in "running" from a prior crash/restart. A
    # running action already passed artist approval and readiness, so recovery
    # must preserve those gates rather than silently returning it to draft.

    result = conn.execute(
        "UPDATE campaign_actions SET status='ready' WHERE status='running'"
    )
    if result.rowcount:
        log.warning("db_reset_stuck_actions", extra={"reset_count": result.rowcount, "event": "db_reset_stuck_actions"})
        print(f"[Release] Reset {result.rowcount} stuck 'running' action(s) to 'ready' at startup")
    conn.commit()
    conn.close()
    log.info("db_ready", extra={"event": "db_ready", "svc": "release_service"})


# ═══════════════════════════════════════════════════════════════════════════════
# DB Helpers
# ═══════════════════════════════════════════════════════════════════════════════

import sqlite3

_RELEASE_COLS = ["id", "artist_id", "title", "release_date", "genre", "mood",
                 "status", "created_at"]
_ACTION_COLS  = ["id", "release_id", "action_type", "scheduled_for", "status",
                 "payload_json", "executed_at", "result_json", "created_at"]


def _conn():
    return sqlite3.connect(str(_DB_PATH))


def _release_to_dict(row, cols) -> dict:
    return dict(zip(cols, row))


def _action_to_dict(row, cols) -> dict:
    d = dict(zip(cols, row))
    try:
        d["payload"] = json.loads(d.pop("payload_json", "{}") or "{}")
    except Exception:
        d["payload"] = {}
    try:
        d["result"] = json.loads(d.pop("result_json", "{}") or "{}")
    except Exception:
        d["result"] = {}
    return d


def _db_create_release(r: dict) -> dict:
    conn = _conn()
    conn.execute(
        "INSERT INTO releases (id,artist_id,title,release_date,genre,mood,status) "
        "VALUES (?,?,?,?,?,?,?)",
        (r["id"], r["artist_id"], r["title"], r["release_date"],
         r.get("genre", ""), r.get("mood", ""), r.get("status", "draft")),
    )
    conn.commit()
    conn.close()
    return r


def _db_get_release(release_id: str) -> Optional[dict]:
    conn = _conn()
    cur  = conn.cursor()
    cur.execute(
        f"SELECT {','.join(_RELEASE_COLS)} FROM releases WHERE id=?", (release_id,)
    )
    row = cur.fetchone()
    conn.close()
    return _release_to_dict(row, _RELEASE_COLS) if row else None


def _db_list_releases(artist_id: str) -> list[dict]:
    conn = _conn()
    cur  = conn.cursor()
    cur.execute(
        f"SELECT {','.join(_RELEASE_COLS)} FROM releases WHERE artist_id=? "
        "ORDER BY release_date DESC",
        (artist_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [_release_to_dict(r, _RELEASE_COLS) for r in rows]


def _db_update_release(release_id: str, updates: dict):
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [release_id]
    conn = _conn()
    conn.execute(f"UPDATE releases SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()


def _db_create_action(a: dict):
    conn = _conn()
    conn.execute(
        "INSERT INTO campaign_actions "
        "(id,release_id,action_type,scheduled_for,status,payload_json) "
        "VALUES (?,?,?,?,?,?)",
        (a["id"], a["release_id"], a["action_type"], a["scheduled_for"],
         a.get("status", "pending"), json.dumps(a.get("payload", {}))),
    )
    conn.commit()
    conn.close()


def _db_list_actions(release_id: str) -> list[dict]:
    conn = _conn()
    cur  = conn.cursor()
    cur.execute(
        f"SELECT {','.join(_ACTION_COLS)} FROM campaign_actions WHERE release_id=? "
        "ORDER BY scheduled_for",
        (release_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [_action_to_dict(r, _ACTION_COLS) for r in rows]


def _db_list_due_actions() -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    cur  = conn.cursor()
    cur.execute(
        f"SELECT {','.join(_ACTION_COLS)} FROM campaign_actions "
        "WHERE status='ready' AND scheduled_for<=? ORDER BY scheduled_for",
        (now,),
    )
    rows = cur.fetchall()
    conn.close()
    return [_action_to_dict(r, _ACTION_COLS) for r in rows]


def _db_update_action(action_id: str, updates: dict):
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [action_id]
    conn = _conn()
    conn.execute(f"UPDATE campaign_actions SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Campaign Generation
# ═══════════════════════════════════════════════════════════════════════════════

def _build_campaign_actions(release: dict) -> list[dict]:
    """
    Build list of campaign_action dicts from the release schedule template.
    Actions in the past are still created (status=pending) so they show in the
    campaign view, but execute-due will fire them immediately.
    """
    release_dt = datetime.fromisoformat(release["release_date"] + "T00:00:00")
    actions    = []

    for day_offset, action_type, label in _CAMPAIGN_SCHEDULE:
        target_dt = release_dt + timedelta(days=day_offset)
        scheduled = target_dt.replace(tzinfo=timezone.utc).isoformat()

        payload = {
            "artist_id":    release["artist_id"],
            "release_id":   release["id"],
            "release_title": release["title"],
            "genre":        release.get("genre", ""),
            "mood":         release.get("mood", ""),
            "wave":         label,
        }
        if action_type == ACTION_PITCH:
            payload["tier_filter"] = ["A", "B"]
            payload["genre_filter"] = release.get("genre", "")
        elif action_type == ACTION_PR:
            payload["tier_filter"] = ["A", "B"]
        elif action_type == ACTION_BOOKING:
            payload["tier_filter"] = ["A"]
        elif action_type == ACTION_SOCIAL:
            payload["platforms"] = ["twitter", "instagram"]
            payload["day_label"] = label

        actions.append({
            "id":           str(uuid.uuid4()),
            "release_id":   release["id"],
            "action_type":  action_type,
            "scheduled_for": scheduled,
            "status":       "awaiting_approval",
            "payload":      payload,
        })

    return actions


# ═══════════════════════════════════════════════════════════════════════════════
# Action Execution
# ═══════════════════════════════════════════════════════════════════════════════

async def _execute_action(action: dict) -> dict:
    """
    Dispatch a campaign action to the appropriate Phase 1/2/3 service.
    Returns a result dict. Does NOT raise — errors are captured in result.
    """
    action_type = action["action_type"]
    payload     = action.get("payload", {})
    artist_id   = payload.get("artist_id", "")

    try:
        if action_type == ACTION_PITCH:
            from pitch_service import _db_list_curators, send_pitch_emails, BatchPitchRequest
            curators = _db_list_curators(
                tier=None, genre=payload.get("genre_filter") or None
            )
            tier_filter = payload.get("tier_filter", ["A", "B"])
            curators    = [c for c in curators if c.get("tier") in tier_filter]
            if not curators:
                return {"status": "skipped", "reason": "no curators matched filters"}
            curator_ids = [c["id"] for c in curators[:10]]  # cap at 10 per wave
            req = BatchPitchRequest(
                artist_id=artist_id,
                curator_ids=curator_ids,
                track_metadata={"name": payload.get("release_title", "New Release"),
                                "genre": payload.get("genre", ""),
                                "mood":  payload.get("mood", "")},
            )
            result = await send_pitch_emails(req)
            return {"status": "ok", "sent": result.get("sent", 0),
                    "failed": result.get("failed", 0)}

        elif action_type == ACTION_PR:
            from pr_service import _db_list_pr_contacts, send_pr_emails, BatchPRRequest
            contacts    = _db_list_pr_contacts(tier=None)
            tier_filter = payload.get("tier_filter", ["A", "B"])
            contacts    = [c for c in contacts if c.get("tier") in tier_filter]
            if not contacts:
                return {"status": "skipped", "reason": "no PR contacts matched filters"}
            contact_ids = [c["id"] for c in contacts[:8]]
            req = BatchPRRequest(
                artist_id=artist_id,
                contact_ids=contact_ids,
                release_metadata={"title": payload.get("release_title", "New Release"),
                                  "genre": payload.get("genre", ""),
                                  "mood":  payload.get("mood", "")},
            )
            result = await send_pr_emails(req)
            return {"status": "ok", "sent": result.get("sent", 0),
                    "failed": result.get("failed", 0)}

        elif action_type == ACTION_BOOKING:
            from booking_service import _db_list_booking_contacts, send_booking_emails, BatchBookingRequest
            contacts    = _db_list_booking_contacts(tier=None)
            tier_filter = payload.get("tier_filter", ["A"])
            contacts    = [c for c in contacts if c.get("tier") in tier_filter]
            if not contacts:
                return {"status": "skipped", "reason": "no booking contacts matched filters"}
            contact_ids = [c["id"] for c in contacts[:5]]
            req = BatchBookingRequest(
                artist_id=artist_id,
                contact_ids=contact_ids,
                event_metadata={"title": payload.get("release_title", "New Release"),
                                "genre": payload.get("genre", ""),
                                "mood":  payload.get("mood", "")},
            )
            result = await send_booking_emails(req)
            return {"status": "ok", "sent": result.get("sent", 0),
                    "failed": result.get("failed", 0)}

        elif action_type == ACTION_SOCIAL:
            from social_service import schedule_posts, BatchPostRequest
            platforms = payload.get("platforms", ["twitter", "instagram"])
            req = BatchPostRequest(
                artist_id=artist_id,
                platforms=platforms,
                theme=f"{payload.get('release_title','New Release')} — "
                      f"{payload.get('day_label','release')} post",
                count=1,
            )
            result = await schedule_posts(req)
            return {"status": "ok", "posts_created": len(result.get("posts", []))}

        else:
            return {"status": "skipped", "reason": f"unknown action_type: {action_type}"}

    except Exception as exc:
        log.error("action_execute_error",
                  extra={"action_id": action["id"], "action_type": action_type,
                         "error": str(exc)})
        return {"status": "error", "error": str(exc)}


# ═══════════════════════════════════════════════════════════════════════════════
# Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

class CreateReleaseRequest(BaseModel):
    artist_id:    str
    title:        str
    release_date: str          # YYYY-MM-DD
    genre:        Optional[str] = ""
    mood:         Optional[str] = ""


class PatchReleaseRequest(BaseModel):
    title:        Optional[str] = None
    release_date: Optional[str] = None
    genre:        Optional[str] = None
    mood:         Optional[str] = None
    status:       Optional[str] = None


@router.post("/api/releases", tags=["releases"])
def create_release(req: CreateReleaseRequest, request: Request = None):
    """Create a new release."""
    try:
        artist_id = require_artist_scope(request, req.artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    try:
        datetime.fromisoformat(req.release_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="release_date must be YYYY-MM-DD")
    release = {
        "id":           str(uuid.uuid4()),
        "artist_id":    artist_id,
        "title":        req.title,
        "release_date": req.release_date,
        "genre":        req.genre or "",
        "mood":         req.mood or "",
        "status":       "draft",
    }
    _db_create_release(release)
    log.info("release_created", extra={"artist_id": artist_id,
             "release_id": release["id"], "action": "create_release"})
    return release


@router.get("/api/releases", tags=["releases"])
def list_releases(artist_id: str, request: Request = None):
    """List all releases for an artist."""
    try:
        artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"releases": _db_list_releases(artist_id)}


@router.get("/api/releases/{release_id}", tags=["releases"])
def get_release(release_id: str, request: Request = None):
    """Get a single release."""
    r = _db_get_release(release_id)
    if not r:
        raise HTTPException(status_code=404, detail="Release not found")
    try:
        require_artist_scope(request, r["artist_id"])
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return r


@router.patch("/api/releases/{release_id}", tags=["releases"])
def patch_release(release_id: str, req: PatchReleaseRequest, request: Request = None):
    """Update release fields."""
    r = _db_get_release(release_id)
    if not r:
        raise HTTPException(status_code=404, detail="Release not found")
    try:
        require_artist_scope(request, r["artist_id"])
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        return r
    if "release_date" in updates:
        try:
            datetime.fromisoformat(updates["release_date"])
        except ValueError:
            raise HTTPException(status_code=400, detail="release_date must be YYYY-MM-DD")
    _db_update_release(release_id, updates)
    return {**r, **updates}


@router.post("/api/releases/{release_id}/generate-campaign", tags=["releases"])
def generate_campaign(release_id: str, request: Request = None):
    """
    Generate campaign_actions for a release. Idempotent — clears existing
    pending actions and regenerates from the current release_date.
    """
    r = _db_get_release(release_id)
    if not r:
        raise HTTPException(status_code=404, detail="Release not found")
    try:
        require_artist_scope(request, r["artist_id"])
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # Clear unexecuted actions from the prior generated plan. Completed and
    # failed history remains durable and is never overwritten.
    conn = _conn()
    conn.execute(
        "DELETE FROM campaign_actions WHERE release_id=? AND status IN ('awaiting_approval','approved','ready')",
        (release_id,),
    )
    conn.commit()
    conn.close()

    actions = _build_campaign_actions(r)
    for a in actions:
        _db_create_action(a)

    _db_update_release(release_id, {"status": "active"})
    log.info("campaign_generated", extra={"artist_id": r["artist_id"],
             "release_id": release_id, "action_count": len(actions)})
    return {"release_id": release_id, "actions_created": len(actions),
            "status": "active"}


@router.post("/api/releases/{release_id}/campaign/approve", tags=["releases"])
def approve_campaign(release_id: str, request: Request = None):
    """Record the artist's approval of every unexecuted campaign action."""
    r = _db_get_release(release_id)
    if not r:
        raise HTTPException(status_code=404, detail="Release not found")
    try:
        require_artist_scope(request, r["artist_id"])
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    actions = _db_list_actions(release_id)
    pending = [a for a in actions if a["status"] == "awaiting_approval"]
    if not pending:
        raise HTTPException(status_code=409, detail="No campaign actions are awaiting artist approval")
    for action in pending:
        _db_update_action(action["id"], {"status": "approved"})
    return {"release_id": release_id, "approved": len(pending), "status": "approved"}


@router.post("/api/releases/{release_id}/campaign/ready", tags=["releases"])
def ready_campaign(release_id: str, request: Request = None):
    """Release an approved campaign to the due-action execution queue."""
    r = _db_get_release(release_id)
    if not r:
        raise HTTPException(status_code=404, detail="Release not found")
    try:
        require_artist_scope(request, r["artist_id"])
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    actions = _db_list_actions(release_id)
    approved = [a for a in actions if a["status"] == "approved"]
    if not approved:
        raise HTTPException(status_code=409, detail="Approve the campaign before marking it ready")
    for action in approved:
        _db_update_action(action["id"], {"status": "ready"})
    return {"release_id": release_id, "ready": len(approved), "status": "ready"}


@router.get("/api/releases/{release_id}/campaign", tags=["releases"])
def get_campaign(release_id: str, request: Request = None):
    """List all campaign actions for a release."""
    r = _db_get_release(release_id)
    if not r:
        raise HTTPException(status_code=404, detail="Release not found")
    try:
        require_artist_scope(request, r["artist_id"])
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    actions = _db_list_actions(release_id)
    return {"release_id": release_id, "actions": actions,
            "counts": {
                "total":   len(actions),
                "pending": sum(1 for a in actions if a["status"] in ("pending", "awaiting_approval")),
                "awaiting_approval": sum(1 for a in actions if a["status"] == "awaiting_approval"),
                "approved": sum(1 for a in actions if a["status"] == "approved"),
                "ready": sum(1 for a in actions if a["status"] == "ready"),
                "done":    sum(1 for a in actions if a["status"] == "done"),
                "failed":  sum(1 for a in actions if a["status"] == "failed"),
            }}


@router.post("/api/releases/{release_id}/campaign/execute-due", tags=["releases"])
async def execute_due_actions(release_id: str, request: Request = None):
    """
    Execute only artist-approved and readiness-released campaign actions that
    are due (scheduled_for <= now). Updates action status to done/failed with result.
    """
    r = _db_get_release(release_id)
    if not r:
        raise HTTPException(status_code=404, detail="Release not found")
    try:
        require_artist_scope(request, r["artist_id"])
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    all_due = _db_list_due_actions()
    due     = [a for a in all_due if a["release_id"] == release_id]

    executed = []
    for action in due:
        _db_update_action(action["id"], {"status": "running"})
        result = await _execute_action(action)
        final_status = "done" if result.get("status") in ("ok", "skipped") else "failed"
        _db_update_action(action["id"], {
            "status":      final_status,
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "result_json": json.dumps(result),
        })
        executed.append({"action_id": action["id"], "action_type": action["action_type"],
                         "status": final_status, "result": result})
        log.info("action_executed", extra={"release_id": release_id,
                 "action_id": action["id"], "action_type": action["action_type"],
                 "result_status": final_status})

    return {"release_id": release_id, "executed": len(executed), "actions": executed}


# ═══════════════════════════════════════════════════════════════════════════════
# Scheduler hook (called from main.py every 1h)
# ═══════════════════════════════════════════════════════════════════════════════

async def execute_all_due_campaign_actions():
    """Sweep pending campaign actions, processing at most SCHEDULER_BATCH_LIMIT per tick.

    Conservative cap prevents a flood of past-due actions (e.g. after downtime) from
    all firing in a single tick. Remaining actions are picked up on the next tick.
    """
    if _SCHEDULER_DRY_RUN:
        log.info("would_have_fired", extra={"event": "would_have_fired", "job_id": "campaign_executor", "dry_run": True})
        return
    due_actions = _db_list_due_actions()
    batch = due_actions[:SCHEDULER_BATCH_LIMIT]
    log.info("scheduler_sweep", extra={"due_count": len(due_actions), "batch": len(batch)})
    if len(due_actions) > SCHEDULER_BATCH_LIMIT:
        log.warning(
            "scheduler_backfill_cap",
            extra={"skipped": len(due_actions) - SCHEDULER_BATCH_LIMIT,
                   "limit": SCHEDULER_BATCH_LIMIT},
        )

    for action in batch:
        _db_update_action(action["id"], {"status": "running"})
        result = await _execute_action(action)
        final_status = "done" if result.get("status") in ("ok", "skipped") else "failed"
        _db_update_action(action["id"], {
            "status":      final_status,
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "result_json": json.dumps(result),
        })
