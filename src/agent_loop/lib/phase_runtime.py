"""Shared per-phase plumbing: worktree create/checkout/cleanup as a CM.

Every read-only audit phase (review, arbitrate, security, librarian) and
every scratch-only phase (proposer, drift, triage) repeats the same
ceremony:

1. ``git_worktree.create(<scratch-name>, base="origin/main", as_cli=...)``
2. (audit only) fetch the PR's head branch + ``git checkout`` it inside
   the worktree.
3. Run an agent.
4. Always ``git_worktree.cleanup(_, delete_branch=True)`` — even if the
   agent failed, raised, or was killed.

This module exposes that as a single context manager so individual
phases don't need to repeat the try/finally + subprocess incantations.
The engineer + responder phases (which push commits back) still call
``git_worktree`` directly because their post-run logic is non-trivial.
"""
from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import git_worktree


@contextmanager
def isolated_worktree(
    scratch_name: str,
    *,
    base: str = "origin/main",
    as_cli: str | None = None,
    checkout_branch: str | None = None,
    delete_branch_on_cleanup: bool = True,
) -> Iterator[Path]:
    """Yield a fresh worktree, cleaning it up unconditionally on exit.

    Parameters
    ----------
    scratch_name:
        The local branch name to create the worktree on (e.g.
        ``f"review-{pr_number}-r{round_n}"``). The worktree directory is
        derived from this name by ``git_worktree.create``.
    base:
        Git revision the worktree's scratch branch is based on. Default
        ``origin/main``.
    as_cli:
        If given and the matching bot is configured, the worktree's local
        user.name / user.email are set to that bot. See
        ``git_worktree.create``.
    checkout_branch:
        If given, the named remote branch is fetched and checked out
        inside the worktree (overlaying the scratch branch). This is the
        pattern audit phases use to inspect a PR's head.
    delete_branch_on_cleanup:
        Passed through to ``git_worktree.cleanup``. Defaults to True since
        scratch branches are always disposable.
    """
    worktree = git_worktree.create(scratch_name, base=base, as_cli=as_cli)
    try:
        if checkout_branch is not None:
            subprocess.run(
                ["git", "fetch", "origin", f"{checkout_branch}:{checkout_branch}", "--force"],
                cwd=worktree, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", checkout_branch],
                cwd=worktree, check=True, capture_output=True,
            )
        yield worktree
    finally:
        git_worktree.cleanup(worktree, delete_branch=delete_branch_on_cleanup)
