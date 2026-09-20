"""Tests for monitoring service, health checks, reporting, chat bot, and CLI."""

from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pipeline.alerts import AlertService
from pipeline.config import MonitoringSettings
from pipeline.monitoring.app import build_monitor, cli_main, parse_args
from pipeline.monitoring.bot import MonitoringBotHandler
from pipeline.monitoring.service import HealthCheckResult, MonitoringReport, PipelineMonitor
from pipeline.state.repository import PipelineStateRepository


class MonitoringRepositoryTests(unittest.TestCase):
    """Test state repository aggregation and metric queries."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_state.db"
        self.repo = PipelineStateRepository(database_path=self.db_path)

    def tearDown(self) -> None:
        self.repo.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_get_pipeline_summary_empty(self) -> None:
        summary = self.repo.get_pipeline_summary()
        self.assertEqual(summary["total_messages"], 0)
        self.assertEqual(summary["review_queue_count"], 0)
        self.assertEqual(summary["active_processing_count"], 0)
        self.assertIsNone(summary["latest_published_at"])
        self.assertEqual(summary["stages"], {})

    def test_get_pipeline_summary_with_stages(self) -> None:
        # Message 1: completed through publish
        self.repo.upsert_stage(1, "ingestion", "completed")
        self.repo.upsert_stage(1, "audio", "completed")
        self.repo.upsert_stage(1, "recognition", "completed")
        self.repo.upsert_stage(1, "alignment", "completed")
        self.repo.upsert_stage(1, "qa_gate", "completed")
        self.repo.upsert_stage(1, "render", "completed")
        self.repo.upsert_stage(1, "publish", "completed")
        self.repo.upsert_stage(1, "orchestration", "completed")

        # Message 2: held for review in qa_gate
        self.repo.upsert_stage(2, "ingestion", "completed")
        self.repo.upsert_stage(2, "audio", "completed")
        self.repo.upsert_stage(2, "qa_gate", "held_for_review", error="Confidence 0.40")

        # Message 3: active processing
        self.repo.upsert_stage(3, "ingestion", "completed")
        self.repo.upsert_stage(3, "audio", "processing")

        summary = self.repo.get_pipeline_summary()
        self.assertEqual(summary["total_messages"], 3)
        self.assertEqual(summary["review_queue_count"], 1)
        self.assertEqual(summary["active_processing_count"], 1)
        self.assertIsNotNone(summary["latest_published_at"])
        self.assertEqual(summary["stages"]["publish"]["completed"], 1)
        self.assertEqual(summary["stages"]["qa_gate"]["held_for_review"], 1)
        self.assertEqual(summary["stages"]["audio"]["processing"], 1)

    def test_get_pipeline_summary_window_hours_filtering(self) -> None:
        self.repo.upsert_stage(10, "publish", "completed")
        # Backdate the updated_at to 48 hours ago
        old_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        with closing(self.repo._connect()) as conn:
            conn.execute(
                "UPDATE pipeline_items SET updated_at = ? WHERE message_id = 10",
                (old_time,),
            )
            conn.commit()

        # Inside 24h window, message 10 should be filtered out
        summary_24h = self.repo.get_pipeline_summary(window_hours=24)
        self.assertEqual(summary_24h["total_messages"], 0)

        # Inside 72h window, message 10 should be included
        summary_72h = self.repo.get_pipeline_summary(window_hours=72)
        self.assertEqual(summary_72h["total_messages"], 1)

    def test_fetch_recent_failures_and_consecutive_counts(self) -> None:
        self.repo.upsert_stage(101, "audio", "failed", error="corrupt audio")
        self.repo.upsert_stage(101, "orchestration", "failed", error="corrupt audio")

        self.repo.upsert_stage(102, "recognition", "failed", error="no surah match")
        self.repo.upsert_stage(102, "orchestration", "failed", error="no surah match")

        failures = self.repo.fetch_recent_failures(limit=5)
        self.assertGreaterEqual(len(failures), 2)
        errors = [f["error"] for f in failures]
        self.assertIn("corrupt audio", errors)
        self.assertIn("no surah match", errors)

        streak = self.repo.count_consecutive_failures()
        self.assertEqual(streak, 2)

        # Succeeded run breaks the failure streak
        self.repo.upsert_stage(103, "publish", "completed")
        self.repo.upsert_stage(103, "orchestration", "completed")
        streak_after = self.repo.count_consecutive_failures()
        self.assertEqual(streak_after, 0)

    def test_fetch_review_queue_count_and_items(self) -> None:
        self.assertEqual(self.repo.fetch_review_queue_count(), 0)
        self.assertEqual(self.repo.fetch_review_queue_items(), [])

        self.repo.upsert_stage(201, "qa_gate", "held_for_review", error="Low alignment 0.65")
        self.repo.upsert_stage(202, "qa_gate", "held_for_review", error="Low confidence 0.70")
        self.repo.upsert_stage(203, "qa_gate", "completed")

        self.assertEqual(self.repo.fetch_review_queue_count(), 2)
        items = self.repo.fetch_review_queue_items(limit=10)
        self.assertEqual(len(items), 2)
        mids = {it["message_id"] for it in items}
        self.assertEqual(mids, {201, 202})


class PipelineMonitorTests(unittest.IsolatedAsyncioTestCase):
    """Test PipelineMonitor reporting, health evaluation, and alert dispatches."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "monitor_state.db"
        self.repo = PipelineStateRepository(database_path=self.db_path)
        self.alert_service = AsyncMock(spec=AlertService)
        self.settings = MonitoringSettings(
            state_db_path=self.db_path,
            failure_threshold=3,
            backlog_threshold=5,
            window_hours=24,
            alert_cooldown_seconds=60,
        )
        self.monitor = PipelineMonitor(
            state_repository=self.repo,
            alert_service=self.alert_service,
            settings=self.settings,
        )

    def tearDown(self) -> None:
        self.repo.close()
        self.temp_dir.cleanup()

    def test_generate_and_format_text_report(self) -> None:
        self.repo.upsert_stage(1, "publish", "completed")
        self.repo.upsert_stage(2, "qa_gate", "held_for_review", error="Low match confidence")
        self.repo.upsert_stage(3, "recognition", "failed", error="ASR timeout occurred")

        report = self.monitor.generate_report(window_hours=24)
        self.assertEqual(report.total_messages, 3)
        self.assertEqual(report.review_queue_count, 1)

        text = self.monitor.format_text_report(report)
        self.assertIn("*Quran Pipeline Status Report* (Past 24h)", text)
        self.assertIn("Total Messages Processed*: 3", text)
        self.assertIn("QA Review Backlog*: 1 held", text)
        self.assertIn("Publish", text)
        self.assertIn("ASR timeout occurred", text)

    def test_format_json_report(self) -> None:
        report = self.monitor.generate_report(window_hours=12)
        data = self.monitor.format_json_report(report)
        self.assertIsInstance(data, dict)
        self.assertEqual(data["window_hours"], 12)
        self.assertIn("stages", data)
        self.assertIn("generated_at", data)
        # Ensure json.dumps serializes cleanly
        dumped = json.dumps(data)
        self.assertIn('"window_hours": 12', dumped)

    def test_check_health_healthy(self) -> None:
        health = self.monitor.check_health()
        self.assertTrue(health.is_healthy)
        self.assertFalse(health.failure_threshold_breached)
        self.assertFalse(health.backlog_threshold_breached)
        self.assertEqual(len(health.reasons), 0)

    def test_check_health_repeated_failures_breach(self) -> None:
        for mid in (1, 2, 3):
            self.repo.upsert_stage(mid, "orchestration", "failed", error="Transcode error")

        health = self.monitor.check_health()
        self.assertFalse(health.is_healthy)
        self.assertTrue(health.failure_threshold_breached)
        self.assertFalse(health.backlog_threshold_breached)
        self.assertTrue(any("Repeated failures detected" in r for r in health.reasons))

    def test_check_health_backlog_breach(self) -> None:
        # Backlog threshold is 5; populate 6 items
        for mid in range(10, 16):
            self.repo.upsert_stage(mid, "qa_gate", "held_for_review", error="Pending review")

        health = self.monitor.check_health()
        self.assertFalse(health.is_healthy)
        self.assertFalse(health.failure_threshold_breached)
        self.assertTrue(health.backlog_threshold_breached)
        self.assertTrue(any("QA review backlog growing" in r for r in health.reasons))

    async def test_dispatch_health_alerts_and_cooldown(self) -> None:
        for mid in (1, 2, 3):
            self.repo.upsert_stage(mid, "orchestration", "failed", error="Transcode error")

        # First alert dispatch
        alerts = await self.monitor.dispatch_health_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertIn("Repeated Failures", alerts[0])
        self.assertEqual(self.alert_service.send_monitoring_alert.call_count, 1)

        # Immediate second check should be suppressed by alert cooldown
        alerts_suppressed = await self.monitor.dispatch_health_alerts()
        self.assertEqual(len(alerts_suppressed), 0)
        self.assertEqual(self.alert_service.send_monitoring_alert.call_count, 1)

    async def test_send_status_report(self) -> None:
        self.repo.upsert_stage(1, "publish", "completed")
        text = await self.monitor.send_status_report(window_hours=24)
        self.assertIn("Quran Pipeline Status Report", text)
        self.assertEqual(self.alert_service.send_status_report.call_count, 1)


class MonitoringBotHandlerTests(unittest.TestCase):
    """Test chat-bot command interpretation and formatting."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "bot_state.db"
        self.repo = PipelineStateRepository(database_path=self.db_path)
        self.settings = MonitoringSettings(
            state_db_path=self.db_path,
            failure_threshold=3,
            backlog_threshold=5,
        )
        self.monitor = PipelineMonitor(state_repository=self.repo, settings=self.settings)
        self.bot = MonitoringBotHandler(monitor=self.monitor)

    def tearDown(self) -> None:
        self.repo.close()
        self.temp_dir.cleanup()

    def test_bot_status_command(self) -> None:
        self.repo.upsert_stage(1, "publish", "completed")
        reply = self.bot.handle_command("/status 48")
        self.assertIn("*Quran Pipeline Status Report* (Past 48h)", reply)
        self.assertIn("Publish", reply)

    def test_bot_status_mention_stripping(self) -> None:
        reply = self.bot.handle_command("/status@QuranBot 12")
        self.assertIn("*Quran Pipeline Status Report* (Past 12h)", reply)

    def test_bot_health_command_healthy_and_unhealthy(self) -> None:
        reply_healthy = self.bot.handle_command("/health")
        self.assertIn("HEALTHY", reply_healthy)

        for mid in (1, 2, 3):
            self.repo.upsert_stage(mid, "orchestration", "failed", error="Render crash")

        reply_unhealthy = self.bot.handle_command("/health")
        self.assertIn("ATTENTION REQUIRED", reply_unhealthy)
        self.assertIn("Repeated failures detected", reply_unhealthy)

    def test_bot_queue_command_empty_and_populated(self) -> None:
        reply_empty = self.bot.handle_command("/queue")
        self.assertIn("QA Review Queue is Empty", reply_empty)

        self.repo.upsert_stage(501, "qa_gate", "held_for_review", error="Low alignment confidence")
        reply_queue = self.bot.handle_command("/queue")
        self.assertIn("QA Gate Review Queue (1 items)", reply_queue)
        self.assertIn("Msg #501", reply_queue)
        self.assertIn("/retry <message_id>", reply_queue)

    def test_bot_retry_command(self) -> None:
        # Missing argument
        reply_no_arg = self.bot.handle_command("/retry")
        self.assertIn("Usage*: `/retry <message_id>`", reply_no_arg)

        # Non-integer argument
        reply_invalid = self.bot.handle_command("/retry abc")
        self.assertIn("Invalid message ID", reply_invalid)

        # Valid argument
        self.repo.upsert_stage(501, "qa_gate", "held_for_review", error="Held")
        reply_valid = self.bot.handle_command("/retry 501")
        self.assertIn("Reprocess Instructions for Message #501", reply_valid)
        self.assertIn("python -m pipeline.orchestration.app 501 --force", reply_valid)

    def test_bot_help_and_unknown_command(self) -> None:
        reply_help = self.bot.handle_command("/help")
        self.assertIn("Quran Pipeline Admin Bot Commands", reply_help)
        self.assertIn("/status", reply_help)
        self.assertIn("/health", reply_help)
        self.assertIn("/queue", reply_help)
        self.assertIn("/retry", reply_help)

        reply_unknown = self.bot.handle_command("/unknown_cmd")
        self.assertIn("Unknown command `/unknown_cmd`", reply_unknown)


class MonitoringCLITests(unittest.IsolatedAsyncioTestCase):
    """Test CLI commands and flags for pipeline.monitoring.app."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "cli_state.db"
        self.repo = PipelineStateRepository(database_path=self.db_path)
        self.repo.upsert_stage(1, "publish", "completed")

    def tearDown(self) -> None:
        self.repo.close()
        self.temp_dir.cleanup()

    def test_parse_args(self) -> None:
        args_report = parse_args(["report", "--window-hours", "6", "--json", "--send-alert"])
        self.assertEqual(args_report.command, "report")
        self.assertEqual(args_report.window_hours, 6)
        self.assertTrue(args_report.json)
        self.assertTrue(args_report.send_alert)

        args_check = parse_args(["check", "--failure-threshold", "2", "--backlog-threshold", "4"])
        self.assertEqual(args_check.command, "check")
        self.assertEqual(args_check.failure_threshold, 2)
        self.assertEqual(args_check.backlog_threshold, 4)

    async def test_cli_report_text_and_json(self) -> None:
        with patch.dict("os.environ", {"STATE_DB_PATH": str(self.db_path)}):
            # Text output
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                code = await cli_main(["report", "--window-hours", "24"])
            self.assertEqual(code, 0)
            output = stdout_buf.getvalue()
            self.assertIn("Quran Pipeline Status Report", output)

            # JSON output
            json_buf = io.StringIO()
            with patch("sys.stdout", json_buf):
                code_json = await cli_main(["report", "--json"])
            self.assertEqual(code_json, 0)
            data = json.loads(json_buf.getvalue())
            self.assertIn("stages", data)
            self.assertEqual(data["total_messages"], 1)

    async def test_cli_check_healthy_and_breach_exit_codes(self) -> None:
        with patch.dict("os.environ", {"STATE_DB_PATH": str(self.db_path)}):
            # Healthy
            code_healthy = await cli_main(["check"])
            self.assertEqual(code_healthy, 0)

            # Force failure breach threshold: 1 failure
            self.repo.upsert_stage(999, "orchestration", "failed", error="Fatal error")
            code_breach = await cli_main(["check", "--failure-threshold", "1"])
            self.assertEqual(code_breach, 2)


class DatabasePrecedenceTests(unittest.TestCase):
    """Test database selection and precedence between STATE_DB_PATH and STATE_DATABASE_URL."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "precedence.db"

    def tearDown(self) -> None:
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_state_db_path_overrides_state_database_url_in_settings(self) -> None:
        """When STATE_DB_PATH is explicitly set, STATE_DATABASE_URL is ignored."""
        env = {
            "STATE_DB_PATH": str(self.db_path),
            "STATE_DATABASE_URL": "postgresql://pipeline:pipeline@localhost:5432/pipeline",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = MonitoringSettings.from_env()
            self.assertEqual(settings.state_db_path, self.db_path.resolve())
            self.assertIsNone(settings.state_database_url)

    def test_state_database_url_preserved_when_state_db_path_unset(self) -> None:
        """When STATE_DB_PATH is not set, STATE_DATABASE_URL is preserved."""
        env = {
            "STATE_DATABASE_URL": "postgresql://pipeline:pipeline@localhost:5432/pipeline",
        }
        with patch.dict("os.environ", env, clear=True):
            settings = MonitoringSettings.from_env()
            self.assertEqual(
                settings.state_database_url,
                "postgresql://pipeline:pipeline@localhost:5432/pipeline",
            )

    def test_build_monitor_prefers_explicit_state_db_path(self) -> None:
        """build_monitor uses SQLite repo at STATE_DB_PATH even if STATE_DATABASE_URL is in env."""
        env = {
            "STATE_DB_PATH": str(self.db_path),
            "STATE_DATABASE_URL": "postgresql://pipeline:pipeline@localhost:5432/pipeline",
        }
        with patch.dict("os.environ", env, clear=True):
            monitor = build_monitor()
            try:
                self.assertEqual(monitor.state_repo.backend, "sqlite")
                self.assertEqual(monitor.state_repo.database_path, self.db_path.resolve())
            finally:
                monitor.state_repo.close()

    def test_cli_main_with_both_env_vars_set(self) -> None:
        """cli_main reads SQLite when both STATE_DB_PATH and STATE_DATABASE_URL are in env."""
        repo = PipelineStateRepository(database_path=self.db_path)
        try:
            repo.upsert_stage(1, "publish", "completed")
            env = {
                "STATE_DB_PATH": str(self.db_path),
                "STATE_DATABASE_URL": "postgresql://pipeline:pipeline@localhost:5432/pipeline",
            }
            with patch.dict("os.environ", env, clear=True):
                # Verify report runs without attempting Postgres connection
                json_buf = io.StringIO()
                with patch("sys.stdout", json_buf):
                    code = asyncio.run(cli_main(["report", "--json"]))
                self.assertEqual(code, 0)
                data = json.loads(json_buf.getvalue())
                self.assertEqual(data["total_messages"], 1)

                # Verify check runs cleanly without attempting Postgres connection
                code_check = asyncio.run(cli_main(["check"]))
                self.assertEqual(code_check, 0)

                # Verify breach detection with threshold 1 after adding failure
                repo.upsert_stage(999, "orchestration", "failed", error="Failure in test")
                code_breach = asyncio.run(cli_main(["check", "--failure-threshold", "1"]))
                self.assertEqual(code_breach, 2)
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
