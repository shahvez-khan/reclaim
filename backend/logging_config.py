"""
Structured logging (Phase 4.4 of the production-hardening loop).

Replaces ad-hoc print() debugging with JSON-formatted log lines carrying a
run_id that ties together every line for one batch run — the piece that
makes the audit trail operable at 2am instead of just pretty on a dashboard.
The dashboard's `print()`-based console output (in run_pipeline.py etc.) is
left as-is on purpose: those are the demo-facing narrative output, not
operational logs, and turning them into JSON would make the CLI demo worse
for no real benefit. This module is for the API/service layer, where a real
operator would actually be tailing logs.

Usage:
    from logging_config import configure_logging, new_run_id
    configure_logging()
    logger = logging.getLogger("revenue_recovery.something")
    logger.info("event_name", extra={"run_id": run_id, "key": "value"})
"""

import json
import logging
import sys
import uuid

from config import LOG_DIR as _CONFIG_LOG_DIR
from config import LOG_LEVEL

LOG_DIR = _CONFIG_LOG_DIR
LOG_FILE = LOG_DIR / "revenue_recovery.jsonl"


class JSONFormatter(logging.Formatter):
    RESERVED = {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int | None = None) -> None:
    """Writes structured JSON logs to logs/revenue_recovery.jsonl (append
    mode) — a real deployment would ship this to a log aggregator instead.
    Kept OUT of stdout on purpose: run_pipeline.py's print()-based narration
    is the demo-facing CLI output and shouldn't be interleaved with JSON log
    lines; this file is where an operator (or a compliance report generator)
    would actually go looking, without re-deriving anything from the DB.
    WARNING and above are additionally echoed to stderr so real failures are
    still visible in a terminal/container log without opening the file.
    """
    if level is None:
        level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)

    root = logging.getLogger("revenue_recovery")
    if root.handlers:
        return  # idempotent — don't double-attach handlers on repeated calls/imports

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(JSONFormatter())
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(JSONFormatter())
    stderr_handler.setLevel(logging.WARNING)
    root.addHandler(stderr_handler)

    root.setLevel(level)
    root.propagate = False


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]
