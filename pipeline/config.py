"""Configuration loading for pipeline services."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(slots=True)
class IngestionSettings:
    telegram_api_id: int | None
    telegram_api_hash: str
    telegram_bot_token: str | None
    telegram_channel_id: str
    telegram_session_name: str
    state_db_path: Path
    storage_root: Path
    alert_webhook_url: str | None
    alert_telegram_bot_token: str | None
    alert_telegram_chat_id: str | None
    retry_attempts: int
    retry_backoff_seconds: tuple[int, ...]
    log_level: str
    log_include_source: bool

    @classmethod
    def from_env(cls) -> "IngestionSettings":
        raw_backoff = os.getenv("INGESTION_RETRY_BACKOFF_SECONDS", "1,2")
        retry_backoff = tuple(
            int(value.strip())
            for value in raw_backoff.split(",")
            if value.strip()
        ) or (1, 2)

        project_root = Path(os.getenv("PIPELINE_PROJECT_ROOT", Path.cwd()))
        storage_root = Path(os.getenv("PIPELINE_STORAGE_ROOT", project_root)).resolve()
        state_db_path = Path(
            os.getenv("STATE_DB_PATH", str(project_root / "pipeline" / "state" / "pipeline.db"))
        ).resolve()

        return cls(
            telegram_api_id=_read_int("TELEGRAM_API_ID"),
            telegram_api_hash=os.getenv("TELEGRAM_API_HASH", ""),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_channel_id=os.getenv("TELEGRAM_CHANNEL_ID", ""),
            telegram_session_name=os.getenv("TELEGRAM_SESSION_NAME", "pipeline_ingestion"),
            state_db_path=state_db_path,
            storage_root=storage_root,
            alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
            alert_telegram_bot_token=os.getenv("ALERT_TELEGRAM_BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or None,
            alert_telegram_chat_id=os.getenv("ALERT_TELEGRAM_CHAT_ID") or None,
            retry_attempts=_read_int("INGESTION_RETRY_ATTEMPTS", 3) or 3,
            retry_backoff_seconds=retry_backoff,
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_include_source=_read_bool("LOG_INCLUDE_SOURCE", False),
        )
