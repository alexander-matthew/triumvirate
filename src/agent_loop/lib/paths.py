"""Path constants are derived from Settings — no project-specific hardcoding.

Older code referenced module-level constants like REPO_ROOT, DB_PATH, etc.
These have moved to `agent_loop.config.Settings` properties so the framework
is portable across projects. Kept as thin re-exports for backward-compat.
"""
from __future__ import annotations

from pathlib import Path

from ..config import settings


def repo_root() -> Path:
    return settings().project_root


def state_dir() -> Path:
    return settings().state_dir


def db_path() -> Path:
    return settings().db_path


def lock_path() -> Path:
    return settings().lock_path


def stop_path() -> Path:
    return settings().stop_path


def worktrees_dir() -> Path:
    return settings().worktrees_dir


def personas_dir() -> Path:
    return settings().personas_dir


def ensure_state_dir() -> None:
    s = settings()
    s.state_dir.mkdir(parents=True, exist_ok=True)
    s.worktrees_dir.mkdir(parents=True, exist_ok=True)
