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

CREATE TABLE IF NOT EXISTS verdicts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    pr_number    INTEGER NOT NULL,
    marker_type  TEXT NOT NULL,
    cli          TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    head_sha     TEXT,
    round_n      INTEGER,
    raw_body     TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdicts_pr_ts ON verdicts(pr_number, ts);
CREATE INDEX IF NOT EXISTS idx_verdicts_cli ON verdicts(cli, marker_type, ts);
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


def record_verdict(
    *,
    pr_number: int,
    marker_type: str,           # 'review' | 'arbiter' | 'security' | 'audit'
    cli: str,
    verdict: str,
    head_sha: str | None = None,
    round_n: int | None = None,
    raw_body: str | None = None,
) -> None:
    """Mirror a verdict comment into the local DB.

    Phases call this after successfully posting a marker comment to
    GitHub. Lets the orchestrator + metrics commands read verdict state
    locally instead of round-tripping through ``gh api`` every tick.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO verdicts "
            "(ts, pr_number, marker_type, cli, verdict, head_sha, round_n, raw_body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), pr_number, marker_type, cli, verdict,
             head_sha, round_n, raw_body),
        )


def verdicts_for_pr(pr_number: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM verdicts WHERE pr_number = ? ORDER BY ts ASC",
            (pr_number,),
        ).fetchall()
    return [dict(r) for r in rows]


def verdict_distribution(marker_type: str) -> dict[str, dict[str, int]]:
    """Count verdicts grouped by cli → {verdict_value: count}.

    Used by ``agent-loop metrics``. E.g. for marker_type='review':
        {'codex':  {'APPROVE': 12, 'REQUEST_CHANGES': 3},
         'gemini': {'APPROVE': 10, 'REQUEST_CHANGES': 5}}
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cli, verdict, COUNT(*) c FROM verdicts "
            "WHERE marker_type = ? GROUP BY cli, verdict ORDER BY cli, verdict",
            (marker_type,),
        ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["cli"], {})[r["verdict"]] = r["c"]
    return out


def consensus_rounds(required_clis: tuple[str, ...]) -> list[int]:
    """For each PR that reached consensus, the round at which it happened.

    "Consensus" = every ``required_clis`` has at least one APPROVE
    verdict. The round reported is the highest round_n among the APPROVE
    verdicts at consensus (i.e. how many rounds it took to converge).

    Returns an empty list if no PR has reached consensus yet.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT pr_number, cli, MIN(round_n) round_n FROM verdicts "
            "WHERE marker_type = 'review' AND verdict = 'APPROVE' "
            "AND round_n IS NOT NULL "
            "GROUP BY pr_number, cli",
        ).fetchall()
    by_pr: dict[int, dict[str, int]] = {}
    for r in rows:
        by_pr.setdefault(r["pr_number"], {})[r["cli"]] = r["round_n"]
    out: list[int] = []
    for pr_n, per_cli in by_pr.items():
        if all(c in per_cli for c in required_clis):
            # Round at which all required CLIs had approved.
            out.append(max(per_cli[c] for c in required_clis))
    return sorted(out)


def today_summary() -> dict[str, int]:
    today_start = time.time() - (time.time() % 86400)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT phase, action, COUNT(*) c FROM events WHERE ts >= ? "
            "GROUP BY phase, action ORDER BY phase, action",
            (today_start,),
        ).fetchall()
    return {f"{r['phase']}.{r['action']}": r["c"] for r in rows}
