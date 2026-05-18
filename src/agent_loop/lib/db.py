"""Append-only event log for the agent loop. SQLite, single file per project."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .paths import ensure_state_dir
from ..config import settings


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    phase        TEXT NOT NULL,
    agent        TEXT,
    issue_number INTEGER,
    pr_number    INTEGER,
    action       TEXT NOT NULL,
    outcome      TEXT,
    duration_s   REAL,
    exit_code    INTEGER,
    notes        TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_phase_ts ON events(phase, ts);
CREATE INDEX IF NOT EXISTS idx_events_pr ON events(pr_number);
"""


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    ensure_state_dir()
    conn = sqlite3.connect(settings().db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def append(
    *,
    phase: str,
    action: str,
    agent: str | None = None,
    issue_number: int | None = None,
    pr_number: int | None = None,
    outcome: str | None = None,
    duration_s: float | None = None,
    exit_code: int | None = None,
    notes: dict | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO events "
            "(ts, phase, agent, issue_number, pr_number, action, outcome, duration_s, exit_code, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(), phase, agent, issue_number, pr_number,
                action, outcome, duration_s, exit_code,
                json.dumps(notes) if notes else None,
            ),
        )


def recent(limit: int = 50) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def events_for_pr(pr_number: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE pr_number = ? ORDER BY ts ASC", (pr_number,)
        ).fetchall()
    return [dict(r) for r in rows]


def today_summary() -> dict[str, int]:
    today_start = time.time() - (time.time() % 86400)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT phase, action, COUNT(*) c FROM events WHERE ts >= ? "
            "GROUP BY phase, action ORDER BY phase, action",
            (today_start,),
        ).fetchall()
    return {f"{r['phase']}.{r['action']}": r["c"] for r in rows}
