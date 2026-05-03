"""Structured logging setup for Symphony."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    """Emit structured JSON log lines."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Carry extra context fields
        for key in ("issue_id", "issue_identifier", "session_id"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info and record.exc_info[1]:
            payload["error"] = str(record.exc_info[1])
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Configure structured JSON logging to stderr."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(StructuredFormatter())

    # Remove existing handlers to avoid duplicates on reload
    root.handlers.clear()
    root.addHandler(handler)
