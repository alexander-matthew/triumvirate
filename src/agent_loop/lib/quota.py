"""Detect subscription-quota errors and persist a retry-after gate."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
from dataclasses import dataclass

from ..config import settings


# Codex prints (verbatim):
#   ERROR: You've hit your usage limit. Upgrade to Pro (...), visit ... or try
#   again at 9:24 PM.
# Claude's CLI message is similar but uses "Reset" / dates; we accept both
# absolute-time and ISO-ish forms. Gemini's CLI emits messages like
#   "Quota exceeded. Try again at 14:30." or RESOURCE_EXHAUSTED-style errors.
_CODEX_LIMIT = re.compile(
    r"You['’]ve hit your usage limit.*?try again at\s+(?P<time>[0-9: ]+\s*[APap][Mm])",
    re.S,
)
_CLAUDE_LIMIT_TIME = re.compile(
    r"(?:rate.?limit|usage limit|usage cap|quota).*?(?:reset|resets|retry|try again)\s*(?:at|on)?\s*"
    r"(?P<time>\d{1,2}:\d{2}\s*[APap][Mm]|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}|\d{1,2}:\d{2})",
    re.I | re.S,
)
_GEMINI_GENERIC_LIMIT = re.compile(
    r"(?:RESOURCE_EXHAUSTED|resource\s+exhausted|quota\s+exceeded|429|rateLimitExceeded)",
    re.I,
)


@dataclass(frozen=True)
class RateLimit:
    cli: str                # 'claude' | 'codex' | 'gemini'
    detected_at_ts: float
    retry_after_ts: float
    raw_message: str


def detect(stderr_text: str, *, cli: str) -> RateLimit | None:
    if not stderr_text:
        return None

    for pat in (_CODEX_LIMIT, _CLAUDE_LIMIT_TIME):
        m = pat.search(stderr_text)
        if not m:
            continue
        raw_time = m.group("time").strip()
        now = time.time()
        retry_ts = _parse_when(raw_time, now=now)
        if retry_ts is None or retry_ts <= now:
            continue
        return RateLimit(
            cli=cli, detected_at_ts=now, retry_after_ts=retry_ts,
            raw_message=stderr_text[m.start():m.end() + 80].strip(),
        )

    m = _GEMINI_GENERIC_LIMIT.search(stderr_text)
    if m:
        now = time.time()
        return RateLimit(
            cli=cli, detected_at_ts=now, retry_after_ts=now + 3600,
            raw_message=stderr_text[max(0, m.start() - 40):m.end() + 80].strip(),
        )

    return None


def _parse_when(text: str, *, now: float) -> float | None:
    text = text.strip()

    m = re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)
    if m:
        try:
            return dt.datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None

    m = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([APap][Mm])", text)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0

    today = dt.datetime.fromtimestamp(now)
    candidate = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += dt.timedelta(days=1)
    return candidate.timestamp()


def record(rl: RateLimit) -> None:
    from . import db  # local import to avoid cycles
    db.append(
        phase="quota", action="rate_limited",
        agent=rl.cli, outcome="skip_until_reset",
        notes={
            "retry_after_ts": rl.retry_after_ts,
            "retry_after_iso": dt.datetime.fromtimestamp(rl.retry_after_ts).isoformat(),
            "raw": rl.raw_message[:300],
        },
    )


def is_blocked(cli: str) -> tuple[bool, float | None]:
    """Returns (blocked, retry_after_ts). Reads the most recent rate-limit
    event for `cli` from the project's runs.sqlite.

    Uses `db._connect()` to ensure the schema is created on first use —
    otherwise a fresh project (no events yet) would crash on
    `OperationalError: no such table: events`.
    """
    from . import db  # local import to avoid cycle
    with db._connect() as conn:
        row = conn.execute(
            "SELECT notes FROM events "
            "WHERE phase='quota' AND agent=? AND action='rate_limited' "
            "ORDER BY ts DESC LIMIT 1",
            (cli,),
        ).fetchone()
    if not row or not row["notes"]:
        return False, None
    try:
        notes = json.loads(row["notes"])
    except Exception:
        return False, None
    ts = notes.get("retry_after_ts")
    if not isinstance(ts, (int, float)):
        return False, None
    if ts <= time.time():
        return False, None
    return True, float(ts)


def gate_message(cli: str) -> str:
    blocked, ts = is_blocked(cli)
    if not blocked:
        return f"{cli}: armed"
    iso = dt.datetime.fromtimestamp(ts).strftime("%H:%M")
    return f"{cli}: rate-limited until {iso}"


def earliest_retry_after(clis: list[str]) -> float | None:
    """Smallest `retry_after_ts` across the supplied CLIs that are currently
    blocked. Returns None if none are blocked. Used by the daemon to extend
    its sleep when every relevant persona is quota-gated — without this, the
    loop wakes every `tick_seconds` only to no-op for the full retry window.
    """
    soonest: float | None = None
    for cli in clis:
        blocked, ts = is_blocked(cli)
        if blocked and ts is not None and (soonest is None or ts < soonest):
            soonest = ts
    return soonest
