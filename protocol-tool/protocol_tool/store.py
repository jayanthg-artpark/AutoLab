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
from typing import Any

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
            # The scientist's rating of a finished run and what to change next time.
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS run_feedback (
                       run_id TEXT PRIMARY KEY,
                       rating TEXT NOT NULL,
                       notes TEXT NOT NULL,
                       updated_at TEXT NOT NULL)"""
            )
            # Runs already written to the experiment notebook, so a record someone deleted stays deleted.
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS experiment_records (
                       run_id TEXT PRIMARY KEY,
                       doc TEXT NOT NULL,
                       written_at TEXT NOT NULL)"""
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

    def designs(self, limit: int = 200) -> list[dict[str, Any]]:
        """Saved designs with a conversation, most recently changed first, for reopening one."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit * 2,)
            ).fetchall()
        found = []
        for (doc,) in rows:
            data = json.loads(doc)
            transcript = data.get("transcript") or []
            if not transcript:
                continue  # opened and never used
            design = data.get("design") or {}
            first = next((e.get("text", "") for e in transcript if e.get("role") == "user"), "")
            title = design.get("title") or (design.get("requirements") or {}).get("assay") or first
            found.append({
                "id": data["id"],
                "title": " ".join(title.split())[:120],
                "first_message": " ".join(first.split())[:200],
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "messages": len(transcript),
                "protocols": len(design.get("protocols") or []),
                "runs": len(data.get("submitted_runs") or []),
            })
            if len(found) == limit:
                break
        return found

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

    def run_feedback(self, run_id: str) -> dict[str, str] | None:
        with self._lock:
            row = self._conn.execute("SELECT rating, notes, updated_at FROM run_feedback WHERE run_id = ?", (run_id,)).fetchone()
        return {"run_id": run_id, "rating": row[0], "notes": row[1], "updated_at": row[2]} if row else None

    def save_run_feedback(self, run_id: str, rating: str, notes: str) -> dict[str, str]:
        now = utcnow().isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO run_feedback (run_id, rating, notes, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT (run_id) DO UPDATE SET rating = excluded.rating, notes = excluded.notes,
                       updated_at = excluded.updated_at""",
                (run_id, rating, notes, now),
            )
        return {"run_id": run_id, "rating": rating, "notes": notes, "updated_at": now}

    def experiment_record(self, run_id: str) -> str | None:
        """The knowledge document id of the run's experiment record, if one was written."""
        with self._lock:
            row = self._conn.execute("SELECT doc FROM experiment_records WHERE run_id = ?", (run_id,)).fetchone()
        return row[0] if row else None

    def save_experiment_record(self, run_id: str, doc: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO experiment_records (run_id, doc, written_at) VALUES (?, ?, ?)
                   ON CONFLICT (run_id) DO UPDATE SET doc = excluded.doc, written_at = excluded.written_at""",
                (run_id, doc, utcnow().isoformat()),
            )

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
