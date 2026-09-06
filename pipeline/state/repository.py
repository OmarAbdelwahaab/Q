"""State-store access for pipeline stages."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class PipelineStateRepository:
    """Persist stage status rows in a lightweight SQLite database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pipeline_items_status ON pipeline_items (status)"
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_pipeline_items_updated_at
                ON pipeline_items (updated_at)
                """
            )
            connection.commit()

    def upsert_stage(
        self,
        message_id: int,
        stage: str,
        status: str,
        error: str | None = None,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO pipeline_items (message_id, stage, status, error)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(message_id, stage) DO UPDATE SET
                    status = excluded.status,
                    error = excluded.error,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (message_id, stage, status, error),
            )
            connection.commit()

    def fetch_stage(self, message_id: int, stage: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT message_id, stage, status, created_at, updated_at, error
                FROM pipeline_items
                WHERE message_id = ? AND stage = ?
                """,
                (message_id, stage),
            ).fetchone()

        return dict(row) if row else None
