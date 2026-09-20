"""Telethon-based listener for new Telegram video messages."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from pipeline.ingestion.service import IngestionResult, IngestionService, IncomingVideoMessage
from pipeline.logging import get_logger


def normalize_telegram_target(target: str | int) -> int | str:
    """Normalize Telegram channel/chat identifier for Telethon resolution.

    - Numeric strings (e.g. "-100123456789" or "123456789") -> int
    - URLs (e.g. "https://t.me/channel" or "t.me/channel") -> "@channel"
    - Invite links (e.g. "+...", "joinchat/...") -> unchanged string
    - Bare usernames (e.g. "channel") -> "@channel"
    - Pre-formatted usernames (e.g. "@channel") -> "@channel"
    """
    if isinstance(target, int):
        return target
    s = str(target).strip()
    if s.startswith("-") and s[1:].isdigit():
        return int(s)
    if s.isdigit():
        return int(s)
    if s.startswith("https://t.me/"):
        s = s[len("https://t.me/"):]
    elif s.startswith("http://t.me/"):
        s = s[len("http://t.me/"):]
    elif s.startswith("t.me/"):
        s = s[len("t.me/"):]

    if s.startswith("+") or s.startswith("joinchat/"):
        return s

    s = s.lstrip("@")
    return f"@{s}" if s else target


class TelethonIngestionListener:
    """Listen for incoming videos via Telethon user or bot account."""

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

        target = normalize_telegram_target(self.channel_id)
        @client.on(events.NewMessage(chats=target))
        async def handle_new_message(event: Any) -> None:
            await self.process_message(event.message, event.chat_id)

        self.logger.info(
            "Telegram ingestion listener started",
            extra={"channel_id": self.channel_id, "session_name": self.session_name},
        )
        await client.run_until_disconnected()

    async def poll_recent_videos(
        self,
        limit: int = 10,
        max_downloads: int | None = None,
    ) -> list[IngestionResult]:
        """Fetch recent channel messages, process any newly arrived videos, and return results."""
        if not self.api_id or not self.api_hash or not self.channel_id:
            raise ValueError("Telegram listener configuration is incomplete.")

        client = self._create_client()

        # In headless CI environments, verify authorization to avoid EOFError on input()
        if hasattr(client, "is_user_authorized"):
            if hasattr(client, "is_connected") and not client.is_connected():
                await client.connect()
            if not await client.is_user_authorized():
                if hasattr(client, "disconnect"):
                    await client.disconnect()
                raise RuntimeError(
                    "Telegram client is not authorized. In headless CI environments (like GitHub Actions), "
                    "a valid TELEGRAM_SESSION_STRING is required. Run 'python scripts/generate_session_string.py' "
                    "locally on your computer to log in and generate the string, then add it to GitHub Secrets."
                )

        if hasattr(client, "start"):
            if self.bot_token:
                await client.start(bot_token=self.bot_token)
            else:
                await client.start()

        target = normalize_telegram_target(self.channel_id)
        results: list[IngestionResult] = []
        download_count = 0
        try:
            try:
                channel = await client.get_entity(target)
            except Exception as exc:
                self.logger.error(
                    "Failed to find Telegram channel or chat entity",
                    extra={"channel_id": self.channel_id, "target": str(target), "error": str(exc)},
                )
                err_text = str(exc)
                exc_type = type(exc).__name__
                if (
                    "UsernameNotOccupied" in exc_type
                    or "not in use" in err_text.lower()
                    or "no user has" in err_text.lower()
                ):
                    hint = (
                        f"The Telegram channel/username '{self.channel_id}' does not exist on Telegram. "
                        "Please verify your channel's public @username or numeric ID, and update TELEGRAM_CHANNEL_ID "
                        "in your repository variables (Settings -> Secrets and variables -> Actions -> Variables) "
                        "or provide it in the 'Run workflow' inputs."
                    )
                elif "ChannelPrivate" in exc_type or "private" in err_text.lower():
                    hint = (
                        f"The Telegram channel '{self.channel_id}' is private and your Telegram account is not a member. "
                        "Please join the channel with your account first, or use a public channel handle."
                    )
                else:
                    hint = (
                        f"Could not resolve Telegram entity '{self.channel_id}' (target: '{target}'): {exc}. "
                        "Ensure the channel username or numeric ID is correct and accessible by your Telegram account."
                    )
                raise RuntimeError(hint) from exc

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

                if max_downloads is not None and download_count >= max_downloads:
                    self.logger.info(
                        "Reached maximum download count for this polling batch",
                        extra={"max_downloads": max_downloads, "downloaded": download_count},
                    )
                    break

                chat_id = getattr(channel, "id", self.channel_id)
                res = await self.process_message(message, chat_id)
                if res:
                    results.append(res)
                    download_count += 1
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
