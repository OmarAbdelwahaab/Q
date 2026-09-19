"""Comprehensive tests for Phase 8 Orchestration, Scheduler, State Dual-Tier, and n8n Workflows."""

from __future__ import annotations

import asyncio
import concurrent.futures
from contextlib import closing
import json
import os
import tempfile
import threading
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

    def test_claim_execution_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = PipelineStateRepository(database_path=Path(temp_dir) / "state.db")

            # 1. First claim succeeds
            claimed, reason = repo.claim_execution(101)
            self.assertTrue(claimed)
            self.assertEqual(reason, "claimed")
            row = repo.fetch_stage(101, "orchestration")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "processing")

            # 2. Second claim while still processing fails without force
            claimed2, reason2 = repo.claim_execution(101, force=False)
            self.assertFalse(claimed2)
            self.assertEqual(reason2, "active_execution")

            # 3. Second claim with force=True alone on fresh active claim is refused
            claimed3, reason3 = repo.claim_execution(101, force=True)
            self.assertFalse(claimed3)
            self.assertEqual(reason3, "active_execution_recent")

            # 4. Second claim with force=True and force_active=True succeeds
            claimed4, reason4 = repo.claim_execution(101, force=True, force_active=True)
            self.assertTrue(claimed4)
            self.assertEqual(reason4, "claimed")

            # 4. If publish stage is completed, claim fails with already_completed
            repo.upsert_stage(101, "publish", "completed")
            claimed4, reason4 = repo.claim_execution(101, force=False)
            self.assertFalse(claimed4)
            self.assertEqual(reason4, "already_completed")

            # 6. If orchestration stage itself was completed, claim fails
            repo.upsert_stage(102, "orchestration", "completed")
            claimed5, reason5 = repo.claim_execution(102, force=False)
            self.assertFalse(claimed5)
            self.assertEqual(reason5, "already_completed")

    def test_claim_execution_force_semantics(self) -> None:
        """Verify Task 2 requirements:
        (1) --force still overrides a stale 'processing' claim.
        (2) --force alone does NOT override a fresh 'processing' claim.
        (3) --force combined with force_active overrides a fresh 'processing' claim.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = PipelineStateRepository(database_path=Path(temp_dir) / "state.db")

            # Create fresh processing claim
            repo.claim_execution(501)

            # (2) --force alone does NOT override fresh processing claim
            claimed_fresh, reason_fresh = repo.claim_execution(501, force=True, force_active=False)
            self.assertFalse(claimed_fresh)
            self.assertEqual(reason_fresh, "active_execution_recent")

            # (3) --force combined with force_active DOES override fresh processing claim
            claimed_active, reason_active = repo.claim_execution(501, force=True, force_active=True)
            self.assertTrue(claimed_active)
            self.assertEqual(reason_active, "claimed")

            # Manually backdate updated_at past 300s (e.g. 400 seconds ago)
            stale_time = (datetime.now(timezone.utc) - timedelta(seconds=400)).strftime("%Y-%m-%d %H:%M:%S")
            with closing(repo._connect()) as conn:
                conn.execute(
                    "UPDATE pipeline_items SET updated_at = ? WHERE message_id = 501 AND stage = 'orchestration'",
                    (stale_time,),
                )
                conn.commit()

            # (1) --force alone DOES override a stale 'processing' claim
            claimed_stale, reason_stale = repo.claim_execution(501, force=True, force_active=False)
            self.assertTrue(claimed_stale)
            self.assertEqual(reason_stale, "claimed")

    def test_reserve_publish_slot_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = PipelineStateRepository(database_path=Path(temp_dir) / "state.db")

            # 1. First reservation succeeds
            reserved, wait_s, reason = repo.reserve_publish_slot(201, min_interval_seconds=1800)
            self.assertTrue(reserved)
            self.assertEqual(wait_s, 0.0)
            self.assertEqual(reason, "reserved")
            row = repo.fetch_stage(201, "publish")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "processing")

            # 2. Another message tries to reserve within the 1800s window -> blocked
            reserved2, wait_s2, reason2 = repo.reserve_publish_slot(202, min_interval_seconds=1800)
            self.assertFalse(reserved2)
            self.assertGreater(wait_s2, 0.0)
            self.assertIn("Rate limit active", reason2)

            # 3. Message 201 completes publication
            repo.upsert_stage(201, "publish", "completed")

            # 4. Re-reserving for already completed message 201 fails
            reserved_self, wait_self, reason_self = repo.reserve_publish_slot(201, min_interval_seconds=1800)
            self.assertFalse(reserved_self)
            self.assertEqual(reason_self, "already_published")

            # 5. With min_interval_seconds=0, reservation passes
            reserved_zero, wait_zero, reason_zero = repo.reserve_publish_slot(203, min_interval_seconds=0)
            self.assertTrue(reserved_zero)
            self.assertEqual(wait_zero, 0.0)

    def test_reserve_publish_slot_cleans_up_stale_processing_reservation(self) -> None:
        """Verify Task 3: when reservation exceeds timeout, superseding reservation marks original as failed."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = PipelineStateRepository(database_path=Path(temp_dir) / "state.db")

            # 1. Message 301 reserves publish slot
            reserved1, wait1, reason1 = repo.reserve_publish_slot(
                301, min_interval_seconds=1800, reservation_timeout_seconds=300
            )
            self.assertTrue(reserved1)
            row_301 = repo.fetch_stage(301, "publish")
            self.assertIsNotNone(row_301)
            self.assertEqual(row_301["status"], "processing")

            # 2. Backdate message 301's updated_at past reservation_timeout_seconds (e.g. 400s ago)
            stale_time = (datetime.now(timezone.utc) - timedelta(seconds=400)).strftime("%Y-%m-%d %H:%M:%S")
            with closing(repo._connect()) as conn:
                conn.execute(
                    "UPDATE pipeline_items SET updated_at = ? WHERE message_id = 301 AND stage = 'publish'",
                    (stale_time,),
                )
                conn.commit()

            # 3. Message 302 triggers a new reservation
            reserved2, wait2, reason2 = repo.reserve_publish_slot(
                302, min_interval_seconds=1800, reservation_timeout_seconds=300
            )
            self.assertTrue(reserved2)
            self.assertEqual(reason2, "reserved")

            # 4. Assert original message 301's status is now 'failed', not orphaned 'processing'
            row_301_after = repo.fetch_stage(301, "publish")
            self.assertIsNotNone(row_301_after)
            self.assertEqual(row_301_after["status"], "failed")
            self.assertIn("presumed crashed worker", row_301_after["error"] or "")

            # 5. Message 302 is successfully 'processing'
            row_302 = repo.fetch_stage(302, "publish")
            self.assertIsNotNone(row_302)
            self.assertEqual(row_302["status"], "processing")

    def test_connection_pool_and_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = PipelineStateRepository(database_path=Path(temp_dir) / "state.db")
            # Close on SQLite repo is a clean no-op
            repo.close()
            self.assertIsNone(repo._pool)

        # When pool is present, close() calls pool.close()
        repo_pg = PipelineStateRepository.__new__(PipelineStateRepository)
        repo_pg.backend = "postgres"
        mock_pool = MagicMock()
        repo_pg._pool = mock_pool
        repo_pg.close()
        mock_pool.close.assert_called_once()
        self.assertIsNone(repo_pg._pool)

    def test_pooled_connection_wrapper_lifecycle_and_delegation(self) -> None:
        """Verify _PooledConnectionWrapper delegates attributes and reclaims connections on close."""
        from pipeline.state.repository import _PooledConnectionWrapper

        # 1. Pool with getconn / putconn
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur

        wrapper = _PooledConnectionWrapper(mock_pool, mock_conn)
        # Attribute delegation
        cur = wrapper.cursor()
        self.assertEqual(cur, mock_cur)
        wrapper.commit()
        mock_conn.commit.assert_called_once()

        # Execute delegation
        wrapper.execute("SELECT 1")
        mock_conn.execute.assert_called_once_with("SELECT 1")

        # Context manager with 'closing'
        with closing(wrapper):
            pass
        mock_pool.putconn.assert_called_once_with(mock_conn)

        # Idempotent close
        wrapper.close()
        self.assertEqual(mock_pool.putconn.call_count, 1)

    def test_pooled_connection_wrapper_with_context_manager_pool(self) -> None:
        """Verify _PooledConnectionWrapper properly exits context managers for connection() pools."""
        from pipeline.state.repository import _PooledConnectionWrapper

        mock_pool = MagicMock()
        del mock_pool.putconn  # Pool only has connection() context manager
        mock_cm = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cm.__enter__.return_value = mock_conn

        wrapper = _PooledConnectionWrapper(mock_pool, mock_conn, context_manager=mock_cm)
        cur = wrapper.cursor()
        self.assertEqual(cur, mock_cur)

        wrapper.close()
        mock_cm.__exit__.assert_called_once_with(None, None, None)

    def test_postgres_connect_returns_connection_not_context_manager(self) -> None:
        """Verify _connect() never returns a raw context manager when pool provides connection()."""
        repo_pg = PipelineStateRepository.__new__(PipelineStateRepository)
        repo_pg.backend = "postgres"
        repo_pg._driver = "psycopg"
        repo_pg.database_url = "postgresql://localhost:5432/pipeline"

        # Simulate a pool like psycopg_pool where connection() returns a context manager
        mock_pool = MagicMock()
        mock_cm = MagicMock()
        mock_real_conn = MagicMock()
        mock_cur = MagicMock()
        mock_real_conn.cursor.return_value = mock_cur
        mock_cm.__enter__.return_value = mock_real_conn
        mock_pool.connection.return_value = mock_cm
        del mock_pool.getconn

        repo_pg._pool = mock_pool

        conn = repo_pg._connect()
        # conn must have cursor(), must NOT be the context manager itself
        self.assertTrue(hasattr(conn, "cursor"))
        cur = conn.cursor()
        self.assertEqual(cur, mock_cur)

        # Closing connection releases context manager
        conn.close()
        mock_cm.__exit__.assert_called_once_with(None, None, None)

    def test_concurrent_threads_claim_execution_mutual_exclusion(self) -> None:
        """Verify that under real multi-threaded concurrent contention, exactly 1 thread claims."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = PipelineStateRepository(database_path=Path(temp_dir) / "state.db")
            num_threads = 8
            barrier = threading.Barrier(num_threads)
            results: list[tuple[bool, str]] = []

            def worker() -> None:
                barrier.wait()
                res = repo.claim_execution(777, stage="orchestration")
                results.append(res)

            with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
                futures = [executor.submit(worker) for _ in range(num_threads)]
                concurrent.futures.wait(futures)

            claimed = [r for r in results if r[0] is True]
            rejected = [r for r in results if r[0] is False]
            self.assertEqual(len(claimed), 1, "Exactly one thread must acquire execution claim")
            self.assertEqual(len(rejected), num_threads - 1, "All other concurrent threads must be rejected")
            for _, reason in rejected:
                self.assertEqual(reason, "active_execution")

    def test_concurrent_threads_reserve_publish_slot_mutual_exclusion(self) -> None:
        """Verify that under real multi-threaded concurrent contention, exactly 1 thread reserves the slot."""
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = PipelineStateRepository(database_path=Path(temp_dir) / "state.db")
            num_threads = 8
            barrier = threading.Barrier(num_threads)
            results: list[tuple[bool, float, str]] = []

            def worker(msg_id: int) -> None:
                barrier.wait()
                res = repo.reserve_publish_slot(msg_id, min_interval_seconds=1800)
                results.append(res)

            with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
                futures = [executor.submit(worker, 1000 + i) for i in range(num_threads)]
                concurrent.futures.wait(futures)

            reserved = [r for r in results if r[0] is True]
            rejected = [r for r in results if r[0] is False]
            self.assertEqual(len(reserved), 1, "Exactly one thread must acquire publish slot reservation")
            self.assertEqual(len(rejected), num_threads - 1, "All other concurrent threads must be rate-limited")
            for _, wait_s, reason in rejected:
                self.assertGreater(wait_s, 0.0)
                self.assertIn("Rate limit active", reason)

    def test_postgres_advisory_lock_queries(self) -> None:
        """Verify that PostgreSQL backend uses pg_advisory_xact_lock in transactions."""
        repo = PipelineStateRepository.__new__(PipelineStateRepository)
        repo.backend = "postgres"
        repo._placeholder = "%s"
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        repo._connect = MagicMock(return_value=mock_conn)

        # 1. claim_execution issues pg_advisory_xact_lock on message_id
        mock_cur.fetchone.return_value = None  # No existing row
        claimed, reason = repo.claim_execution(555)
        self.assertTrue(claimed)
        executed_sqls = [call[0][0] for call in mock_cur.execute.call_args_list]
        self.assertTrue(any("pg_advisory_xact_lock" in sql and "claim_execution_" in sql for sql in executed_sqls))

        # 2. reserve_publish_slot issues pg_advisory_xact_lock on global slot
        mock_cur.reset_mock()
        mock_cur.fetchone.return_value = None
        reserved, wait_s, reason = repo.reserve_publish_slot(666, min_interval_seconds=1800)
        self.assertTrue(reserved)
        executed_sqls_reserve = [call[0][0] for call in mock_cur.execute.call_args_list]
        self.assertTrue(any("pg_advisory_xact_lock" in sql and "reserve_publish_slot" in sql for sql in executed_sqls_reserve))


# ---------------------------------------------------------------------------
# PostgreSQL Advisory Lock Integration Tests
# ---------------------------------------------------------------------------

class PostgreSqlAdvisoryLockIntegrationTests(unittest.TestCase):
    """Real mutual-exclusion test against a live PostgreSQL instance.

    Skipped gracefully if neither psycopg nor psycopg2 is installed,
    or if the PostgreSQL service (e.g. from docker-compose) is unreachable.
    """

    driver: Any = None
    db_url: str = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db_url = os.environ.get(
            "STATE_DATABASE_URL",
            "postgresql://pipeline:pipeline@localhost:5432/pipeline",
        )
        try:
            import psycopg  # type: ignore[import-untyped]
            cls.driver = psycopg
        except ImportError:
            try:
                import psycopg2  # type: ignore[import-untyped]
                cls.driver = psycopg2
            except ImportError:
                raise unittest.SkipTest(
                    "Neither psycopg nor psycopg2 is installed; skipping PostgreSQL advisory lock integration test."
                )

        # Probe reachability via fast socket connection before calling driver
        try:
            import socket
            from urllib.parse import urlparse

            parsed = urlparse(cls.db_url)
            host = parsed.hostname or "localhost"
            port = parsed.port or 5432
            probe_hosts = ["127.0.0.1", host] if host == "localhost" else [host]
            connected = False
            last_err = None
            for probe_host in probe_hosts:
                try:
                    sock = socket.create_connection((probe_host, port), timeout=1.0)
                    sock.close()
                    connected = True
                    break
                except Exception as probe_err:
                    last_err = probe_err
            if not connected:
                raise TimeoutError(f"Could not connect to {host}:{port}: {last_err}")
        except Exception as exc:
            raise unittest.SkipTest(
                f"PostgreSQL at {cls.db_url} unreachable ({exc}); skipping integration test."
            )

        # Try connecting to the database
        try:
            conn = cls.driver.connect(cls.db_url, connect_timeout=2)
            conn.close()
        except Exception as exc:
            raise unittest.SkipTest(
                f"PostgreSQL at {cls.db_url} unreachable ({exc}); skipping integration test."
            )

    def test_real_advisory_lock_mutual_exclusion(self) -> None:
        """Spawn two connections: verify Connection A acquires lock, Connection B fails, and unlocks cleanly."""
        lock_key = 888888888
        conn_a = self.driver.connect(self.db_url)
        conn_b = self.driver.connect(self.db_url)
        try:
            cur_a = conn_a.cursor()
            cur_b = conn_b.cursor()

            # Connection A acquires advisory lock
            cur_a.execute("SELECT pg_try_advisory_lock(%s);", (lock_key,))
            res_a = cur_a.fetchone()
            acquired_a = res_a[0] if res_a else False
            self.assertTrue(acquired_a, "Connection A should acquire pg_try_advisory_lock")

            # Connection B attempts to acquire the same advisory lock; must fail
            cur_b.execute("SELECT pg_try_advisory_lock(%s);", (lock_key,))
            res_b = cur_b.fetchone()
            acquired_b = res_b[0] if res_b else False
            self.assertFalse(acquired_b, "Connection B must fail to acquire the same lock while A holds it")

            # Connection A releases the lock
            cur_a.execute("SELECT pg_advisory_unlock(%s);", (lock_key,))
            res_unlock = cur_a.fetchone()
            unlocked_a = res_unlock[0] if res_unlock else False
            self.assertTrue(unlocked_a, "Connection A should release pg_advisory_unlock")

            # Connection B now attempts to acquire the lock; must succeed
            cur_b.execute("SELECT pg_try_advisory_lock(%s);", (lock_key,))
            res_b_after = cur_b.fetchone()
            acquired_b_after = res_b_after[0] if res_b_after else False
            self.assertTrue(acquired_b_after, "Connection B should acquire lock after Connection A releases it")

            # Clean up: Connection B releases the lock
            cur_b.execute("SELECT pg_advisory_unlock(%s);", (lock_key,))
        finally:
            conn_a.close()
            conn_b.close()


# ---------------------------------------------------------------------------
# 3. PipelineOrchestrator End-to-End Tests
# ---------------------------------------------------------------------------

class PipelineOrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temp_dir.name)
        self.state_repo = PipelineStateRepository(self.storage_root / "test_state.db")
        self.alerts = RecordingAlertService()

        # Seed dummy raw video files so mock tests pass the ingestion stage check
        self.raw_dir = self.storage_root / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        for mid in (201, 302, 303, 304, 305, 402, 888, 901, 902, 950, 960, 970, 971):
            (self.raw_dir / f"{mid}.mp4").write_bytes(b"mock video data")

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
        self.assertIn("ingestion", summary.stages)
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

        with patch.object(closed_scheduler, "evaluate", return_value=ScheduleDecision(False, 15.0, "Wait 15s")), \
             patch.object(closed_scheduler, "is_within_posting_window", return_value=True):
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

    def test_orchestrator_skips_when_active_execution_detected(self) -> None:
        # Simulate an ongoing execution by another worker
        self.state_repo.upsert_stage(309, "orchestration", "processing")

        summary = asyncio.run(self.orchestrator.run(message_id=309))

        self.assertEqual(summary.status, "skipped_active_execution")
        self.mock_audio.extract.assert_not_called()
        self.mock_render.render.assert_not_called()
        self.mock_publish.publish.assert_not_called()

    def test_orchestrator_rate_limit_reservation_halts_when_locked(self) -> None:
        # Scheduler enabled with open window but another message already reserved the rate-limit slot
        open_scheduler = PostingWindowScheduler(
            enabled=True, start_hour=0, end_hour=23, timezone_name="UTC", min_interval_seconds=1800
        )
        self.orchestrator.scheduler = open_scheduler

        # Another message reserved the slot
        self.state_repo.reserve_publish_slot(401, min_interval_seconds=1800)

        # Message 402 stages 1-5 pass
        self.mock_audio.extract = AsyncMock(return_value=AudioExtractionResult(402, "completed", None, None))
        self.mock_recognition.recognize = AsyncMock(return_value=RecognitionResult(402, "completed", None, None))
        self.mock_alignment.align = AsyncMock(return_value=AlignmentResult(402, "completed", None, 1.0))
        self.mock_qa_gate.evaluate = AsyncMock(return_value=QAGateResult(402, "approved", True))
        self.mock_render.render = AsyncMock(return_value=RenderResult(402, "completed", None, 10.0))

        summary = asyncio.run(
            self.orchestrator.run(message_id=402, enforce_scheduler=True, wait_for_window=False)
        )

        self.assertEqual(summary.status, "scheduled")
        self.assertIn("Rate limit active", summary.error or "")
        self.mock_render.render.assert_awaited_once()
        self.mock_publish.publish.assert_not_called()

        # Verify orchestration claim was safely released as scheduled (not deadlocked in processing)
        orch_stage = self.state_repo.fetch_stage(402, "orchestration")
        self.assertIsNotNone(orch_stage)
        self.assertEqual(orch_stage["status"], "scheduled")

    def test_scheduled_outcome_retry_does_not_deadlock(self) -> None:
        """Verify Finding 1 fix: message deferred by closed window is NOT permanently deadlocked on retry."""
        # Step A: Run with closed scheduler and wait_for_window=False
        closed_scheduler = PostingWindowScheduler(
            enabled=True, start_hour=9, end_hour=23, timezone_name="UTC"
        )
        self.orchestrator.scheduler = closed_scheduler
        frozen_time = datetime(2026, 9, 12, 3, 0, tzinfo=timezone.utc)  # Outside window

        self.mock_audio.extract = AsyncMock(return_value=AudioExtractionResult(888, "completed", None, None))
        self.mock_recognition.recognize = AsyncMock(return_value=RecognitionResult(888, "completed", None, None))
        self.mock_alignment.align = AsyncMock(return_value=AlignmentResult(888, "completed", None, 1.0))
        self.mock_qa_gate.evaluate = AsyncMock(return_value=QAGateResult(888, "approved", True))

        with patch("pipeline.orchestration.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = frozen_time
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            summary1 = asyncio.run(
                self.orchestrator.run(message_id=888, enforce_scheduler=True, wait_for_window=False)
            )

        self.assertEqual(summary1.status, "scheduled")

        # Crucial check: orchestration claim MUST NOT be left in 'processing'
        orch_row = self.state_repo.fetch_stage(888, "orchestration")
        self.assertIsNotNone(orch_row)
        self.assertEqual(orch_row["status"], "scheduled")

        # Step B: Re-run the SAME message when posting window opens, WITHOUT --force
        open_time = datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc)  # Inside window
        self.mock_render.render = AsyncMock(return_value=RenderResult(888, "completed", None, 10.0))
        self.mock_publish.publish = AsyncMock(return_value=PublishExecutionResult(888, "completed", None))

        with patch("pipeline.orchestration.scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = open_time
            mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

            summary2 = asyncio.run(
                self.orchestrator.run(message_id=888, enforce_scheduler=True, wait_for_window=False, force=False)
            )

        # Must NOT be skipped_active_execution; must succeed completely!
        self.assertNotEqual(summary2.status, "skipped_active_execution")
        self.assertEqual(summary2.status, "completed")
        self.mock_render.render.assert_awaited_once()
        self.mock_publish.publish.assert_awaited_once()

    def test_downstream_render_failure_does_not_dangle_publish_reservation(self) -> None:
        """Verify Finding 2 fix: render failure on message A does not block unrelated message B."""
        open_scheduler = PostingWindowScheduler(
            enabled=True, start_hour=0, end_hour=23, timezone_name="UTC", min_interval_seconds=1800
        )
        self.orchestrator.scheduler = open_scheduler

        # 1. Message 901 runs and fails in render stage
        self.mock_audio.extract = AsyncMock(return_value=AudioExtractionResult(901, "completed", None, None))
        self.mock_recognition.recognize = AsyncMock(return_value=RecognitionResult(901, "completed", None, None))
        self.mock_alignment.align = AsyncMock(return_value=AlignmentResult(901, "completed", None, 1.0))
        self.mock_qa_gate.evaluate = AsyncMock(return_value=QAGateResult(901, "approved", True))
        self.mock_render.render = AsyncMock(
            return_value=RenderResult(901, "failed", None, None, error="GPU out of memory")
        )

        summary_a = asyncio.run(self.orchestrator.run(message_id=901, enforce_scheduler=True))
        self.assertEqual(summary_a.status, "failed")

        # Crucial check: message 901 must NOT have left publish stage in 'processing'
        pub_row_a = self.state_repo.fetch_stage(901, "publish")
        self.assertTrue(pub_row_a is None or pub_row_a["status"] != "processing")

        # 2. Message 902 (completely unrelated) runs immediately after
        self.mock_audio.extract = AsyncMock(return_value=AudioExtractionResult(902, "completed", None, None))
        self.mock_recognition.recognize = AsyncMock(return_value=RecognitionResult(902, "completed", None, None))
        self.mock_alignment.align = AsyncMock(return_value=AlignmentResult(902, "completed", None, 1.0))
        self.mock_qa_gate.evaluate = AsyncMock(return_value=QAGateResult(902, "approved", True))
        self.mock_render.render = AsyncMock(return_value=RenderResult(902, "completed", None, 10.0))
        self.mock_publish.publish = AsyncMock(return_value=PublishExecutionResult(902, "completed", None))

        summary_b = asyncio.run(self.orchestrator.run(message_id=902, enforce_scheduler=True))

        # Message 902 must NOT be told 'Rate limit active' or 'scheduled' due to message 901!
        self.assertEqual(summary_b.status, "completed")
        self.mock_publish.publish.assert_awaited_once()

    def test_posting_window_closing_during_render_blocks_publish(self) -> None:
        """Verify Issue 2 fix: posting window closing during render blocks publish and marks scheduled."""
        msg_id = 950
        scheduler = PostingWindowScheduler(
            enabled=True, start_hour=9, end_hour=23, timezone_name="UTC", min_interval_seconds=0
        )
        self.orchestrator.scheduler = scheduler

        self.mock_audio.extract = AsyncMock(return_value=AudioExtractionResult(msg_id, "completed", None, None))
        self.mock_recognition.recognize = AsyncMock(return_value=RecognitionResult(msg_id, "completed", None, None))
        self.mock_alignment.align = AsyncMock(return_value=AlignmentResult(msg_id, "completed", None, 1.0))
        self.mock_qa_gate.evaluate = AsyncMock(return_value=QAGateResult(msg_id, "approved", True))
        self.mock_render.render = AsyncMock(return_value=RenderResult(msg_id, "completed", None, 10.0))
        self.mock_publish.publish = AsyncMock(return_value=PublishExecutionResult(msg_id, "completed", None))

        call_count = 0

        def dynamic_window_check(*args: Any, **kwargs: Any) -> bool:
            nonlocal call_count
            call_count += 1
            # Call 1 (step 6 scheduler check before render) -> True
            # Subsequent calls (step 8 check right before publish) -> False
            return call_count <= 1

        with patch.object(scheduler, "is_within_posting_window", side_effect=dynamic_window_check):
            summary = asyncio.run(
                self.orchestrator.run(message_id=msg_id, enforce_scheduler=True, wait_for_window=False)
            )

        self.assertEqual(summary.status, "scheduled")
        self.mock_render.render.assert_awaited_once()
        self.mock_publish.publish.assert_not_called()

        pub_row = self.state_repo.fetch_stage(msg_id, "publish")
        self.assertIsNotNone(pub_row)
        self.assertEqual(pub_row["status"], "scheduled")

        orch_row = self.state_repo.fetch_stage(msg_id, "orchestration")
        self.assertIsNotNone(orch_row)
        self.assertEqual(orch_row["status"], "scheduled")

    def test_held_for_review_requires_force_to_retry(self) -> None:
        """Verify Issue 4 fix: held_for_review messages require --force to retry."""
        msg_id = 960
        self.mock_audio.extract = AsyncMock(return_value=AudioExtractionResult(msg_id, "completed", None, None))
        self.mock_recognition.recognize = AsyncMock(return_value=RecognitionResult(msg_id, "completed", None, None))
        self.mock_alignment.align = AsyncMock(return_value=AlignmentResult(msg_id, "completed", None, 1.0))
        self.mock_qa_gate.evaluate = AsyncMock(return_value=QAGateResult(msg_id, "approved", True))
        self.mock_render.render = AsyncMock(return_value=RenderResult(msg_id, "completed", None, 10.0))
        self.mock_publish.publish = AsyncMock(return_value=PublishExecutionResult(msg_id, "completed", None))

        # 1. Set message status to held_for_review
        self.state_repo.upsert_stage(msg_id, "orchestration", "held_for_review")

        # 2. Run orchestrator with force=False -> assert claim rejected, status remains held_for_review, pipeline does not run
        summary_no_force = asyncio.run(
            self.orchestrator.run(message_id=msg_id, force=False)
        )
        self.assertEqual(summary_no_force.status, "held_for_review")
        self.mock_audio.extract.assert_not_called()
        self.mock_publish.publish.assert_not_called()

        stage_row = self.state_repo.fetch_stage(msg_id, "orchestration")
        self.assertIsNotNone(stage_row)
        self.assertEqual(stage_row["status"], "held_for_review")

        # 3. Run orchestrator with force=True -> assert claim acquired, pipeline runs and completes
        summary_force = asyncio.run(
            self.orchestrator.run(message_id=msg_id, force=True)
        )
        self.assertEqual(summary_force.status, "completed")
        self.mock_audio.extract.assert_awaited_once()
        self.mock_publish.publish.assert_awaited_once()

        orch_final = self.state_repo.fetch_stage(msg_id, "orchestration")
        self.assertIsNotNone(orch_final)
        self.assertEqual(orch_final["status"], "completed")

    def test_cold_start_without_raw_video_fails_cleanly(self) -> None:
        """Verify Issue 3: cold start with missing raw video and no --source fails cleanly at ingestion stage."""
        msg_id = 9999
        # Ensure raw directory has no video for this message
        raw_video = self.storage_root / "raw" / f"{msg_id}.mp4"
        raw_video.unlink(missing_ok=True)

        summary = asyncio.run(
            self.orchestrator.run(message_id=msg_id, source_path=None)
        )

        # 1. Summary status is 'failed', not an unhandled exception or crash
        self.assertEqual(summary.status, "failed")
        self.assertIsNotNone(summary.error)
        self.assertIn("Source video does not exist", summary.error or "")

        # 2. Ingestion stage recorded as failed in state repository and summary
        self.assertIn("ingestion", summary.stages)
        self.assertEqual(summary.stages["ingestion"]["status"], "failed")

        ingestion_row = self.state_repo.fetch_stage(msg_id, "ingestion")
        self.assertIsNotNone(ingestion_row)
        self.assertEqual(ingestion_row["status"], "failed")
        self.assertIn("Source video does not exist", ingestion_row["error"] or "")

        orch_row = self.state_repo.fetch_stage(msg_id, "orchestration")
        self.assertIsNotNone(orch_row)
        self.assertEqual(orch_row["status"], "failed")

        # 3. Downstream stages were never invoked
        self.mock_audio.extract.assert_not_called()
        self.mock_recognition.recognize.assert_not_called()
        self.mock_render.render.assert_not_called()
        self.mock_publish.publish.assert_not_called()

        # 4. Ingestion failure alert was dispatched
        self.assertTrue(len(self.alerts.alerts) > 0)
        self.assertTrue(any("ingestion" in a for a in self.alerts.alerts))

        # 5. CLI returns exit code 1 cleanly without raising uncaught exceptions
        with patch("pipeline.orchestration.app.build_orchestrator") as mock_build:
            mock_build.return_value = (self.orchestrator, self.state_repo)
            code = asyncio.run(cli_main([str(msg_id), "--json"]))
            self.assertEqual(code, 1)

    def test_orchestrator_force_active_execution_behavior(self) -> None:
        """Verify orchestrator respects safe force semantics on active execution."""
        msg_id = 970
        self.mock_audio.extract = AsyncMock(return_value=AudioExtractionResult(msg_id, "completed", None, None))
        self.mock_recognition.recognize = AsyncMock(return_value=RecognitionResult(msg_id, "completed", None, None))
        self.mock_alignment.align = AsyncMock(return_value=AlignmentResult(msg_id, "completed", None, 1.0))
        self.mock_qa_gate.evaluate = AsyncMock(return_value=QAGateResult(msg_id, "approved", True))
        self.mock_render.render = AsyncMock(return_value=RenderResult(msg_id, "completed", None, 10.0))
        self.mock_publish.publish = AsyncMock(return_value=PublishExecutionResult(msg_id, "completed", None))

        # 1. Message is actively processing (fresh)
        self.state_repo.upsert_stage(msg_id, "orchestration", "processing")

        # 2. force=True without force_active -> skipped_active_execution
        summary_force = asyncio.run(
            self.orchestrator.run(message_id=msg_id, force=True, force_active=False)
        )
        self.assertEqual(summary_force.status, "skipped_active_execution")
        self.assertIn("actively processing", summary_force.error or "")
        self.mock_audio.extract.assert_not_called()

        # 3. force=True with force_active=True -> successfully runs and completes
        summary_force_active = asyncio.run(
            self.orchestrator.run(message_id=msg_id, force=True, force_active=True)
        )
        self.assertEqual(summary_force_active.status, "completed")
        self.mock_audio.extract.assert_awaited_once()

        # 4. Another message with stale claim -> force=True alone succeeds
        msg_id_stale = 971
        self.mock_audio.extract.reset_mock()
        self.mock_audio.extract = AsyncMock(return_value=AudioExtractionResult(msg_id_stale, "completed", None, None))
        self.mock_recognition.recognize = AsyncMock(return_value=RecognitionResult(msg_id_stale, "completed", None, None))
        self.mock_alignment.align = AsyncMock(return_value=AlignmentResult(msg_id_stale, "completed", None, 1.0))
        self.mock_qa_gate.evaluate = AsyncMock(return_value=QAGateResult(msg_id_stale, "approved", True))
        self.mock_render.render = AsyncMock(return_value=RenderResult(msg_id_stale, "completed", None, 10.0))
        self.mock_publish.publish = AsyncMock(return_value=PublishExecutionResult(msg_id_stale, "completed", None))

        self.state_repo.upsert_stage(msg_id_stale, "orchestration", "processing")
        stale_time = (datetime.now(timezone.utc) - timedelta(seconds=400)).strftime("%Y-%m-%d %H:%M:%S")
        with closing(self.state_repo._connect()) as conn:
            conn.execute(
                "UPDATE pipeline_items SET updated_at = ? WHERE message_id = ? AND stage = 'orchestration'",
                (stale_time, msg_id_stale),
            )
            conn.commit()

        summary_stale = asyncio.run(
            self.orchestrator.run(message_id=msg_id_stale, force=True, force_active=False)
        )
        self.assertEqual(summary_stale.status, "completed")
        self.mock_audio.extract.assert_awaited_once()




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

        # Required collapsed nodes (6 nodes total including dual-branch exit code handling)
        expected_nodes = [
            "Telegram Video Trigger",
            "Run Pipeline Orchestrator",
            "Check Orchestration Exit Code",
            "Alert QA Review Queue",
            "Check Execution Succeeded",
            "Alert Pipeline Failure",
        ]
        self.assertEqual(len(nodes), len(expected_nodes), f"Expected exactly {len(expected_nodes)} nodes, found {len(nodes)}")
        for expected in expected_nodes:
            self.assertIn(expected, nodes, f"Missing required node: {expected}")

        # Per-stage nodes must be completely removed
        obsolete_nodes = [
            "Ingest Video Message",
            "Extract Audio WAV",
            "Recognize Quran Verses",
            "CTC Forced Alignment",
            "QA Gate Verification",
            "Check QA Gate Approved",
            "Posting Window & Rate Limit Check",
            "Check Posting Window Allowed",
            "Render 1080x1920 Video",
            "Multi-Platform Publish",
        ]
        for obsolete in obsolete_nodes:
            self.assertNotIn(obsolete, nodes, f"Obsolete per-stage node must be removed: {obsolete}")

        # Verify Run Pipeline Orchestrator node configuration
        orch_node = nodes["Run Pipeline Orchestrator"]
        self.assertEqual(orch_node["type"], "n8n-nodes-base.executeCommand")
        self.assertTrue(orch_node.get("continueOnFail", False), "Run Pipeline Orchestrator must set continueOnFail to True")
        cmd = orch_node["parameters"]["command"]
        self.assertIn("python -m pipeline.orchestration.app", cmd)
        self.assertIn("{{$('Telegram Video Trigger').item.json.message.message_id}}", cmd)
        self.assertIn("--json", cmd)

        # Verify Check Orchestration Exit Code IF node (exitCode == 2)
        if_node = nodes["Check Orchestration Exit Code"]
        self.assertEqual(if_node["type"], "n8n-nodes-base.if")
        conditions = if_node["parameters"]["conditions"]
        self.assertIn("number", conditions)
        num_cond = conditions["number"][0]
        self.assertEqual(num_cond["value1"], '={{$json["exitCode"]}}')
        self.assertEqual(num_cond["operation"], "equal")
        self.assertEqual(num_cond["value2"], 2)

        # Verify Check Execution Succeeded IF node (exitCode == 0)
        succ_node = nodes["Check Execution Succeeded"]
        self.assertEqual(succ_node["type"], "n8n-nodes-base.if")
        succ_conditions = succ_node["parameters"]["conditions"]
        self.assertIn("number", succ_conditions)
        succ_cond = succ_conditions["number"][0]
        self.assertEqual(succ_cond["value1"], '={{$json["exitCode"]}}')
        self.assertEqual(succ_cond["operation"], "equal")
        self.assertEqual(succ_cond["value2"], 0)

        # Verify Alert Pipeline Failure HTTP node
        fail_node = nodes["Alert Pipeline Failure"]
        self.assertEqual(fail_node["type"], "n8n-nodes-base.httpRequest")

        # Verify Connections
        connections = data["connections"]
        self.assertEqual(
            connections["Telegram Video Trigger"]["main"][0][0]["node"],
            "Run Pipeline Orchestrator",
        )
        self.assertEqual(
            connections["Run Pipeline Orchestrator"]["main"][0][0]["node"],
            "Check Orchestration Exit Code",
        )

        # Both output arrays must exist for Check Orchestration Exit Code
        self.assertEqual(
            len(connections["Check Orchestration Exit Code"]["main"]),
            2,
            "Check Orchestration Exit Code must provide two output connection arrays (true and false branches)",
        )
        # Output 0 (True: exitCode == 2) -> Alert QA Review Queue
        self.assertEqual(
            connections["Check Orchestration Exit Code"]["main"][0][0]["node"],
            "Alert QA Review Queue",
        )
        # Output 1 (False: exitCode != 2) -> Check Execution Succeeded
        self.assertEqual(
            connections["Check Orchestration Exit Code"]["main"][1][0]["node"],
            "Check Execution Succeeded",
        )
        # False branch of Check Execution Succeeded (exitCode != 0) -> Alert Pipeline Failure
        self.assertEqual(
            connections["Check Execution Succeeded"]["main"][1][0]["node"],
            "Alert Pipeline Failure",
        )

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
        args = parse_args(["505", "--draft", "--skip-scheduler", "--wait-for-window", "--force", "--force-active", "--json"])
        self.assertEqual(args.message_id, 505)
        self.assertTrue(args.draft)
        self.assertTrue(args.skip_scheduler)
        self.assertTrue(args.wait_for_window)
        self.assertTrue(args.force)
        self.assertTrue(args.force_active)
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

            # Skipped active execution -> 0
            mock_orch.run = AsyncMock(
                return_value=PipelineExecutionSummary(601, "skipped_active_execution")
            )
            code = asyncio.run(cli_main(["601"]))
            self.assertEqual(code, 0)

            # Skipped already published -> 0
            mock_orch.run = AsyncMock(
                return_value=PipelineExecutionSummary(601, "skipped_already_published")
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

    def test_build_orchestrator_real_instantiation(self) -> None:
        """Verify unmocked build_orchestrator() constructs cleanly without constructor kwarg errors."""
        import tempfile
        from pipeline.orchestration.app import build_orchestrator
        from pipeline.publish.client import MultiPlatformPublishClient, StubPublishClient

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            corpus_file = temp_path / "seeded_corpus.json"
            corpus_file.write_text(
                json.dumps([{"surah": 1, "ayah": 1, "text": "بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ"}]),
                encoding="utf-8",
            )
            db_file = temp_path / "test.db"

            # 1. Test stub client instantiation (default)
            with patch.dict(
                "os.environ",
                {
                    "QURAN_CORPUS_CACHE_PATH": str(corpus_file),
                    "STATE_DB_PATH": str(db_file),
                    "PIPELINE_STORAGE_ROOT": str(temp_path),
                },
                clear=False,
            ):
                os.environ.pop("PUBLISH_API_KEY", None)
                settings = OrchestrationSettings.from_env()
                orch, repo = build_orchestrator(settings)
                try:
                    self.assertIsInstance(orch, PipelineOrchestrator)
                    self.assertIsInstance(orch.publish_service.client, StubPublishClient)
                finally:
                    repo.close()

            # 2. Test production client instantiation (with Ayrshare API credentials)
            with patch.dict(
                "os.environ",
                {
                    "QURAN_CORPUS_CACHE_PATH": str(corpus_file),
                    "STATE_DB_PATH": str(db_file),
                    "PIPELINE_STORAGE_ROOT": str(temp_path),
                    "PUBLISH_API_KEY": "ayr_test_api_key_123",
                    "PUBLISH_API_BASE_URL": "https://app.ayrshare.com/api",
                },
                clear=False,
            ):
                settings = OrchestrationSettings.from_env()
                orch, repo = build_orchestrator(settings)
                try:
                    self.assertIsInstance(orch, PipelineOrchestrator)
                    self.assertIsInstance(orch.publish_service.client, MultiPlatformPublishClient)
                    self.assertEqual(orch.publish_service.client.api_base_url, "https://app.ayrshare.com/api")
                finally:
                    repo.close()



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

