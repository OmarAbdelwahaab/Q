from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from pipeline.ingestion.service import IngestionService
from pipeline.ingestion.telegram_listener import TelethonIngestionListener
from pipeline.state.repository import PipelineStateRepository
from pipeline.storage import LocalArtifactStorage


class RecordingAlertService:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_ingestion_failure(self, message: str) -> None:
        self.messages.append(message)


class FakeTelegramMessage:
    def __init__(self, message_id: int, duration: int | None, download_behavior) -> None:
        self.id = message_id
        self.date = datetime(2026, 9, 6, tzinfo=timezone.utc)
        self.video = SimpleNamespace(duration=duration)
        self._download_behavior = download_behavior

    async def download_media(self, file: str) -> str | None:
        return await self._download_behavior(file)


class IngestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mocked_video_message_is_saved_and_state_is_updated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_repository = PipelineStateRepository(temp_path / "pipeline.db")
            alert_service = RecordingAlertService()
            service = IngestionService(
                storage=LocalArtifactStorage(temp_path),
                state_repository=state_repository,
                alert_service=alert_service,
            )
            listener = TelethonIngestionListener(
                api_id=1,
                api_hash="hash",
                session_name="session",
                channel_id="channel",
                ingestion_service=service,
            )

            async def download_behavior(file: str) -> str:
                Path(file).write_bytes(b"video-data")
                return file

            message = FakeTelegramMessage(message_id=101, duration=42, download_behavior=download_behavior)
            result = await listener.process_message(message, chat_id=-1001234567890)

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.attempts, 1)
            self.assertEqual((temp_path / "raw" / "101.mp4").read_bytes(), b"video-data")

            state_row = state_repository.fetch_stage(101, "ingestion")
            self.assertIsNotNone(state_row)
            assert state_row is not None
            self.assertEqual(state_row["status"], "completed")
            self.assertIsNone(state_row["error"])
            self.assertEqual(alert_service.messages, [])

    async def test_failed_download_retries_three_times_and_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_repository = PipelineStateRepository(temp_path / "pipeline.db")
            alert_service = RecordingAlertService()
            sleeps: list[float] = []

            async def fake_sleep(delay: float) -> None:
                sleeps.append(delay)

            service = IngestionService(
                storage=LocalArtifactStorage(temp_path),
                state_repository=state_repository,
                alert_service=alert_service,
                retry_attempts=3,
                retry_backoff_seconds=(1, 2),
                sleep=fake_sleep,
            )
            listener = TelethonIngestionListener(
                api_id=1,
                api_hash="hash",
                session_name="session",
                channel_id="channel",
                ingestion_service=service,
            )

            attempts = {"count": 0}

            async def download_behavior(file: str) -> str | None:
                attempts["count"] += 1
                raise RuntimeError("network timeout")

            message = FakeTelegramMessage(message_id=202, duration=65, download_behavior=download_behavior)
            result = await listener.process_message(message, chat_id=-1001234567890)

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.attempts, 3)
            self.assertEqual(attempts["count"], 3)
            self.assertEqual(sleeps, [1, 2])
            self.assertEqual(len(alert_service.messages), 1)
            self.assertIn("202", alert_service.messages[0])
            self.assertFalse((temp_path / "raw" / "202.mp4").exists())

            state_row = state_repository.fetch_stage(202, "ingestion")
            self.assertIsNotNone(state_row)
            assert state_row is not None
            self.assertEqual(state_row["status"], "failed")
            self.assertEqual(state_row["error"], "network timeout")


if __name__ == "__main__":
    unittest.main()
