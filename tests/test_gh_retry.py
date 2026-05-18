"""Retry-on-transient-error behavior in lib.gh._run.

The agentdeck loop died overnight on a single `gh` HTTP 401 — `_run` raised
GhError on first failure with no retry. These tests pin the fix.
"""
from __future__ import annotations

import subprocess
from typing import Any

import pytest

from agent_loop.lib import gh


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh.time, "sleep", lambda _s: None)


def _patch_run(monkeypatch: pytest.MonkeyPatch, results: list[_FakeProc]) -> list[list[str]]:
    calls: list[list[str]] = []
    it = iter(results)

    def fake(argv: list[str], *_a: Any, **_kw: Any) -> _FakeProc:
        calls.append(argv)
        return next(it)

    monkeypatch.setattr(subprocess, "run", fake)
    return calls


def test_success_first_try(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch, [_FakeProc(0, stdout="ok\n")])
    assert gh._run(["pr", "list"]).strip() == "ok"
    assert len(calls) == 1


def test_retries_on_http_401(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch, [
        _FakeProc(1, stderr="HTTP 401: Bad credentials"),
        _FakeProc(0, stdout="recovered\n"),
    ])
    assert gh._run(["pr", "list"]).strip() == "recovered"
    assert len(calls) == 2


def test_retries_on_429_and_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch, [
        _FakeProc(1, stderr="HTTP 429: rate limit"),
        _FakeProc(1, stderr="HTTP 502 bad gateway"),
        _FakeProc(0, stdout="ok\n"),
    ])
    assert gh._run(["api", "/x"]).strip() == "ok"
    assert len(calls) == 3


def test_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch, [
        _FakeProc(1, stderr="HTTP 500 server error") for _ in range(10)
    ])
    with pytest.raises(gh.GhError):
        gh._run(["pr", "list"])
    # 1 initial + len(_RETRY_DELAYS_S) retries
    assert len(calls) == 1 + len(gh._RETRY_DELAYS_S)


def test_does_not_retry_non_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch, [
        _FakeProc(1, stderr="HTTP 404: not found"),
    ])
    with pytest.raises(gh.GhError):
        gh._run(["pr", "view", "999999"])
    assert len(calls) == 1


def test_check_false_returns_stdout_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, [_FakeProc(1, stdout="partial", stderr="HTTP 500")])
    # `check=False` should not retry, not raise — matches prior contract.
    assert gh._run(["pr", "list"], check=False) == "partial"
