"""Runnable entrypoint for the Telegram ingestion worker."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable

from pipeline.alerts import CompositeAlertService
from pipeline.config import IngestionSettings
from pipeline.ingestion.service import IngestionService
from pipeline.ingestion.telegram_listener import TelethonIngestionListener
from pipeline.logging import configure_logging, get_logger
from pipeline.state.repository import PipelineStateRepository
from pipeline.storage import LocalArtifactStorage


def build_ingestion_listener(
    settings: IngestionSettings,
    on_ingested: Callable[[int], Awaitable[Any]] | None = None,
) -> TelethonIngestionListener:
    storage = LocalArtifactStorage(settings.storage_root)
    state_repository = PipelineStateRepository(settings.state_db_path)
    alert_service = CompositeAlertService(
        webhook_url=settings.alert_webhook_url,
        telegram_bot_token=settings.alert_telegram_bot_token,
        telegram_chat_id=settings.alert_telegram_chat_id,
    )
    ingestion_service = IngestionService(
        storage=storage,
        state_repository=state_repository,
        alert_service=alert_service,
        retry_attempts=settings.retry_attempts,
        retry_backoff_seconds=settings.retry_backoff_seconds,
    )

    if on_ingested is None and settings.auto_orchestrate:
        from pipeline.config import OrchestrationSettings
        from pipeline.orchestration.app import build_orchestrator

        orchestrator, _ = build_orchestrator(OrchestrationSettings.from_env())
        logger = get_logger(__name__, service="ingestion")

        async def _trigger_orchestration(message_id: int) -> None:
            logger.info(
                "Auto-triggering pipeline orchestration",
                extra={"message_id": message_id},
            )
            summary = await orchestrator.run(message_id)
            logger.info(
                "Pipeline orchestration completed",
                extra={
                    "message_id": message_id,
                    "status": summary.status,
                    "duration_seconds": summary.duration_seconds,
                    "error": summary.error,
                },
            )

        on_ingested = _trigger_orchestration

    return TelethonIngestionListener(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        session_name=settings.telegram_session_name,
        channel_id=settings.telegram_channel_id,
        ingestion_service=ingestion_service,
        bot_token=settings.telegram_bot_token,
        on_ingested=on_ingested,
        session_string=settings.telegram_session_string,
    )


async def main() -> None:
    settings = IngestionSettings.from_env()
    configure_logging(service_name="ingestion", level=settings.log_level)
    listener = build_ingestion_listener(settings)
    await listener.start()


if __name__ == "__main__":
    asyncio.run(main())
