"""Shared structured logging configuration for pipeline modules."""

from __future__ import annotations

import json
import logging
import os
import socket
from datetime import datetime, timezone
from typing import Any


class ServiceContextFilter(logging.Filter):
    """Ensure every record carries a stable service identifier."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service"):
            record.service = self.service_name
        return True


class JsonFormatter(logging.Formatter):
    """Serialize log records as JSON for consistent downstream parsing."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": getattr(record, "service", "pipeline"),
            "hostname": socket.gethostname(),
        }

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        if os.getenv("LOG_INCLUDE_SOURCE", "false").lower() == "true":
            payload["source"] = {
                "path": record.pathname,
                "function": record.funcName,
                "line": record.lineno,
            }

        for key, value in record.__dict__.items():
            if key.startswith("_") or key in {
                "args",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
            }:
                continue
            payload[key] = value

        return json.dumps(payload, ensure_ascii=False)


def configure_logging(service_name: str = "pipeline", level: str | None = None) -> None:
    """Configure the root logger once for the whole process."""

    root = logging.getLogger()
    root.setLevel((level or os.getenv("LOG_LEVEL", "INFO")).upper())

    if root.handlers:
        for handler in root.handlers:
            handler.setFormatter(JsonFormatter())
            handler.addFilter(ServiceContextFilter(service_name))
        return

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ServiceContextFilter(service_name))
    root.addHandler(handler)


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter:
    """Return a logger adapter that carries structured context fields."""

    return logging.LoggerAdapter(logging.getLogger(name), context)
