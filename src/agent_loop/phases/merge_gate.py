"""Merge gate: pure logic, no agent. Decides whether to auto-merge a PR."""
from __future__ import annotations

import time

from ..config import settings
from ..lib import db, gh, kill_switch, protected, review_baton, rotation


def _ci_state(pr: dict) -> str:
    rolls = pr.get("statusCheckRollup") or []
    if not rolls:
        return "unknown"
    states = set()
    for r in rolls:
        c = r.get("conclusion") or r.get("state")
        sst = r.get("status")
        if sst and sst != "COMPLETED":
            states.add("pending")
        if c:
            states.add(c.upper())
    if "pending" in states or {"IN_PROGRESS", "QUEUED", "WAITING"} & states:
        return "pending"
    bad = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
    if bad & states:
        return "failing"
    if states <= {"SUCCESS", "COMPLETED", "NEUTRAL", "SKIPPED"}:
        return "green"
    return "unknown"


def _gate_reasons(pr: dict) -> list[str]:
    s = settings()
    reasons: list[str] = []

    labels = {l["name"] for l in pr.get("labels", [])}
    if s.label("veto") in labels:
        reasons.append(f"{s.label('veto')} label set")
    if s.label("needs_human") in labels:
        reasons.append(f"{s.label('needs_human')} label set")
    if s.label("protected_violation") in labels:
        reasons.append(f"{s.label('protected_violation')} label set")
    if s.label("too_large") in labels:
        reasons.append(f"{s.label('too_large')} label set")
    if s.label("security_flag") in labels:
        reasons.append(f"{s.label('security_flag')} label set")
    if s.label("librarian_flag") in labels:
        reasons.append(f"{s.label('librarian_flag')} label set")
    # Both pre-merge clearances must be present.
    if s.label("security_cleared") not in labels:
        reasons.append(f"{s.label('security_cleared')} label missing")
    if s.label("librarian_cleared") not in labels:
        reasons.append(f"{s.label('librarian_cleared')} label missing")
    if pr.get("isDraft"):
        reasons.append("PR is draft")

    # Consensus check: every required cli must have APPROVE'd the latest commit.
    pr_number = pr["number"]
    verdicts = rotation.reviewer_verdicts(pr_number)
    missing = [c for c in s.required_reviewer_clis if c not in verdicts]
    if missing:
        reasons.append(f"missing reviews from required agents: {missing}")
    for cli, v in verdicts.items():
        if v != "APPROVE":
            reasons.append(f"latest {cli} verdict is {v}")

    files = pr.get("files") or []
    bad = protected.violations([f.get("path") for f in files if f.get("path")])
    if bad:
        reasons.append(f"diff touches protected paths: {bad}")

    adds = pr.get("additions", 0)
    dels = pr.get("deletions", 0)
    if (adds + dels) > s.max_diff_loc:
        reasons.append(f"diff is {adds + dels} LOC, cap is {s.max_diff_loc}")

    state = _ci_state(pr)
    if state != "green":
        reasons.append(f"CI state is {state}")

    mergeable = (pr.get("mergeable") or "").upper()
    if mergeable == "CONFLICTING":
        reasons.append("PR has merge conflicts")

    return reasons


def evaluate(pr_number: int) -> int:
    """Returns 0 on merge, 1 on hold, 2 on error."""
    kill_switch.check(reason="merge_gate start")
    try:
        pr = gh.get_pr(pr_number)
    except Exception as e:
        db.append(phase="merge", action="error", pr_number=pr_number,
                  notes={"error": repr(e)})
        return 2

    reasons = _gate_reasons(pr)
    if reasons:
        db.append(phase="merge", action="skip", pr_number=pr_number,
                  outcome="held", notes={"reasons": reasons})
        return 1

    started = time.time()
    db.append(phase="merge", action="start", pr_number=pr_number)
    try:
        gh.merge_pr(number=pr_number, method="squash")
    except Exception as e:
        db.append(phase="merge", action="error", pr_number=pr_number,
                  duration_s=time.time() - started,
                  notes={"error": repr(e)})
        return 2

    db.append(phase="merge", action="finish", pr_number=pr_number,
              outcome="merged", duration_s=time.time() - started)
    review_baton.clear(pr_number)
    return 0
