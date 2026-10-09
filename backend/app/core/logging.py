"""Structured logging: one JSON object per line on stdout, with the current request ID.

Never log cookies, tokens, passwords or request bodies. As a second line of defence the
formatter masks session cookies and bearer tokens if one ever reaches a log message.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone

from app.core.config import Settings

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

# Structured fields callers may attach with ``extra={...}``; anything else is ignored.
FIELDS = ("event", "method", "path", "status", "duration_ms", "environment", "check")
_SECRETS = [
    (re.compile(r"(gov_preview=)[^;\s,\"']+"), r"\1[redacted]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[redacted]"),
    (re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;\"']+"), r"\1[redacted]"),
]
_MARK = "_governance_handler"


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    return text


class StdoutHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stdout`` is at emit time (survives redirection and test capture)."""

    def __init__(self):
        super().__init__(sys.stdout)

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, _value):
        pass


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if getattr(record, "request_id", None):
            entry["request_id"] = record.request_id
        for field in FIELDS:
            if hasattr(record, field):
                entry[field] = getattr(record, field)
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = f"{record.levelname} {record.name}: {redact(record.getMessage())}"
        if getattr(record, "request_id", None):
            line += f" [request_id={record.request_id}]"
        extras = " ".join(f"{f}={getattr(record, f)}" for f in FIELDS if hasattr(record, f))
        line = f"{line} {extras}".rstrip()
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


def configure_logging(settings: Settings) -> None:
    """Idempotent: replaces only the handler this function installed earlier."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _MARK, False)]:
        root.removeHandler(handler)
    handler = StdoutHandler()
    setattr(handler, _MARK, True)
    handler.setFormatter(JsonFormatter() if settings.governance_log_format == "json" else TextFormatter())
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)
    root.setLevel(settings.governance_log_level.upper())
    # Uvicorn's own messages go through the same JSON handler; its access log is replaced by the
    # request log written by the observability middleware (which includes the request ID).
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    # HTTP client libraries log full URLs (including query strings) at INFO; keep them quiet.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True
