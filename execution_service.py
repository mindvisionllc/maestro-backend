"""
Durable execution ledger for the backend's existing single-action providers.

Only Gmail one-off sends and scheduling one existing social post are supported.
Batch outreach, PR, pitch, booking, and campaign actions intentionally remain
outside this service.
"""

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from email.utils import make_msgid, parseaddr
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import pitch_service
import social_service


log = logging.getLogger("execution_service")
router = APIRouter()

_DB_PATH = Path(os.environ.get("DB_PATH", "/data/memory.db"))

GMAIL_SEND = "gmail.send"
SOCIAL_SCHEDULE = "social.buffer.schedule"
SUPPORTED_ACTIONS = {GMAIL_SEND, SOCIAL_SCHEDULE}

TERMINAL_STATUSES = {"succeeded", "failed"}
EXECUTABLE_STATUSES = {"pending", "failed_retryable"}
RECONCILABLE_STATUSES = {"unknown"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    existing_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(execution_operations)").fetchall()
    }
    if "resource_key" not in existing_cols:
        conn.execute("ALTER TABLE execution_operations ADD COLUMN resource_key TEXT")
    if "profile_binding" not in existing_cols:
        conn.execute("ALTER TABLE execution_operations ADD COLUMN profile_binding TEXT")
    if "approved_at" not in existing_cols:
        conn.execute("ALTER TABLE execution_operations ADD COLUMN approved_at TEXT")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_operation_resource
           ON execution_operations (action_type, resource_key)
           WHERE resource_key IS NOT NULL"""
    )
    # Never blindly retry a provider call after process loss: the provider may
    # have accepted it before the response or local commit was lost.
    conn.execute(
        """UPDATE execution_operations
           SET status='unknown',
               error_code='process_interrupted',
               error_detail='Execution was interrupted after provider dispatch; reconcile before retrying.',
               updated_at=?
           WHERE status='executing'""",
        (_now(),),
    )
    conn.commit()
    conn.close()
    log.info("db_ready", extra={"event": "db_ready", "svc": "execution_service"})


_OP_COLS = [
    "id", "artist_id", "action_type", "idempotency_key", "payload", "status",
    "provider", "provider_reference", "provider_result", "correlation_key",
    "resource_key", "profile_binding", "approved_at", "attempt_count",
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
    return operation


def _get_operation(operation_id: str) -> dict:
    conn = sqlite3.connect(str(_DB_PATH))
    row = conn.execute(
        f"SELECT {','.join(_OP_COLS)} FROM execution_operations WHERE id=?",
        (operation_id,),
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
        normalized["to"] = payload["to"].strip()
        if not _valid_single_email_target(normalized["to"]):
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_recipient", "message": "A single valid recipient email is required."},
            )
    elif action_type == SOCIAL_SCHEDULE:
        if not isinstance(payload.get("post_id"), str) or not payload["post_id"]:
            raise HTTPException(status_code=422, detail="social.buffer.schedule requires post_id")
        profiles = payload.get("buffer_profile_ids")
        if not isinstance(profiles, list) or not profiles or not all(isinstance(item, str) and item for item in profiles):
            raise HTTPException(
                status_code=422,
                detail="social.buffer.schedule requires non-empty buffer_profile_ids",
            )
        normalized["buffer_profile_ids"] = sorted(set(profiles))
    return normalized


def _create_or_get_operation(
    artist_id: str,
    action_type: str,
    idempotency_key: str,
    payload: dict,
) -> tuple[dict, bool]:
    payload = _validate_payload(action_type, payload)
    if not artist_id.strip() or not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="artist_id and idempotency_key are required")

    operation_id = str(uuid.uuid4())
    provider = "gmail" if action_type == GMAIL_SEND else "buffer"
    correlation_key = _message_id_for(operation_id) if action_type == GMAIL_SEND else operation_id
    resource_key = payload["post_id"] if action_type == SOCIAL_SCHEDULE else None
    profile_binding = payload.get("buffer_profile_ids") if action_type == SOCIAL_SCHEDULE else None
    timestamp = _now()
    encoded_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))

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
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "resource_already_has_operation",
                    "operation_id": conflicting["id"],
                    "message": "This social post is already bound to an execution operation.",
                },
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
        timestamp = _now()
        conn.execute(
            """UPDATE execution_operations
               SET status='executing', attempt_count=attempt_count+1,
                   provider_started_at=?, updated_at=?, error_code=NULL, error_detail=NULL
               WHERE id=?""",
            (timestamp, timestamp, operation_id),
        )
        conn.commit()
        return _get_operation(operation_id), True
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
) -> dict:
    timestamp = _now()
    conn = sqlite3.connect(str(_DB_PATH))
    where = "WHERE id=?"
    params = [
        status,
        json.dumps(provider_result) if provider_result is not None else None,
        provider_reference,
        error_code,
        error_detail,
        timestamp,
        status,
        timestamp,
        1 if reconciled else 0,
        timestamp,
        operation_id,
    ]
    if expected_status:
        where += " AND status=?"
        params.append(expected_status)
    conn.execute(
        """UPDATE execution_operations
           SET status=?, provider_result=?, provider_reference=?,
               error_code=?, error_detail=?, updated_at=?,
                completed_at=CASE WHEN ? IN ('succeeded','failed') THEN ? ELSE completed_at END,
               reconciled_at=CASE WHEN ? THEN ? ELSE reconciled_at END
           """ + where,
        params,
    )
    conn.commit()
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


def _restore_social_post(operation: dict):
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """UPDATE social_posts SET status='draft'
           WHERE id=? AND artist_id=? AND status='scheduling'""",
        (operation["payload"]["post_id"], operation["artist_id"]),
    )
    conn.commit()
    conn.close()


def _require_operation_owner(operation: dict, artist_id: Optional[str]):
    if artist_id is not None and operation["artist_id"] != artist_id:
        raise HTTPException(status_code=404, detail="Operation not found")


def approve_operation(operation_id: str, artist_id: Optional[str] = None) -> dict:
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
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """UPDATE execution_operations
           SET approved_at=COALESCE(approved_at, ?), updated_at=?
           WHERE id=? AND status IN ('pending','failed_retryable')""",
        (timestamp, timestamp, operation_id),
    )
    conn.commit()
    conn.close()
    return _get_operation(operation_id)


async def execute_operation(operation_id: str, artist_id: Optional[str] = None) -> dict:
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
        # Mock/local execution must never make a live Buffer request. In live
        # mode, fail closed by revalidating the immutable profile binding
        # immediately before the write-capable provider call.
        if social_service._BUFFER_LIVE:
            try:
                profiles = await social_service._buffer_list_profiles(operation["artist_id"])
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
        provider_reference = result.get("id")
        social_service._db_update_post(
            post["id"],
            {"status": "scheduled", "buffer_update_id": provider_reference or ""},
        )
        return _finish(
            operation_id,
            "succeeded",
            provider_result=result,
            provider_reference=provider_reference,
            expected_status="executing",
        )
    except (pitch_service.GmailNotConnected, pitch_service.GmailAuthExpired) as exc:
        return _finish(
            operation_id,
            "failed_retryable",
            error_code=type(exc).__name__,
            error_detail=str(exc),
            expected_status="executing",
        )
    except social_service.BufferNotConnected as exc:
        if operation["action_type"] == SOCIAL_SCHEDULE:
            _restore_social_post(operation)
        return _finish(
            operation_id,
            "failed_retryable",
            error_code=type(exc).__name__,
            error_detail=str(exc),
            expected_status="executing",
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
                reconciled=True,
                expected_status="unknown",
            )
        except Exception as exc:
            return _finish(
                operation_id,
                "unknown",
                error_code="reconciliation_failed",
                error_detail=str(exc),
                reconciled=True,
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


@router.post("/api/operations", tags=["operations"])
def create_operation(req: OperationRequest):
    operation, created = _create_or_get_operation(
        req.artist_id,
        req.action_type,
        req.idempotency_key,
        req.payload,
    )
    return {"operation": operation, "created": created}


@router.post("/api/operations/execute", tags=["operations"])
async def create_and_execute_operation(req: OperationRequest):
    operation, created = _create_or_get_operation(
        req.artist_id,
        req.action_type,
        req.idempotency_key,
        req.payload,
    )
    operation = await execute_operation(operation["id"])
    return {"operation": operation, "created": created}


@router.post("/api/operations/{operation_id}/approve", tags=["operations"])
def api_approve_operation(operation_id: str, artist_id: str):
    return approve_operation(operation_id, artist_id=artist_id)


@router.get("/api/operations/{operation_id}", tags=["operations"])
def get_operation(operation_id: str, artist_id: str):
    operation = _get_operation(operation_id)
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    _require_operation_owner(operation, artist_id)
    return operation


@router.post("/api/operations/{operation_id}/execute", tags=["operations"])
async def api_execute_operation(operation_id: str, artist_id: str):
    return await execute_operation(operation_id, artist_id=artist_id)


@router.post("/api/operations/{operation_id}/reconcile", tags=["operations"])
async def api_reconcile_operation(operation_id: str, artist_id: str):
    return await reconcile_operation(operation_id, artist_id=artist_id)