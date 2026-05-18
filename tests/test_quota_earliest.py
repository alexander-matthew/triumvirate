"""quota.earliest_retry_after — the daemon's adaptive-sleep signal."""
from __future__ import annotations

import time

import pytest

from agent_loop.lib import quota


def test_returns_none_when_nothing_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quota, "is_blocked", lambda _cli: (False, None))
    assert quota.earliest_retry_after(["claude", "codex", "gemini"]) is None


def test_returns_min_ts_across_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    table = {
        "claude": (True, now + 600),
        "codex": (True, now + 300),
        "gemini": (False, None),
    }
    monkeypatch.setattr(quota, "is_blocked", lambda cli: table[cli])
    soonest = quota.earliest_retry_after(list(table.keys()))
    assert soonest == pytest.approx(now + 300)


def test_ignores_unblocked_even_with_stale_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    now = time.time()
    table = {
        "claude": (False, now + 5),  # is_blocked says no — ignore the ts
        "codex": (True, now + 9999),
    }
    monkeypatch.setattr(quota, "is_blocked", lambda cli: table[cli])
    assert quota.earliest_retry_after(list(table.keys())) == pytest.approx(now + 9999)
