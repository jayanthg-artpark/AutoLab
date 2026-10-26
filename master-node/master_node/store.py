"""SQLite persistence for runs. Each run is stored as one JSON document."""

from __future__ import annotations

import sqlite3
import threading

from .models import Run, RunStatus, utcnow


class RunStore:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS runs (
                       id TEXT PRIMARY KEY,
                       created_at TEXT NOT NULL,
                       status TEXT NOT NULL,
                       doc TEXT NOT NULL)"""
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS runs_created ON runs (created_at)")

    def save(self, run: Run) -> None:
        run.updated_at = utcnow()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO runs (id, created_at, status, doc) VALUES (?, ?, ?, ?)
                   ON CONFLICT (id) DO UPDATE SET status = excluded.status, doc = excluded.doc""",
                (run.id, run.created_at.isoformat(), run.status.value, run.model_dump_json()),
            )

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._conn.execute("SELECT doc FROM runs WHERE id = ?", (run_id,)).fetchone()
        return Run.model_validate_json(row[0]) if row else None

    def list(self, *, status: RunStatus | None = None, limit: int = 50) -> list[Run]:
        """Newest first."""
        query = "SELECT doc FROM runs"
        args: tuple = ()
        if status is not None:
            query += " WHERE status = ?"
            args = (status.value,)
        query += " ORDER BY created_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (*args, limit)).fetchall()
        return [Run.model_validate_json(row[0]) for row in rows]

    def close(self) -> None:
        self._conn.close()
