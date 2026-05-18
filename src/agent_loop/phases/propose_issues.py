"""Proposer phase: Claude scans the repo, files 1-3 spec proposals as issues."""
from __future__ import annotations

import re
import time
from pathlib import Path

from ..config import settings
from ..lib import agent_run, db, gh, git_worktree, kill_switch, quota
from ..lib.persona import Persona


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


def run() -> int:
    s = settings()
    kill_switch.check(reason="propose_issues start")
    persona = Persona.load("proposer")

    blocked, retry_after = quota.is_blocked(persona.cli)
    if blocked:
        db.append(phase="propose", action="skip", agent=persona.cli,
                  outcome="rate_limited", notes={"retry_after_ts": retry_after})
        return 1

    started = time.time()
    db.append(phase="propose", action="start", agent=persona.cli,
              notes={"persona": persona.name})

    worktree: Path | None = None
    try:
        worktree = git_worktree.create(f"propose-{int(started)}", base="origin/main")
        prompt = persona.render()
        run_ = agent_run.run_persona(persona, prompt=prompt, cwd=worktree)
        duration = run_.duration_s

        if run_.rate_limited:
            db.append(phase="propose", action="error", agent=persona.cli,
                      duration_s=duration, outcome="rate_limited",
                      notes={"retry_after_ts": run_.retry_after_ts})
            return 1

        if run_.timed_out or run_.returncode != 0:
            db.append(phase="propose", action="error", agent=persona.cli,
                      exit_code=run_.returncode, duration_s=duration,
                      outcome="timed_out" if run_.timed_out else "nonzero_exit",
                      notes={"stderr": run_.stderr[-1500:]})
            return 2

        proposals = _parse_proposals(run_.stdout)
        if not proposals:
            db.append(phase="propose", action="skip", agent=persona.cli,
                      duration_s=duration, outcome="no_proposals",
                      notes={"stdout_tail": run_.stdout[-500:]})
            return 1

        filed: list[int] = []
        errors: list[str] = []
        for p in proposals[:3]:
            labels = [s.label("proposal")] + p["labels"]
            try:
                from ..lib.gh import _run  # type: ignore
                out = _run([
                    "issue", "create",
                    "--title", p["title"],
                    "--body", p["body"],
                    *sum([["--label", l] for l in labels], []),
                ])
                num = int(out.strip().rsplit("/", 1)[-1])
                filed.append(num)
            except Exception as e:
                errors.append(f"{p['title']}: {e!r}")
                db.append(phase="propose", action="error", agent=persona.cli,
                          notes={"error": repr(e), "title": p["title"]})

        if filed:
            db.append(phase="propose", action="finish", agent=persona.cli,
                      duration_s=duration, outcome="filed",
                      notes={"issues": filed, "count": len(filed),
                             "errors": errors or None})
            return 0
        # Every `gh issue create` failed; surface as an error not a successful
        # zero-count finish. Without this the loop silently logs `count=0` and
        # the operator has no signal that anything went wrong.
        db.append(phase="propose", action="error", agent=persona.cli,
                  duration_s=duration, outcome="all_creates_failed",
                  notes={"attempted": len(proposals[:3]), "errors": errors})
        return 2

    except kill_switch.HaltRequested as e:
        db.append(phase="propose", action="halted", agent=persona.cli,
                  notes={"reason": str(e)})
        return 2
    except Exception as e:
        db.append(phase="propose", action="error", agent=persona.cli,
                  notes={"error": repr(e)})
        return 2
    finally:
        if worktree is not None:
            git_worktree.cleanup(worktree, delete_branch=True)
