"""State-store access for pipeline stages supporting SQLite and PostgreSQL backends."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class PipelineStateRepository:
    """Persist stage status rows in SQLite (default/local) or PostgreSQL (multi-container)."""

    def __init__(
        self,
        database_path: Path | str | None = None,
        database_url: str | None = None,
    ) -> None:
        self.database_url = database_url
        if database_url:
            self.backend = "postgres"
            self.database_path = None
        else:
            self.backend = "sqlite"
            self.database_path = Path(database_path or "pipeline/state/pipeline.db")
            self.database_path.parent.mkdir(parents=True, exist_ok=True)

        self._placeholder = "%s" if self.backend == "postgres" else "?"
        self._initialize()

    def _connect(self) -> Any:
        if self.backend == "postgres":
            try:
                import psycopg  # psycopg 3
                return psycopg.connect(self.database_url)
            except ImportError:
                pass
            try:
                import psycopg2  # psycopg 2
                return psycopg2.connect(self.database_url)
            except ImportError as exc:
                raise ImportError(
                    "PostgreSQL database URL provided, but neither 'psycopg' nor 'psycopg2' is installed. "
                    "Install 'psycopg[binary]' to connect to PostgreSQL."
                ) from exc
        else:
            connection = sqlite3.connect(self.database_path)
            connection.row_factory = sqlite3.Row
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
            if isinstance(res, datetime):
                return res if res.tzinfo else res.replace(tzinfo=timezone.utc)
            if isinstance(res, str):
                try:
                    dt = datetime.fromisoformat(res)
                    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    # SQLite CURRENT_TIMESTAMP format: "YYYY-MM-DD HH:MM:SS"
                    dt = datetime.strptime(res, "%Y-%m-%d %H:%M:%S")
                    return dt.replace(tzinfo=timezone.utc)
            return None

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

