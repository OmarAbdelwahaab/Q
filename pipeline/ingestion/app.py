"""Runnable entrypoint for the Telegram ingestion worker."""

from __future__ import annotations

import asyncio

from pipeline.alerts import CompositeAlertService
from pipeline.config import IngestionSettings
from pipeline.ingestion.service import IngestionService
from pipeline.ingestion.telegram_listener import TelethonIngestionListener
from pipeline.logging import configure_logging
from pipeline.state.repository import PipelineStateRepository
from pipeline.storage import LocalArtifactStorage


def build_ingestion_listener(settings: IngestionSettings) -> TelethonIngestionListener:
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

    return TelethonIngestionListener(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        session_name=settings.telegram_session_name,
        channel_id=settings.telegram_channel_id,
        ingestion_service=ingestion_service,
        bot_token=settings.telegram_bot_token,
    )


async def main() -> None:
    settings = IngestionSettings.from_env()
    configure_logging(service_name="ingestion", level=settings.log_level)
    listener = build_ingestion_listener(settings)
    await listener.start()


if __name__ == "__main__":
    asyncio.run(main())
