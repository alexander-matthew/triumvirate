"""Frozen per-tick snapshot of a PR's loop-relevant state.

Before this module, every dispatch step re-walked ``gh.marker_posts(pr)``
five separate times (latest reviewer verdict, latest arbiter verdict,
reviewer round count, commits-since-review, commits-since-arbiter) and
re-parsed the same comment bodies with ad-hoc regex. ``PRView`` builds
that state in a single pass and exposes it as a frozen dataclass that's
trivially testable and cheap to ask multiple questions of.

The view is intentionally pure-functional: build once from the
``gh.get_pr()`` payload, then read. If you need fresher data, build a
new view.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from . import gh
from .markers import Marker, Section, extract_inline


# ---- helpers --------------------------------------------------------------


# Unambiguous sentinel embedded in arbiter-wrapper APPROVE posts. A
# bracketed token that cannot appear by accident in reviewer text or
# prose. This is the *sole* mechanism for identifying override posts —
# no legacy substring fallback, because a substring check is necessarily
# either too narrow (misses casing variations) or too broad (excludes
# legitimate reviewer notes that happen to mention the phrase). The
# sentinel is the contract; the wrapper at arbitrate_pr.py always emits
# it.
_ARBITER_OVERRIDE_SENTINEL = "[wrapper:arbiter-override]"


def _is_arbiter_override(post: dict) -> bool:
    """True if a REVIEW-marker post is actually an arbiter-wrapper APPROVE.

    The arbiter, after deciding APPROVE_FOR_MERGE, posts a synthetic
    ``##VERDICT: APPROVE`` on behalf of the agent it overrode. We filter
    these out of the reviewer-post stream so they don't double-count.
    """
    return _ARBITER_OVERRIDE_SENTINEL in (post.get("body") or "")


# ---- the view -------------------------------------------------------------


@dataclass(frozen=True)
class PRView:
    """One-pass cached view of a PR's loop-relevant state.

    Build via :meth:`from_pr` (which hits the GitHub-CLI cache once via
    ``gh.marker_posts``); read freely from there.
    """

    pr: dict
    number: int
    labels: frozenset[str]
    latest_commit_ts: str
    reviewer_posts: tuple[dict, ...]
    arbiter_posts: tuple[dict, ...]
    latest_review_verdict: str | None
    latest_arbiter_verdict: str | None
    latest_arbiter_ts: str | None
    reviewer_rounds: int
    commits_since_review: bool
    commits_since_arbiter: bool

    # ---- construction -----------------------------------------------------

    @classmethod
    def from_pr(cls, pr: dict) -> "PRView":
        commits = pr.get("commits") or []
        latest_commit_ts = (commits[-1].get("committedDate") or "") if commits else ""

        # Reviewer posts: drop synthetic arbiter-override APPROVEs.
        reviewer_posts = tuple(
            p for p in gh.marker_posts(pr, marker=Marker.REVIEW)
            if not _is_arbiter_override(p)
        )
        arbiter_posts = tuple(gh.marker_posts(pr, marker=Marker.ARBITER))

        latest_review_verdict = (
            extract_inline(reviewer_posts[-1]["body"], Section.VERDICT)
            if reviewer_posts else None
        )

        latest_arbiter_verdict: str | None = None
        latest_arbiter_ts: str | None = None
        if arbiter_posts:
            latest_arbiter_ts = arbiter_posts[-1]["ts"] or None
            latest_arbiter_verdict = extract_inline(
                arbiter_posts[-1]["body"], Section.ARBITER_VERDICT,
            )

        commits_since_review = _commits_since(reviewer_posts, commits, default=True)
        commits_since_arbiter = (
            _commits_since(arbiter_posts, commits, default=False)
            if arbiter_posts else False
        )

        labels = frozenset(l["name"] for l in (pr.get("labels") or []))

        return cls(
            pr=pr,
            number=pr["number"],
            labels=labels,
            latest_commit_ts=latest_commit_ts,
            reviewer_posts=reviewer_posts,
            arbiter_posts=arbiter_posts,
            latest_review_verdict=latest_review_verdict,
            latest_arbiter_verdict=latest_arbiter_verdict,
            latest_arbiter_ts=latest_arbiter_ts,
            reviewer_rounds=len(reviewer_posts),
            commits_since_review=commits_since_review,
            commits_since_arbiter=commits_since_arbiter,
        )

    # ---- label predicates -------------------------------------------------

    def is_agent_pr(self) -> bool:
        return any(name.startswith("agent:") for name in self.labels)

    def is_stalled(self, bad_labels: Iterable[str]) -> bool:
        return any(name in self.labels for name in bad_labels)

    def has_label(self, name: str) -> bool:
        return name in self.labels


# ---- private --------------------------------------------------------------


def _commits_since(posts: tuple[dict, ...], commits: list[dict], *, default: bool) -> bool:
    """True if the newest commit is newer than the newest post.

    ``default`` is the value returned when there are no posts to compare
    against — different callers want different conventions:
      - For reviewer posts, "no posts yet" means "yes, work to review"
        → default True.
      - For arbiter posts, "no posts yet" means "nothing to be newer than"
        → default False.
    """
    if not posts:
        return default
    last_ts = posts[-1]["ts"]
    if not commits:
        return False
    return (commits[-1].get("committedDate") or "") > last_ts
