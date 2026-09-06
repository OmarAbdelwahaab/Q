"""Core ingestion workflow for Telegram video messages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from pipeline.logging import get_logger
from pipeline.storage import DownloadToPath, LocalArtifactStorage


class StateRepository(Protocol):
    def upsert_stage(
        self,
        message_id: int,
        stage: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Persist a pipeline stage status."""


class AlertService(Protocol):
    async def send_ingestion_failure(self, message: str) -> None:
        """Send a failure alert."""


@dataclass(slots=True)
class IncomingVideoMessage:
    message_id: int
    timestamp: datetime
    channel_id: int | str
    duration: int | None
    downloader: DownloadToPath


@dataclass(slots=True)
class IngestionResult:
    message_id: int
    status: str
    storage_path: Path | None
    attempts: int
    error: str | None = None


SleepCallable = Callable[[float], Awaitable[None]]


class IngestionService:
    """Download incoming Telegram videos and register ingestion state."""

    stage_name = "ingestion"

    def __init__(
        self,
        storage: LocalArtifactStorage,
        state_repository: StateRepository,
        alert_service: AlertService,
        retry_attempts: int = 3,
        retry_backoff_seconds: tuple[int, ...] = (1, 2),
        sleep: SleepCallable = asyncio.sleep,
    ) -> None:
        self.storage = storage
        self.state_repository = state_repository
        self.alert_service = alert_service
        self.retry_attempts = retry_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self.sleep = sleep
        self.logger = get_logger(__name__, service="ingestion")

    async def ingest(self, message: IncomingVideoMessage) -> IngestionResult:
        storage_key = f"raw/{message.message_id}.mp4"
        self.state_repository.upsert_stage(
            message_id=message.message_id,
            stage=self.stage_name,
            status="processing",
        )

        last_error: str | None = None
        for attempt in range(1, self.retry_attempts + 1):
            try:
                destination = await self.storage.write_from_downloader(
                    storage_key,
                    message.downloader,
                )
                self.state_repository.upsert_stage(
                    message_id=message.message_id,
                    stage=self.stage_name,
                    status="completed",
                )
                self.logger.info(
                    "Video message ingested",
                    extra={
                        "message_id": message.message_id,
                        "channel_id": message.channel_id,
                        "duration": message.duration,
                        "storage_key": storage_key,
                        "attempt": attempt,
                    },
                )
                return IngestionResult(
                    message_id=message.message_id,
                    status="completed",
                    storage_path=destination,
                    attempts=attempt,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                self.logger.warning(
                    "Video download attempt failed",
                    extra={
                        "message_id": message.message_id,
                        "channel_id": message.channel_id,
                        "attempt": attempt,
                        "error": last_error,
                    },
                )

                if attempt < self.retry_attempts:
                    await self.sleep(self._backoff_for_attempt(attempt))
                    continue

        self.state_repository.upsert_stage(
            message_id=message.message_id,
            stage=self.stage_name,
            status="failed",
            error=last_error,
        )

        alert_message = (
            f"Pipeline ingestion failed for Telegram message {message.message_id} "
            f"after {self.retry_attempts} attempts. Error: {last_error}"
        )
        await self.alert_service.send_ingestion_failure(alert_message)

        return IngestionResult(
            message_id=message.message_id,
            status="failed",
            storage_path=None,
            attempts=self.retry_attempts,
            error=last_error,
        )

    def _backoff_for_attempt(self, attempt: int) -> int:
        if not self.retry_backoff_seconds:
            return 0
        index = min(attempt - 1, len(self.retry_backoff_seconds) - 1)
        return self.retry_backoff_seconds[index]
