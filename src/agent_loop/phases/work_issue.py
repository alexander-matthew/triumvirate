"""Engineer phase (worker mode): Claude implements one approved issue.

Called by the orchestrator when the state machine sees an approved issue
and no in-flight PR.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from ..config import settings
from ..lib import agent_run, db, gh, git_worktree, kill_switch, protected, quota, trust
from ..lib.persona import Persona


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-")
    return s[:48] or "issue"


def _pick_issue() -> dict | None:
    s = settings()
    issues = gh.list_issues(labels=[s.label("approved")], state="open", limit=20)
    fresh = [
        i for i in issues
        if not any(l["name"] == s.label("in_progress") for l in i.get("labels", []))
        and trust.issue_is_trusted(i)
    ]
    if not fresh:
        return None
    fresh.sort(key=lambda i: i["createdAt"])
    return fresh[0]


def run() -> int:
    s = settings()
    kill_switch.check(reason="work_issue start")

    persona = Persona.load("engineer")

    blocked, retry_after = quota.is_blocked(persona.cli)
    if blocked:
        db.append(phase="work", action="skip", agent=persona.cli,
                  outcome="rate_limited", notes={"retry_after_ts": retry_after})
        return 1

    issue = _pick_issue()
    if not issue:
        db.append(phase="work", action="skip", outcome="no_approved_issue")
        return 1

    n = issue["number"]
    title = issue["title"]
    branch = f"agent/{n}-{_slug(title)}"
    db.append(phase="work", action="start", agent=persona.cli, issue_number=n,
              notes={"branch": branch, "title": title, "persona": persona.name})

    gh.add_label(kind="issue", number=n, label=s.label("in_progress"), as_cli=persona.cli)
    worktree: Path | None = None
    try:
        worktree = git_worktree.create(branch, base="origin/main", as_cli=persona.cli)
        task_context = (
            f"## Task — implement issue #{n}\n\n"
            f"You are starting fresh on a new branch based on `origin/main`. "
            f"A wrapper script will push the branch and open a PR linked to "
            f"this issue after you exit.\n\n"
            f"### Issue #{n}: {title}\n\n"
            + trust.wrap_untrusted(f"issue #{n} body", issue.get("body") or "")
        )
        prompt = persona.render(TASK_CONTEXT=task_context)

        run_ = agent_run.run_persona(persona, prompt=prompt, cwd=worktree)
        duration = run_.duration_s

        if run_.rate_limited:
            db.append(phase="work", action="error", agent=persona.cli,
                      issue_number=n, duration_s=duration, outcome="rate_limited",
                      notes={"retry_after_ts": run_.retry_after_ts})
            gh.remove_label(kind="issue", number=n, label=s.label("in_progress"), as_cli=persona.cli)
            return 1

        if run_.timed_out or run_.returncode != 0:
            db.append(phase="work", action="error", agent=persona.cli,
                      issue_number=n, exit_code=run_.returncode,
                      duration_s=duration,
                      outcome="timed_out" if run_.timed_out else "nonzero_exit",
                      notes={"stderr": run_.stderr[-2000:]})
            gh.remove_label(kind="issue", number=n, label=s.label("in_progress"), as_cli=persona.cli)
            return 2

        if not git_worktree.has_commits_since_base(worktree):
            db.append(phase="work", action="error", agent=persona.cli,
                      issue_number=n, duration_s=duration, outcome="no_commits",
                      notes={"stdout_tail": run_.stdout[-500:]})
            gh.remove_label(kind="issue", number=n, label=s.label("in_progress"), as_cli=persona.cli)
            return 2

        adds, dels = git_worktree.diff_stats(worktree)
        too_large = (adds + dels) > s.max_diff_loc
        changed = git_worktree.changed_paths(worktree)
        bad_paths = protected.violations(changed)

        git_worktree.push(worktree, branch, as_cli=persona.cli)

        body_lines = [
            f"Closes #{n}",
            "",
            f"Authored by the agent loop ({persona.name} persona, cli={persona.cli}).",
            "",
            f"Diff: +{adds}/-{dels} across {len(changed)} files.",
        ]
        if too_large:
            body_lines += ["", f"⚠️ Diff exceeds {s.max_diff_loc} LOC. Auto-flagged for human review."]
        if bad_paths:
            body_lines += ["", "⚠️ Diff touches protected paths:", "",
                           *[f"- `{p}`" for p in bad_paths]]
        body = "\n".join(body_lines)

        labels = [s.label("authored_by_claude")]
        if too_large:
            labels.append(s.label("too_large"))
        if bad_paths:
            labels.append(s.label("protected_violation"))
            labels.append(s.label("needs_human"))

        pr_number = gh.create_pr(
            head=branch, base="main", title=title, body=body, labels=labels,
            as_cli=persona.cli,
        )

        outcome = "opened"
        if bad_paths:
            outcome = "opened_protected_violation"
        elif too_large:
            outcome = "opened_too_large"

        db.append(phase="work", action="finish", agent=persona.cli,
                  issue_number=n, pr_number=pr_number, outcome=outcome,
                  duration_s=duration,
                  notes={"adds": adds, "dels": dels, "files": len(changed),
                         "protected_violations": bad_paths})
        return 0

    except kill_switch.HaltRequested as e:
        db.append(phase="work", action="halted", agent=persona.cli,
                  issue_number=n, notes={"reason": str(e)})
        gh.remove_label(kind="issue", number=n, label=s.label("in_progress"), as_cli=persona.cli)
        return 2
    except Exception as e:
        db.append(phase="work", action="error", agent=persona.cli,
                  issue_number=n, notes={"error": repr(e)})
        gh.remove_label(kind="issue", number=n, label=s.label("in_progress"), as_cli=persona.cli)
        return 2
    finally:
        if worktree is not None:
            git_worktree.cleanup(worktree)
