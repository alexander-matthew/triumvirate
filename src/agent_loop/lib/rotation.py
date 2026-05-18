"""Pick which reviewer CLI runs against a given PR.

Rules:
  - Consensus: all `Settings.required_reviewer_clis` must provide an APPROVE
    verdict for the CURRENT commit before a PR is eligible for merge.
  - Sticky per round: a specific agent owns its review thread per commit.
  - Quota-aware: prefer whichever required cli is armed.
  - Arbiter: the third leg of the stool, invoked when consensus fails.
"""
from __future__ import annotations

import re
import sqlite3

from . import gh, quota
from ..config import settings


def required_reviewer_clis() -> tuple[str, ...]:
    return settings().required_reviewer_clis


# ---- consensus state --------------------------------------------------------


def reviewer_verdicts(pr_number: int) -> dict[str, str]:
    """Latest verdict from each agent for the CURRENT commit of the PR.

    Returns {cli: verdict} where verdict is APPROVE | REQUEST_CHANGES | COMMENT.
    Only considers reviews posted *after* the latest commit. Honors arbiter
    overrides — if the arbiter posted APPROVE_FOR_MERGE, we treat that as a
    synthetic APPROVE from the agent it replaced.
    """
    pr = gh.get_pr(pr_number)
    commits = pr.get("commits") or []
    if not commits:
        return {}

    latest_commit_ts = commits[-1].get("committedDate") or ""
    posts = gh.marker_posts(pr)
    verdicts: dict[str, str] = {}

    # Arbiter overrides. Regex anchors to the `*arbiter: <cli>` trailer line so
    # prose mentions of "arbiter:" inside the verdict body can't be confused
    # with the agent identity.
    arb_posts = gh.marker_posts(pr, marker="##ARBITER_VERDICT:")
    if arb_posts and arb_posts[-1]["ts"] > latest_commit_ts:
        m = re.search(r"##ARBITER_VERDICT:\s*(\S+)", arb_posts[-1]["body"])
        if m and m.group(1) == "APPROVE_FOR_MERGE":
            m_arb_agent = re.search(r"\*arbiter:\s*(\w+)", arb_posts[-1]["body"])
            if m_arb_agent:
                arb_agent = m_arb_agent.group(1)
                overridden_agent = "codex" if arb_agent == "gemini" else "gemini"
                verdicts[overridden_agent] = "APPROVE"

    for post in reversed(posts):
        if post["ts"] <= latest_commit_ts:
            break
        # Anchor to the `*Round N/M · reviewer: <cli>` trailer line.
        m_agent = re.search(r"\*Round\s+\d+/\d+\s*·\s*reviewer:\s*(\w+)", post["body"])
        m_verdict = re.search(r"##VERDICT:\s*(\S+)", post["body"])
        if m_agent and m_verdict:
            agent = m_agent.group(1)
            verdict = m_verdict.group(1)
            if agent not in verdicts:
                verdicts[agent] = verdict

    return verdicts


def pending_reviewer_clis(pr_number: int) -> list[str]:
    verdicts = reviewer_verdicts(pr_number)
    return [c for c in required_reviewer_clis() if c not in verdicts]


def pick_reviewer_cli(pr_number: int) -> str | None:
    """Pick one of the pending reviewers to run next.

    Prioritizes: (1) pending reviewers that aren't rate-limited;
    (2) load balance — fewest total reviews in history.
    Returns None if no reviewers are pending for the current commit.
    """
    pending = pending_reviewer_clis(pr_number)
    if not pending:
        return None

    armed = [c for c in pending if not quota.is_blocked(c)[0]]
    if not armed:
        # All blocked: return whichever resets soonest so we record the wait.
        return min(pending, key=lambda c: quota.is_blocked(c)[1] or float("inf"))
    if len(armed) == 1:
        return armed[0]

    def total_reviews(cli: str) -> int:
        conn = sqlite3.connect(settings().db_path, timeout=5)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM events WHERE phase='review' AND action='finish' AND agent=?",
                (cli,)
            ).fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    return min(armed, key=total_reviews)


def reviewer_persona_name(cli: str) -> str:
    return {"codex": "reviewer-codex", "gemini": "reviewer-gemini"}[cli]


# ---- arbiter ----------------------------------------------------------------


def pick_arbiter_cli(pr_number: int) -> str | None:
    """Choose the cli to arbitrate a stuck PR.

    Order: (1) cli with fewest rounds on this PR; (2) tie-break by armed
    quota; (3) deterministic PR-parity alternation if all else equal.
    """
    pr = gh.get_pr(pr_number)
    posts = gh.marker_posts(pr)

    clis = required_reviewer_clis()
    counts = {c: 0 for c in clis}
    for post in posts:
        m = re.search(r"\*Round\s+\d+/\d+\s*·\s*reviewer:\s*(\w+)", post["body"])
        if m and m.group(1) in counts:
            counts[m.group(1)] += 1

    min_count = min(counts.values())
    candidates = [c for c, n in counts.items() if n == min_count]
    if len(candidates) == 1:
        return candidates[0]

    armed = [c for c in candidates if not quota.is_blocked(c)[0]]
    if len(armed) == 1:
        return armed[0]
    if not armed:
        return min(candidates, key=lambda c: quota.is_blocked(c)[1] or float("inf"))

    return armed[pr_number % len(armed)]


def arbiter_persona_name(cli: str) -> str:
    return {"codex": "arbiter-codex", "gemini": "arbiter-gemini"}[cli]
