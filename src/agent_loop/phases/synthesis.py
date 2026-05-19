"""Synthesis phase: weekly self-improvement loop.

The synthesis persona reads the recent arbitration + reviewer verdict
history out of the local DB, identifies systematic patterns, and files
proposal issues whose body proposes specific edits to the relevant
persona prompts. Those proposals loop back through the normal pipeline:
triage → (engineer rewrites `templates/personas/<x>.md`) → consensus
review → merge.

This is the mechanism by which the loop is supposed to get better at
reviewing its own code over time — distinct from `drift_watcher`, which
proposes *code* cleanups; synthesis proposes *prompt* cleanups.

The phase consumes purely local state (verdicts + events DB rows). No
GitHub API calls happen during prompt construction; only the issue
filing afterwards.
"""
from __future__ import annotations

import json
import time

from ..config import settings
from ..lib import agent_run, db, kill_switch, quota
from ..lib.persona import Persona
from ..lib.phase_runtime import isolated_worktree
from .spec_writer import _file_issues, _parse_proposals


_LABEL_SOURCE = "source:synthesis"


def _arbitration_log(limit: int = 30) -> str:
    """Render the recent arbiter-verdict rows as a markdown table for the prompt."""
    with db._connect() as conn:                                    # noqa: SLF001
        rows = conn.execute(
            "SELECT * FROM verdicts WHERE marker_type = 'arbiter' "
            "ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
    if not rows:
        return "(no arbitrations recorded yet)"
    lines = [
        "| ts | PR | arbiter | verdict | raw_body excerpt |",
        "|----|----|---------|---------|------------------|",
    ]
    for r in reversed(rows):
        body = (r["raw_body"] or "").replace("\n", " ")[:140]
        lines.append(
            f"| {r['ts']:.0f} | {r['pr_number']} | {r['cli']} | "
            f"{r['verdict']} | {body} |"
        )
    return "\n".join(lines)


def _distribution(marker_type: str) -> str:
    dist = db.verdict_distribution(marker_type)
    if not dist:
        return "(none recorded)"
    return json.dumps(dist, indent=2, sort_keys=True)


def run() -> int:
    s = settings()
    kill_switch.check(reason="synthesis start")
    persona = Persona.load("synthesis")

    blocked, retry_after = quota.is_blocked(persona.cli)
    if blocked:
        db.append(phase="synthesis", action="skip", agent=persona.cli,
                  outcome="rate_limited", notes={"retry_after_ts": retry_after})
        return 1

    started = time.time()
    db.append(phase="synthesis", action="start", agent=persona.cli,
              notes={"persona": persona.name})

    try:
        with isolated_worktree(f"synthesis-{int(started)}",
                               as_cli=persona.cli) as worktree:
            prompt = persona.render(
                ARBITRATION_LOG=_arbitration_log(),
                REVIEWER_DISTRIBUTION=_distribution("review"),
                SECURITY_DISTRIBUTION=_distribution("security"),
                LIBRARIAN_DISTRIBUTION=_distribution("audit"),
            )
            run_ = agent_run.run_persona(persona, prompt=prompt, cwd=worktree)
            duration = run_.duration_s

            if run_.rate_limited:
                db.append(phase="synthesis", action="error", agent=persona.cli,
                          duration_s=duration, outcome="rate_limited",
                          notes={"retry_after_ts": run_.retry_after_ts})
                return 1
            if run_.timed_out or run_.returncode != 0:
                db.append(phase="synthesis", action="error", agent=persona.cli,
                          exit_code=run_.returncode, duration_s=duration,
                          outcome="timed_out" if run_.timed_out else "nonzero_exit",
                          notes={"stderr": run_.stderr[-1500:]})
                return 2

            proposals = _parse_proposals(run_.stdout)
            if not proposals:
                db.append(phase="synthesis", action="skip", agent=persona.cli,
                          duration_s=duration, outcome="no_proposals",
                          notes={"stdout_tail": run_.stdout[-500:]})
                return 1

            filed, errors = _file_issues(
                persona,
                proposer_labels=[s.label("proposal")],
                proposals=proposals,
                extra_labels=(_LABEL_SOURCE,),
                track_errors=False,
            )

            db.append(phase="synthesis", action="finish", agent=persona.cli,
                      duration_s=duration, outcome="filed",
                      notes={"issues": filed, "count": len(filed),
                             "errors": errors or None})
            return 0 if filed else 1

    except kill_switch.HaltRequested as e:
        db.append(phase="synthesis", action="halted", agent=persona.cli,
                  notes={"reason": str(e)})
        return 2
    except Exception as e:
        db.append(phase="synthesis", action="error", agent=persona.cli,
                  notes={"error": repr(e)})
        return 2
