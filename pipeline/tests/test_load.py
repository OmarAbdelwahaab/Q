"""Concurrency burst load testing for the Quran video repurposing pipeline.

Tests rapid burst arrivals of multiple videos across multi-threaded workers
using ThreadPoolExecutor and threading.Barrier to ensure thread safety, atomic
claim mutual exclusion, rate limiting serialization, and graceful posting window deferral.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from pipeline.alerts import CompositeAlertService
from pipeline.alignment.service import AlignmentResult, AlignmentService
from pipeline.audio.service import AudioDetails, AudioExtractionResult, AudioExtractionService
from pipeline.orchestration.runner import PipelineExecutionSummary, PipelineOrchestrator
from pipeline.orchestration.scheduler import PostingWindowScheduler
from pipeline.publish.service import PublishExecutionResult, PublishService
from pipeline.qa_gate.service import QAGateResult, QAGateService
from pipeline.recognition.matcher import VerseMatch
from pipeline.recognition.service import RecognitionResult, RecognitionService
from pipeline.render.service import RenderResult, RenderService
from pipeline.state.repository import PipelineStateRepository


class ConcurrencyBurstLoadTests(unittest.TestCase):
    """Load test suite validating multi-threaded concurrency under burst traffic."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temp_dir.name)
        self.db_path = self.storage_root / "test_burst_state.db"
        self.state_repo = PipelineStateRepository(database_path=self.db_path)
        self.alert_service = MagicMock(spec=CompositeAlertService)
        self.alert_service.send_orchestration_failure = AsyncMock()
        self.alert_service.send_qa_held_alert = AsyncMock()
        self.alert_service.send_monitoring_alert = AsyncMock()
        self.alert_service.send_status_report = AsyncMock()

        self.raw_dir = self.storage_root / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.state_repo.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _build_mock_services(self) -> dict[str, MagicMock]:
        """Construct mock stage services with dynamic per-message result factories."""
        mock_audio = MagicMock(spec=AudioExtractionService)

        async def _extract(message_id: int, **kwargs):
            return AudioExtractionResult(
                message_id=message_id,
                status="completed",
                storage_path=self.storage_root / "audio" / f"{message_id}.wav",
                details=AudioDetails(duration_seconds=15.0, sample_rate=16000, channels=1),
            )

        mock_audio.extract = AsyncMock(side_effect=_extract)

        mock_recognition = MagicMock(spec=RecognitionService)

        async def _recognize(message_id: int, **kwargs):
            return RecognitionResult(
                message_id=message_id,
                status="completed",
                storage_path=self.storage_root / "match" / f"{message_id}.json",
                match=VerseMatch(1, 1, 3, "بِسْمِ اللَّهِ", 0.98),
            )

        mock_recognition.recognize = AsyncMock(side_effect=_recognize)

        mock_alignment = MagicMock(spec=AlignmentService)

        async def _align(message_id: int, **kwargs):
            return AlignmentResult(
                message_id=message_id,
                status="completed",
                storage_path=self.storage_root / "align" / f"{message_id}.json",
                coverage=1.0,
            )

        mock_alignment.align = AsyncMock(side_effect=_align)

        mock_qa_gate = MagicMock(spec=QAGateService)

        async def _qa(message_id: int, **kwargs):
            return QAGateResult(
                message_id=message_id,
                status="approved",
                approved=True,
                match_confidence=0.98,
                alignment_coverage=1.0,
            )

        mock_qa_gate.evaluate = AsyncMock(side_effect=_qa)

        mock_render = MagicMock(spec=RenderService)

        async def _render(message_id: int, **kwargs):
            return RenderResult(
                message_id=message_id,
                status="completed",
                output_path=self.storage_root / "render" / f"{message_id}.mp4",
                duration_seconds=15.0,
            )

        mock_render.render = AsyncMock(side_effect=_render)

        mock_publish = MagicMock(spec=PublishService)

        async def _publish(message_id: int, **kwargs):
            return PublishExecutionResult(
                message_id=message_id,
                status="completed",
                artifact_path=self.storage_root / "publish" / f"{message_id}.json",
            )

        mock_publish.publish = AsyncMock(side_effect=_publish)

        return {
            "audio": mock_audio,
            "recognition": mock_recognition,
            "alignment": mock_alignment,
            "qa_gate": mock_qa_gate,
            "render": mock_render,
            "publish": mock_publish,
        }

    def test_burst_concurrent_video_processing(self) -> None:
        """Verify a burst of 16 distinct videos processed simultaneously all complete cleanly."""
        num_videos = 16
        message_ids = [1000 + i for i in range(num_videos)]

        # Seed mock raw video files
        for mid in message_ids:
            (self.raw_dir / f"{mid}.mp4").write_bytes(b"dummy video data")

        services = self._build_mock_services()
        # Disable scheduler window and rate limit interval for pure processing concurrency
        scheduler = PostingWindowScheduler(enabled=False, min_interval_seconds=0)

        orchestrator = PipelineOrchestrator(
            storage_root=self.storage_root,
            state_repository=self.state_repo,
            alert_service=self.alert_service,
            audio_service=services["audio"],
            recognition_service=services["recognition"],
            alignment_service=services["alignment"],
            qa_gate_service=services["qa_gate"],
            render_service=services["render"],
            publish_service=services["publish"],
            scheduler=scheduler,
        )

        barrier = threading.Barrier(num_videos)
        results: list[PipelineExecutionSummary] = []
        errors: list[Exception] = []

        def worker(mid: int) -> None:
            try:
                barrier.wait(timeout=10)
                summary = asyncio.run(orchestrator.run(mid))
                results.append(summary)
            except Exception as exc:
                errors.append(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_videos) as executor:
            futures = [executor.submit(worker, mid) for mid in message_ids]
            concurrent.futures.wait(futures, timeout=30)

        self.assertEqual(len(errors), 0, f"Encountered unexpected worker exceptions: {errors}")
        self.assertEqual(len(results), num_videos)

        for summary in results:
            self.assertEqual(
                summary.status,
                "completed",
                f"Video {summary.message_id} failed with error: {summary.error}",
            )
            # Verify state repository record
            stage_info = self.state_repo.fetch_stage(summary.message_id, "orchestration")
            self.assertIsNotNone(stage_info)
            self.assertEqual(stage_info["status"], "completed")

    def test_burst_duplicate_message_concurrency(self) -> None:
        """Verify that when 16 concurrent threads race for the exact same message, exactly 1 claims."""
        target_mid = 2000
        (self.raw_dir / f"{target_mid}.mp4").write_bytes(b"dummy video data")

        services = self._build_mock_services()
        scheduler = PostingWindowScheduler(enabled=False, min_interval_seconds=0)

        async def _slow_extract(message_id: int, **kwargs):
            await asyncio.sleep(0.05)
            return AudioExtractionResult(
                message_id=message_id,
                status="completed",
                storage_path=self.storage_root / "audio" / f"{message_id}.wav",
                details=AudioDetails(duration_seconds=15.0, sample_rate=16000, channels=1),
            )

        services["audio"].extract = AsyncMock(side_effect=_slow_extract)

        orchestrator = PipelineOrchestrator(
            storage_root=self.storage_root,
            state_repository=self.state_repo,
            alert_service=self.alert_service,
            audio_service=services["audio"],
            recognition_service=services["recognition"],
            alignment_service=services["alignment"],
            qa_gate_service=services["qa_gate"],
            render_service=services["render"],
            publish_service=services["publish"],
            scheduler=scheduler,
        )

        num_threads = 16
        barrier = threading.Barrier(num_threads)
        results: list[PipelineExecutionSummary] = []

        def worker() -> None:
            barrier.wait(timeout=10)
            summary = asyncio.run(orchestrator.run(target_mid))
            results.append(summary)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker) for _ in range(num_threads)]
            concurrent.futures.wait(futures, timeout=30)

        self.assertEqual(len(results), num_threads)
        completed = [r for r in results if r.status == "completed"]
        skipped = [r for r in results if r.status.startswith("skipped")]

        self.assertEqual(
            len(completed),
            1,
            f"Expected exactly 1 thread to complete execution; got {len(completed)}",
        )
        self.assertEqual(
            len(skipped),
            num_threads - 1,
            f"Expected {num_threads - 1} threads to be skipped; got {len(skipped)}",
        )
        for s in skipped:
            self.assertIn(s.status, ("skipped_active_execution", "skipped_already_published"))

    def test_burst_rate_limiting_mutual_exclusion(self) -> None:
        """Verify that 16 concurrent threads racing to reserve a publish slot serialize cleanly."""
        num_threads = 16
        barrier = threading.Barrier(num_threads)
        results: list[tuple[bool, float, str]] = []

        def worker(mid: int) -> None:
            barrier.wait(timeout=10)
            res = self.state_repo.reserve_publish_slot(mid, min_interval_seconds=1800)
            results.append(res)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker, 5000 + i) for i in range(num_threads)]
            concurrent.futures.wait(futures, timeout=30)

        self.assertEqual(len(results), num_threads)
        reserved = [r for r in results if r[0] is True]
        rate_limited = [r for r in results if r[0] is False]

        self.assertEqual(
            len(reserved),
            1,
            f"Expected exactly 1 thread to reserve publish slot; got {len(reserved)}",
        )
        self.assertEqual(
            len(rate_limited),
            num_threads - 1,
            f"Expected {num_threads - 1} threads to be rate limited; got {len(rate_limited)}",
        )
        for _, wait_s, reason in rate_limited:
            self.assertGreater(wait_s, 0.0)
            self.assertIn("Rate limit active", reason)

    def test_burst_posting_window_closed_deferral(self) -> None:
        """Verify that 16 concurrent items arriving when posting window is closed defer cleanly."""
        num_videos = 16
        message_ids = [3000 + i for i in range(num_videos)]

        for mid in message_ids:
            (self.raw_dir / f"{mid}.mp4").write_bytes(b"dummy video data")

        services = self._build_mock_services()
        # Configure scheduler with posting window closed
        scheduler = PostingWindowScheduler(
            enabled=True,
            start_hour=1,
            end_hour=2,
            timezone_name="UTC",
            min_interval_seconds=0,
        )
        scheduler.is_within_posting_window = MagicMock(return_value=False)
        scheduler.seconds_until_next_window = MagicMock(return_value=3600.0)

        orchestrator = PipelineOrchestrator(
            storage_root=self.storage_root,
            state_repository=self.state_repo,
            alert_service=self.alert_service,
            audio_service=services["audio"],
            recognition_service=services["recognition"],
            alignment_service=services["alignment"],
            qa_gate_service=services["qa_gate"],
            render_service=services["render"],
            publish_service=services["publish"],
            scheduler=scheduler,
        )

        barrier = threading.Barrier(num_videos)
        results: list[PipelineExecutionSummary] = []

        def worker(mid: int) -> None:
            barrier.wait(timeout=10)
            summary = asyncio.run(orchestrator.run(mid))
            results.append(summary)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_videos) as executor:
            futures = [executor.submit(worker, mid) for mid in message_ids]
            concurrent.futures.wait(futures, timeout=30)

        self.assertEqual(len(results), num_videos)
        for summary in results:
            self.assertEqual(
                summary.status,
                "scheduled",
                f"Video {summary.message_id} status should be 'scheduled', got: {summary.status}",
            )
            stage_info = self.state_repo.fetch_stage(summary.message_id, "orchestration")
            self.assertIsNotNone(stage_info)
            self.assertEqual(stage_info["status"], "scheduled")


if __name__ == "__main__":
    unittest.main()
