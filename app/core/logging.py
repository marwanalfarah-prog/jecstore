"""Structured logging (Part II 5) — no `print()` anywhere in the codebase.

Every record carries a `request_id` when one is bound, so a single storefront
request or admin action can be traced end to end across modules.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

from app.core.config import settings

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class _SafeLogger(logging.Logger):
    """A logger whose ``extra=`` can never crash the caller.

    Python's logging raises ``KeyError`` if ``extra`` contains a reserved
    ``LogRecord`` attribute — ``module``, ``filename``, ``name``, ``args`` and
    friends. Those are ordinary words in this domain ("which permission module?",
    "which filename was uploaded?"), so the collision is easy to walk into and
    the failure is disproportionate: an observability call taking down a
    checkout.

    Colliding keys are prefixed with ``ctx_`` instead. Logging degrades; the
    request survives.
    """

    def makeRecord(  # noqa: N802 - matching the stdlib API
        self, name, level, fn, lno, msg, args, exc_info,
        func=None, extra=None, sinfo=None,
    ):
        if extra:
            safe = {}
            for key, value in extra.items():
                safe[f"ctx_{key}" if key in _RESERVED else key] = value
            extra = safe
        return super().makeRecord(
            name, level, fn, lno, msg, args, exc_info, func, extra, sinfo
        )


# Installed at import time. Every application module reaches logging through
# `from app.core.logging import get_logger`, so this runs before any of their
# loggers are created.
logging.setLoggerClass(_SafeLogger)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def current_request_id() -> str | None:
    return _request_id.get()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if request_id := current_request_id():
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _RESERVED:
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


class _ConsoleFormatter(logging.Formatter):
    """Human-readable in development; extras are appended as key=value pairs."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if request_id := current_request_id():
            extras["request_id"] = request_id
        if extras:
            base += "  " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            _ConsoleFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # uvicorn ships its own handlers; route them through ours so output stays uniform.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    logging.getLogger("sqlalchemy.engine").setLevel(
        "INFO" if settings.database_echo else "WARNING"
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
