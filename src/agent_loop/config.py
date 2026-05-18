"""Per-project configuration. Loaded from `agents/config.toml` at the project root.

The framework code (everything in `agent_loop.lib` and `agent_loop.phases`)
consults `Settings` instead of holding any project-specific constants. To use
the loop in a new project, drop a `config.toml` and persona files in
`agents/` and point the CLI at that project root.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def project_root() -> Path:
    """Where the loop is being run against.

    Order of precedence:
      1. AGENT_LOOP_PROJECT_ROOT env var (absolute path)
      2. Walk up from cwd looking for `agents/config.toml`
      3. Cwd itself

    The systemd unit explicitly sets the env var so daemon runs are
    deterministic; interactive `agent-loop tick` finds the project by
    walking up.
    """
    env = os.environ.get("AGENT_LOOP_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd().resolve()
    for d in (cwd, *cwd.parents):
        if (d / "agents" / "config.toml").exists():
            return d
    return cwd


@dataclass(frozen=True)
class Settings:
    """All per-project configuration. Frozen — instantiate once, pass around."""

    # --- project identity ---
    project_root: Path
    project_name: str
    repo: str                       # "owner/name" — passed to gh commands implicitly via repo cwd
    trusted_authors: frozenset[str]

    # --- time gates ---
    off_hours_start: int            # 24h, inclusive
    off_hours_end: int              # 24h, exclusive
    tick_seconds: int

    # --- diff / round caps ---
    max_diff_loc: int
    max_review_rounds: int

    # --- guard rails ---
    protected_paths: tuple[str, ...]
    sensitive_path_prefixes: tuple[str, ...]   # security check
    librarian_trigger_paths: tuple[str, ...]   # cross-project check

    # --- reviewer consensus ---
    required_reviewer_clis: tuple[str, ...]

    # --- labels ---
    labels: dict[str, str]          # logical name → actual GitHub label string

    # --- directory layout ---
    personas_dir: Path
    state_dir: Path

    @property
    def db_path(self) -> Path:
        return self.state_dir / "runs.sqlite"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "loop.lock"

    @property
    def stop_path(self) -> Path:
        return self.project_root / "agents" / "STOP"

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

    def label(self, name: str) -> str:
        """Look up an actual GitHub label string by logical name. Falls back
        to a sensible default if the project's config.toml didn't declare it.

        Logical names the framework uses:
          approved, in_progress, proposal, halt, veto, needs_human,
          protected_violation, too_large, authored_by_claude,
          security_cleared, security_flag, librarian_cleared, librarian_flag.
        """
        return self.labels.get(name, _DEFAULT_LABELS[name])


_DEFAULT_LABELS = {
    "approved": "agent:approved",
    "in_progress": "agent:in-progress",
    "proposal": "agent:proposal",
    "halt": "agent:halt",
    "veto": "agent:veto",
    "needs_human": "agent:needs-human",
    "protected_violation": "agent:protected-violation",
    "too_large": "agent:too-large",
    "authored_by_claude": "agent:authored-by-claude",
    "security_cleared": "agent:security-cleared",
    "security_flag": "agent:security-flag",
    "librarian_cleared": "agent:librarian-cleared",
    "librarian_flag": "agent:librarian-flag",
}


def load(config_path: Path | None = None) -> Settings:
    """Load Settings from `config_path` (or auto-discover under project_root)."""
    root = project_root()
    if config_path is None:
        config_path = root / "agents" / "config.toml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"agent-loop config not found at {config_path}. "
            f"See README.md for the bootstrap commands."
        )

    data = tomllib.loads(config_path.read_text())

    proj = data.get("project", {})
    paths = data.get("paths", {})
    guards = data.get("guards", {})
    security = data.get("security", {})
    librarian = data.get("librarian", {})
    reviewers = data.get("reviewers", {})
    labels = data.get("labels", {})

    personas_dir = root / paths.get("personas_dir", "agents/personas")
    state_dir = root / paths.get("state_dir", "agents/state")

    return Settings(
        project_root=root,
        project_name=proj.get("name", root.name),
        repo=proj.get("repo", ""),
        trusted_authors=frozenset(a.lower() for a in proj.get("trusted_authors", [])),
        off_hours_start=int(proj.get("off_hours_start", 23)),
        off_hours_end=int(proj.get("off_hours_end", 6)),
        tick_seconds=int(proj.get("tick_seconds", 60)),
        max_diff_loc=int(guards.get("max_diff_loc", 400)),
        max_review_rounds=int(guards.get("max_review_rounds", 3)),
        protected_paths=tuple(guards.get("protected_paths", ())),
        sensitive_path_prefixes=tuple(security.get("sensitive_path_prefixes", ())),
        librarian_trigger_paths=tuple(librarian.get("trigger_paths", ())),
        required_reviewer_clis=tuple(reviewers.get("required_clis", ("codex", "gemini"))),
        labels={**_DEFAULT_LABELS, **labels},
        personas_dir=personas_dir.resolve(),
        state_dir=state_dir.resolve(),
    )


# ---- process-wide singleton -------------------------------------------------
# Settings is effectively immutable for a process lifetime. Phase scripts and
# lib modules call `settings()` to get the same instance.

_INSTANCE: Settings | None = None


def settings() -> Settings:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = load()
    return _INSTANCE


def reset() -> None:
    """For tests: drop the cached settings so the next `settings()` reloads."""
    global _INSTANCE
    _INSTANCE = None
