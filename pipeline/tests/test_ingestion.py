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
            metadata = state_repository.fetch_message_metadata(101)
            self.assertIsNotNone(metadata)
            assert metadata is not None
            self.assertEqual(metadata["channel_id"], "-1001234567890")
            self.assertEqual(metadata["message_timestamp"], "2026-09-06T00:00:00+00:00")
            self.assertEqual(metadata["duration_seconds"], 42.0)
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

    async def test_successful_ingestion_triggers_on_ingested_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_repository = PipelineStateRepository(temp_path / "pipeline.db")
            alert_service = RecordingAlertService()
            service = IngestionService(
                storage=LocalArtifactStorage(temp_path),
                state_repository=state_repository,
                alert_service=alert_service,
            )

            orchestrated_ids: list[int] = []

            async def mock_on_ingested(message_id: int) -> None:
                orchestrated_ids.append(message_id)

            listener = TelethonIngestionListener(
                api_id=1,
                api_hash="hash",
                session_name="session",
                channel_id="channel",
                ingestion_service=service,
                on_ingested=mock_on_ingested,
            )

            async def download_behavior(file: str) -> str:
                Path(file).write_bytes(b"video-data")
                return file

            message = FakeTelegramMessage(message_id=303, duration=15, download_behavior=download_behavior)
            result = await listener.process_message(message, chat_id=-1001234567890)

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "completed")
            self.assertEqual(orchestrated_ids, [303])

    async def test_failed_ingestion_does_not_trigger_on_ingested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_repository = PipelineStateRepository(temp_path / "pipeline.db")
            alert_service = RecordingAlertService()
            service = IngestionService(
                storage=LocalArtifactStorage(temp_path),
                state_repository=state_repository,
                alert_service=alert_service,
                retry_attempts=1,
                retry_backoff_seconds=(),
            )

            orchestrated_ids: list[int] = []

            async def mock_on_ingested(message_id: int) -> None:
                orchestrated_ids.append(message_id)

            listener = TelethonIngestionListener(
                api_id=1,
                api_hash="hash",
                session_name="session",
                channel_id="channel",
                ingestion_service=service,
                on_ingested=mock_on_ingested,
            )

            async def fail_download(file: str) -> str | None:
                raise RuntimeError("download failed")

            message = FakeTelegramMessage(message_id=404, duration=15, download_behavior=fail_download)
            result = await listener.process_message(message, chat_id=-1001234567890)

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result.status, "failed")
            self.assertEqual(orchestrated_ids, [])

    def test_listener_initialization_with_session_string(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            service = IngestionService(
                storage=LocalArtifactStorage(temp_path),
                state_repository=PipelineStateRepository(temp_path / "pipeline.db"),
                alert_service=RecordingAlertService(),
            )
            listener = TelethonIngestionListener(
                api_id=12345,
                api_hash="abcde",
                session_name="my_session",
                channel_id="emamoathen",
                ingestion_service=service,
                session_string="1BVtsOH...",
            )
            self.assertEqual(listener.session_string, "1BVtsOH...")
            self.assertEqual(listener.session_name, "my_session")

    async def test_poll_recent_videos_skips_existing_and_processes_new(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_repo = PipelineStateRepository(temp_path / "pipeline.db")
            # Mark message 501 as already completed
            state_repo.upsert_stage(501, "ingestion", "completed")

            service = IngestionService(
                storage=LocalArtifactStorage(temp_path),
                state_repository=state_repo,
                alert_service=RecordingAlertService(),
            )

            processed_ids: list[int] = []

            async def mock_on_ingested(message_id: int) -> None:
                processed_ids.append(message_id)

            listener = TelethonIngestionListener(
                api_id=1,
                api_hash="hash",
                session_name="session",
                channel_id="channel",
                ingestion_service=service,
                on_ingested=mock_on_ingested,
            )

            # Create mock client for poll_recent_videos
            class FakeChannel:
                id = -1001234567890

            async def dl_501(f: str) -> str:
                Path(f).write_bytes(b"data")
                return f

            async def dl_503(f: str) -> str:
                Path(f).write_bytes(b"data-503")
                return f

            class MockClient:
                async def start(self, **kwargs) -> None:
                    pass

                async def get_entity(self, channel_id: str) -> FakeChannel:
                    return FakeChannel()

                async def iter_messages(self, channel: Any, limit: int = 10):
                    # Message 501 (already completed)
                    yield FakeTelegramMessage(501, 30, dl_501)
                    # Message 502 (text only, no video)
                    msg_text = SimpleNamespace(id=502, video=None, date=None)
                    yield msg_text
                    # Message 503 (new video)
                    yield FakeTelegramMessage(503, 45, dl_503)

                async def disconnect(self) -> None:
                    pass

            listener._create_client = lambda: MockClient()  # type: ignore[method-assign]

            results = await listener.poll_recent_videos(limit=5)
            # Only message 503 should be processed!
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].message_id, 503)
            self.assertEqual(processed_ids, [503])
            self.assertEqual((temp_path / "raw" / "503.mp4").read_bytes(), b"data-503")

    async def test_poll_main_cli_success(self) -> None:
        from pipeline.ingestion.poll import poll_main
        from unittest.mock import patch, AsyncMock

        with patch("pipeline.ingestion.poll.build_ingestion_listener") as mock_builder:
            mock_listener = AsyncMock()
            mock_listener.poll_recent_videos.return_value = []
            mock_builder.return_value = mock_listener

            exit_code = await poll_main(["--limit", "5"])
            self.assertEqual(exit_code, 0)
            mock_listener.poll_recent_videos.assert_awaited_once_with(limit=5)


if __name__ == "__main__":
    unittest.main()
