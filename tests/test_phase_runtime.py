"""Tests for the isolated_worktree context manager."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from agent_loop.lib.phase_runtime import isolated_worktree


@pytest.fixture
def fake_worktree(monkeypatch):
    """Stub git_worktree.create + cleanup so tests don't touch the filesystem."""
    created = MagicMock(return_value=Path("/tmp/fake-worktree"))
    cleanup = MagicMock()
    monkeypatch.setattr("agent_loop.lib.phase_runtime.git_worktree.create", created)
    monkeypatch.setattr("agent_loop.lib.phase_runtime.git_worktree.cleanup", cleanup)
    return {"create": created, "cleanup": cleanup}


def test_yields_worktree_and_cleans_up_on_success(fake_worktree):
    with patch("agent_loop.lib.phase_runtime.subprocess.run") as run:
        with isolated_worktree("scratch-1") as wt:
            assert wt == Path("/tmp/fake-worktree")

    fake_worktree["create"].assert_called_once_with(
        "scratch-1", base="origin/main", as_cli=None,
    )
    fake_worktree["cleanup"].assert_called_once_with(
        Path("/tmp/fake-worktree"), delete_branch=True,
    )
    # No checkout_branch → no subprocess calls.
    run.assert_not_called()


def test_cleanup_runs_even_when_body_raises(fake_worktree):
    with pytest.raises(RuntimeError, match="boom"):
        with isolated_worktree("scratch-1"):
            raise RuntimeError("boom")
    fake_worktree["cleanup"].assert_called_once_with(
        Path("/tmp/fake-worktree"), delete_branch=True,
    )


def test_fetches_and_checks_out_branch_when_requested(fake_worktree):
    with patch("agent_loop.lib.phase_runtime.subprocess.run") as run:
        run.return_value.returncode = 0
        with isolated_worktree("scratch-2", checkout_branch="feature/foo"):
            pass

    assert run.call_args_list == [
        call(
            ["git", "fetch", "origin", "feature/foo:feature/foo", "--force"],
            cwd=Path("/tmp/fake-worktree"), check=True, capture_output=True,
        ),
        call(
            ["git", "checkout", "feature/foo"],
            cwd=Path("/tmp/fake-worktree"), check=True, capture_output=True,
        ),
    ]


def test_as_cli_forwarded_to_create(fake_worktree):
    with isolated_worktree("scratch-3", as_cli="claude"):
        pass
    fake_worktree["create"].assert_called_once_with(
        "scratch-3", base="origin/main", as_cli="claude",
    )


def test_delete_branch_flag_forwarded_to_cleanup(fake_worktree):
    with isolated_worktree("scratch-4", delete_branch_on_cleanup=False):
        pass
    fake_worktree["cleanup"].assert_called_once_with(
        Path("/tmp/fake-worktree"), delete_branch=False,
    )
