"""State-store access for pipeline stages supporting SQLite and PostgreSQL backends."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class _PooledConnectionWrapper:
    """Wrapper ensuring connections retrieved from a pool are returned on close."""

    def __init__(self, pool: Any, connection: Any, context_manager: Any = None) -> None:
        self._pool = pool
        self._connection = connection
        self._cm = context_manager
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._cm is not None:
            self._cm.__exit__(None, None, None)
        elif hasattr(self._pool, "putconn"):
            self._pool.putconn(self._connection)
        elif hasattr(self._connection, "close"):
            self._connection.close()

    def __enter__(self) -> Any:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any:
        self.close()
        return False

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        if hasattr(self._connection, "execute"):
            return self._connection.execute(*args, **kwargs)
        cur = self._connection.cursor()
        return cur.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class PipelineStateRepository:
    """Persist stage status rows in SQLite (default/local) or PostgreSQL (multi-container).

    Backend Concurrency & Known Limitations:
      - SQLite (default/local): Uses 'BEGIN IMMEDIATE' for critical claim and reservation
        transactions (claim_execution, reserve_publish_slot). This acquires a write lock
        on the entire database file, meaning concurrent operations for DIFFERENT message_ids
        will serialize rather than executing in parallel. This is completely suitable for
        local development, CLI invocations, and single-worker test suites per ADR 001.
      - PostgreSQL (multi-container / production): Uses per-key advisory transaction locks
        ('SELECT pg_advisory_xact_lock(...)') scoped to specific message IDs or global rate
        slots. This avoids whole-database locking and allows parallel pipeline execution
        across multiple worker processes or containers. PostgreSQL should always be used
        for deployments requiring multi-worker concurrency throughput.

    See docs/decisions/001-state-database-strategy.md for architecture details.
    """

    def __init__(
        self,
        database_path: Path | str | None = None,
        database_url: str | None = None,
    ) -> None:
        self.database_url = database_url
        self._pool: Any = None
        self._driver: str | None = None
        if database_url:
            self.backend = "postgres"
            self.database_path = None
            self._driver = self._resolve_postgres_driver()
            self._init_postgres_pool()
        else:
            self.backend = "sqlite"
            self.database_path = Path(database_path or "pipeline/state/pipeline.db")
            self.database_path.parent.mkdir(parents=True, exist_ok=True)

        self._placeholder = "%s" if self.backend == "postgres" else "?"
        self._initialize()

    def _resolve_postgres_driver(self) -> str:
        """Ensure a PostgreSQL driver is installed, returning 'psycopg' or 'psycopg2'."""
        try:
            import psycopg  # psycopg 3
            if psycopg is not None:
                return "psycopg"
        except ImportError:
            pass
        try:
            import psycopg2  # psycopg 2
            if psycopg2 is not None:
                return "psycopg2"
        except ImportError:
            pass
        raise ImportError(
            "PostgreSQL database URL provided, but neither 'psycopg' nor 'psycopg2' is installed. "
            "Install 'psycopg[binary]' to connect to PostgreSQL."
        )

    def _init_postgres_pool(self) -> None:
        """Initialize PostgreSQL connection pool based on the resolved driver."""
        if self._driver == "psycopg":
            try:
                from psycopg_pool import ConnectionPool
                self._pool = ConnectionPool(self.database_url, min_size=1, max_size=10, open=True)
                return
            except (ImportError, Exception):
                pass
        elif self._driver == "psycopg2":
            try:
                from psycopg2.pool import ThreadedConnectionPool
                self._pool = ThreadedConnectionPool(minconn=1, maxconn=10, dsn=self.database_url)
                return
            except (ImportError, Exception):
                pass
        self._pool = None

    def close(self) -> None:
        """Release connection pool resources if open."""
        if self._pool is not None and hasattr(self._pool, "close"):
            self._pool.close()
            self._pool = None

    def _connect(self) -> Any:
        if self.backend == "postgres":
            if self._pool is not None:
                if hasattr(self._pool, "getconn"):
                    return _PooledConnectionWrapper(self._pool, self._pool.getconn())
                if hasattr(self._pool, "connection"):
                    cm = self._pool.connection()
                    conn = cm.__enter__()
                    return _PooledConnectionWrapper(self._pool, conn, context_manager=cm)
            if self._driver == "psycopg":
                import psycopg  # psycopg 3
                return psycopg.connect(self.database_url)
            elif self._driver == "psycopg2":
                import psycopg2  # psycopg 2
                return psycopg2.connect(self.database_url)
            else:
                raise ImportError(
                    "PostgreSQL database URL provided, but neither 'psycopg' nor 'psycopg2' is installed. "
                    "Install 'psycopg[binary]' to connect to PostgreSQL."
                )
        else:
            connection = sqlite3.connect(self.database_path, timeout=30.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=30000")
            return connection


    def _format_sql(self, query: str) -> str:
        """Translate SQLite '?' parameter markers to PostgreSQL '%s' when needed."""
        if self.backend == "postgres":
            return query.replace("?", "%s")
        return query

    @staticmethod
    def _row_to_dict(cursor: Any, row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        if isinstance(row, dict):
            return dict(row)
        if hasattr(row, "keys"):
            return dict(row)
        if hasattr(cursor, "description") and cursor.description:
            columns = [col[0] for col in cursor.description]
            return dict(zip(columns, row))
        return dict(row)

    def _initialize(self) -> None:
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            if self.backend == "postgres":
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pipeline_items (
                        message_id BIGINT NOT NULL,
                        stage VARCHAR(64) NOT NULL,
                        status VARCHAR(32) NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        error TEXT,
                        PRIMARY KEY (message_id, stage)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pipeline_items_status ON pipeline_items (status)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pipeline_items_updated_at ON pipeline_items (updated_at)"
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pipeline_messages (
                        message_id BIGINT PRIMARY KEY,
                        channel_id TEXT NOT NULL,
                        message_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                        duration_seconds REAL,
                        created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            else:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pipeline_items (
                        message_id INTEGER NOT NULL,
                        stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        error TEXT,
                        PRIMARY KEY (message_id, stage)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pipeline_items_status ON pipeline_items (status)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pipeline_items_updated_at ON pipeline_items (updated_at)"
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS pipeline_messages (
                        message_id INTEGER PRIMARY KEY,
                        channel_id TEXT NOT NULL,
                        message_timestamp TEXT NOT NULL,
                        duration_seconds REAL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            conn.commit()

    def upsert_message_metadata(
        self,
        message_id: int,
        channel_id: int | str,
        timestamp: datetime,
        duration_seconds: int | float | None,
    ) -> None:
        """Capture the Telegram source attributes required for every ingestion."""
        sql = self._format_sql(
            """
            INSERT INTO pipeline_messages (
                message_id, channel_id, message_timestamp, duration_seconds
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (message_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_timestamp = excluded.message_timestamp,
                duration_seconds = excluded.duration_seconds,
                updated_at = CURRENT_TIMESTAMP
            """
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(
                sql,
                (
                    message_id,
                    str(channel_id),
                    timestamp.isoformat(),
                    duration_seconds,
                ),
            )
            conn.commit()

    def upsert_stage(
        self,
        message_id: int,
        stage: str,
        status: str,
        error: str | None = None,
    ) -> None:
        """Persist or update status for a pipeline stage."""
        sql = self._format_sql(
            """
            INSERT INTO pipeline_items (message_id, stage, status, error)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (message_id, stage) DO UPDATE SET
                status = excluded.status,
                error = excluded.error,
                updated_at = CURRENT_TIMESTAMP
            """
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql, (message_id, stage, status, error))
            conn.commit()

    def fetch_stage(self, message_id: int, stage: str) -> dict[str, Any] | None:
        """Return the status row for one message and stage."""
        sql = self._format_sql(
            """
            SELECT message_id, stage, status, created_at, updated_at, error
            FROM pipeline_items
            WHERE message_id = ? AND stage = ?
            """
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql, (message_id, stage))
            row = cur.fetchone()
            return self._row_to_dict(cur, row)

    def fetch_all_stages(self, message_id: int) -> list[dict[str, Any]]:
        """Return all recorded stage rows for a given message_id ordered by created_at."""
        sql = self._format_sql(
            """
            SELECT message_id, stage, status, created_at, updated_at, error
            FROM pipeline_items
            WHERE message_id = ?
            ORDER BY created_at ASC
            """
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql, (message_id,))
            rows = cur.fetchall()
            return [d for r in rows if (d := self._row_to_dict(cur, r)) is not None]

    def fetch_message_metadata(self, message_id: int) -> dict[str, Any] | None:
        """Return the captured source metadata for one Telegram message."""
        sql = self._format_sql(
            """
            SELECT message_id, channel_id, message_timestamp, duration_seconds,
                   created_at, updated_at
            FROM pipeline_messages
            WHERE message_id = ?
            """
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql, (message_id,))
            row = cur.fetchone()
            return self._row_to_dict(cur, row)

    @staticmethod
    def _parse_datetime(res: Any) -> datetime | None:
        """Helper to convert database timestamp values to timezone-aware UTC datetimes."""
        if res is None:
            return None
        if isinstance(res, datetime):
            return res if res.tzinfo else res.replace(tzinfo=timezone.utc)
        if isinstance(res, str):
            try:
                dt = datetime.fromisoformat(res)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                try:
                    # SQLite CURRENT_TIMESTAMP format: "YYYY-MM-DD HH:MM:SS"
                    dt = datetime.strptime(res, "%Y-%m-%d %H:%M:%S")
                    return dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    return None
        return None

    def fetch_latest_published_timestamp(self) -> datetime | None:
        """Return the timestamp of the most recently published video item, if any."""
        sql = self._format_sql(
            """
            SELECT updated_at
            FROM pipeline_items
            WHERE stage = ? AND status = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql, ("publish", "completed"))
            row = cur.fetchone()
            if not row:
                return None
            res = row[0] if isinstance(row, (tuple, list)) else row["updated_at"]
            return self._parse_datetime(res)

    def fetch_items_by_status(self, stage: str, status: str) -> list[dict[str, Any]]:
        """Return items matching a given stage and status (e.g. for review queue or retry monitoring)."""
        sql = self._format_sql(
            """
            SELECT message_id, stage, status, created_at, updated_at, error
            FROM pipeline_items
            WHERE stage = ? AND status = ?
            ORDER BY updated_at DESC
            """
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql, (stage, status))
            rows = cur.fetchall()
            return [d for r in rows if (d := self._row_to_dict(cur, r)) is not None]

    def claim_execution(
        self,
        message_id: int,
        stage: str = "orchestration",
        force: bool = False,
        force_active: bool = False,
        stale_threshold_seconds: int = 300,
    ) -> tuple[bool, str]:
        """Atomically claim pipeline execution token for message_id to prevent concurrent races.

        Returns (claimed, reason):
          - (True, "claimed") if execution token was successfully acquired
          - (False, "already_completed") if the item has already completed publishing
          - (False, "held_for_review") if the item was held for review (requires force=True to retry)
          - (False, "active_execution") if another worker is currently processing this message
          - (False, "active_execution_recent") if another worker claimed this message recently
            (<stale_threshold_seconds); requires force_active=True to override
        """
        with closing(self._connect()) as conn:
            try:
                cur = conn.cursor()
                if self.backend == "sqlite":
                    conn.execute("BEGIN IMMEDIATE")
                else:
                    conn.execute("BEGIN")
                    # Acquire transaction-level advisory lock on message_id in PostgreSQL
                    sql_lock = self._format_sql(
                        "SELECT pg_advisory_xact_lock(hashtext('claim_execution_' || CAST(? AS text)))"
                    )
                    cur.execute(sql_lock, (message_id,))

                # 1. Check if publish is already completed
                sql_pub = self._format_sql(
                    "SELECT status FROM pipeline_items WHERE message_id = ? AND stage = ?"
                )
                cur.execute(sql_pub, (message_id, "publish"))
                pub_row = cur.fetchone()
                if pub_row:
                    status = pub_row[0] if isinstance(pub_row, (tuple, list)) else pub_row["status"]
                    if status == "completed" and not force:
                        conn.commit()
                        return False, "already_completed"

                # 2. Check existing claim stage status
                sql_stage = self._format_sql(
                    "SELECT status, updated_at FROM pipeline_items WHERE message_id = ? AND stage = ?"
                )
                cur.execute(sql_stage, (message_id, stage))
                stage_row = cur.fetchone()
                if stage_row:
                    status = stage_row[0] if isinstance(stage_row, (tuple, list)) else stage_row["status"]
                    updated_at_val = stage_row[1] if isinstance(stage_row, (tuple, list)) else stage_row["updated_at"]
                    claim_dt = self._parse_datetime(updated_at_val)

                    if status == "completed" and not force:
                        conn.commit()
                        return False, "already_completed"
                    if status == "held_for_review" and not force:
                        conn.commit()
                        return False, "held_for_review"
                    if status == "processing":
                        if not force and not force_active:
                            conn.commit()
                            return False, "active_execution"
                        # force or force_active is True: check staleness
                        is_stale = False
                        if claim_dt is not None:
                            elapsed = (datetime.now(timezone.utc) - claim_dt).total_seconds()
                            if elapsed >= stale_threshold_seconds:
                                is_stale = True
                        if not is_stale and not force_active:
                            conn.commit()
                            return False, "active_execution_recent"

                # Also check if qa_gate was held_for_review
                if not force and stage != "qa_gate":
                    sql_qa = self._format_sql(
                        "SELECT status FROM pipeline_items WHERE message_id = ? AND stage = ?"
                    )
                    cur.execute(sql_qa, (message_id, "qa_gate"))
                    qa_row = cur.fetchone()
                    if qa_row:
                        qa_status = qa_row[0] if isinstance(qa_row, (tuple, list)) else qa_row["status"]
                        if qa_status in ("held_for_review", "rejected"):
                            conn.commit()
                            return False, "held_for_review"

                # 3. Atomically upsert claim status to 'processing'
                sql_claim = self._format_sql(
                    """
                    INSERT INTO pipeline_items (message_id, stage, status, error)
                    VALUES (?, ?, 'processing', NULL)
                    ON CONFLICT (message_id, stage) DO UPDATE SET
                        status = 'processing',
                        error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    """
                )
                cur.execute(sql_claim, (message_id, stage))
                conn.commit()
                return True, "claimed"
            except Exception:
                conn.rollback()
                raise

    def release_execution_claim(
        self,
        message_id: int,
        stage: str = "orchestration",
        status: str = "scheduled",
        error: str | None = None,
    ) -> None:
        """Release an execution claim by resetting its status (e.g. to 'scheduled' or 'failed')."""
        self.upsert_stage(message_id, stage, status, error)

    def clear_publish_reservation(
        self,
        message_id: int,
        status: str = "failed",
        error: str | None = None,
    ) -> None:
        """Clear an active publish slot reservation so subsequent messages are unblocked."""
        self.upsert_stage(message_id, "publish", status, error)

    def reserve_publish_slot(
        self,
        message_id: int,
        min_interval_seconds: int = 1800,
        reservation_timeout_seconds: int = 300,
    ) -> tuple[bool, float, str]:
        """Atomically evaluate rate limits and reserve a publication slot.

        Returns (reserved, wait_seconds, reason):
          - (True, 0.0, "reserved") if slot was successfully reserved
          - (False, remaining_seconds, reason) if locked by another publication or active reservation
        """
        with closing(self._connect()) as conn:
            try:
                cur = conn.cursor()
                if self.backend == "sqlite":
                    conn.execute("BEGIN IMMEDIATE")
                else:
                    conn.execute("BEGIN")
                    # Acquire transaction-level advisory lock on global publish slot in PostgreSQL
                    sql_lock = self._format_sql(
                        "SELECT pg_advisory_xact_lock(hashtext('reserve_publish_slot'))"
                    )
                    cur.execute(sql_lock)

                # 1. Check if this message was already published
                sql_self = self._format_sql(
                    "SELECT status FROM pipeline_items WHERE message_id = ? AND stage = ?"
                )
                cur.execute(sql_self, (message_id, "publish"))
                self_row = cur.fetchone()
                if self_row:
                    s = self_row[0] if isinstance(self_row, (tuple, list)) else self_row["status"]
                    if s == "completed":
                        conn.commit()
                        return False, 0.0, "already_published"

                # 2. Check latest publication or active reservation across other messages
                sql_latest = self._format_sql(
                    """
                    SELECT message_id, status, updated_at
                    FROM pipeline_items
                    WHERE stage = ? AND status IN (?, ?) AND message_id != ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """
                )
                cur.execute(sql_latest, ("publish", "completed", "processing", message_id))
                row = cur.fetchone()
                last_dt = None
                last_status = None
                last_msg_id = None
                if row:
                    last_msg_id = row[0] if isinstance(row, (tuple, list)) else row["message_id"]
                    last_status = row[1] if isinstance(row, (tuple, list)) else row["status"]
                    res = row[2] if isinstance(row, (tuple, list)) else row["updated_at"]
                    last_dt = self._parse_datetime(res)

                if last_dt is not None:
                    now_dt = datetime.now(timezone.utc)
                    elapsed = (now_dt - last_dt).total_seconds()
                    # An active 'processing' reservation expires after reservation_timeout_seconds (default 300s)
                    # to prevent a dead/crashed worker from blocking the rate limit slot indefinitely.
                    if min_interval_seconds > 0:
                        effective_interval = (
                            min_interval_seconds
                            if last_status == "completed"
                            else min(min_interval_seconds, reservation_timeout_seconds)
                        )
                        remaining = effective_interval - elapsed
                        if remaining > 0:
                            conn.commit()
                            return False, remaining, f"Rate limit active: must wait {remaining:.1f}s"

                    # If the latest other row was an active 'processing' reservation that expired,
                    # explicitly mark that orphaned row as failed in this same atomic transaction.
                    if (
                        last_status == "processing"
                        and elapsed >= reservation_timeout_seconds
                        and last_msg_id is not None
                    ):
                        stale_err = f"Reservation expired after {int(elapsed)}s, presumed crashed worker"
                        sql_fail_stale = self._format_sql(
                            """
                            UPDATE pipeline_items
                            SET status = 'failed',
                                error = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE message_id = ? AND stage = ? AND status = 'processing'
                            """
                        )
                        cur.execute(sql_fail_stale, (stale_err, last_msg_id, "publish"))

                # 3. Reserve slot atomically by marking publish stage 'processing'
                sql_reserve = self._format_sql(
                    """
                    INSERT INTO pipeline_items (message_id, stage, status, error)
                    VALUES (?, ?, 'processing', NULL)
                    ON CONFLICT (message_id, stage) DO UPDATE SET
                        status = 'processing',
                        error = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    """
                )
                cur.execute(sql_reserve, (message_id, "publish"))
                conn.commit()
                return True, 0.0, "reserved"
            except Exception:
                conn.rollback()
                raise

    def get_pipeline_summary(self, window_hours: int | None = None) -> dict[str, Any]:
        """Return high-level pipeline metrics and breakdown by stage and status."""
        since_dt = None
        if window_hours is not None:
            since_dt = datetime.now(timezone.utc) - timedelta(hours=window_hours)

        sql = self._format_sql(
            """
            SELECT message_id, stage, status, updated_at
            FROM pipeline_items
            """
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()

            total_messages_set: set[int] = set()
            stages_breakdown: dict[str, dict[str, int]] = {}
            review_queue_count = 0
            active_processing_count = 0
            latest_published_at = None

            for r in rows:
                try:
                    mid = r[0]
                    stg = r[1]
                    status = r[2]
                    up_val = r[3]
                except (IndexError, TypeError):
                    mid = r["message_id"]
                    stg = r["stage"]
                    status = r["status"]
                    up_val = r["updated_at"]
                dt = self._parse_datetime(up_val)

                if since_dt is not None and dt is not None and dt < since_dt:
                    continue

                total_messages_set.add(mid)

                stage_dict = stages_breakdown.setdefault(stg, {})
                stage_dict[status] = stage_dict.get(status, 0) + 1

                if stg == "qa_gate" and status == "held_for_review":
                    review_queue_count += 1
                if status == "processing":
                    active_processing_count += 1
                if stg == "publish" and status == "completed":
                    if dt is not None and (latest_published_at is None or dt > latest_published_at):
                        latest_published_at = dt

            if latest_published_at is None:
                latest_published_at = self.fetch_latest_published_timestamp()

            return {
                "window_hours": window_hours,
                "total_messages": len(total_messages_set),
                "stages": stages_breakdown,
                "review_queue_count": review_queue_count,
                "active_processing_count": active_processing_count,
                "latest_published_at": latest_published_at,
            }

    def fetch_recent_failures(
        self, limit: int = 10, window_hours: int | None = None
    ) -> list[dict[str, Any]]:
        """Return the most recent failed pipeline stages with error diagnostics."""
        since_dt = None
        if window_hours is not None:
            since_dt = datetime.now(timezone.utc) - timedelta(hours=window_hours)

        sql = self._format_sql(
            """
            SELECT message_id, stage, status, error, created_at, updated_at
            FROM pipeline_items
            WHERE status = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql, ("failed", limit if since_dt is None else limit * 5))
            rows = cur.fetchall()
            results = []
            for r in rows:
                d = self._row_to_dict(cur, r)
                if not d:
                    continue
                dt = self._parse_datetime(d.get("updated_at"))
                if since_dt is not None and dt is not None and dt < since_dt:
                    continue
                d["updated_at_dt"] = dt
                results.append(d)
                if len(results) >= limit:
                    break
            return results

    def count_consecutive_failures(self, limit: int = 10) -> int:
        """Return the number of consecutive failed pipeline items from most recent."""
        sql = self._format_sql(
            """
            SELECT message_id, status, updated_at
            FROM pipeline_items
            WHERE stage = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql, ("orchestration", limit))
            rows = cur.fetchall()
            if not rows:
                cur.execute(
                    self._format_sql(
                        """
                        SELECT message_id, status, updated_at
                        FROM pipeline_items
                        ORDER BY updated_at DESC
                        LIMIT ?
                        """
                    ),
                    (limit * 2,),
                )
                rows = cur.fetchall()

            seen_ids: set[int] = set()
            consecutive = 0
            for r in rows:
                try:
                    mid = r[0]
                    st = r[1]
                except (IndexError, TypeError):
                    mid = r["message_id"]
                    st = r["status"]

                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
                if st == "failed":
                    consecutive += 1
                else:
                    break
            return consecutive

    def fetch_review_queue_count(self) -> int:
        """Return total count of items currently held for review in qa_gate."""
        sql = self._format_sql(
            "SELECT COUNT(*) FROM pipeline_items WHERE stage = ? AND status = ?"
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql, ("qa_gate", "held_for_review"))
            row = cur.fetchone()
            if not row:
                return 0
            try:
                return int(row[0])
            except (IndexError, TypeError, KeyError):
                return int(row["count"])

    def fetch_review_queue_items(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return items currently held in qa_gate review queue ordered by updated_at DESC."""
        sql = self._format_sql(
            """
            SELECT message_id, stage, status, error, created_at, updated_at
            FROM pipeline_items
            WHERE stage = ? AND status = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """
        )
        with closing(self._connect()) as conn:
            cur = conn.cursor()
            cur.execute(sql, ("qa_gate", "held_for_review", limit))
            rows = cur.fetchall()
            return [d for r in rows if (d := self._row_to_dict(cur, r)) is not None]


