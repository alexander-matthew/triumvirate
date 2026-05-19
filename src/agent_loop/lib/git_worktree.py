"""Isolated git worktrees for parallel agent runs."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from ..config import settings
from . import bots
from .paths import ensure_state_dir


def _git(*args: str, cwd: Path | str | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd or settings().project_root,
        capture_output=True, text=True, check=True,
    )
    return proc.stdout


def _apply_bot_identity(path: Path, as_cli: str) -> None:
    """Set worktree-local user.name/user.email so commits attribute to the bot.

    Worktrees inherit config from the parent repo by default; setting these
    inside the worktree dir overrides only for this worktree's commits.
    """
    if not bots.is_configured(as_cli):
        return
    login = bots.bot_login(as_cli)
    email = bots.bot_email(as_cli)
    _git("config", "user.name", login, cwd=path)
    _git("config", "user.email", email, cwd=path)


def create(branch: str, *, base: str = "origin/main", as_cli: Optional[str] = None) -> Path:
    """Create a fresh worktree on `branch` based on `base`. Returns the path.

    If `as_cli` is given AND its bot is configured, the worktree's local
    user.name/user.email are set to that bot — so any commits the agent
    makes inside this worktree are attributed to e.g. claude-bot[bot].

    Uses `worktree add -B` so a leftover local branch from a previous failed
    run is force-reset to `base` rather than colliding."""
    ensure_state_dir()
    path = settings().worktrees_dir / branch.replace("/", "_")
    if path.exists():
        cleanup(path)
    _git("fetch", "origin", "main", "--quiet")
    _git("worktree", "add", "-B", branch, str(path), base)
    if as_cli:
        _apply_bot_identity(path, as_cli)
    return path


def cleanup(path: Path, *, delete_branch: bool = False) -> None:
    """Remove a worktree (force) and prune git's bookkeeping.

    `delete_branch=True` also force-deletes the local branch reference, which
    is what reviewer/responder scratch worktrees want. The worker leaves
    `delete_branch=False` because its branch IS the PR's remote-tracking branch."""
    branch = None
    if delete_branch:
        proc = subprocess.run(
            ["git", "-C", str(path), "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True,
        )
        if proc.returncode == 0:
            branch = proc.stdout.strip()

    repo_root = settings().project_root
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=repo_root, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=repo_root, capture_output=True, text=True,
    )
    if branch:
        subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=repo_root, capture_output=True, text=True,
        )


def push(path: Path, branch: str, *, as_cli: Optional[str] = None) -> None:
    """Push the worktree's branch to origin.

    If `as_cli` is given AND its bot is configured, authenticate the push
    with that bot's installation token (via http.extraheader). Otherwise
    fall back to the host's normal git credentials (ssh key / cached PAT).
    """
    if as_cli and bots.is_configured(as_cli):
        repo = settings().repo
        if not repo:
            raise RuntimeError("push as_cli requires [project].repo in config.toml")
        # Bypass the configured `origin` (which may be SSH) and push over
        # HTTPS with the bot's installation token in an extra header. Token
        # stays out of argv / reflog because http.extraheader is in-process.
        header = bots.http_extraheader(as_cli, repo=repo)
        url = f"https://github.com/{repo}.git"
        _git("-c", f"http.extraheader={header}",
             "push", url, f"HEAD:refs/heads/{branch}", cwd=path)
        return
    _git("push", "-u", "origin", branch, cwd=path)


def has_commits_since_base(path: Path, base: str = "origin/main") -> bool:
    out = _git("rev-list", "--count", f"{base}..HEAD", cwd=path).strip()
    return int(out or "0") > 0


def changed_paths(path: Path, base: str = "origin/main") -> list[str]:
    out = _git("diff", "--name-only", f"{base}...HEAD", cwd=path)
    return [line for line in out.splitlines() if line]


def diff_stats(path: Path, base: str = "origin/main") -> tuple[int, int]:
    out = _git("diff", "--shortstat", f"{base}...HEAD", cwd=path).strip()
    adds = dels = 0
    for tok in out.split(","):
        tok = tok.strip()
        if "insertion" in tok:
            adds = int(tok.split()[0])
        elif "deletion" in tok:
            dels = int(tok.split()[0])
    return adds, dels
