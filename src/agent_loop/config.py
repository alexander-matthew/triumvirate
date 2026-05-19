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


def config_root() -> Path:
    """Where the loop reads its config + personas + state from.

    Two valid layouts:

    A. Co-located (older — used by personal-site initially).
       The git repo and the loop config live in the same tree:
         <repo>/
           agents/config.toml
           agents/personas/
           agents/state/
       Config-root == project-root.

    B. Split (preferred for shared hosts / multi-project users).
       Loop config lives in its own directory, often a dedicated repo:
         agent-loop-configs/
           <project>/
             config.toml
             personas/
             state/
             systemd/
       Config-root is `agent-loop-configs/<project>/`; project-root is
       whatever `[project].git_root` says in config.toml.

    Discovery order:
      1. AGENT_LOOP_CONFIG_ROOT env var (preferred — set by systemd unit)
      2. AGENT_LOOP_PROJECT_ROOT env var (backwards compat — old layout)
      3. Walk up from cwd looking for `config.toml`
      4. Walk up from cwd looking for `agents/config.toml` (old layout)
      5. Cwd itself
    """
    env = os.environ.get("AGENT_LOOP_CONFIG_ROOT")
    if env:
        return Path(env).resolve()
    env = os.environ.get("AGENT_LOOP_PROJECT_ROOT")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd().resolve()
    for d in (cwd, *cwd.parents):
        if (d / "config.toml").exists():
            return d
        if (d / "agents" / "config.toml").exists():
            return d
    return cwd


def project_root() -> Path:
    """Back-compat alias. Resolves to config_root unless a Settings instance
    is loaded — then callers should prefer `settings().project_root`."""
    return config_root()


@dataclass(frozen=True)
class Settings:
    """All per-project configuration. Frozen — instantiate once, pass around."""

    # --- locations ---
    config_root: Path               # where config.toml, personas/, state/ live
    config_file: Path               # exact config.toml path (for STOP placement)
    project_root: Path              # where the git repo lives (== config_root in old layout)

    # --- project identity ---
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
    requires_full_consensus_paths: tuple[str, ...]  # all three CLIs must APPROVE

    # --- reviewer consensus ---
    required_reviewer_clis: tuple[str, ...]

    # --- phase enablement ---
    enabled_phases: frozenset[str]  # set of phase tags the orchestrator may dispatch

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
        # STOP lives next to config.toml — so it's at `agents/STOP` in the
        # old co-located layout and at `<config_root>/STOP` in the split
        # layout. Either way, "next to config.toml" is the stable reference.
        return self.config_file.parent / "STOP"

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

    def phase_enabled(self, phase: str) -> bool:
        """True if ``phase`` is in the configured enabled-phase set.

        Phases are identified by the same tag used in dispatch / db rows:
        ``work``, ``review``, ``respond``, ``arbitrate``, ``security``,
        ``librarian``, ``merge``, ``propose``, ``triage``, ``drift``.
        Projects opt out of optional phases by listing only the ones they
        want in ``[phases].enabled``.
        """
        return phase in self.enabled_phases

    def label(self, name: str) -> str:
        """Look up an actual GitHub label string by logical name. Falls back
        to a sensible default if the project's config.toml didn't declare it.

        Logical names the framework uses:
          approved, in_progress, proposal, halt, veto, needs_human,
          protected_violation, too_large, authored_by_claude,
          security_cleared, security_flag, librarian_cleared, librarian_flag.
        """
        return self.labels.get(name, _DEFAULT_LABELS[name])


_ALL_PHASES = frozenset({
    "work", "review", "respond", "arbitrate", "security",
    "librarian", "merge", "propose", "triage", "drift", "synthesis",
})


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
    cfg_root = config_root()
    if config_path is None:
        # Prefer the split layout (config.toml at root); fall back to the
        # old co-located layout (agents/config.toml).
        candidate = cfg_root / "config.toml"
        if candidate.exists():
            config_path = candidate
        else:
            config_path = cfg_root / "agents" / "config.toml"
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
    phases_cfg = data.get("phases", {})
    labels = data.get("labels", {})

    # project_root: where the git repo lives. May be different from cfg_root
    # in the split layout. Default to cfg_root for backwards compat.
    git_root_raw = proj.get("git_root")
    proj_root = Path(git_root_raw).resolve() if git_root_raw else cfg_root

    # Path defaults differ by layout. If config.toml is at <cfg_root>/config.toml
    # (split layout), defaults drop the "agents/" prefix. If it's at
    # <cfg_root>/agents/config.toml (old layout), defaults keep "agents/".
    is_split_layout = config_path.parent == cfg_root
    default_personas = "personas" if is_split_layout else "agents/personas"
    default_state = "state" if is_split_layout else "agents/state"

    personas_dir = cfg_root / paths.get("personas_dir", default_personas)
    state_dir = cfg_root / paths.get("state_dir", default_state)

    return Settings(
        config_root=cfg_root,
        config_file=config_path.resolve(),
        project_root=proj_root,
        project_name=proj.get("name", proj_root.name),
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
        requires_full_consensus_paths=tuple(
            guards.get("requires_full_consensus_paths", ())
        ),
        required_reviewer_clis=tuple(reviewers.get("required_clis", ("codex", "gemini"))),
        enabled_phases=frozenset(phases_cfg.get("enabled", _ALL_PHASES)),
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
