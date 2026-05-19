"""Pre-merge librarian check: cross-project consistency audit (Gemini).

Trigger paths come from `Settings.librarian_trigger_paths`.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from ..config import settings
from ..lib import agent_run, db, gh, git_worktree, kill_switch, quota
from ..lib.persona import Persona


def _matches_trigger(path: str) -> bool:
    for prefix in settings().librarian_trigger_paths:
        if path == prefix or path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    return False


def _trigger_paths_touched(pr: dict) -> list[str]:
    files = pr.get("files") or []
    hits = []
    for f in files:
        p = f.get("path", "")
        if p and _matches_trigger(p):
            hits.append(p)
    return sorted(set(hits))


_VERDICT = re.compile(r"^##AUDIT_VERDICT:\s*(AUDIT_PASS|AUDIT_FAIL)\s*$", re.M)
_NOTES = re.compile(r"^##CONSISTENCY_NOTES:\s*\n(.*?)\Z", re.M | re.S)


def _parse(text: str) -> dict | None:
    v = _VERDICT.search(text or "")
    n = _NOTES.search(text or "")
    if not v:
        return None
    return {
        "verdict": v.group(1),
        "notes": n.group(1).strip() if n else "(no notes)",
    }


def _already_audited(pr: dict) -> bool:
    s = settings()
    labels = {l["name"] for l in pr.get("labels", [])}
    return s.label("librarian_flag") in labels or s.label("librarian_cleared") in labels


def _extract_issue_ref(pr_body: str) -> int | None:
    m = re.search(r"Closes\s+#(\d+)", pr_body)
    return int(m.group(1)) if m else None


def check(pr_number: int) -> int:
    s = settings()
    kill_switch.check(reason="librarian_check start")
    persona = Persona.load("librarian-gemini")

    blocked, retry_after = quota.is_blocked(persona.cli)
    if blocked:
        db.append(phase="librarian", action="skip", agent=persona.cli,
                  pr_number=pr_number, outcome="rate_limited",
                  notes={"retry_after_ts": retry_after})
        return 1

    pr = gh.get_pr(pr_number)
    if _already_audited(pr):
        db.append(phase="librarian", action="skip", pr_number=pr_number,
                  outcome="already_audited")
        return 1

    paths = _trigger_paths_touched(pr)
    if not paths:
        gh.add_label(kind="pr", number=pr_number, label=s.label("librarian_cleared"), as_cli=persona.cli)
        db.append(phase="librarian", action="finish", pr_number=pr_number,
                  outcome="not_cross_project")
        return 0

    db.append(phase="librarian", action="start", agent=persona.cli,
              pr_number=pr_number,
              notes={"persona": persona.name, "trigger_paths": paths})

    branch = pr.get("headRefName", "")
    worktree: Path | None = None
    try:
        worktree = git_worktree.create(f"librarian-{pr_number}", base="origin/main", as_cli=persona.cli)
        subprocess.run(["git", "fetch", "origin", f"{branch}:{branch}", "--force"],
                       cwd=worktree, check=True, capture_output=True)
        subprocess.run(["git", "checkout", branch],
                       cwd=worktree, check=True, capture_output=True)

        prompt = persona.render(
            PR_NUMBER=pr["number"],
            PR_TITLE=pr["title"],
            ISSUE_NUMBER=_extract_issue_ref(pr.get("body") or "") or "?",
            ADDITIONS=pr.get("additions", 0),
            DELETIONS=pr.get("deletions", 0),
            CHANGED_FILES=pr.get("changedFiles", 0),
            PR_BODY=pr.get("body") or "",
            TRIGGER_PATHS="\n".join(f"- {p}" for p in paths),
        )

        run_ = agent_run.run_persona(persona, prompt=prompt, cwd=worktree)
        duration = run_.duration_s

        if run_.rate_limited:
            db.append(phase="librarian", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=duration,
                      outcome="rate_limited",
                      notes={"retry_after_ts": run_.retry_after_ts})
            return 1
        if run_.timed_out:
            db.append(phase="librarian", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=duration,
                      exit_code=run_.returncode, outcome="timed_out")
            return 2

        parsed = _parse(run_.final_message)
        if not parsed:
            db.append(phase="librarian", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=duration,
                      outcome="parse_failed",
                      notes={"stdout_tail": run_.stdout[-1500:],
                             "stderr_tail": run_.stderr[-1500:]})
            return 2

        body = (
            f"##AUDIT_VERDICT: {parsed['verdict']}\n"
            f"##CONSISTENCY_NOTES:\n{parsed['notes']}\n"
            f"\n---\n*librarian: {persona.cli} · "
            f"{time.strftime('%Y-%m-%d %H:%M')}*"
        )
        gh.comment(kind="pr", number=pr_number, body=body, as_cli=persona.cli)

        if parsed["verdict"] == "AUDIT_FAIL":
            gh.add_label(kind="pr", number=pr_number, label=s.label("librarian_flag"), as_cli=persona.cli)
            gh.add_label(kind="pr", number=pr_number, label=s.label("needs_human"), as_cli=persona.cli)
        else:
            gh.add_label(kind="pr", number=pr_number, label=s.label("librarian_cleared"), as_cli=persona.cli)

        db.append(phase="librarian", action="finish", agent=persona.cli,
                  pr_number=pr_number, duration_s=duration,
                  outcome=parsed["verdict"].lower(),
                  notes={"persona": persona.name, "trigger_paths": paths,
                         "verdict_notes": parsed["notes"][:1500]})
        return 0

    except kill_switch.HaltRequested as e:
        db.append(phase="librarian", action="halted", agent=persona.cli,
                  pr_number=pr_number, notes={"reason": str(e)})
        return 2
    except Exception as e:
        db.append(phase="librarian", action="error", agent=persona.cli,
                  pr_number=pr_number, notes={"error": repr(e)})
        return 2
    finally:
        if worktree is not None:
            git_worktree.cleanup(worktree, delete_branch=True)
