from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class StateStore:
    """Small SQLite store used to resume downloads across sessions."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                message_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL,
                category TEXT,
                file_path TEXT,
                file_size INTEGER,
                extracted_path TEXT,
                error TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.connection.commit()

    def get(self, message_id: int) -> sqlite3.Row | None:
        cursor = self.connection.execute(
            "SELECT * FROM downloads WHERE message_id = ?",
            (message_id,),
        )
        return cursor.fetchone()

    def mark_done(
        self,
        message_id: int,
        category: str,
        file_path: Path,
        file_size: int | None,
        extracted_path: Path | None = None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO downloads (
                message_id, status, category, file_path, file_size, extracted_path, error
            )
            VALUES (?, 'done', ?, ?, ?, ?, NULL)
            ON CONFLICT(message_id) DO UPDATE SET
                status = excluded.status,
                category = excluded.category,
                file_path = excluded.file_path,
                file_size = excluded.file_size,
                extracted_path = excluded.extracted_path,
                error = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                message_id,
                category,
                str(file_path),
                file_size,
                str(extracted_path) if extracted_path else None,
            ),
        )
        self.connection.commit()

    def mark_failed(
        self,
        message_id: int,
        category: str,
        file_path: Path | None,
        error_message: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO downloads (message_id, status, category, file_path, error)
            VALUES (?, 'failed', ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                status = excluded.status,
                category = excluded.category,
                file_path = excluded.file_path,
                error = excluded.error,
                updated_at = CURRENT_TIMESTAMP
            """,
            (message_id, category, str(file_path) if file_path else None, error_message),
        )
        self.connection.commit()

    def mark_skipped(
        self,
        message_id: int,
        category: str,
        file_path: Path,
        file_size: int | None,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO downloads (message_id, status, category, file_path, file_size, error)
            VALUES (?, 'skipped', ?, ?, ?, NULL)
            ON CONFLICT(message_id) DO UPDATE SET
                status = excluded.status,
                category = excluded.category,
                file_path = excluded.file_path,
                file_size = excluded.file_size,
                error = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (message_id, category, str(file_path), file_size),
        )
        self.connection.commit()

    def list_history(
        self,
        *,
        statuses: tuple[str, ...] = ("done", "skipped"),
        limit: int | None = 2000,
    ) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in statuses)
        sql = f"""
            SELECT message_id, status, category, file_path, file_size, extracted_path, error, updated_at
            FROM downloads
            WHERE status IN ({placeholders})
            ORDER BY updated_at DESC
        """
        params: list[Any] = list(statuses)
        if limit is not None:
            sql += "\nLIMIT ?"
            params.append(limit)
        cursor = self.connection.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def list_completed(self, limit: int = 2000) -> list[dict[str, Any]]:
        return self.list_history(statuses=("done", "skipped"), limit=limit)

    def list_failed(self, limit: int | None = 500) -> list[dict[str, Any]]:
        sql = """
            SELECT message_id, status, category, file_path, file_size, extracted_path, error, updated_at
            FROM downloads
            WHERE status = 'failed'
            ORDER BY updated_at DESC
        """
        params: list[Any] = []
        if limit is not None:
            sql += "\nLIMIT ?"
            params.append(limit)
        cursor = self.connection.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

    def counts(self) -> dict[str, int]:
        cursor = self.connection.execute(
            """
            SELECT status, COUNT(*) AS total
            FROM downloads
            GROUP BY status
            """
        )
        return {str(row["status"]): int(row["total"]) for row in cursor.fetchall()}

    def close(self) -> None:
        self.connection.close()
