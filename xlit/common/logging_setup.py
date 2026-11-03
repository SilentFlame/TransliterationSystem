"""Structured logging shared by the central registry and the local service.

Every process writes JSON lines to ``<log_dir>/<service>.log`` (rotating) and a
short human-readable line to stderr. Call sites use :func:`log_event` so that
fields stay machine-parseable (``jq`` friendly) instead of being baked into
free-form strings.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import socket
import time
from pathlib import Path
from typing import Any

ROOT_LOGGER = "xlit"

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service
        self._host = socket.gethostname()

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}"
            + time.strftime("%z", time.localtime(record.created)),
            "level": record.levelname,
            "service": self._service,
            "host": self._host,
            "pid": record.process,
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            payload.update(fields)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{time.strftime('%H:%M:%S', time.localtime(record.created))} {record.levelname:<5} {record.name}: {record.getMessage()}"
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields:
            base += " " + " ".join(f"{k}={v}" for k, v in fields.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(
    service: str,
    log_dir: Path,
    level: str = "INFO",
    console: bool = True,
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 5,
) -> Path:
    """Attach file + console handlers to the ``xlit`` logger tree.

    Returns the log file path. Safe to call once per process.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{service}.log"

    root = logging.getLogger(ROOT_LOGGER)
    root.setLevel(os.environ.get("XLIT_LOG_LEVEL", level).upper())
    root.propagate = False
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter(service))
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(ConsoleFormatter())
        root.addHandler(stream)

    # Werkzeug's per-request access lines duplicate our own request logging.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    return log_path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")


def log_event(logger: logging.Logger, level: int, event: str, **fields: Any) -> None:
    logger.log(level, event, extra={"fields": fields})
