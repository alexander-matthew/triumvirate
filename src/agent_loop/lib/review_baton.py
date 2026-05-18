"""Compact reviewer handoff state for a PR.

The first reviewer on a PR should still get broad context. Later review runs
can use this baton plus a delta diff to avoid rediscovering unchanged facts.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..config import settings


MAX_FIELD_CHARS = 1600
MAX_PROMPT_CHARS = 7000


def _dir() -> Path:
    path = settings().state_dir / "review_batons"
    path.mkdir(parents=True, exist_ok=True)
    return path


def path_for(pr_number: int) -> Path:
    return _dir() / f"pr-{pr_number}.json"


def clear(pr_number: int) -> None:
    path_for(pr_number).unlink(missing_ok=True)


def load(pr_number: int) -> dict[str, Any]:
    path = path_for(pr_number)
    if not path.exists():
        return {"pr_number": pr_number, "reviews": []}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"pr_number": pr_number, "reviews": []}
    if not isinstance(data, dict):
        return {"pr_number": pr_number, "reviews": []}
    data.setdefault("pr_number", pr_number)
    data.setdefault("reviews", [])
    return data


def _clip(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[truncated]"


def append_review(
    *,
    pr_number: int,
    head_sha: str,
    reviewer: str,
    round_n: int,
    verdict: str,
    summary: str,
    checklist: str,
    notes: str,
    additions: int,
    deletions: int,
    changed_files: int,
) -> None:
    data = load(pr_number)
    reviews = data.setdefault("reviews", [])
    reviews.append({
        "ts": time.time(),
        "head_sha": head_sha,
        "reviewer": reviewer,
        "round": round_n,
        "verdict": verdict,
        "summary": _clip(summary),
        "checklist": _clip(checklist),
        "notes": _clip(notes),
        "additions": additions,
        "deletions": deletions,
        "changed_files": changed_files,
    })
    data["updated_at"] = time.time()
    path_for(pr_number).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def has_reviews(pr_number: int) -> bool:
    return bool(load(pr_number).get("reviews"))


def latest_head_sha(pr_number: int) -> str | None:
    reviews = load(pr_number).get("reviews") or []
    for review in reversed(reviews):
        sha = review.get("head_sha")
        if isinstance(sha, str) and sha:
            return sha
    return None


def render(pr_number: int, *, current_head: str, delta_diff: str = "") -> str:
    data = load(pr_number)
    reviews = list(data.get("reviews") or [])
    if not reviews:
        return ""

    latest_prior_head = latest_head_sha(pr_number) or "unknown"
    lines = [
        "## Review Baton",
        "",
        "Use this baton to avoid spending tokens rediscovering prior review",
        "state. Keep the same review standards as the other reviewer, but focus",
        "on disagreements, uncovered risks, and changes since the last review.",
        "",
        f"Current head: `{current_head or 'unknown'}`",
        f"Last reviewed head: `{latest_prior_head}`",
        "",
        "### Prior Reviewer State",
    ]

    for review in reviews[-4:]:
        head = str(review.get("head_sha") or "unknown")[:12]
        lines.extend([
            "",
            f"- reviewer: `{review.get('reviewer', '?')}` round {review.get('round', '?')} at `{head}`",
            f"  verdict: `{review.get('verdict', '?')}`",
            f"  summary: {review.get('summary', '').strip() or '(none)'}",
        ])
        checklist = str(review.get("checklist") or "").strip()
        notes = str(review.get("notes") or "").strip()
        if checklist:
            lines.extend(["  checklist:", _indent(checklist, "    ")])
        if notes:
            lines.extend(["  notes:", _indent(notes, "    ")])

    if delta_diff.strip():
        lines.extend([
            "",
            "### Delta Since Last Reviewed Head",
            "",
            "Review this delta first, then spot-check the full branch only where",
            "needed. If the delta invalidates earlier approval, say so explicitly.",
            "",
            "```diff",
            _clip(delta_diff, 4500),
            "```",
        ])
    else:
        lines.extend([
            "",
            "### Delta Since Last Reviewed Head",
            "",
            "No branch-head delta was available. Use the baton as prior context,",
            "then review the current branch normally.",
        ])

    rendered = "\n".join(lines).strip()
    return _clip(rendered, MAX_PROMPT_CHARS)


def _indent(text: str, prefix: str) -> str:
    return "\n".join(f"{prefix}{line}" if line else prefix.rstrip() for line in text.splitlines())
