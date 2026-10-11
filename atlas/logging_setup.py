"""Logging: concise human-readable lines on stderr, structured JSON lines in a log file."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    KEYS = ("run_id", "instrument_id", "timeframe", "code", "reason", "inserted", "duplicates",
            "unfinished_skipped", "attempt", "delay_s", "error", "resumed_from", "checkpoint_ms")

    def format(self, record: logging.LogRecord) -> str:
        extras = " ".join(f"{k}={record.__dict__[k]}" for k in self.KEYS if k in record.__dict__)
        return f"{record.levelname:<7} {record.getMessage()} {extras}".rstrip()


def setup_logging(log_path: Path | None, level: int = logging.INFO) -> None:
    root = logging.getLogger("atlas")
    root.setLevel(level)
    root.handlers.clear()
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(JsonLinesFormatter())
        root.addHandler(fh)
