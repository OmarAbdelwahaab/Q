"""Telethon-based listener for new Telegram video messages."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from pipeline.ingestion.service import IngestionResult, IngestionService, IncomingVideoMessage
from pipeline.logging import get_logger


class TelethonIngestionListener:
    """Subscribe to a channel and pass video posts into the ingestion service."""

    def __init__(
        self,
        api_id: int | None,
        api_hash: str,
        session_name: str,
        channel_id: str,
        ingestion_service: IngestionService,
        bot_token: str | None = None,
        on_ingested: Callable[[int], Awaitable[Any]] | None = None,
        session_string: str | None = None,
    ) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.channel_id = channel_id
        self.ingestion_service = ingestion_service
        self.bot_token = bot_token
        self.on_ingested = on_ingested
        self.session_string = session_string
        self.logger = get_logger(__name__, service="ingestion")

    def _create_client(self) -> Any:
        try:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except ImportError as exc:  # pragma: no cover - dependency checked at runtime
            raise RuntimeError(
                "Telethon is required for the Telegram listener. Install project dependencies first."
            ) from exc

        session: Any = (
            StringSession(self.session_string)
            if self.session_string
            else self.session_name
        )
        return TelegramClient(session, self.api_id, self.api_hash)

    async def start(self) -> None:
        if not self.api_id or not self.api_hash or not self.channel_id:
            raise ValueError("Telegram listener configuration is incomplete.")

        try:
            from telethon import events
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("Telethon is required for the Telegram listener.") from exc

        client = self._create_client()
        if self.bot_token:
            await client.start(bot_token=self.bot_token)
        else:
            await client.start()

        @client.on(events.NewMessage(chats=self.channel_id))
        async def handle_new_message(event: Any) -> None:
            await self.process_message(event.message, event.chat_id)

        self.logger.info(
            "Telegram ingestion listener started",
            extra={"channel_id": self.channel_id, "session_name": self.session_name},
        )
        await client.run_until_disconnected()

    async def poll_recent_videos(self, limit: int = 10) -> list[IngestionResult]:
        """Fetch recent channel messages, process any newly arrived videos, and return results."""
        if not self.api_id or not self.api_hash or not self.channel_id:
            raise ValueError("Telegram listener configuration is incomplete.")

        client = self._create_client()
        if self.bot_token:
            await client.start(bot_token=self.bot_token)
        else:
            await client.start()

        results: list[IngestionResult] = []
        try:
            channel = await client.get_entity(self.channel_id)
            async for message in client.iter_messages(channel, limit=limit):
                if not getattr(message, "video", None):
                    continue

                existing = self.ingestion_service.state_repository.fetch_stage(
                    message.id, "ingestion"
                )
                if existing and existing.get("status") in ("completed", "processing"):
                    self.logger.info(
                        "Skipping already ingested message during poll",
                        extra={"message_id": message.id, "status": existing.get("status")},
                    )
                    continue

                chat_id = getattr(channel, "id", self.channel_id)
                res = await self.process_message(message, chat_id)
                if res:
                    results.append(res)
        finally:
            await client.disconnect()

        return results

    async def process_message(self, telegram_message: Any, chat_id: int | str) -> IngestionResult | None:
        if not getattr(telegram_message, "video", None):
            return None

        normalized_message = self._normalize_message(telegram_message, chat_id)
        result = await self.ingestion_service.ingest(normalized_message)
        if result and result.status == "completed" and self.on_ingested:
            try:
                await self.on_ingested(result.message_id)
            except Exception as exc:
                self.logger.error(
                    "Error executing on_ingested callback",
                    extra={"message_id": result.message_id, "error": str(exc)},
                )
        return result

    def _normalize_message(self, telegram_message: Any, chat_id: int | str) -> IncomingVideoMessage:
        video = getattr(telegram_message, "video", None)

        async def downloader(destination: Path) -> None:
            downloaded = await telegram_message.download_media(file=str(destination))
            if not downloaded:
                raise RuntimeError("Telegram media download did not return a file path.")

        timestamp = getattr(telegram_message, "date", None) or datetime.now(timezone.utc)

        return IncomingVideoMessage(
            message_id=int(telegram_message.id),
            timestamp=timestamp,
            channel_id=chat_id,
            duration=getattr(video, "duration", None),
            downloader=downloader,
        )
