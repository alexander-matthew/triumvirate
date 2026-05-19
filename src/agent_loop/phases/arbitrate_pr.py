"""Arbiter phase: third leg of the stool decides stuck PRs."""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from ..config import settings
from ..lib import agent_run, db, gh, git_worktree, kill_switch, quota, rotation, trust
from ..lib.persona import Persona


_VERDICT = re.compile(
    r"^##ARBITER_VERDICT:\s*(APPROVE_FOR_MERGE|REQUEST_FINAL_CHANGES|ESCALATE_TO_HUMAN)\s*$",
    re.M,
)
_REASONING = re.compile(r"^##REASONING:\s*\n(.*?)\Z", re.M | re.S)


def _parse(text: str) -> dict | None:
    v = _VERDICT.search(text or "")
    r = _REASONING.search(text or "")
    if not v:
        return None
    return {
        "verdict": v.group(1),
        "reasoning": r.group(1).strip() if r else "(no reasoning block)",
    }


def _extract_issue_ref(pr_body: str) -> int | None:
    m = re.search(r"Closes\s+#(\d+)", pr_body)
    return int(m.group(1)) if m else None


def _build_review_history(pr: dict) -> str:
    posts = gh.marker_posts(pr)
    commits = pr.get("commits") or []
    rows: list[tuple[str, str, str]] = []
    for p in posts:
        rows.append((p["ts"], "review", p["body"]))
    for c in commits:
        ts = c.get("committedDate") or ""
        msg = c.get("messageHeadline") or "(no message)"
        sha = (c.get("oid") or "")[:8]
        rows.append((ts, "commit", f"{sha}  {msg}"))
    rows.sort(key=lambda r: r[0])
    return "\n".join(
        f"### {kind} ({ts})\n{content}\n" for ts, kind, content in rows
    ) or "(no marker posts yet)"


def arbitrate(pr_number: int) -> int:
    s = settings()
    kill_switch.check(reason="arbitrate_pr start")

    arbiter_cli = rotation.pick_arbiter_cli(pr_number)
    if arbiter_cli is None:
        gh.add_label(kind="pr", number=pr_number, label=s.label("needs_human"))
        gh.comment(
            kind="pr", number=pr_number,
            body=("🟠 Arbiter could not be assigned: both reviewer CLIs have "
                  "been involved in this PR's review chain. Escalating to human."),
        )
        db.append(phase="arbitrate", action="finish", pr_number=pr_number,
                  outcome="no_clean_arbiter")
        return 1

    persona_name = rotation.arbiter_persona_name(arbiter_cli)
    persona = Persona.load(persona_name)

    blocked, retry_after = quota.is_blocked(persona.cli)
    if blocked:
        db.append(phase="arbitrate", action="skip", agent=persona.cli,
                  pr_number=pr_number, outcome="rate_limited",
                  notes={"retry_after_ts": retry_after, "persona": persona.name})
        return 1

    pr = gh.get_pr(pr_number)
    issue_n = _extract_issue_ref(pr.get("body") or "") or 0
    issue_body = ""
    if issue_n:
        try:
            issue_body = (gh.get_issue(issue_n) or {}).get("body", "") or ""
        except Exception:
            pass

    db.append(phase="arbitrate", action="start", agent=persona.cli,
              pr_number=pr_number, notes={"persona": persona.name})
    branch = pr.get("headRefName", "")
    worktree: Path | None = None
    try:
        worktree = git_worktree.create(f"arbitrate-{pr_number}", base="origin/main", as_cli=persona.cli)
        subprocess.run(["git", "fetch", "origin", f"{branch}:{branch}", "--force"],
                       cwd=worktree, check=True, capture_output=True)
        subprocess.run(["git", "checkout", branch],
                       cwd=worktree, check=True, capture_output=True)

        prompt = persona.render(
            PR_NUMBER=pr["number"],
            PR_TITLE=pr["title"],
            ISSUE_NUMBER=issue_n or "?",
            ADDITIONS=pr.get("additions", 0),
            DELETIONS=pr.get("deletions", 0),
            CHANGED_FILES=pr.get("changedFiles", 0),
            ISSUE_BODY=trust.wrap_untrusted(f"issue #{issue_n} body", issue_body),
            REVIEW_HISTORY=_build_review_history(pr),
        )

        run_ = agent_run.run_persona(persona, prompt=prompt, cwd=worktree)
        duration = run_.duration_s

        if run_.rate_limited:
            db.append(phase="arbitrate", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=duration,
                      outcome="rate_limited",
                      notes={"retry_after_ts": run_.retry_after_ts})
            return 1
        if run_.timed_out:
            db.append(phase="arbitrate", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=duration,
                      exit_code=run_.returncode, outcome="timed_out")
            return 2

        parsed = _parse(run_.final_message)
        if not parsed:
            db.append(phase="arbitrate", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=duration,
                      exit_code=run_.returncode, outcome="parse_failed",
                      notes={"stdout_tail": run_.stdout[-1500:],
                             "stderr_tail": run_.stderr[-1500:]})
            return 2

        body = (
            f"##ARBITER_VERDICT: {parsed['verdict']}\n"
            f"##REASONING:\n{parsed['reasoning']}\n"
            f"\n---\n*arbiter: {persona.cli} · "
            f"{time.strftime('%Y-%m-%d %H:%M')}*"
        )
        gh.comment(kind="pr", number=pr_number, body=body, as_cli=persona.cli)

        if parsed["verdict"] == "ESCALATE_TO_HUMAN":
            gh.add_label(kind="pr", number=pr_number, label=s.label("needs_human"), as_cli=persona.cli)
        elif parsed["verdict"] == "APPROVE_FOR_MERGE":
            gh.comment(
                kind="pr", number=pr_number, as_cli=persona.cli,
                body=("##VERDICT: APPROVE\n"
                      "##SUMMARY: Arbiter override — see arbiter verdict above.\n"
                      "##CHECKLIST:\n- [x] Arbiter approved for merge\n"
                      "##NOTES:\nThis APPROVE is posted by the arbiter wrapper "
                      "to satisfy the merge-gate's latest-verdict check. The "
                      "arbiter's full reasoning is in the comment immediately "
                      "above this one.\n"
                      f"\n---\n*arbiter override · {persona.cli}*"),
            )

        db.append(phase="arbitrate", action="finish", agent=persona.cli,
                  pr_number=pr_number, duration_s=duration,
                  outcome=parsed["verdict"].lower(),
                  notes={"persona": persona.name,
                         "reasoning": parsed["reasoning"][:1000]})
        return 0

    except kill_switch.HaltRequested as e:
        db.append(phase="arbitrate", action="halted", agent=persona.cli,
                  pr_number=pr_number, notes={"reason": str(e)})
        return 2
    except Exception as e:
        db.append(phase="arbitrate", action="error", agent=persona.cli,
                  pr_number=pr_number, notes={"error": repr(e)})
        return 2
    finally:
        if worktree is not None:
            git_worktree.cleanup(worktree, delete_branch=True)
