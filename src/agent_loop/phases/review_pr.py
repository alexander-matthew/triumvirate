"""Reviewer phase: Codex or Gemini reviews a PR with structured output.

The orchestrator picks the cli via rotation and passes it in. Wrapper
enforces protected-path and diff-size overrides regardless of the
reviewer's opinion.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from ..config import settings
from ..lib import (
    agent_run,
    db,
    gh,
    git_worktree,
    kill_switch,
    protected,
    quota,
    review_baton,
    rotation,
)
from ..lib.persona import Persona


# ---- structured-output parsing --------------------------------------------


_MARKER_VERDICT = re.compile(r"^##VERDICT:\s*(APPROVE|REQUEST_CHANGES|COMMENT)\s*$", re.M)
_MARKER_SUMMARY = re.compile(r"^##SUMMARY:\s*(.+)$", re.M)
_MARKER_CHECKLIST = re.compile(r"^##CHECKLIST:\s*\n(.*?)(?=^##|\Z)", re.M | re.S)
_MARKER_NOTES = re.compile(r"^##NOTES:\s*\n(.*?)\Z", re.M | re.S)


def _parse_review(text: str) -> dict | None:
    v = _MARKER_VERDICT.search(text)
    s = _MARKER_SUMMARY.search(text)
    c = _MARKER_CHECKLIST.search(text)
    n = _MARKER_NOTES.search(text)
    if not (v and s and c):
        return None
    return {
        "verdict": v.group(1),
        "summary": s.group(1).strip(),
        "checklist": c.group(1).strip(),
        "notes": n.group(1).strip() if n else "",
    }


def _round_number(pr_number: int) -> int:
    pr = gh.get_pr(pr_number)
    return len(gh.marker_posts(pr)) + 1


def _needs_review(pr: dict) -> bool:
    """True if at least one required reviewer still owes a verdict on the
    current commit (consensus mode)."""
    if pr.get("isDraft"):
        return False
    return bool(rotation.pending_reviewer_clis(pr["number"]))


def _extract_issue_ref(pr_body: str) -> int | None:
    m = re.search(r"Closes\s+#(\d+)", pr_body)
    return int(m.group(1)) if m else None


def _head_sha(pr: dict) -> str:
    commits = pr.get("commits") or []
    if not commits:
        return ""
    return commits[-1].get("oid") or ""


def _delta_diff(worktree: Path, previous_head: str, current_head: str) -> str:
    if not previous_head or not current_head or previous_head == current_head:
        return ""
    stat = subprocess.run(
        ["git", "diff", "--stat", f"{previous_head}..{current_head}"],
        cwd=worktree, capture_output=True, text=True,
    )
    diff = subprocess.run(
        ["git", "diff", "--find-renames", f"{previous_head}..{current_head}"],
        cwd=worktree, capture_output=True, text=True,
    )
    if stat.returncode != 0 or diff.returncode != 0:
        return ""
    return (stat.stdout.strip() + "\n\n" + diff.stdout.strip()).strip()


def _matches_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _wrapper_facts(
    pr: dict,
    *,
    head_sha: str,
    changed: list[str],
    bad_paths: list[str],
    too_large: bool,
) -> str:
    s = settings()
    labels = sorted(l.get("name", "") for l in pr.get("labels", []) if l.get("name"))
    sensitive = sorted(
        p for p in changed
        if _matches_prefix(p, s.sensitive_path_prefixes)
    )
    trigger_paths = sorted(
        p for p in changed
        if _matches_prefix(p, s.librarian_trigger_paths)
    )
    adds = pr.get("additions", 0)
    dels = pr.get("deletions", 0)
    mergeable = pr.get("mergeable") or "unknown"
    ci_items = pr.get("statusCheckRollup") or []
    ci_summary = ", ".join(
        filter(None, [
            str(item.get("name") or item.get("workflowName") or item.get("context") or "").strip()
            for item in ci_items[:8]
        ])
    ) or "none reported"

    lines = [
        "## Wrapper-Computed Review Facts",
        "",
        "These facts were computed by the loop before invoking you. Treat them",
        "as inputs to your review, not as a substitute for judgment.",
        "",
        f"- Head SHA: `{head_sha or 'unknown'}`",
        f"- Diff size: +{adds}/-{dels} ({adds + dels} LOC), cap {s.max_diff_loc}: "
        f"{'over cap' if too_large else 'within cap'}",
        f"- Changed files ({len(changed)}):",
        *[f"  - `{p}`" for p in changed[:30]],
    ]
    if len(changed) > 30:
        lines.append(f"  - ... {len(changed) - 30} more")
    lines.extend([
        f"- Protected path violations: {', '.join(f'`{p}`' for p in bad_paths) if bad_paths else 'none'}",
        f"- Sensitive paths touched: {', '.join(f'`{p}`' for p in sensitive) if sensitive else 'none'}",
        f"- Librarian trigger paths touched: {', '.join(f'`{p}`' for p in trigger_paths) if trigger_paths else 'none'}",
        f"- PR labels: {', '.join(f'`{label}`' for label in labels) if labels else 'none'}",
        f"- Mergeable: `{mergeable}`",
        f"- Status checks seen: {ci_summary}",
    ])
    return "\n".join(lines)


def _build_prompt(
    persona: Persona,
    pr: dict,
    issue_body: str,
    *,
    wrapper_facts: str = "",
    baton_context: str = "",
) -> str:
    prompt = persona.render(
        PR_NUMBER=pr["number"],
        PR_TITLE=pr["title"],
        ISSUE_NUMBER=_extract_issue_ref(pr.get("body") or "") or "?",
        ADDITIONS=pr.get("additions", 0),
        DELETIONS=pr.get("deletions", 0),
        CHANGED_FILES=pr.get("changedFiles", 0),
        PR_BODY=pr.get("body") or "",
        ISSUE_BODY=issue_body,
    )
    if wrapper_facts:
        prompt += (
            "\n\n---\n\n"
            f"{wrapper_facts}\n"
        )
    if baton_context:
        prompt += (
            "\n\n---\n\n"
            "## Runtime Optimization Context\n\n"
            "This is a follow-up review in the same PR review cycle. Keep the "
            "same review standards as usual, but use this addendum to avoid "
            "rediscovering unchanged context. If a delta diff is present, review "
            "that first and only reopen the full branch diff where the delta, "
            "baton, or checklist points to unresolved risk. Do not rubber-stamp "
            "another reviewer if you disagree.\n\n"
            f"{baton_context}\n"
        )
    return prompt


def review(pr_number: int, *, cli: str | None = None) -> int:
    """Returns 0 on success, 1 on skip, 2 on failure."""
    s = settings()
    kill_switch.check(reason="review_pr start")

    chosen_cli = cli or rotation.pick_reviewer_cli(pr_number)
    if chosen_cli is None:
        db.append(phase="review", action="skip", pr_number=pr_number,
                  outcome="no_pending_reviewer")
        return 1
    persona = Persona.load(rotation.reviewer_persona_name(chosen_cli))

    blocked, retry_after = quota.is_blocked(persona.cli)
    if blocked:
        db.append(phase="review", action="skip", agent=persona.cli,
                  pr_number=pr_number, outcome="rate_limited",
                  notes={"retry_after_ts": retry_after, "persona": persona.name})
        return 1

    pr = gh.get_pr(pr_number)
    if not _needs_review(pr):
        db.append(phase="review", action="skip", pr_number=pr_number,
                  outcome="already_current")
        return 1

    round_n = _round_number(pr_number)

    # Hard pre-checks the wrapper enforces regardless of reviewer opinion.
    adds = pr.get("additions", 0)
    dels = pr.get("deletions", 0)
    too_large = (adds + dels) > s.max_diff_loc
    changed = [p for p in (f.get("path") for f in (pr.get("files") or [])) if p]
    bad_paths = protected.violations(changed)

    db.append(phase="review", action="start", agent=persona.cli,
              pr_number=pr_number, notes={"round": round_n, "persona": persona.name})

    branch = pr.get("headRefName", "")
    worktree: Path | None = None
    try:
        worktree = git_worktree.create(f"review-{pr_number}-r{round_n}", base="origin/main")
        subprocess.run(["git", "fetch", "origin", f"{branch}:{branch}", "--force"],
                       cwd=worktree, check=True, capture_output=True)
        subprocess.run(["git", "checkout", branch],
                       cwd=worktree, check=True, capture_output=True)

        issue_number = _extract_issue_ref(pr.get("body") or "")
        issue_body = ""
        if issue_number:
            try:
                issue_body = (gh.get_issue(issue_number) or {}).get("body", "") or ""
            except Exception:
                pass

        current_head = _head_sha(pr)
        prior_head = review_baton.latest_head_sha(pr_number)
        baton_context = ""
        if review_baton.has_reviews(pr_number):
            baton_context = review_baton.render(
                pr_number,
                current_head=current_head,
                delta_diff=_delta_diff(worktree, prior_head or "", current_head),
            )

        wrapper_facts = _wrapper_facts(
            pr,
            head_sha=current_head,
            changed=changed,
            bad_paths=bad_paths,
            too_large=too_large,
        )
        prompt = _build_prompt(
            persona,
            pr,
            issue_body,
            wrapper_facts=wrapper_facts,
            baton_context=baton_context,
        )
        run_ = agent_run.run_persona(persona, prompt=prompt, cwd=worktree)

        if run_.rate_limited:
            db.append(phase="review", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=run_.duration_s,
                      outcome="rate_limited",
                      notes={"retry_after_ts": run_.retry_after_ts, "round": round_n})
            return 1

        if run_.timed_out:
            db.append(phase="review", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=run_.duration_s,
                      exit_code=run_.returncode, outcome="timed_out",
                      notes={"round": round_n})
            return 2

        parsed = _parse_review(run_.final_message)
        if not parsed:
            if persona.on_parse_fail == "comment_and_retry":
                gh.comment(
                    kind="pr", number=pr_number,
                    body=("⚠️ Reviewer agent produced unparseable output. Raw last message:\n\n"
                          f"```\n{run_.final_message[:3000]}\n```"),
                )
            db.append(phase="review", action="error", agent=persona.cli,
                      pr_number=pr_number, duration_s=run_.duration_s,
                      outcome="parse_failed", exit_code=run_.returncode,
                      notes={"stdout_tail": run_.stdout[-1500:],
                             "stderr_tail": run_.stderr[-1500:],
                             "last_msg_len": len(run_.final_message),
                             "round": round_n})
            return 2

        # Wrapper-enforced overrides (reviewer cannot approve if these fail).
        enforced_verdict = parsed["verdict"]
        if bad_paths or too_large:
            enforced_verdict = "REQUEST_CHANGES"
            extra = []
            if bad_paths:
                extra.append("**Protected-path violations** (wrapper-enforced):\n"
                             + "\n".join(f"- `{p}`" for p in bad_paths))
                gh.add_label(kind="pr", number=pr_number, label=s.label("protected_violation"))
            if too_large:
                extra.append(f"**Diff exceeds {s.max_diff_loc} LOC** (+{adds}/-{dels}, wrapper-enforced).")
                gh.add_label(kind="pr", number=pr_number, label=s.label("too_large"))
            parsed["checklist"] = "\n".join(extra) + "\n\n" + parsed["checklist"]

        review_body = (
            f"##VERDICT: {enforced_verdict}\n"
            f"##SUMMARY: {parsed['summary']}\n"
            f"##CHECKLIST:\n{parsed['checklist']}\n"
            f"##NOTES:\n{parsed['notes']}\n"
            f"\n---\n*Round {round_n}/{s.max_review_rounds} · reviewer: {persona.cli} · {time.strftime('%Y-%m-%d %H:%M')}*"
        )

        verdict_to_flag = {
            "APPROVE": "approve",
            "REQUEST_CHANGES": "request-changes",
            "COMMENT": "comment",
        }
        gh.review(pr_number=pr_number,
                  verdict=verdict_to_flag[enforced_verdict],
                  body=review_body)

        review_baton.append_review(
            pr_number=pr_number,
            head_sha=current_head,
            reviewer=persona.cli,
            round_n=round_n,
            verdict=enforced_verdict,
            summary=parsed["summary"],
            checklist=parsed["checklist"],
            notes=parsed["notes"],
            additions=adds,
            deletions=dels,
            changed_files=pr.get("changedFiles", 0),
        )

        db.append(phase="review", action="finish", agent=persona.cli,
                  pr_number=pr_number, duration_s=run_.duration_s,
                  outcome=enforced_verdict.lower(),
                  notes={"round": round_n,
                         "model_verdict": parsed["verdict"],
                         "enforced_verdict": enforced_verdict,
                         "protected_violations": bad_paths,
                         "too_large": too_large,
                         "baton_used": bool(baton_context),
                         "head_sha": current_head,
                         "persona": persona.name})
        return 0

    except kill_switch.HaltRequested as e:
        db.append(phase="review", action="halted", agent=persona.cli,
                  pr_number=pr_number, notes={"reason": str(e)})
        return 2
    except Exception as e:
        db.append(phase="review", action="error", agent=persona.cli,
                  pr_number=pr_number, notes={"error": repr(e)})
        return 2
    finally:
        if worktree is not None:
            git_worktree.cleanup(worktree, delete_branch=True)
