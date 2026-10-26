"""SQLite persistence for design sessions. Each session is one JSON document.

Recently used sessions stay in memory and every caller gets the same object,
so an edit made while Claude is mid-reply (the scientist changing the reader
settings) is not overwritten when the chat turn saves.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import OrderedDict

from .models import Session, utcnow


class SessionStore:
    def __init__(self, path: str, cache_size: int = 200):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, Session] = OrderedDict()
        self._cache_size = cache_size
        with self._lock, self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                       id TEXT PRIMARY KEY,
                       updated_at TEXT NOT NULL,
                       doc TEXT NOT NULL)"""
            )
            # The AI model's summary of a finished run ("Explain" on the Runs tab).
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS run_notes (
                       run_id TEXT PRIMARY KEY,
                       status TEXT NOT NULL,
                       text TEXT NOT NULL,
                       created_at TEXT NOT NULL)"""
            )
            # The design exactly as it was sent as each run, so it can be opened and run again
            # later, even if the scientist kept changing the design afterwards.
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS run_designs (
                       run_id TEXT PRIMARY KEY,
                       session_id TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       send TEXT NOT NULL,
                       doc TEXT NOT NULL)"""
            )

    def save(self, session: Session) -> None:
        session.updated_at = utcnow()
        self._remember(session)
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO sessions (id, updated_at, doc) VALUES (?, ?, ?)
                   ON CONFLICT (id) DO UPDATE SET updated_at = excluded.updated_at, doc = excluded.doc""",
                (session.id, session.updated_at.isoformat(), session.model_dump_json()),
            )

    def get(self, session_id: str) -> Session | None:
        if session_id in self._cache:
            self._cache.move_to_end(session_id)
            return self._cache[session_id]
        with self._lock:
            row = self._conn.execute("SELECT doc FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return None
        session = Session.model_validate_json(row[0])
        self._remember(session)
        return session

    def run_note(self, run_id: str) -> dict[str, str] | None:
        with self._lock:
            row = self._conn.execute("SELECT status, text, created_at FROM run_notes WHERE run_id = ?", (run_id,)).fetchone()
        return {"run_id": run_id, "status": row[0], "text": row[1], "created_at": row[2]} if row else None

    def save_run_note(self, run_id: str, status: str, text: str) -> dict[str, str]:
        now = utcnow().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO run_notes (run_id, status, text, created_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT (run_id) DO UPDATE SET status = excluded.status, text = excluded.text,
                       created_at = excluded.created_at""",
                (run_id, status, text, now),
            )
        return {"run_id": run_id, "status": status, "text": text, "created_at": now}

    def save_run_design(self, run_id: str, session: Session, send: dict) -> None:
        """Snapshot of the design (and the run details entered) as it was sent as `run_id`."""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO run_designs (run_id, session_id, created_at, send, doc) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (run_id) DO UPDATE SET session_id = excluded.session_id, send = excluded.send, doc = excluded.doc""",
                (run_id, session.id, utcnow().isoformat(), json.dumps(send), session.model_dump_json()),
            )

    def run_design(self, run_id: str) -> tuple[Session, dict, str] | None:
        """The design a run was sent from: (session, send details, "snapshot" | "current").

        Runs sent before snapshots existed fall back to the session that sent them, as it is now."""
        with self._lock:
            row = self._conn.execute("SELECT doc, send FROM run_designs WHERE run_id = ?", (run_id,)).fetchone()
        if row:
            return Session.model_validate_json(row[0]), json.loads(row[1]), "snapshot"
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE doc LIKE ? ORDER BY updated_at DESC", (f'%"{run_id}"%',)
            ).fetchall()
        for (session_id,) in rows:
            session = self.get(session_id)
            if session and run_id in session.submitted_runs:
                return session, {}, "current"
        return None

    def _remember(self, session: Session) -> None:
        self._cache[session.id] = session
        self._cache.move_to_end(session.id)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def close(self) -> None:
        self._conn.close()
