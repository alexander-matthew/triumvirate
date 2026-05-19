"""Tests for PRView — the per-tick cached snapshot of PR loop state."""
from __future__ import annotations

import pytest

from agent_loop.lib import gh, pr_view
from agent_loop.lib.pr_view import PRView


# ---- fixtures -------------------------------------------------------------


@pytest.fixture(autouse=True)
def stub_trust(monkeypatch):
    """gh.marker_posts filters by trusted authors via lib.trust. Bypass it in
    unit tests — we just want PRView's parsing behavior, not auth."""
    from agent_loop.lib import trust
    monkeypatch.setattr(trust, "filter_trusted_marker_posts", lambda posts: posts)


def _pr(
    *,
    number: int = 42,
    labels: list[str] | None = None,
    commits: list[str] | None = None,
    reviews: list[tuple[str, str]] | None = None,   # (ts, body)
    comments: list[tuple[str, str]] | None = None,  # (ts, body)
) -> dict:
    """Synthesize a minimal gh.get_pr() payload."""
    return {
        "number": number,
        "labels": [{"name": n} for n in (labels or [])],
        "commits": [{"committedDate": ts} for ts in (commits or [])],
        "reviews": [
            {"submittedAt": ts, "body": body, "author": {"login": "alexander-matthew"}}
            for ts, body in (reviews or [])
        ],
        "comments": [
            {"createdAt": ts, "body": body, "author": {"login": "alexander-matthew"}}
            for ts, body in (comments or [])
        ],
    }


# ---- empty PR -------------------------------------------------------------


def test_view_of_empty_pr():
    v = PRView.from_pr(_pr())
    assert v.number == 42
    assert v.labels == frozenset()
    assert v.latest_commit_ts == ""
    assert v.reviewer_posts == ()
    assert v.arbiter_posts == ()
    assert v.latest_review_verdict is None
    assert v.latest_arbiter_verdict is None
    assert v.latest_arbiter_ts is None
    assert v.reviewer_rounds == 0
    # No reviewer posts → assume there's something new to review.
    assert v.commits_since_review is True
    # No arbiter posts → no arbiter timestamp to be newer than.
    assert v.commits_since_arbiter is False


# ---- labels ---------------------------------------------------------------


def test_label_predicates():
    v = PRView.from_pr(_pr(labels=["agent:authored-by-claude", "wontfix"]))
    assert v.is_agent_pr() is True
    assert v.has_label("agent:authored-by-claude")
    assert not v.has_label("agent:halt")
    assert v.is_stalled({"agent:halt", "agent:needs-human"}) is False
    assert v.is_stalled({"agent:halt", "wontfix"}) is True


def test_is_agent_pr_requires_agent_prefix():
    v = PRView.from_pr(_pr(labels=["bug", "good first issue"]))
    assert v.is_agent_pr() is False


# ---- reviewer parsing -----------------------------------------------------


def test_latest_review_verdict_extracted():
    v = PRView.from_pr(_pr(
        commits=["2026-05-18T10:00:00Z"],
        reviews=[
            ("2026-05-18T11:00:00Z", "##VERDICT: REQUEST_CHANGES\n##SUMMARY: see notes\n"),
            ("2026-05-18T12:00:00Z", "##VERDICT: APPROVE\n##SUMMARY: lgtm\n"),
        ],
    ))
    assert v.latest_review_verdict == "APPROVE"
    assert v.reviewer_rounds == 2


def test_arbiter_override_posts_excluded_from_reviewer_stream():
    # An arbiter-wrapper APPROVE posts a synthetic ##VERDICT: APPROVE on
    # behalf of the overridden agent. PRView must not count it as a real
    # reviewer round, otherwise the round cap drifts.
    override_body = (
        "##VERDICT: APPROVE\n"
        "##SUMMARY: Arbiter override — see arbiter verdict above.\n"
        "##NOTES:\nThis APPROVE is posted by the arbiter wrapper\n"
        "[wrapper:arbiter-override]\n"
    )
    real_review_body = "##VERDICT: REQUEST_CHANGES\n##SUMMARY: needs work\n"
    v = PRView.from_pr(_pr(
        commits=["2026-05-18T10:00:00Z"],
        reviews=[("2026-05-18T11:00:00Z", real_review_body)],
        comments=[("2026-05-18T12:00:00Z", override_body)],
    ))
    assert v.reviewer_rounds == 1
    assert v.latest_review_verdict == "REQUEST_CHANGES"


def test_arbiter_override_requires_sentinel():
    # Sole mechanism is the [wrapper:arbiter-override] sentinel. Posts
    # without it — even those whose summary mentions "arbiter override"
    # — are real reviewer posts and must NOT be filtered.
    summary_mentioning_phrase = (
        "##VERDICT: APPROVE\n"
        "##SUMMARY: Arbiter override looked reasonable; agreeing.\n"
        "##CHECKLIST:\n- [x] y\n"
    )
    v = PRView.from_pr(_pr(
        commits=["2026-05-18T10:00:00Z"],
        comments=[("2026-05-18T12:00:00Z", summary_mentioning_phrase)],
    ))
    assert v.reviewer_rounds == 1  # NOT excluded — no sentinel


def test_reviewer_mentioning_arbiter_override_is_not_excluded():
    # gemini's R1 review of PR #3 flagged that any non-sentinel-based
    # detection would risk false positives on reviewer prose.
    real_review_with_mention = (
        "##VERDICT: REQUEST_CHANGES\n"
        "##SUMMARY: see notes\n"
        "##CHECKLIST:\n- [ ] x\n"
        "##NOTES:\nThe prior arbiter override of this PR was wrong; "
        "please revisit. [wrapper:arbiter-override-not-this-one]\n"
    )
    v = PRView.from_pr(_pr(
        commits=["2026-05-18T10:00:00Z"],
        reviews=[("2026-05-18T12:00:00Z", real_review_with_mention)],
    ))
    assert v.reviewer_rounds == 1
    assert v.latest_review_verdict == "REQUEST_CHANGES"


# ---- arbiter parsing ------------------------------------------------------


def test_latest_arbiter_verdict_extracted():
    v = PRView.from_pr(_pr(
        commits=["2026-05-18T10:00:00Z"],
        comments=[
            ("2026-05-18T13:00:00Z",
             "##ARBITER_VERDICT: APPROVE_FOR_MERGE\n##REASONING:\nlooks fine\n"),
        ],
    ))
    assert v.latest_arbiter_verdict == "APPROVE_FOR_MERGE"
    assert v.latest_arbiter_ts == "2026-05-18T13:00:00Z"


# ---- commits_since_* ------------------------------------------------------


def test_commits_since_review_true_when_new_commit_after_review():
    v = PRView.from_pr(_pr(
        commits=["2026-05-18T10:00:00Z", "2026-05-18T14:00:00Z"],
        reviews=[("2026-05-18T12:00:00Z", "##VERDICT: REQUEST_CHANGES\n##SUMMARY: x\n")],
    ))
    assert v.commits_since_review is True


def test_commits_since_review_false_when_no_new_commit():
    v = PRView.from_pr(_pr(
        commits=["2026-05-18T10:00:00Z"],
        reviews=[("2026-05-18T12:00:00Z", "##VERDICT: REQUEST_CHANGES\n##SUMMARY: x\n")],
    ))
    assert v.commits_since_review is False


def test_commits_since_arbiter_false_when_no_arbiter_post():
    v = PRView.from_pr(_pr(commits=["2026-05-18T10:00:00Z"]))
    assert v.commits_since_arbiter is False


def test_commits_since_arbiter_true_when_commit_after_arbiter():
    v = PRView.from_pr(_pr(
        commits=["2026-05-18T15:00:00Z"],
        comments=[("2026-05-18T13:00:00Z",
                   "##ARBITER_VERDICT: REQUEST_FINAL_CHANGES\n##REASONING:\nfix x\n")],
    ))
    assert v.commits_since_arbiter is True


# ---- frozenness -----------------------------------------------------------


def test_view_is_frozen():
    v = PRView.from_pr(_pr())
    with pytest.raises(Exception):
        v.number = 99  # type: ignore[misc]
