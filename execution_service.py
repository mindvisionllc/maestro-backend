"""
Durable execution ledger for the backend's existing single-action providers.

Only Gmail one-off sends and scheduling one existing social post are supported.
Batch outreach, PR, pitch, booking, and campaign actions intentionally remain
outside this service.
"""

import base64
import binascii
import json
from fastapi import Request
from artist_identity import (
    ArtistAuthError,
    decode_oauth_state,
    identity_configured,
    issue_oauth_state,
    require_artist_scope,
)
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from email.utils import make_msgid, parseaddr
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

import pitch_service
import social_service


log = logging.getLogger("execution_service")
router = APIRouter()

_DB_PATH = Path(os.environ.get("DB_PATH", "/data/memory.db"))

GMAIL_SEND = "gmail.send"
SOCIAL_SCHEDULE = "social.buffer.schedule"
SUPPORTED_ACTIONS = {GMAIL_SEND, SOCIAL_SCHEDULE}

# Keep the durable ledger bounded before user-controlled values are persisted
# or handed to a provider. These are application limits, not provider limits.
MAX_IDENTIFIER_LENGTH = 256
MAX_OPERATION_CURSOR_LENGTH = 1024
MAX_EMAIL_SUBJECT_LENGTH = 998
MAX_EMAIL_BODY_LENGTH = 256 * 1024
MAX_SOCIAL_ID_LENGTH = 256
MAX_BUFFER_PROFILES = 20
MAX_OPERATION_PAYLOAD_BYTES = 512 * 1024
MAX_PROVIDER_RESULT_BYTES = 64 * 1024
MAX_ERROR_DETAIL_LENGTH = 2048
MAX_OPERATION_EVENTS = 100
MAX_OPERATION_EVENT_METADATA_BYTES = 16 * 1024

SUPPORTED_SOCIAL_PLATFORMS = {
    "facebook": "facebook",
    "instagram": "instagram",
    "tiktok": "tiktok",
    "twitter": "twitter",
    "x": "twitter",
    "youtube": "youtube",
}

TERMINAL_STATUSES = {"succeeded", "failed", "canceled"}
EXECUTABLE_STATUSES = {"pending", "failed_retryable"}
RECONCILABLE_STATUSES = {"unknown"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_provider_result(value) -> dict | list | str | int | float | bool | None:
    """Keep provider diagnostics bounded before writing them to the ledger."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return {"truncated": True, "reason": "provider_result_not_json"}
    if len(encoded.encode("utf-8")) <= MAX_PROVIDER_RESULT_BYTES:
        return value
    return {
        "truncated": True,
        "reason": "provider_result_too_large",
        "max_bytes": MAX_PROVIDER_RESULT_BYTES,
    }


def _bounded_error_detail(value: Optional[str]) -> Optional[str]:
    """Prevent provider/library exception text from bloating artist history."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if len(value) <= MAX_ERROR_DETAIL_LENGTH:
        return value
    suffix = "… [detail truncated]"
    return value[: MAX_ERROR_DETAIL_LENGTH - len(suffix)] + suffix


def _bounded_event_metadata(value) -> dict:
    """Keep one durable audit event bounded without dropping the event itself."""
    if not isinstance(value, dict):
        return {"truncated": True, "reason": "event_metadata_not_object"}
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return {"truncated": True, "reason": "event_metadata_not_json"}
    if len(encoded.encode("utf-8")) <= MAX_OPERATION_EVENT_METADATA_BYTES:
        return value
    return {
        "truncated": True,
        "reason": "event_metadata_too_large",
        "max_bytes": MAX_OPERATION_EVENT_METADATA_BYTES,
    }


def _message_id_for(operation_id: str) -> str:
    # A deterministic RFC 5322 Message-ID gives Gmail reconciliation a stable
    # provider-side correlation key even if the send response is lost.
    return make_msgid(idstring=f"plmkr-{operation_id}", domain="operations.plmkr.local")


def init_execution_db():
    """Create the operation ledger and conservatively recover interrupted work."""
    Path(_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS execution_operations (
            id                  TEXT PRIMARY KEY,
            artist_id           TEXT NOT NULL,
            action_type         TEXT NOT NULL,
            idempotency_key     TEXT NOT NULL,
            payload             TEXT NOT NULL,
            status              TEXT NOT NULL DEFAULT 'pending',
            provider            TEXT NOT NULL,
            provider_reference  TEXT,
            provider_result     TEXT,
            correlation_key     TEXT,
            resource_key        TEXT,
            profile_binding     TEXT,
            approved_at         TEXT,
            attempt_count       INTEGER NOT NULL DEFAULT 0,
            error_code          TEXT,
            error_detail        TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            provider_started_at TEXT,
            completed_at        TEXT,
            reconciled_at       TEXT,
            UNIQUE (artist_id, action_type, idempotency_key)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_operations_status "
        "ON execution_operations (status, updated_at)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS execution_operation_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id   TEXT NOT NULL,
            artist_id      TEXT NOT NULL,
            event_type     TEXT NOT NULL,
            from_status    TEXT,
            to_status      TEXT NOT NULL,
            metadata       TEXT,
            occurred_at    TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_operation_events "
        "ON execution_operation_events (operation_id, id)"
    )
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(execution_operations)").fetchall()
    }
    if "resource_key" not in existing_cols:
        conn.execute("ALTER TABLE execution_operations ADD COLUMN resource_key TEXT")
    if "profile_binding" not in existing_cols:
        conn.execute("ALTER TABLE execution_operations ADD COLUMN profile_binding TEXT")
    if "approved_at" not in existing_cols:
        conn.execute("ALTER TABLE execution_operations ADD COLUMN approved_at TEXT")
    if "ready_at" not in existing_cols:
        conn.execute("ALTER TABLE execution_operations ADD COLUMN ready_at TEXT")
    conn.execute("DROP INDEX IF EXISTS uq_execution_operation_resource")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_operation_resource
           ON execution_operations (action_type, resource_key)
           WHERE resource_key IS NOT NULL
             AND status IN ('pending', 'failed_retryable', 'executing', 'unknown')"""
    )
    # Never blindly retry a provider call after process loss: the provider may
    # have accepted it before the response or local commit was lost.
    interrupted_operations = conn.execute(
        "SELECT id, artist_id FROM execution_operations WHERE status='executing'"
    ).fetchall()
    conn.execute(
        """UPDATE execution_operations
           SET status='unknown',
               error_code='process_interrupted',
               error_detail='Execution was interrupted after provider dispatch; reconcile before retrying.',
               updated_at=?
           WHERE status='executing'""",
        (_now(),),
    )
    for operation_id, artist_id in interrupted_operations:
        _insert_operation_event(
            conn, operation_id, artist_id, "process_interrupted", "unknown",
            from_status="executing",
        )
    conn.commit()
    conn.close()
    log.info("db_ready", extra={"event": "db_ready", "svc": "execution_service"})


_OP_COLS = [
    "id", "artist_id", "action_type", "idempotency_key", "payload", "status",
    "provider", "provider_reference", "provider_result", "correlation_key",
    "resource_key", "profile_binding", "approved_at", "ready_at", "attempt_count",
    "error_code", "error_detail", "created_at", "updated_at",
    "provider_started_at", "completed_at", "reconciled_at",
]


def _row_to_operation(row) -> dict:
    operation = dict(zip(_OP_COLS, row))
    for key in ("payload", "provider_result", "profile_binding"):
        raw = operation.get(key)
        if raw:
            try:
                operation[key] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                operation[key] = {}
        else:
            operation[key] = None if key in ("provider_result", "profile_binding") else {}
    operation["reconciliation_required"] = operation["status"] in RECONCILABLE_STATUSES
    operation["events"] = _list_operation_events(operation["id"], operation["artist_id"])
    operation["events_truncated"] = _operation_event_count(
        operation["id"], operation["artist_id"],
    ) > len(operation["events"])
    return operation


def _list_operation_events(operation_id: str, artist_id: str) -> list[dict]:
    conn = sqlite3.connect(str(_DB_PATH))
    rows = conn.execute(
        """SELECT event_type, from_status, to_status, metadata, occurred_at
           FROM execution_operation_events
           WHERE operation_id=? AND artist_id=?
           ORDER BY id DESC
           LIMIT ?""",
        (operation_id, artist_id, MAX_OPERATION_EVENTS),
    ).fetchall()
    conn.close()
    events = []
    for event_type, from_status, to_status, metadata, occurred_at in reversed(rows):
        try:
            parsed_metadata = json.loads(metadata) if metadata else {}
        except json.JSONDecodeError:
            parsed_metadata = {}
        events.append({
            "event_type": event_type,
            "from_status": from_status,
            "to_status": to_status,
            "metadata": parsed_metadata,
            "occurred_at": occurred_at,
        })
    return events


def _operation_event_count(operation_id: str, artist_id: str) -> int:
    conn = sqlite3.connect(str(_DB_PATH))
    row = conn.execute(
        """SELECT COUNT(*)
           FROM execution_operation_events
           WHERE operation_id=? AND artist_id=?""",
        (operation_id, artist_id),
    ).fetchone()
    conn.close()
    return int(row[0]) if row else 0


def _record_operation_event(
    operation_id: str,
    artist_id: str,
    event_type: str,
    to_status: str,
    *,
    from_status: Optional[str] = None,
    metadata: Optional[dict] = None,
):
    conn = sqlite3.connect(str(_DB_PATH))
    _insert_operation_event(
        conn, operation_id, artist_id, event_type, to_status,
        from_status=from_status, metadata=metadata,
    )
    conn.commit()
    conn.close()


def _insert_operation_event(
    conn,
    operation_id: str,
    artist_id: str,
    event_type: str,
    to_status: str,
    *,
    from_status: Optional[str] = None,
    metadata: Optional[dict] = None,
):
    """Append an event using an existing transaction.

    State changes and their audit records must share a transaction.  Keeping
    this small primitive separate also lets the recovery path record its
    interruption event before exposing the recovered operation.
    """
    bounded_metadata = _bounded_event_metadata(metadata or {})
    conn.execute(
        """INSERT INTO execution_operation_events
           (operation_id, artist_id, event_type, from_status, to_status, metadata, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            operation_id, artist_id, event_type, from_status, to_status,
            json.dumps(bounded_metadata, sort_keys=True, separators=(",", ":")), _now(),
        ),
    )


def _get_operation(operation_id: str) -> dict:
    conn = sqlite3.connect(str(_DB_PATH))
    row = conn.execute(
        f"SELECT {','.join(_OP_COLS)} FROM execution_operations WHERE id=?",
        (operation_id,),
    ).fetchone()
    conn.close()
    return _row_to_operation(row) if row else {}


def _list_operations(
    artist_id: str,
    *,
    limit: int = 50,
    status: Optional[str] = None,
    before: Optional[str] = None,
) -> list[dict]:
    """Return a bounded, newest-first execution history for one artist."""
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    if status is not None and status not in {
        "pending", "failed_retryable", "executing", "succeeded", "failed", "unknown", "canceled",
    }:
        raise HTTPException(status_code=422, detail={"code": "invalid_operation_status"})
    if before is not None and len(before) > MAX_OPERATION_CURSOR_LENGTH:
        raise HTTPException(status_code=422, detail={"code": "invalid_operation_cursor"})

    where = ["artist_id=?"]
    params: list[object] = [artist_id]
    if status is not None:
        where.append("status=?")
        params.append(status)
    if before:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(before.encode("ascii") + b"=" * (-len(before) % 4)))
            before_updated_at = decoded["updated_at"]
            before_id = decoded["id"]
            if not isinstance(before_updated_at, str) or not isinstance(before_id, str):
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError, binascii.Error):
            raise HTTPException(status_code=422, detail={"code": "invalid_operation_cursor"})
        where.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
        params.extend([before_updated_at, before_updated_at, before_id])
    params.append(limit)

    conn = sqlite3.connect(str(_DB_PATH))
    rows = conn.execute(
        f"""SELECT {','.join(_OP_COLS)}
            FROM execution_operations
            WHERE {' AND '.join(where)}
            ORDER BY updated_at DESC, id DESC
            LIMIT ?""",
        params,
    ).fetchall()
    conn.close()
    return [_row_to_operation(row) for row in rows]


def _operation_cursor(operation: dict) -> str:
    payload = json.dumps(
        {"updated_at": operation["updated_at"], "id": operation["id"]},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _get_bound_operation(resource_key: str, artist_id: str) -> dict:
    """Return the artist-owned durable operation bound to one resource."""
    conn = sqlite3.connect(str(_DB_PATH))
    try:
        row = conn.execute(
            f"""SELECT {','.join(_OP_COLS)}
                FROM execution_operations
                WHERE artist_id=? AND action_type=? AND resource_key=?
                ORDER BY
                    CASE WHEN status IN ('pending', 'failed_retryable', 'executing', 'unknown')
                         THEN 0 ELSE 1 END,
                    updated_at DESC,
                    id DESC
                LIMIT 1""",
            (artist_id, SOCIAL_SCHEDULE, resource_key),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        row = None
    finally:
        conn.close()
    return _row_to_operation(row) if row else {}


def _get_operation_by_idempotency(
    artist_id: str,
    action_type: str,
    idempotency_key: str,
) -> dict:
    """Look up one artist-owned operation without scanning bounded history."""
    conn = sqlite3.connect(str(_DB_PATH))
    row = conn.execute(
        f"""SELECT {','.join(_OP_COLS)}
            FROM execution_operations
            WHERE artist_id=? AND action_type=? AND idempotency_key=?""",
        (artist_id, action_type, idempotency_key),
    ).fetchone()
    conn.close()
    return _row_to_operation(row) if row else {}


def _valid_single_email_target(value: str) -> bool:
    if any(char in value for char in ("\r", "\n", ",", ";")):
        return False
    display_name, address = parseaddr(value)
    if display_name or address != value.strip():
        return False
    local, separator, domain = address.rpartition("@")
    return bool(
        separator
        and local
        and domain
        and "." in domain
        and not local.startswith(".")
        and not local.endswith(".")
        and " " not in address
    )


def _require_bounded_string(value, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{field} is required")
    normalized = value.strip()
    if len(normalized) > maximum:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "field_too_long",
                "field": field,
                "max_length": maximum,
            },
        )
    return normalized


def _normalize_social_platform(value, field: str = "platform") -> str:
    platform = _require_bounded_string(value, field, MAX_IDENTIFIER_LENGTH).lower()
    normalized = SUPPORTED_SOCIAL_PLATFORMS.get(platform)
    if not normalized:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unsupported_social_platform",
                "field": field,
                "platform": platform,
            },
        )
    return normalized


def _validate_payload(action_type: str, payload: dict) -> dict:
    if action_type not in SUPPORTED_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unsupported_action",
                "action_type": action_type,
                "supported_actions": sorted(SUPPORTED_ACTIONS),
            },
        )
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="payload must be an object")
    normalized = dict(payload)
    if action_type == GMAIL_SEND:
        missing = [key for key in ("to", "subject", "body") if not isinstance(payload.get(key), str) or not payload[key]]
        if missing:
            raise HTTPException(status_code=422, detail=f"Missing Gmail fields: {', '.join(missing)}")
        normalized["to"] = _require_bounded_string(payload["to"], "to", MAX_IDENTIFIER_LENGTH)
        normalized["subject"] = _require_bounded_string(
            payload["subject"], "subject", MAX_EMAIL_SUBJECT_LENGTH,
        )
        normalized["body"] = _require_bounded_string(
            payload["body"], "body", MAX_EMAIL_BODY_LENGTH,
        )
        if not _valid_single_email_target(normalized["to"]):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_recipient", "message": "A single valid recipient email is required."},
            )
    elif action_type == SOCIAL_SCHEDULE:
        normalized["post_id"] = _require_bounded_string(
            payload.get("post_id"), "post_id", MAX_SOCIAL_ID_LENGTH,
        )
        if "platform" in payload:
            normalized["platform"] = _normalize_social_platform(payload["platform"])
        profiles = payload.get("buffer_profile_ids")
        if (
            not isinstance(profiles, list)
            or not profiles
            or len(profiles) > MAX_BUFFER_PROFILES
            or not all(isinstance(item, str) and item.strip() for item in profiles)
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "invalid_buffer_profiles",
                    "message": "social.buffer.schedule requires 1-20 non-empty profile IDs",
                },
            )
        normalized_profiles = [
            _require_bounded_string(item, "buffer_profile_id", MAX_SOCIAL_ID_LENGTH)
            for item in profiles
        ]
        normalized["buffer_profile_ids"] = sorted(set(normalized_profiles))
    return normalized


def _create_or_get_operation(
    artist_id: str,
    action_type: str,
    idempotency_key: str,
    payload: dict,
) -> tuple[dict, bool]:
    payload = _validate_payload(action_type, payload)
    artist_id = _require_bounded_string(artist_id, "artist_id", MAX_IDENTIFIER_LENGTH)
    idempotency_key = _require_bounded_string(
        idempotency_key, "idempotency_key", MAX_IDENTIFIER_LENGTH,
    )

    operation_id = str(uuid.uuid4())
    provider = "gmail" if action_type == GMAIL_SEND else "buffer"
    correlation_key = _message_id_for(operation_id) if action_type == GMAIL_SEND else operation_id
    resource_key = payload["post_id"] if action_type == SOCIAL_SCHEDULE else None
    profile_binding = payload.get("buffer_profile_ids") if action_type == SOCIAL_SCHEDULE else None
    # Reject an existing social post owned by another artist before creating
    # any durable operation. Missing posts remain deferred to execution-time
    # validation for legacy recovery flows that materialize posts later.
    if action_type == SOCIAL_SCHEDULE:
        post = social_service._db_get_post(resource_key)
        if post and post.get("artist_id") != artist_id:
            raise HTTPException(status_code=404, detail="Operation not found")

    timestamp = _now()
    encoded_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(encoded_payload.encode("utf-8")) > MAX_OPERATION_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "payload_too_large",
                "max_bytes": MAX_OPERATION_PAYLOAD_BYTES,
            },
        )

    conn = sqlite3.connect(str(_DB_PATH))
    try:
        conn.execute(
            """INSERT INTO execution_operations
               (id,artist_id,action_type,idempotency_key,payload,status,provider,
                correlation_key,resource_key,profile_binding,created_at,updated_at)
               VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?)""",
            (
                operation_id, artist_id, action_type, idempotency_key,
                encoded_payload, provider, correlation_key, resource_key,
                json.dumps(profile_binding) if profile_binding is not None else None,
                timestamp, timestamp,
            ),
        )
        _insert_operation_event(
            conn, operation_id, artist_id, "created", "pending",
        )
        conn.commit()
        created = True
    except sqlite3.IntegrityError:
        conn.rollback()
        created = False
    finally:
        conn.close()

    if not created:
        conn = sqlite3.connect(str(_DB_PATH))
        row = conn.execute(
            f"""SELECT {','.join(_OP_COLS)}
                FROM execution_operations
                WHERE artist_id=? AND action_type=? AND idempotency_key=?""",
            (artist_id, action_type, idempotency_key),
        ).fetchone()
        if not row and resource_key is not None:
            conflicting_row = conn.execute(
                f"""SELECT {','.join(_OP_COLS)}
                    FROM execution_operations
                    WHERE action_type=? AND resource_key=?""",
                (action_type, resource_key),
            ).fetchone()
            conn.close()
            conflicting = _row_to_operation(conflicting_row)
            # Resource IDs are normally globally unique, but never assume
            # that when constructing an error response.  A caller must not
            # learn another artist's operation ID through a collision.
            detail = {
                "code": "resource_already_has_operation",
                "message": "This social post is already bound to an execution operation.",
            }
            if conflicting.get("artist_id") == artist_id:
                detail["operation_id"] = conflicting["id"]
            raise HTTPException(
                status_code=409,
                detail=detail,
            )
        conn.close()
        existing = _row_to_operation(row)
        if json.dumps(existing["payload"], sort_keys=True, separators=(",", ":")) != encoded_payload:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "idempotency_conflict",
                    "operation_id": existing["id"],
                    "message": "The idempotency key is already bound to a different payload.",
                },
            )
        return existing, False
    return _get_operation(operation_id), True


def _claim_pending(operation_id: str) -> tuple[dict, bool]:
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            f"SELECT {','.join(_OP_COLS)} FROM execution_operations WHERE id=?",
            (operation_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Operation not found")
        operation = _row_to_operation(row)
        if operation["status"] not in EXECUTABLE_STATUSES:
            conn.rollback()
            return operation, False
        if not operation["approved_at"]:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "operation_not_approved",
                    "operation_id": operation_id,
                    "message": "Approve the durable operation before execution.",
                },
            )
        if not operation["ready_at"]:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "operation_not_ready",
                    "operation_id": operation_id,
                    "message": "Confirm the final execution details before dispatch.",
                },
            )
        timestamp = _now()
        conn.execute(
            """UPDATE execution_operations
               SET status='executing', attempt_count=attempt_count+1,
                   provider_started_at=?, updated_at=?, error_code=NULL, error_detail=NULL
               WHERE id=?""",
            (timestamp, timestamp, operation_id),
        )
        _insert_operation_event(
            conn,
            operation_id,
            operation["artist_id"],
            "dispatch_started",
            "executing",
            from_status=operation["status"],
            metadata={"attempt_count": operation["attempt_count"] + 1},
        )
        conn.commit()
        claimed_operation = _get_operation(operation_id)
        return claimed_operation, True
    finally:
        conn.close()


def _finish(
    operation_id: str,
    status: str,
    *,
    provider_result: Optional[dict] = None,
    provider_reference: Optional[str] = None,
    error_code: Optional[str] = None,
    error_detail: Optional[str] = None,
    reconciled: bool = False,
    expected_status: Optional[str] = None,
    social_post_id: Optional[str] = None,
    social_post_reference: Optional[str] = None,
    restore_social_post_id: Optional[str] = None,
) -> dict:
    timestamp = _now()
    bounded_provider_result = _bounded_provider_result(provider_result)
    bounded_error_detail = _bounded_error_detail(error_detail)
    # An ambiguous provider response still ends the dispatch attempt. Keep
    # that timestamp separate from a later reconciliation timestamp.
    attempt_completed_at = timestamp if status in {"succeeded", "failed"} or (
        status == "unknown" and not reconciled and expected_status == "executing"
    ) else None
    conn = sqlite3.connect(str(_DB_PATH))
    try:
        # Publish the authoritative status and its audit event atomically.
        conn.execute("BEGIN IMMEDIATE")
        if social_post_id is not None:
            post_cursor = conn.execute(
                """UPDATE social_posts
                   SET status='scheduled', buffer_update_id=?
                   WHERE id=? AND status='scheduling'""",
                (social_post_reference or "", social_post_id),
            )
            if post_cursor.rowcount != 1:
                raise RuntimeError(
                    "The social post could not be durably marked scheduled; reconciliation is required."
                )
        if restore_social_post_id is not None:
            post_cursor = conn.execute(
                """UPDATE social_posts SET status='draft'
                   WHERE id=? AND status='scheduling'""",
                (restore_social_post_id,),
            )
            if post_cursor.rowcount != 1:
                raise RuntimeError(
                    "The social post could not be durably restored after the provider was unavailable."
                )
        where = "WHERE id=?"
        params = [
            status,
            json.dumps(bounded_provider_result) if provider_result is not None else None,
            provider_reference,
            error_code,
            bounded_error_detail,
            timestamp,
            attempt_completed_at,
            1 if reconciled else 0,
            timestamp,
            operation_id,
        ]
        if expected_status:
            where += " AND status=?"
            params.append(expected_status)
        cursor = conn.execute(
            """UPDATE execution_operations
               SET status=?, provider_result=?, provider_reference=?,
                   error_code=?, error_detail=?, updated_at=?,
                   completed_at=COALESCE(?, completed_at),
                   reconciled_at=CASE WHEN ? THEN ? ELSE reconciled_at END
            """ + where,
            params,
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                "The execution operation changed before completion; reconciliation is required."
            )
        artist_row = conn.execute(
            "SELECT artist_id FROM execution_operations WHERE id=?",
            (operation_id,),
        ).fetchone()
        event_type = (
            "reconciled"
            if reconciled
            else "reconciliation_failed"
            if expected_status == "unknown"
            else "dispatch_finished"
        )
        _insert_operation_event(
            conn,
            operation_id,
            artist_row[0],
            event_type,
            status,
            from_status=expected_status or "executing",
            metadata={
                key: value for key, value in {
                    "error_code": error_code,
                    "provider_reference": provider_reference,
                }.items() if value is not None
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _get_operation(operation_id)


def _claim_social_post(operation: dict) -> bool:
    post_id = operation["payload"]["post_id"]
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """UPDATE social_posts
               SET status='scheduling'
               WHERE id=? AND artist_id=? AND status='draft'""",
            (post_id, operation["artist_id"]),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _require_operation_owner(operation: dict, artist_id: Optional[str]):
    if artist_id is not None and operation["artist_id"] != artist_id:
        raise HTTPException(status_code=404, detail="Operation not found")


def approve_operation(operation_id: str, artist_id: Optional[str] = None) -> dict:
    operation_id = _require_bounded_string(operation_id, "operation_id", MAX_IDENTIFIER_LENGTH)
    operation = _get_operation(operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    _require_operation_owner(operation, artist_id)
    if operation["status"] not in EXECUTABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "operation_not_approvable",
                "status": operation["status"],
            },
        )
    timestamp = _now()
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            f"SELECT {','.join(_OP_COLS)} FROM execution_operations WHERE id=?",
            (operation_id,),
        ).fetchone()
        if not current:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Operation not found")
        current_operation = _row_to_operation(current)
        _require_operation_owner(current_operation, artist_id)
        if current_operation["status"] not in EXECUTABLE_STATUSES:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "operation_not_approvable",
                    "status": current_operation["status"],
                },
            )
        if not current_operation["approved_at"]:
            conn.execute(
                """UPDATE execution_operations
                   SET approved_at=?, updated_at=?
                   WHERE id=?""",
                (timestamp, timestamp, operation_id),
            )
            _insert_operation_event(
                conn, operation_id, current_operation["artist_id"],
                "artist_approved", current_operation["status"],
            )
        conn.commit()
    finally:
        conn.close()
    return _get_operation(operation_id)


def mark_operation_ready(operation_id: str, artist_id: Optional[str] = None) -> dict:
    """Record the artist's final execution-detail confirmation."""
    operation_id = _require_bounded_string(operation_id, "operation_id", MAX_IDENTIFIER_LENGTH)
    operation = _get_operation(operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    _require_operation_owner(operation, artist_id)
    if operation["status"] not in EXECUTABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={"code": "operation_not_ready", "status": operation["status"]},
        )
    if not operation["approved_at"]:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "operation_not_approved",
                "operation_id": operation_id,
                "message": "Approve the durable operation before confirming readiness.",
            },
        )
    timestamp = _now()
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            f"SELECT {','.join(_OP_COLS)} FROM execution_operations WHERE id=?",
            (operation_id,),
        ).fetchone()
        if not current:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Operation not found")
        current_operation = _row_to_operation(current)
        _require_operation_owner(current_operation, artist_id)
        if current_operation["status"] not in EXECUTABLE_STATUSES:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail={"code": "operation_not_ready", "status": current_operation["status"]},
            )
        if not current_operation["approved_at"]:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "operation_not_approved",
                    "operation_id": operation_id,
                    "message": "Approve the durable operation before confirming readiness.",
                },
            )
        if not current_operation["ready_at"]:
            conn.execute(
                """UPDATE execution_operations
                   SET ready_at=?, updated_at=?
                   WHERE id=?""",
                (timestamp, timestamp, operation_id),
            )
            _insert_operation_event(
                conn, operation_id, current_operation["artist_id"],
                "execution_ready", current_operation["status"],
            )
        conn.commit()
    finally:
        conn.close()
    return _get_operation(operation_id)


def cancel_operation(operation_id: str, artist_id: Optional[str] = None) -> dict:
    """Withdraw queued work before any provider dispatch begins."""
    operation_id = _require_bounded_string(operation_id, "operation_id", MAX_IDENTIFIER_LENGTH)
    operation = _get_operation(operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    _require_operation_owner(operation, artist_id)
    if operation["status"] not in EXECUTABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "operation_not_cancellable",
                "status": operation["status"],
                "message": "Only queued operations can be withdrawn before provider dispatch.",
            },
        )

    timestamp = _now()
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            f"SELECT {','.join(_OP_COLS)} FROM execution_operations WHERE id=?",
            (operation_id,),
        ).fetchone()
        if not current:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Operation not found")
        current_operation = _row_to_operation(current)
        _require_operation_owner(current_operation, artist_id)
        if current_operation["status"] not in EXECUTABLE_STATUSES:
            conn.rollback()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "operation_not_cancellable",
                    "status": current_operation["status"],
                    "message": "Only queued operations can be withdrawn before provider dispatch.",
                },
            )
        conn.execute(
            """UPDATE execution_operations
               SET status='canceled', updated_at=?, completed_at=?,
                   error_code=NULL, error_detail=NULL
               WHERE id=?""",
            (timestamp, timestamp, operation_id),
        )
        _insert_operation_event(
            conn, operation_id, current_operation["artist_id"],
            "artist_canceled", "canceled",
            from_status=current_operation["status"],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return _get_operation(operation_id)


async def execute_operation(operation_id: str, artist_id: Optional[str] = None) -> dict:
    operation_id = _require_bounded_string(operation_id, "operation_id", MAX_IDENTIFIER_LENGTH)
    existing = _get_operation(operation_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Operation not found")
    _require_operation_owner(existing, artist_id)
    operation, claimed = _claim_pending(operation_id)
    if not claimed:
        return operation

    payload = operation["payload"]
    try:
        if operation["action_type"] == GMAIL_SEND:
            result = await pitch_service.send_email(
                operation["artist_id"],
                payload["to"],
                payload["subject"],
                payload["body"],
                message_id_header=operation["correlation_key"],
            )
            if not isinstance(result, dict) or not result.get("message_id"):
                return _finish(
                    operation_id,
                    "unknown",
                    provider_result=result if isinstance(result, dict) else {"raw_result": str(result)},
                    error_code="provider_reference_missing",
                    error_detail="Gmail returned no message ID; reconciliation is required before any retry.",
                    expected_status="executing",
                )
            return _finish(
                operation_id,
                "succeeded",
                provider_result=result,
                provider_reference=result.get("message_id"),
                expected_status="executing",
            )

        post = social_service._db_get_post(payload["post_id"])
        if not post or post.get("artist_id") != operation["artist_id"]:
            return _finish(
                operation_id,
                "failed",
                error_code="social_post_not_found",
                error_detail="The social post does not exist for this artist.",
                expected_status="executing",
            )
        if payload.get("buffer_profile_ids") != operation.get("profile_binding"):
            return _finish(
                operation_id,
                "failed",
                error_code="profile_binding_mismatch",
                error_detail="The operation payload no longer matches its durable Buffer profile binding.",
                expected_status="executing",
            )
        requested_platform = payload.get("platform")
        stored_platform = str(post.get("platform") or "").strip().lower()
        post_platform = SUPPORTED_SOCIAL_PLATFORMS.get(stored_platform, stored_platform)
        if requested_platform and requested_platform != post_platform:
            return _finish(
                operation_id,
                "failed",
                error_code="social_platform_mismatch",
                error_detail="The approved platform no longer matches the durable social post.",
                expected_status="executing",
            )
        # Mock/local execution must never make a live Buffer request. In live
        # mode, fail closed by revalidating the immutable profile binding
        # immediately before the write-capable provider call.
        if social_service._BUFFER_LIVE:
            try:
                profiles = await social_service._buffer_list_profiles(operation["artist_id"])
            except social_service.BufferAuthExpired as exc:
                return _finish(
                    operation_id,
                    "failed_retryable",
                    error_code=type(exc).__name__,
                    error_detail=str(exc),
                    expected_status="executing",
                )
            except RuntimeError as exc:
                return _finish(
                    operation_id,
                    "failed_retryable",
                    error_code="buffer_profile_discovery_failed",
                    error_detail=str(exc),
                    expected_status="executing",
                )
            available_profile_ids = {
                profile.get("id") for profile in profiles if isinstance(profile, dict) and profile.get("id")
            }
            invalid_profile_ids = sorted(set(operation["profile_binding"]) - available_profile_ids)
            if invalid_profile_ids:
                return _finish(
                    operation_id,
                    "failed",
                    error_code="invalid_buffer_profile",
                    error_detail=f"Selected Buffer profiles are unavailable: {', '.join(invalid_profile_ids)}",
                    expected_status="executing",
                )
            binding_platform = requested_platform or post_platform
            if binding_platform:
                mismatched_profile_ids = sorted(
                    profile.get("id")
                    for profile in profiles
                    if isinstance(profile, dict)
                    and profile.get("id") in operation["profile_binding"]
                    and str(profile.get("service") or "").strip().lower() != binding_platform
                )
                if mismatched_profile_ids:
                    return _finish(
                        operation_id,
                        "failed",
                        error_code="buffer_profile_platform_mismatch",
                        error_detail=(
                            "Selected Buffer profiles do not match the approved social platform: "
                            + ", ".join(mismatched_profile_ids)
                        ),
                        expected_status="executing",
                    )
        if not _claim_social_post(operation):
            return _finish(
                operation_id,
                "failed",
                error_code="social_post_not_schedulable",
                error_detail="The social post must be in draft status and unclaimed.",
                expected_status="executing",
            )
        result = await social_service._buffer_schedule_post(
            operation["artist_id"],
            post["content"],
            payload["buffer_profile_ids"],
            media_url=post.get("media_url", ""),
            scheduled_at=post.get("scheduled_at"),
        )
        if not isinstance(result, dict) or not result.get("id"):
            return _finish(
                operation_id,
                "unknown",
                provider_result=result if isinstance(result, dict) else {"raw_result": str(result)},
                error_code="provider_reference_missing",
                error_detail="Buffer returned no update ID; reconciliation is required before any retry.",
                expected_status="executing",
            )
        provider_reference = result.get("id")
        return _finish(
            operation_id,
            "succeeded",
            provider_result=result,
            provider_reference=provider_reference,
            expected_status="executing",
            social_post_id=post["id"],
            social_post_reference=provider_reference,
        )
    except (pitch_service.GmailNotConnected, pitch_service.GmailAuthExpired) as exc:
        return _finish(
            operation_id,
            "failed_retryable",
            error_code=type(exc).__name__,
            error_detail=str(exc),
            expected_status="executing",
        )
    except (social_service.BufferNotConnected, social_service.BufferAuthExpired) as exc:
        return _finish(
            operation_id,
            "failed_retryable",
            error_code=type(exc).__name__,
            error_detail=str(exc),
            expected_status="executing",
            restore_social_post_id=(
                operation["payload"]["post_id"]
                if operation["action_type"] == SOCIAL_SCHEDULE else None
            ),
        )
    except Exception as exc:
        log.error(
            "provider_outcome_unknown",
            extra={
                "event": "provider_outcome_unknown",
                "operation_id": operation_id,
                "provider": operation["provider"],
                "error": type(exc).__name__,
            },
        )
        return _finish(
            operation_id,
            "unknown",
            error_code=type(exc).__name__,
            error_detail=str(exc),
            expected_status="executing",
        )


def _find_gmail_message_by_rfc822_id(artist_id: str, message_id: str) -> Optional[dict]:
    service = pitch_service._get_gmail_service(artist_id)
    result = pitch_service._gmail_execute_with_retry(
        service.users().messages().list(
            userId="me",
            q=f"rfc822msgid:{message_id}",
            maxResults=1,
        ),
        artist_id=artist_id,
    )
    messages = result.get("messages") or []
    return messages[0] if messages else None


async def reconcile_operation(operation_id: str, artist_id: Optional[str] = None) -> dict:
    # Keep the service boundary consistent with execution and every other
    # operation mutation. The HTTP route validates this path too, but the
    # helper is also used by local recovery/test flows and must fail closed
    # before querying the durable ledger.
    operation_id = _require_bounded_string(operation_id, "operation_id", MAX_IDENTIFIER_LENGTH)
    operation = _get_operation(operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    _require_operation_owner(operation, artist_id)
    if operation["status"] not in RECONCILABLE_STATUSES:
        return operation

    if operation["action_type"] == GMAIL_SEND:
        try:
            message = _find_gmail_message_by_rfc822_id(
                operation["artist_id"],
                operation["correlation_key"],
            )
        except (pitch_service.GmailNotConnected, pitch_service.GmailAuthExpired) as exc:
            return _finish(
                operation_id,
                "unknown",
                error_code="reconciliation_unavailable",
                error_detail=str(exc),
                # The provider could not be queried, so the operation must
                # remain explicitly reconcilable for a later safe retry.
                reconciled=False,
                expected_status="unknown",
            )
        except Exception as exc:
            return _finish(
                operation_id,
                "unknown",
                error_code="reconciliation_failed",
                error_detail=str(exc),
                # A failed lookup is not a completed reconciliation. Keep the
                # unknown outcome visible until a lookup actually completes.
                reconciled=False,
                expected_status="unknown",
            )
        if message:
            result = {
                "message_id": message.get("id"),
                "thread_id": message.get("threadId"),
                "status": "sent",
                "reconciled": True,
            }
            return _finish(
                operation_id,
                "succeeded",
                provider_result=result,
                provider_reference=message.get("id"),
                reconciled=True,
                expected_status="unknown",
            )
        return _finish(
            operation_id,
            "unknown",
            error_code="provider_result_not_found",
            error_detail="Gmail did not return a message for the operation correlation key; manual review is required.",
            reconciled=True,
            expected_status="unknown",
        )

    return _finish(
        operation_id,
        "unknown",
        error_code="manual_reconciliation_required",
        error_detail="Buffer has no safe correlation lookup for an operation whose create response was lost; do not retry automatically.",
        reconciled=True,
        expected_status="unknown",
    )


class OperationRequest(BaseModel):
    artist_id: str
    action_type: str
    idempotency_key: str
    payload: dict


@router.get("/api/operations", tags=["operations"])
def list_operations(
    artist_id: str,
    limit: int = Query(50, ge=1, le=100),
    status: Optional[str] = None,
    before: Optional[str] = None,
    request: Request = None,
):
    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    scoped_artist_id = _require_bounded_string(
        scoped_artist_id, "artist_id", MAX_IDENTIFIER_LENGTH,
    )
    operations = _list_operations(scoped_artist_id, limit=limit, status=status, before=before)
    return {
        "operations": operations,
        "limit": limit,
        "next_before": _operation_cursor(operations[-1]) if len(operations) == limit else None,
    }


@router.post("/api/operations", tags=["operations"])
def create_operation(req: OperationRequest, request: Request = None):
    try:
        artist_id = require_artist_scope(request, req.artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    operation, created = _create_or_get_operation(
        artist_id,
        req.action_type,
        req.idempotency_key,
        req.payload,
    )
    return {"operation": operation, "created": created}


@router.get("/api/operations/lookup", tags=["operations"])
def lookup_operation(
    artist_id: str,
    action_type: str,
    idempotency_key: str,
    request: Request = None,
):
    """Recover a durable operation after a client lost its local operation ID."""
    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    # Recovery is a read-only path, but it still accepts artist-controlled
    # identifiers. Apply the same bounded-input contract as operation
    # creation before querying the durable ledger.
    scoped_artist_id = _require_bounded_string(scoped_artist_id, "artist_id", MAX_IDENTIFIER_LENGTH)
    action_type = _require_bounded_string(action_type, "action_type", MAX_IDENTIFIER_LENGTH)
    idempotency_key = _require_bounded_string(
        idempotency_key, "idempotency_key", MAX_IDENTIFIER_LENGTH,
    )

    if action_type not in SUPPORTED_ACTIONS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "unsupported_action",
                "action_type": action_type,
                "supported_actions": sorted(SUPPORTED_ACTIONS),
            },
        )

    operation = _get_operation_by_idempotency(
        scoped_artist_id, action_type, idempotency_key,
    )
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    return operation



@router.post("/api/operations/execute", tags=["operations"])
async def create_and_execute_operation(req: OperationRequest, request: Request = None):
    try:
        artist_id = require_artist_scope(request, req.artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    operation, created = _create_or_get_operation(
        artist_id,
        req.action_type,
        req.idempotency_key,
        req.payload,
    )
    # A newly-created operation has never passed through the artist approval
    # and final-readiness gates. Do not enter the execution state machine for
    # that convenience-route case; callers must use the explicit approval and
    # readiness endpoints before dispatch is possible.
    if created:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "operation_not_approved",
                "operation_id": operation["id"],
                "message": "Approve the durable operation and confirm final details before execution.",
            },
        )
    operation = await execute_operation(operation["id"], artist_id=artist_id)
    return {"operation": operation, "created": created}



@router.post("/api/operations/{operation_id}/approve", tags=["operations"])
def api_approve_operation(
    operation_id: str,
    artist_id: str,
    request: Request = None,
):
    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    operation_id = _require_bounded_string(operation_id, "operation_id", MAX_IDENTIFIER_LENGTH)
    return approve_operation(operation_id, artist_id=scoped_artist_id)


@router.post("/api/operations/{operation_id}/ready", tags=["operations"])
def api_mark_operation_ready(
    operation_id: str,
    artist_id: str,
    request: Request = None,
):
    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    operation_id = _require_bounded_string(operation_id, "operation_id", MAX_IDENTIFIER_LENGTH)
    return mark_operation_ready(operation_id, artist_id=scoped_artist_id)


@router.post("/api/operations/{operation_id}/cancel", tags=["operations"])
def api_cancel_operation(
    operation_id: str,
    artist_id: str,
    request: Request = None,
):
    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    operation_id = _require_bounded_string(operation_id, "operation_id", MAX_IDENTIFIER_LENGTH)
    return cancel_operation(operation_id, artist_id=scoped_artist_id)



@router.get("/api/operations/{operation_id}", tags=["operations"])
def get_operation(
    operation_id: str,
    artist_id: str,
    request: Request = None,
):
    operation_id = _require_bounded_string(operation_id, "operation_id", MAX_IDENTIFIER_LENGTH)

    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    operation = _get_operation(operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    _require_operation_owner(operation, scoped_artist_id)
    return operation



@router.post("/api/operations/{operation_id}/execute", tags=["operations"])
async def api_execute_operation(
    operation_id: str,
    artist_id: str,
    request: Request = None,
):
    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    scoped_artist_id = _require_bounded_string(
        scoped_artist_id, "artist_id", MAX_IDENTIFIER_LENGTH,
    )
    operation_id = _require_bounded_string(operation_id, "operation_id", MAX_IDENTIFIER_LENGTH)
    return await execute_operation(operation_id, artist_id=scoped_artist_id)



@router.post("/api/operations/{operation_id}/reconcile", tags=["operations"])
async def api_reconcile_operation(
    operation_id: str,
    artist_id: str,
    request: Request = None,
):
    try:
        scoped_artist_id = require_artist_scope(request, artist_id)
    except ArtistAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    scoped_artist_id = _require_bounded_string(
        scoped_artist_id, "artist_id", MAX_IDENTIFIER_LENGTH,
    )
    operation_id = _require_bounded_string(operation_id, "operation_id", MAX_IDENTIFIER_LENGTH)
    return await reconcile_operation(operation_id, artist_id=scoped_artist_id)
