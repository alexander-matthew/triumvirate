"""Spec writer phase: Claude scans the repo and files specs as issues.

Two cadences share one implementation:

- ``propose()`` — daily proposer (persona ``proposer``, phase tag
  ``propose``). Files 1-3 new spec issues per run.
- ``drift()`` — weekly drift watcher (persona ``drift_watcher``, phase
  tag ``drift``). Same proposal grammar; filed issues are additionally
  tagged ``source:drift`` so triage/humans can distinguish them.

Before this merge, ``propose_issues.py`` and ``drift_watcher.py`` were
two nearly-identical files (~80 LOC each) with a duplicated proposal
regex. They are now one.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable

from ..config import settings
from ..lib import agent_run, db, gh, kill_switch, quota
from ..lib.persona import Persona
from ..lib.phase_runtime import isolated_worktree


# ---- proposal-block parser (shared) ---------------------------------------


_BLOCK = re.compile(
    r"##PROPOSAL\s*\n"
    r"##TITLE:\s*(?P<title>.+?)\s*\n"
    r"##LABELS:\s*(?P<labels>.+?)\s*\n"
    r"##BODY:\s*\n(?P<body>.*?)\n##END",
    re.S,
)


def _parse_proposals(text: str) -> list[dict]:
    out = []
    for m in _BLOCK.finditer(text or ""):
        labels = [l.strip() for l in m.group("labels").split(",") if l.strip()]
        out.append({
            "title": m.group("title").strip(),
            "labels": labels,
            "body": m.group("body").strip(),
        })
    return out


# ---- mode descriptor ------------------------------------------------------


@dataclass(frozen=True)
class _Mode:
    """Per-cadence knobs for the shared spec-writer engine."""

    phase: str                  # db tag: "propose" or "drift"
    persona: str                # Persona to load
    worktree_prefix: str        # scratch-branch prefix
    extra_labels: tuple[str, ...] = ()      # always-added labels
    success_requires_filed: bool = False    # drift returns 1 if no issues filed
    all_failed_is_error: bool = False       # proposer returns 2 if every create failed


PROPOSE = _Mode(
    phase="propose",
    persona="proposer",
    worktree_prefix="propose",
    all_failed_is_error=True,
)


DRIFT = _Mode(
    phase="drift",
    persona="drift_watcher",
    worktree_prefix="drift",
    extra_labels=("source:drift",),
    success_requires_filed=True,
)


# ---- engine ---------------------------------------------------------------


def _file_issues(
    persona: Persona,
    proposer_labels: Iterable[str],
    proposals: list[dict],
    *,
    extra_labels: tuple[str, ...],
    track_errors: bool,
) -> tuple[list[int], list[str]]:
    """File up to 3 proposals as GitHub issues. Returns (filed numbers, errors)."""
    filed: list[int] = []
    errors: list[str] = []
    for p in proposals[:3]:
        # extra_labels first so drift's "source:drift" sticks even if the model
        # tries to set it itself.
        labels = list(proposer_labels) + [
            l for l in extra_labels if l not in p["labels"]
        ] + p["labels"]
        try:
            from ..lib.gh import _run  # type: ignore
            out = _run([
                "issue", "create",
                "--title", p["title"],
                "--body", p["body"],
                *sum([["--label", l] for l in labels], []),
            ], as_cli=persona.cli)
            num = int(out.strip().rsplit("/", 1)[-1])
            filed.append(num)
        except Exception as e:
            errors.append(f"{p['title']}: {e!r}")
            if track_errors:
                db.append(
                    phase="propose" if track_errors else "drift",
                    action="error", agent=persona.cli,
                    notes={"error": repr(e), "title": p["title"]},
                )
    return filed, errors


def _run(mode: _Mode) -> int:
    s = settings()
    kill_switch.check(reason=f"{mode.phase} start")
    persona = Persona.load(mode.persona)

    blocked, retry_after = quota.is_blocked(persona.cli)
    if blocked:
        db.append(phase=mode.phase, action="skip", agent=persona.cli,
                  outcome="rate_limited", notes={"retry_after_ts": retry_after})
        return 1

    started = time.time()
    db.append(phase=mode.phase, action="start", agent=persona.cli,
              notes={"persona": persona.name})

    try:
        with isolated_worktree(
            f"{mode.worktree_prefix}-{int(started)}",
            as_cli=persona.cli,
        ) as worktree:
            prompt = persona.render()
            run_ = agent_run.run_persona(persona, prompt=prompt, cwd=worktree)
            duration = run_.duration_s

            if run_.rate_limited:
                db.append(phase=mode.phase, action="error", agent=persona.cli,
                          duration_s=duration, outcome="rate_limited",
                          notes={"retry_after_ts": run_.retry_after_ts})
                return 1
            if run_.timed_out or run_.returncode != 0:
                db.append(phase=mode.phase, action="error", agent=persona.cli,
                          exit_code=run_.returncode, duration_s=duration,
                          outcome="timed_out" if run_.timed_out else "nonzero_exit",
                          notes={"stderr": run_.stderr[-1500:]})
                return 2

            proposals = _parse_proposals(run_.stdout)
            if not proposals:
                db.append(phase=mode.phase, action="skip", agent=persona.cli,
                          duration_s=duration, outcome="no_proposals",
                          notes={"stdout_tail": run_.stdout[-500:]})
                return 1

            filed, errors = _file_issues(
                persona,
                proposer_labels=[s.label("proposal")],
                proposals=proposals,
                extra_labels=mode.extra_labels,
                track_errors=(mode.phase == "drift"),
            )

            if filed:
                db.append(phase=mode.phase, action="finish", agent=persona.cli,
                          duration_s=duration, outcome="filed",
                          notes={"issues": filed, "count": len(filed),
                                 "errors": errors or None})
                return 0

            if mode.all_failed_is_error:
                # Without this the proposer would silently log count=0 and the
                # operator would have no signal that anything went wrong.
                db.append(phase=mode.phase, action="error", agent=persona.cli,
                          duration_s=duration, outcome="all_creates_failed",
                          notes={"attempted": len(proposals[:3]),
                                 "errors": errors})
                return 2

            db.append(phase=mode.phase, action="finish", agent=persona.cli,
                      duration_s=duration, outcome="filed",
                      notes={"issues": filed, "count": 0})
            return 1 if mode.success_requires_filed else 0

    except kill_switch.HaltRequested as e:
        db.append(phase=mode.phase, action="halted", agent=persona.cli,
                  notes={"reason": str(e)})
        return 2
    except Exception as e:
        db.append(phase=mode.phase, action="error", agent=persona.cli,
                  notes={"error": repr(e)})
        return 2


# ---- public entry points (called from orchestrator) -----------------------


def propose() -> int:
    """Daily proposer: file 1-3 new spec issues for triage to consider."""
    return _run(PROPOSE)


def drift() -> int:
    """Weekly drift sweep: cleanup-oriented proposals tagged source:drift."""
    return _run(DRIFT)
