"""Engineer phase (responder mode): addresses reviewer checklist, pushes commits."""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from ..config import settings
from ..lib import agent_run, db, gh, git_worktree, kill_switch, protected, quota
from ..lib.persona import Persona


def _latest_codex_review(pr: dict) -> dict | None:
    """The most recent review-or-comment carrying the structured marker."""
    posts = gh.marker_posts(pr)
    if not posts:
        return None
    p = posts[-1]
    return {"body": p["body"], "submittedAt": p["ts"]}


def _parse_review_body(body: str) -> dict:
    v = re.search(r"^##VERDICT:\s*(\S+)", body, re.M)
    s = re.search(r"^##SUMMARY:\s*(.+)$", body, re.M)
    c = re.search(r"^##CHECKLIST:\s*\n(.*?)(?=^##|\Z)", body, re.M | re.S)
    n = re.search(r"^##NOTES:\s*\n(.*?)(?=^---|\Z)", body, re.M | re.S)
    return {
        "verdict": v.group(1) if v else "",
        "summary": s.group(1).strip() if s else "",
        "checklist": c.group(1).strip() if c else "",
        "notes": n.group(1).strip() if n else "",
    }


def _round_number_from_body(body: str) -> int:
    m = re.search(r"Round (\d+)/\d+", body)
    return int(m.group(1)) if m else 1


def _build_prompt(persona: Persona, pr: dict, review_parsed: dict, round_n: int) -> str:
    s = settings()
    issue_ref = re.search(r"Closes\s+#(\d+)", pr.get("body") or "")
    issue_n = issue_ref.group(1) if issue_ref else "?"
    task_context = (
        f"## Task — respond to PR review (round {round_n}/{s.max_review_rounds})\n\n"
        f"This PR is your earlier work on issue #{issue_n}. The reviewer agent "
        f"posted a review with one or more unchecked items. The branch is already "
        f"checked out; add follow-up commits that address every unchecked item.\n\n"
        f"### PR #{pr['number']}: {pr['title']}\n"
        f"Linked issue: #{issue_n}\n\n"
        f"### Reviewer's verdict\n\n"
        f"**Summary:** {review_parsed['summary']}\n\n"
        f"**Checklist:**\n{review_parsed['checklist']}\n\n"
        f"**Notes:**\n{review_parsed['notes']}\n\n"
        f"Address every unchecked item and exit. Do not push or comment."
    )
    return persona.render(TASK_CONTEXT=task_context)


def respond(pr_number: int) -> int:
    s = settings()
    kill_switch.check(reason="respond_to_review start")
    persona = Persona.load("engineer")

    blocked, retry_after = quota.is_blocked(persona.cli)
    if blocked:
        db.append(phase="respond", action="skip", agent=persona.cli,
                  pr_number=pr_number, outcome="rate_limited",
                  notes={"retry_after_ts": retry_after})
        return 1

    pr = gh.get_pr(pr_number)
    if pr.get("isDraft"):
        return 1
    latest_review = _latest_codex_review(pr)
    if not latest_review:
        return 1
    parsed = _parse_review_body(latest_review.get("body", ""))
    if parsed["verdict"] != "REQUEST_CHANGES":
        return 1

    commits = pr.get("commits") or []
    if commits:
        last_commit_ts = commits[-1].get("committedDate") or ""
        review_ts = latest_review.get("submittedAt") or ""
        if last_commit_ts > review_ts:
            db.append(phase="respond", action="skip", pr_number=pr_number,
                      outcome="already_responded")
            return 1

    round_n = _round_number_from_body(latest_review.get("body", ""))
    if round_n >= s.max_review_rounds:
        gh.add_label(kind="pr", number=pr_number, label=s.label("needs_human"), as_cli=persona.cli)
        db.append(phase="respond", action="finish", pr_number=pr_number,
                  outcome="escalated_max_rounds", notes={"round": round_n})
        return 1

    db.append(phase="respond", action="start", agent=persona.cli,
              pr_number=pr_number, notes={"round": round_n, "persona": persona.name})

    branch = pr.get("headRefName", "")
    worktree: Path | None = None
    try:
        worktree = git_worktree.create(f"respond-{pr_number}-r{round_n}", base="origin/main", as_cli=persona.cli)
        subprocess.run(["git", "fetch", "origin", f"{branch}:{branch}", "--force"],
                       cwd=worktree, check=True, capture_output=True)
        subprocess.run(["git", "checkout", branch],
                       cwd=worktree, check=True, capture_output=True)

        prompt = _build_prompt(persona, pr, parsed, round_n + 1)
        run_ = agent_run.run_persona(persona, prompt=prompt, cwd=worktree)
        duration = run_.duration_s

        if run_.rate_limited:
            db.append(phase="respond", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=duration,
                      outcome="rate_limited",
                      notes={"retry_after_ts": run_.retry_after_ts, "round": round_n})
            return 1

        if run_.timed_out:
            db.append(phase="respond", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=duration,
                      exit_code=run_.returncode, outcome="timed_out",
                      notes={"round": round_n})
            return 2

        new_log = subprocess.run(
            ["git", "rev-list", "--count", f"origin/{branch}..HEAD"],
            cwd=worktree, capture_output=True, text=True,
        ).stdout.strip()
        added_commits = int(new_log or "0")

        if run_.returncode != 0 or added_commits == 0:
            db.append(phase="respond", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=duration,
                      exit_code=run_.returncode, outcome="no_commits",
                      notes={"stderr": run_.stderr[-1500:],
                             "stdout_tail": run_.stdout[-500:]})
            return 2

        adds, dels = git_worktree.diff_stats(worktree)
        too_large = (adds + dels) > s.max_diff_loc
        changed = git_worktree.changed_paths(worktree)
        bad_paths = protected.violations(changed)

        git_worktree.push(worktree, branch, as_cli=persona.cli)

        comment_lines = [
            f"Responded to round-{round_n} review.",
            f"Added {added_commits} commit(s). Diff now +{adds}/-{dels}.",
        ]
        if bad_paths:
            comment_lines += ["", "⚠️ Touched protected paths:",
                              *[f"- `{p}`" for p in bad_paths]]
            gh.add_label(kind="pr", number=pr_number, label=s.label("protected_violation"), as_cli=persona.cli)
            gh.add_label(kind="pr", number=pr_number, label=s.label("needs_human"), as_cli=persona.cli)
        if too_large:
            comment_lines += ["", f"⚠️ Diff now exceeds {s.max_diff_loc} LOC."]
            gh.add_label(kind="pr", number=pr_number, label=s.label("too_large"), as_cli=persona.cli)
        gh.comment(kind="pr", number=pr_number, body="\n".join(comment_lines), as_cli=persona.cli)

        db.append(phase="respond", action="finish", agent=persona.cli,
                  pr_number=pr_number, duration_s=duration,
                  outcome="commits_pushed",
                  notes={"added_commits": added_commits, "adds": adds, "dels": dels,
                         "protected_violations": bad_paths, "too_large": too_large,
                         "round": round_n + 1})
        return 0

    except kill_switch.HaltRequested as e:
        db.append(phase="respond", action="halted", agent=persona.cli,
                  pr_number=pr_number, notes={"reason": str(e)})
        return 2
    except Exception as e:
        db.append(phase="respond", action="error", agent=persona.cli,
                  pr_number=pr_number, notes={"error": repr(e)})
        return 2
    finally:
        if worktree is not None:
            git_worktree.cleanup(worktree, delete_branch=True)
