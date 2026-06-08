"""Structured logging setup.

By default, log records are formatted as JSON objects (one per line) on
stdout. Set ``LOG_FORMAT=text`` in `.env` to revert to a plain human-readable
format (useful for local dev or `--reload` loops).

The format is deliberately small and stable — fields beyond the canonical
``timestamp / level / logger / message`` come from ``LogRecord`` attributes,
which means ``logger.info("foo %s bar", x, extra={"date": d})`` includes
``date`` in the JSON output.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Default LogRecord attributes we DON'T want to forward into the JSON payload.
# Everything else found on the record is treated as user-supplied context.
_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """One JSON object per record, on stdout."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        # Forward any non-standard attributes (e.g. `logger.info(..., extra={"date":...})`).
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)  # serialisable?
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", *, structured: bool = True) -> None:
    """Configure the root logger.

    Idempotent — calling twice doesn't add duplicate handlers.
    """
    root = logging.getLogger()
    # Clear any handlers a previous call (or basicConfig) added.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream=sys.stdout)
    if structured:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
