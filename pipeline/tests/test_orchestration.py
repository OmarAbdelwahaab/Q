"""Comprehensive tests for Phase 8 Orchestration, Scheduler, State Dual-Tier, and n8n Workflows."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from pipeline.alignment.ctc import AlignedWord
from pipeline.alignment.service import AlignmentResult, AlignmentService
from pipeline.audio.ffmpeg import AudioDetails
from pipeline.audio.service import AudioExtractionResult, AudioExtractionService
from pipeline.config import OrchestrationSettings
from pipeline.orchestration.app import build_orchestrator, main as cli_main, parse_args
from pipeline.orchestration.runner import PipelineExecutionSummary, PipelineOrchestrator
from pipeline.orchestration.scheduler import PostingWindowScheduler, ScheduleDecision
from pipeline.publish.client import PlatformPublishResult, PublishResponse
from pipeline.publish.service import PublishExecutionResult, PublishService
from pipeline.qa_gate.service import QAGateResult, QAGateService
from pipeline.recognition.matcher import VerseMatch
from pipeline.recognition.service import RecognitionResult, RecognitionService
from pipeline.render.service import RenderResult, RenderService
from pipeline.state.repository import PipelineStateRepository


# ---------------------------------------------------------------------------
# Test Helpers & Stubs
# ---------------------------------------------------------------------------

class RecordingAlertService:
    def __init__(self) -> None:
        self.alerts: list[str] = []

    async def send_orchestration_failure(self, message: str) -> None:
        self.alerts.append(message)

    async def send_alert(self, message: str) -> None:
        self.alerts.append(message)


# ---------------------------------------------------------------------------
# 1. PostingWindowScheduler Tests
# ---------------------------------------------------------------------------

class PostingWindowSchedulerTests(unittest.TestCase):
    def test_scheduler_disabled_allows_posting_anytime(self) -> None:
        scheduler = PostingWindowScheduler(enabled=False, start_hour=9, end_hour=23)
        # Any arbitrary time outside 9-23
        test_dt = datetime(2026, 9, 12, 3, 30, tzinfo=timezone.utc)
        self.assertTrue(scheduler.is_within_posting_window(test_dt))
        self.assertEqual(scheduler.seconds_until_next_window(test_dt), 0.0)
        decision = scheduler.evaluate(now=test_dt)
        self.assertTrue(decision.can_post)
        self.assertEqual(decision.wait_seconds, 0.0)

    def test_scheduler_within_standard_daytime_window(self) -> None:
        scheduler = PostingWindowScheduler(
            enabled=True, start_hour=9, end_hour=23, timezone_name="UTC"
        )
        test_dt = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)
        self.assertTrue(scheduler.is_within_posting_window(test_dt))
        self.assertEqual(scheduler.seconds_until_next_window(test_dt), 0.0)
        decision = scheduler.evaluate(now=test_dt)
        self.assertTrue(decision.can_post)

    def test_scheduler_outside_window_before_start(self) -> None:
        scheduler = PostingWindowScheduler(
            enabled=True, start_hour=9, end_hour=23, timezone_name="UTC"
        )
        # 07:00 is 2 hours (7200 seconds) before 09:00
        test_dt = datetime(2026, 9, 12, 7, 0, tzinfo=timezone.utc)
        self.assertFalse(scheduler.is_within_posting_window(test_dt))
        wait = scheduler.seconds_until_next_window(test_dt)
        self.assertAlmostEqual(wait, 7200.0, places=1)
        decision = scheduler.evaluate(now=test_dt)
        self.assertFalse(decision.can_post)
        self.assertIn("Outside posting window", decision.reason)

    def test_scheduler_outside_window_after_end(self) -> None:
        scheduler = PostingWindowScheduler(
            enabled=True, start_hour=9, end_hour=23, timezone_name="UTC"
        )
        # 23:30 is 9.5 hours (34200 seconds) before tomorrow 09:00
        test_dt = datetime(2026, 9, 12, 23, 30, tzinfo=timezone.utc)
        self.assertFalse(scheduler.is_within_posting_window(test_dt))
        wait = scheduler.seconds_until_next_window(test_dt)
        self.assertAlmostEqual(wait, 34200.0, places=1)
        decision = scheduler.evaluate(now=test_dt)
        self.assertFalse(decision.can_post)

    def test_scheduler_overnight_window_spanning_midnight(self) -> None:
        # e.g., 22:00 to 06:00
        scheduler = PostingWindowScheduler(
            enabled=True, start_hour=22, end_hour=6, timezone_name="UTC"
        )
        # 23:00 is inside
        self.assertTrue(scheduler.is_within_posting_window(datetime(2026, 9, 12, 23, 0, tzinfo=timezone.utc)))
        # 04:00 is inside
        self.assertTrue(scheduler.is_within_posting_window(datetime(2026, 9, 13, 4, 0, tzinfo=timezone.utc)))
        # 12:00 is outside
        dt_outside = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(scheduler.is_within_posting_window(dt_outside))
        wait = scheduler.seconds_until_next_window(dt_outside)
        # 12:00 until 22:00 is 10 hours = 36000s
        self.assertAlmostEqual(wait, 36000.0, places=1)

    def test_scheduler_rate_limit_calculation(self) -> None:
        scheduler = PostingWindowScheduler(
            enabled=False, min_interval_seconds=1800
        )
        now = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
        # No previous post
        self.assertEqual(scheduler.check_rate_limit(None, now), 0.0)

        # Published 10 minutes (600s) ago -> must wait 1200s
        last_pub = now - timedelta(seconds=600)
        remaining = scheduler.check_rate_limit(last_pub, now)
        self.assertAlmostEqual(remaining, 1200.0, places=1)

        # Published 35 minutes (2100s) ago -> clear
        last_pub_old = now - timedelta(seconds=2100)
        self.assertEqual(scheduler.check_rate_limit(last_pub_old, now), 0.0)

    def test_scheduler_evaluate_rate_limit_when_window_is_open(self) -> None:
        scheduler = PostingWindowScheduler(
            enabled=True,
            start_hour=9,
            end_hour=23,
            timezone_name="UTC",
            min_interval_seconds=1800,
        )
        now = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)
        last_pub = now - timedelta(seconds=300)  # 5 min ago

        decision = scheduler.evaluate(last_published_at=last_pub, now=now)
        self.assertFalse(decision.can_post)
        self.assertAlmostEqual(decision.wait_seconds, 1500.0, places=1)
        self.assertIn("Rate limit active", decision.reason)

    def test_scheduler_timezone_conversion(self) -> None:
        # Africa/Cairo is UTC+3 in September (or UTC+2 depending on DST)
        scheduler = PostingWindowScheduler(
            enabled=True,
            start_hour=9,
            end_hour=23,
            timezone_name="Africa/Cairo",
        )
        # UTC 05:00 corresponds to 08:00 in Cairo (UTC+3) -> outside window (starts at 9)
        utc_dt = datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc)
        self.assertFalse(scheduler.is_within_posting_window(utc_dt))

        # UTC 07:00 corresponds to 10:00 in Cairo -> inside window
        utc_dt_inside = datetime(2026, 9, 12, 7, 0, tzinfo=timezone.utc)
        self.assertTrue(scheduler.is_within_posting_window(utc_dt_inside))

    def test_scheduler_invalid_parameters(self) -> None:
        with self.assertRaises(ValueError):
            PostingWindowScheduler(start_hour=25)
        with self.assertRaises(ValueError):
            PostingWindowScheduler(end_hour=-1)
        with self.assertRaises(ValueError):
            PostingWindowScheduler(min_interval_seconds=-10)


# ---------------------------------------------------------------------------
# 2. PipelineStateRepository Dual-Backend & Queries Tests
# ---------------------------------------------------------------------------

class PipelineStateRepositoryTests(unittest.TestCase):
    def test_sqlite_backend_full_lifecycle_and_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "state.db"
            repo = PipelineStateRepository(database_path=db_path)
            self.assertEqual(repo.backend, "sqlite")
            self.assertEqual(repo._placeholder, "?")

            # 1. Upsert metadata
            now = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)
            repo.upsert_message_metadata(101, "channel_quran", now, 45.5)
            meta = repo.fetch_message_metadata(101)
            self.assertIsNotNone(meta)
            self.assertEqual(meta["message_id"], 101)
            self.assertEqual(meta["channel_id"], "channel_quran")
            self.assertAlmostEqual(meta["duration_seconds"], 45.5)

            # 2. Upsert stages
            repo.upsert_stage(101, "audio", "completed")
            repo.upsert_stage(101, "recognition", "completed")
            repo.upsert_stage(101, "alignment", "completed")
            repo.upsert_stage(101, "qa_gate", "approved")
            repo.upsert_stage(101, "render", "completed")
            repo.upsert_stage(101, "publish", "completed")

            audio_row = repo.fetch_stage(101, "audio")
            self.assertIsNotNone(audio_row)
            self.assertEqual(audio_row["status"], "completed")

            # 3. Fetch all stages
            all_stages = repo.fetch_all_stages(101)
            self.assertEqual(len(all_stages), 6)
            stage_names = [s["stage"] for s in all_stages]
            self.assertIn("audio", stage_names)
            self.assertIn("publish", stage_names)

            # 4. Fetch latest published timestamp
            latest_pub = repo.fetch_latest_published_timestamp()
            self.assertIsNotNone(latest_pub)
            self.assertIsInstance(latest_pub, datetime)
            self.assertIsNotNone(latest_pub.tzinfo)

            # 5. Fetch items by status
            approved = repo.fetch_items_by_status("qa_gate", "approved")
            self.assertEqual(len(approved), 1)
            self.assertEqual(approved[0]["message_id"], 101)

    def test_sql_format_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_sqlite = PipelineStateRepository(database_path=Path(temp_dir) / "test.db")
            query = "SELECT * FROM pipeline_items WHERE message_id = ? AND stage = ?"
            self.assertEqual(repo_sqlite._format_sql(query), query)

            # PostgreSQL dialect replaces ? with %s
            repo_sqlite.backend = "postgres"
            self.assertEqual(
                repo_sqlite._format_sql(query),
                "SELECT * FROM pipeline_items WHERE message_id = %s AND stage = %s",
            )

    def test_postgres_backend_missing_driver_raises_importerror(self) -> None:
        with patch.dict("sys.modules", {"psycopg": None, "psycopg2": None}):
            with self.assertRaises(ImportError) as ctx:
                PipelineStateRepository(database_url="postgresql://localhost:5432/pipeline")
            self.assertIn("neither 'psycopg' nor 'psycopg2' is installed", str(ctx.exception))

    def test_row_to_dict_helper(self) -> None:
        # None row
        self.assertIsNone(PipelineStateRepository._row_to_dict(MagicMock(), None))
        # Dict row
        d = {"a": 1, "b": 2}
        self.assertEqual(PipelineStateRepository._row_to_dict(MagicMock(), d), d)
        # Tuple row with description
        mock_cursor = MagicMock()
        mock_cursor.description = [("col1", None), ("col2", None)]
        row_tuple = ("val1", "val2")
        result = PipelineStateRepository._row_to_dict(mock_cursor, row_tuple)
        self.assertEqual(result, {"col1": "val1", "col2": "val2"})


# ---------------------------------------------------------------------------
# 3. PipelineOrchestrator End-to-End Tests
# ---------------------------------------------------------------------------

class PipelineOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temp_dir.name)
        self.state_repo = PipelineStateRepository(self.storage_root / "test_state.db")
        self.alerts = RecordingAlertService()

        # Mock stage services
        self.mock_audio = MagicMock(spec=AudioExtractionService)
        self.mock_recognition = MagicMock(spec=RecognitionService)
        self.mock_alignment = MagicMock(spec=AlignmentService)
        self.mock_qa_gate = MagicMock(spec=QAGateService)
        self.mock_render = MagicMock(spec=RenderService)
        self.mock_publish = MagicMock(spec=PublishService)
        self.scheduler = PostingWindowScheduler(enabled=False)

        self.orchestrator = PipelineOrchestrator(
            storage_root=self.storage_root,
            state_repository=self.state_repo,
            alert_service=self.alerts,
            audio_service=self.mock_audio,
            recognition_service=self.mock_recognition,
            alignment_service=self.mock_alignment,
            qa_gate_service=self.mock_qa_gate,
            render_service=self.mock_render,
            publish_service=self.mock_publish,
            scheduler=self.scheduler,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_orchestrator_happy_path_completes_all_stages(self) -> None:
        # Set up successful results for all stages
        self.mock_audio.extract = AsyncMock(
            return_value=AudioExtractionResult(
                message_id=201,
                status="completed",
                storage_path=self.storage_root / "audio" / "201.wav",
                details=AudioDetails(duration_seconds=15.0, sample_rate=16000, channels=1),
            )
        )
        self.mock_recognition.recognize = AsyncMock(
            return_value=RecognitionResult(
                message_id=201,
                status="completed",
                storage_path=self.storage_root / "match" / "201.json",
                match=VerseMatch(1, 1, 3, "بِسْمِ اللَّهِ", 0.98),
            )
        )
        self.mock_alignment.align = AsyncMock(
            return_value=AlignmentResult(
                message_id=201,
                status="completed",
                storage_path=self.storage_root / "align" / "201.json",
                coverage=1.0,
            )
        )
        self.mock_qa_gate.evaluate = AsyncMock(
            return_value=QAGateResult(
                message_id=201,
                status="approved",
                approved=True,
                match_confidence=0.98,
                alignment_coverage=1.0,
            )
        )
        self.mock_render.render = AsyncMock(
            return_value=RenderResult(
                message_id=201,
                status="completed",
                output_path=self.storage_root / "render" / "201.mp4",
                duration_seconds=15.0,
            )
        )
        self.mock_publish.publish = AsyncMock(
            return_value=PublishExecutionResult(
                message_id=201,
                status="completed",
                artifact_path=self.storage_root / "publish" / "201.json",
            )
        )

        summary = asyncio.run(self.orchestrator.run(message_id=201))

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.message_id, 201)
        self.assertIn("audio", summary.stages)
        self.assertIn("recognition", summary.stages)
        self.assertIn("alignment", summary.stages)
        self.assertIn("qa_gate", summary.stages)
        self.assertIn("render", summary.stages)
        self.assertIn("publish", summary.stages)

        # Verify call counts
        self.mock_audio.extract.assert_awaited_once()
        self.mock_recognition.recognize.assert_awaited_once()
        self.mock_alignment.align.assert_awaited_once()
        self.mock_qa_gate.evaluate.assert_awaited_once()
        self.mock_render.render.assert_awaited_once()
        self.mock_publish.publish.assert_awaited_once()

    def test_orchestrator_skips_if_already_published(self) -> None:
        # Mark publish completed in DB and create artifact
        self.state_repo.upsert_stage(301, "publish", "completed")
        pub_file = self.storage_root / "publish" / "301.json"
        pub_file.parent.mkdir(parents=True, exist_ok=True)
        pub_file.write_text("{}", encoding="utf-8")

        summary = asyncio.run(self.orchestrator.run(message_id=301))

        self.assertEqual(summary.status, "skipped_already_published")
        self.mock_audio.extract.assert_not_called()
        self.mock_render.render.assert_not_called()
        self.mock_publish.publish.assert_not_called()

    def test_orchestrator_qa_gate_rejection_halts_before_render_or_publish(self) -> None:
        self.mock_audio.extract = AsyncMock(
            return_value=AudioExtractionResult(302, "completed", None, None)
        )
        self.mock_recognition.recognize = AsyncMock(
            return_value=RecognitionResult(302, "completed", None, None)
        )
        self.mock_alignment.align = AsyncMock(
            return_value=AlignmentResult(302, "completed", None, 1.0)
        )
        # QA Gate rejects
        self.mock_qa_gate.evaluate = AsyncMock(
            return_value=QAGateResult(
                message_id=302,
                status="rejected",
                approved=False,
                match_confidence=0.75,
                alignment_coverage=0.80,
                reasons=("match_confidence 0.7500 < 0.9000",),
            )
        )

        summary = asyncio.run(self.orchestrator.run(message_id=302))

        self.assertEqual(summary.status, "held_for_review")
        self.assertIn("match_confidence 0.7500 < 0.9000", summary.error or "")

        # RENDER AND PUBLISH MUST NEVER BE CALLED WHEN QA GATE REJECTS
        self.mock_render.render.assert_not_called()
        self.mock_publish.publish.assert_not_called()

    def test_orchestrator_stage_failure_halts_immediately(self) -> None:
        # Audio extraction fails
        self.mock_audio.extract = AsyncMock(
            return_value=AudioExtractionResult(
                303, "failed", None, None, error="Corrupt media header"
            )
        )

        summary = asyncio.run(self.orchestrator.run(message_id=303))

        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.error, "Corrupt media header")

        # Downstream stages should not be called
        self.mock_recognition.recognize.assert_not_called()
        self.mock_render.render.assert_not_called()
        self.mock_publish.publish.assert_not_called()

        # Alert should be emitted
        self.assertTrue(len(self.alerts.alerts) > 0 or summary.error is not None)

    def test_orchestrator_scheduler_pauses_when_outside_window(self) -> None:
        # Enable scheduler with closed window
        closed_scheduler = PostingWindowScheduler(
            enabled=True, start_hour=9, end_hour=23, timezone_name="UTC"
        )
        # Patch current time to 03:00 UTC (outside window)
        frozen_time = datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)

        self.orchestrator.scheduler = closed_scheduler
        self.mock_audio.extract = AsyncMock(return_value=AudioExtractionResult(304, "completed", None, None))
        self.mock_recognition.recognize = AsyncMock(return_value=RecognitionResult(304, "completed", None, None))
        self.mock_alignment.align = AsyncMock(return_value=AlignmentResult(304, "completed", None, 1.0))
        self.mock_qa_gate.evaluate = AsyncMock(return_value=QAGateResult(304, "approved", True))

        with patch("pipeline.orchestration.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = frozen_time
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            summary = asyncio.run(
                self.orchestrator.run(message_id=304, enforce_scheduler=True, wait_for_window=False)
            )

            self.assertEqual(summary.status, "scheduled")
            self.assertIn("Outside posting window", summary.error or "")

            # Render and publish should NOT run yet
            self.mock_render.render.assert_not_called()
            self.mock_publish.publish.assert_not_called()

            # State store records scheduled status
            pub_stage = self.state_repo.fetch_stage(304, "publish")
            self.assertIsNotNone(pub_stage)
            self.assertEqual(pub_stage["status"], "scheduled")

    def test_orchestrator_wait_for_window_sleeps_and_proceeds(self) -> None:
        # Scheduler enabled and requires 5s wait
        closed_scheduler = PostingWindowScheduler(
            enabled=True, start_hour=9, end_hour=23, timezone_name="UTC"
        )
        self.orchestrator.scheduler = closed_scheduler

        self.mock_audio.extract = AsyncMock(return_value=AudioExtractionResult(305, "completed", None, None))
        self.mock_recognition.recognize = AsyncMock(return_value=RecognitionResult(305, "completed", None, None))
        self.mock_alignment.align = AsyncMock(return_value=AlignmentResult(305, "completed", None, 1.0))
        self.mock_qa_gate.evaluate = AsyncMock(return_value=QAGateResult(305, "approved", True))
        self.mock_render.render = AsyncMock(return_value=RenderResult(305, "completed", None, 10.0))
        self.mock_publish.publish = AsyncMock(return_value=PublishExecutionResult(305, "completed", None))

        slept_durations: list[float] = []

        async def fake_sleep(duration: float) -> None:
            slept_durations.append(duration)

        with patch.object(closed_scheduler, "evaluate", return_value=ScheduleDecision(False, 15.0, "Wait 15s")):
            summary = asyncio.run(
                self.orchestrator.run(
                    message_id=305,
                    enforce_scheduler=True,
                    wait_for_window=True,
                    sleep_fn=fake_sleep,
                )
            )

            self.assertEqual(summary.status, "completed")
            self.assertEqual(slept_durations, [15.0])
            self.mock_render.render.assert_awaited_once()
            self.mock_publish.publish.assert_awaited_once()


# ---------------------------------------------------------------------------
# 4. n8n Workflow JSON Structure & Integrity Tests
# ---------------------------------------------------------------------------

class N8nWorkflowIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow_path = Path("pipeline/orchestration/workflow.json")
        self.error_workflow_path = Path("pipeline/orchestration/error_workflow.json")

    def test_main_workflow_json_structure_and_nodes(self) -> None:
        self.assertTrue(self.workflow_path.exists(), "workflow.json must exist")
        data = json.loads(self.workflow_path.read_text(encoding="utf-8"))

        self.assertEqual(data["name"], "Quran Video Pipeline - Orchestration")
        nodes = {node["name"]: node for node in data["nodes"]}

        # Required nodes
        expected_nodes = [
            "Telegram Video Trigger",
            "Ingest Video Message",
            "Extract Audio WAV",
            "Recognize Quran Verses",
            "CTC Forced Alignment",
            "QA Gate Verification",
            "Check QA Gate Approved",
            "Posting Window & Rate Limit Check",
            "Render 1080x1920 Video",
            "Multi-Platform Publish",
            "Alert QA Review Queue",
        ]
        for expected in expected_nodes:
            self.assertIn(expected, nodes, f"Missing required node: {expected}")

        # Connections check
        connections = data["connections"]
        self.assertEqual(
            connections["Telegram Video Trigger"]["main"][0][0]["node"],
            "Ingest Video Message",
        )
        self.assertEqual(
            connections["QA Gate Verification"]["main"][0][0]["node"],
            "Check QA Gate Approved",
        )

        # IF node branching: output 0 -> posting window check, output 1 -> review alert
        if_outputs = connections["Check QA Gate Approved"]["main"]
        self.assertEqual(len(if_outputs), 2)
        self.assertEqual(if_outputs[0][0]["node"], "Posting Window & Rate Limit Check")
        self.assertEqual(if_outputs[1][0]["node"], "Alert QA Review Queue")

        # Verify errorWorkflow linkage
        self.assertEqual(
            data["settings"]["errorWorkflow"], "Quran Pipeline Error Handler"
        )

    def test_error_workflow_json_structure_and_nodes(self) -> None:
        self.assertTrue(self.error_workflow_path.exists(), "error_workflow.json must exist")
        data = json.loads(self.error_workflow_path.read_text(encoding="utf-8"))

        self.assertEqual(data["name"], "Quran Pipeline Error Handler")
        nodes = {node["name"]: node for node in data["nodes"]}

        expected_nodes = [
            "Error Trigger",
            "Format Error Context",
            "Update State Store Failed",
            "Send Pipeline Failure Alert",
        ]
        for expected in expected_nodes:
            self.assertIn(expected, nodes, f"Missing required error node: {expected}")

        connections = data["connections"]
        self.assertEqual(
            connections["Error Trigger"]["main"][0][0]["node"],
            "Format Error Context",
        )
        self.assertEqual(
            connections["Format Error Context"]["main"][0][0]["node"],
            "Update State Store Failed",
        )
        self.assertEqual(
            connections["Update State Store Failed"]["main"][0][0]["node"],
            "Send Pipeline Failure Alert",
        )


# ---------------------------------------------------------------------------
# 5. CLI Arguments & Orchestration Settings Tests
# ---------------------------------------------------------------------------

class OrchestrationCLITests(unittest.TestCase):
    def test_cli_argument_parsing(self) -> None:
        args = parse_args(["505", "--draft", "--skip-scheduler", "--wait-for-window", "--json"])
        self.assertEqual(args.message_id, 505)
        self.assertTrue(args.draft)
        self.assertTrue(args.skip_scheduler)
        self.assertTrue(args.wait_for_window)
        self.assertTrue(args.json)

    def test_orchestration_settings_from_env(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "POSTING_WINDOW_ENABLED": "true",
                "POSTING_WINDOW_START_HOUR": "10",
                "POSTING_WINDOW_END_HOUR": "22",
                "POSTING_WINDOW_TIMEZONE": "Africa/Cairo",
                "RATE_LIMIT_MIN_INTERVAL_SECONDS": "3600",
            },
        ):
            settings = OrchestrationSettings.from_env()
            self.assertTrue(settings.posting_window_enabled)
            self.assertEqual(settings.posting_window_start_hour, 10)
            self.assertEqual(settings.posting_window_end_hour, 22)
            self.assertEqual(settings.posting_window_timezone, "Africa/Cairo")
            self.assertEqual(settings.rate_limit_min_interval_seconds, 3600)

    def test_cli_main_exit_code_mapping(self) -> None:
        with patch("pipeline.orchestration.app.build_orchestrator") as mock_build:
            mock_orch = MagicMock()
            mock_build.return_value = (mock_orch, MagicMock())

            # Completed -> 0
            mock_orch.run = AsyncMock(
                return_value=PipelineExecutionSummary(601, "completed")
            )
            code = asyncio.run(cli_main(["601"]))
            self.assertEqual(code, 0)

            # Held for review -> 2
            mock_orch.run = AsyncMock(
                return_value=PipelineExecutionSummary(602, "held_for_review")
            )
            code = asyncio.run(cli_main(["602"]))
            self.assertEqual(code, 2)

            # Failed -> 1
            mock_orch.run = AsyncMock(
                return_value=PipelineExecutionSummary(603, "failed", error="err")
            )
            code = asyncio.run(cli_main(["603"]))
            self.assertEqual(code, 1)


# ---------------------------------------------------------------------------
# 6. End-to-End Pipeline Dry-Run Integration Test (Real Video -> Draft Publish)
# ---------------------------------------------------------------------------

class PipelineDryRunIntegrationTests(unittest.TestCase):
    """End-to-end integration test with live media tools and publish in draft mode."""

    def test_end_to_end_dry_run_with_draft_publishing_and_idempotency(self) -> None:
        import subprocess

        # Verify ffmpeg is accessible
        try:
            subprocess.run(
                ["ffmpeg", "-version"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.SubprocessError, FileNotFoundError):
            self.skipTest("ffmpeg binary is not available in system PATH")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            message_id = 7001

            # 1. Synthesize a 2-second real test video with AAC audio
            raw_dir = root / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            source_mp4 = raw_dir / f"{message_id}.mp4"

            cmd = [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=navy:s=720x1280:r=25:d=2.0",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=16000:duration=2.0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                str(source_mp4),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertTrue(source_mp4.is_file())

            # 2. Setup state repository and alerts
            state_repo = PipelineStateRepository(root / "pipeline.db")
            now = datetime(2026, 9, 12, 11, 0, tzinfo=timezone.utc)
            state_repo.upsert_message_metadata(message_id, "channel_quran", now, 2.0)
            alerts = RecordingAlertService()

            # 3. Setup real audio extractor
            from pipeline.audio.ffmpeg import FFmpegAudioExtractor
            audio_extractor = FFmpegAudioExtractor()
            audio_service = AudioExtractionService(
                storage_root=root,
                state_repository=state_repo,
                alert_service=alerts,
                extractor=audio_extractor,
            )

            # 4. Setup recognition service with mocked Quran matcher output
            mock_transcriber = MagicMock()
            mock_transcriber.transcribe.return_value = "بسم الله الرحمن الرحيم"
            mock_matcher = MagicMock()
            mock_matcher.match.return_value = VerseMatch(
                surah=1,
                ayah_start=1,
                ayah_end=1,
                canonical_text="بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ",
                confidence=0.99,
            )
            recognition_service = RecognitionService(
                storage_root=root,
                state_repository=state_repo,
                alert_service=alerts,
                transcriber=mock_transcriber,
                matcher=mock_matcher,
            )

            # 5. Setup alignment service with mocked word timestamps
            mock_aligner = MagicMock()
            mock_aligner.align.return_value = [
                AlignedWord("بِسْمِ", 0.1, 0.5),
                AlignedWord("اللَّهِ", 0.5, 0.9),
                AlignedWord("الرَّحْمَٰنِ", 0.9, 1.4),
                AlignedWord("الرَّحِيمِ", 1.4, 1.9),
            ]
            alignment_service = AlignmentService(
                storage_root=root,
                state_repository=state_repo,
                alert_service=alerts,
                aligner=mock_aligner,
            )

            # 6. Setup QA gate service
            qa_gate_service = QAGateService(
                storage_root=root,
                state_repository=state_repo,
                alert_service=alerts,
                match_confidence_threshold=0.90,
                alignment_coverage_threshold=0.95,
            )

            # 7. Setup scheduler (immediate posting)
            scheduler = PostingWindowScheduler(enabled=False)

            # 8. Setup real render service
            from pipeline.render.background import BackgroundAssetPool
            from pipeline.render.ffmpeg import FFmpegRenderer
            from pipeline.render.subtitles import KaraokeSubtitleGenerator

            bg_pool = BackgroundAssetPool(Path("pipeline/assets/backgrounds"), strategy="round_robin")
            renderer = FFmpegRenderer()
            sub_gen = KaraokeSubtitleGenerator(font_path=Path("pipeline/assets/fonts/arabic-display.ttf"))
            render_service = RenderService(
                storage_root=root,
                state_repository=state_repo,
                alert_service=alerts,
                background_pool=bg_pool,
                subtitle_generator=sub_gen,
                renderer=renderer,
                branding_logo_path=Path("pipeline/assets/branding/logo.png"),
                branding_handle="@QuranRepurposed",
                branding_font_path=Path("pipeline/assets/fonts/arabic-display.ttf"),
            )

            # 9. Setup publish service with StubPublishClient in draft mode
            from pipeline.publish.client import StubPublishClient
            from pipeline.publish.templating import CaptionTemplater
            from pipeline.publish.uploader import StubMediaUploader

            pub_client = StubPublishClient()
            pub_service = PublishService(
                storage_root=root,
                state_repository=state_repo,
                alert_service=alerts,
                client=pub_client,
                media_uploader=StubMediaUploader(),
                templater=CaptionTemplater(),
                default_platforms=("tiktok", "instagram", "youtube"),
                default_draft_mode=True,
            )


            # 10. Instantiate orchestrator
            orchestrator = PipelineOrchestrator(
                storage_root=root,
                state_repository=state_repo,
                alert_service=alerts,
                audio_service=audio_service,
                recognition_service=recognition_service,
                alignment_service=alignment_service,
                qa_gate_service=qa_gate_service,
                render_service=render_service,
                publish_service=pub_service,
                scheduler=scheduler,
            )

            # Execute run 1: Complete pipeline through draft publish
            summary = asyncio.run(orchestrator.run(message_id=message_id, draft=True))

            self.assertEqual(summary.status, "completed")
            self.assertEqual(summary.message_id, message_id)

            # Verify render output
            rendered_mp4 = root / "render" / f"{message_id}.mp4"
            self.assertTrue(rendered_mp4.is_file())
            probe = renderer.probe_media(rendered_mp4)
            self.assertEqual(probe.width, 1080)
            self.assertEqual(probe.height, 1920)
            self.assertTrue(probe.has_audio)

            # Verify publish output artifact
            pub_artifact = root / "publish" / f"{message_id}.json"
            self.assertTrue(pub_artifact.is_file())
            pub_data = json.loads(pub_artifact.read_text(encoding="utf-8"))
            self.assertTrue(pub_data["draft_mode"])
            self.assertEqual(pub_data["overall_status"], "completed")
            self.assertEqual(len(pub_data["platforms"]), 3)


            # Verify all stage statuses in state store
            self.assertEqual(state_repo.fetch_stage(message_id, "audio")["status"], "completed")
            self.assertEqual(state_repo.fetch_stage(message_id, "recognition")["status"], "completed")
            self.assertEqual(state_repo.fetch_stage(message_id, "alignment")["status"], "completed")
            self.assertEqual(state_repo.fetch_stage(message_id, "qa_gate")["status"], "approved")
            self.assertEqual(state_repo.fetch_stage(message_id, "render")["status"], "completed")
            self.assertEqual(state_repo.fetch_stage(message_id, "publish")["status"], "completed")

            # Execute run 2: Idempotency check (must skip already published video)
            second_run = asyncio.run(orchestrator.run(message_id=message_id, draft=True))
            self.assertEqual(second_run.status, "skipped_already_published")


if __name__ == "__main__":
    unittest.main()

