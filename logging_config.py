"""
Structured logging configuration for PLMKR.

- JSON formatter when RAILWAY_ENVIRONMENT is set (production).
- Human-readable formatter otherwise (local dev).
- request_id bound per-request via contextvars; available to all loggers in that request.
- Ring buffer handler (max 200 entries, thread-safe) for /api/admin/diagnostics.
"""

import json
import logging
import threading
import uuid
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

# ── Request-ID contextvar ─────────────────────────────────────────────────────

_request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def bind_request_id(request_id: str) -> None:
    """Bind request_id for the current async task / thread."""
    _request_id_var.set(request_id)


def get_request_id() -> str:
    return _request_id_var.get()


# ── JSON formatter ─────────────────────────────────────────────────────────────

class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict[str, Any] = {
            "ts":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
        }
        rid = _request_id_var.get()
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Include any extra fields the caller passed
        skip = logging.LogRecord.__dict__.keys() | {
            "message", "asctime", "args", "msg", "levelno", "levelname",
            "pathname", "filename", "module", "exc_info", "exc_text",
            "stack_info", "lineno", "funcName", "created", "msecs",
            "relativeCreated", "thread", "threadName", "processName", "process", "name",
        }
        for key, val in record.__dict__.items():
            if key not in skip:
                payload[key] = val
        return json.dumps(payload, default=str)


# ── Human-readable formatter ──────────────────────────────────────────────────

class _HumanFormatter(logging.Formatter):
    FMT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
    DATEFMT = "%Y-%m-%dT%H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        formatted = super().format(record)
        rid = _request_id_var.get()
        if rid:
            formatted += f" [rid={rid[:8]}]"
        return formatted

    def __init__(self):
        super().__init__(fmt=self.FMT, datefmt=self.DATEFMT)


# ── In-memory ring buffer handler (for /api/admin/diagnostics) ────────────────

class RingBufferHandler(logging.Handler):
    """Thread-safe bounded ring buffer that retains the last *maxlen* ERROR+ entries."""

    def __init__(self, maxlen: int = 200, level: int = logging.ERROR):
        super().__init__(level)
        self._buf: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "level":   record.levelname,
                "logger":  record.name,
                "msg":     record.getMessage(),
            }
            rid = _request_id_var.get()
            if rid:
                entry["request_id"] = rid
            with self._lock:
                self._buf.append(entry)
        except Exception:  # noqa: BLE001 — handler must never raise
            self.handleError(record)

    def get_entries(self) -> list[dict]:
        with self._lock:
            return list(self._buf)


# Singleton ring buffer; A2 reads from it
_ring_buffer: Optional[RingBufferHandler] = None


def get_ring_buffer() -> Optional[RingBufferHandler]:
    return _ring_buffer


# ── Public API ────────────────────────────────────────────────────────────────

def setup_logging() -> None:
    """Configure root logger. Call once at application boot, before app creation."""
    global _ring_buffer

    import os
    on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT"))

    formatter: logging.Formatter = _JSONFormatter() if on_railway else _HumanFormatter()

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    _ring_buffer = RingBufferHandler(maxlen=200, level=logging.ERROR)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Avoid duplicate handlers on reload (tests)
    root.handlers.clear()
    root.addHandler(handler)
    root.addHandler(_ring_buffer)

    # Silence noisy third-party loggers
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # anthropic SDK: its own "Retrying request to ... in N seconds" line (the
    # one that shows up in our logs today) is INFO, but the status code that
    # actually caused the retry (429/408/409/5xx, vs. a connection/timeout
    # error with no status at all) is only ever logged by the SDK at DEBUG —
    # invisible at our root INFO level, so a retry couldn't be attributed to a
    # cause (VOICE_DIAGNOSIS.md Pass 3 §5 — investigate only, no retry-behavior
    # change). Bump the SDK logger to DEBUG but filter it down to just the
    # retry-cause lines instead of turning on its full per-request/response
    # firehose (which would also be needlessly verbose in production).
    class _AnthropicRetryCauseFilter(logging.Filter):
        _ALLOW_PREFIXES = (
            "Retrying due to status code",
            "Retrying as header",
            "Not retrying as header",
            "Encountered httpx.TimeoutException",
            "Raising timeout error",
            "Raising connection error",
        )
        def filter(self, record: logging.LogRecord) -> bool:
            if record.levelno >= logging.INFO:
                return True
            return record.getMessage().startswith(self._ALLOW_PREFIXES)

    _anthropic_logger = logging.getLogger("anthropic")
    _anthropic_logger.setLevel(logging.DEBUG)
    _anthropic_logger.addFilter(_AnthropicRetryCauseFilter())


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Requires setup_logging() called first."""
    return logging.getLogger(name)
