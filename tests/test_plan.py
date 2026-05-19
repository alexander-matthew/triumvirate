"""Tests for orchestrator.plan() — the pure decision step.

These exercise every branch of the state machine that, before B3, was
hidden inside _dispatch and was effectively untested. The setup is
verbose because plan() reads from many small surfaces (gh, rotation,
quota, db, time); the payoff is that the entire dispatch logic is now
covered by ~150 LOC of unit tests instead of "we trust the daemon."
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pytest

from agent_loop import config, orchestrator
from agent_loop.lib import gh as gh_lib
from agent_loop.lib import rotation
from agent_loop.lib import trust


# ---- fixtures -------------------------------------------------------------


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Spin up a Settings against a synthesized config_root in tmp_path."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("""
[project]
name = "test"
repo = "x/y"
trusted_authors = ["alexander-matthew"]
off_hours_start = 23
off_hours_end   = 6
[reviewers]
required_clis = ["codex", "gemini"]
""")
    monkeypatch.setenv("AGENT_LOOP_CONFIG_ROOT", str(tmp_path))
    config.reset()
    yield config.settings()
    config.reset()


@pytest.fixture
def stubs(monkeypatch, settings):
    """Stub every external surface plan() reads from.

    Tests configure the stubs via the dict this returns; ``plan()`` then
    behaves deterministically.
    """
    state = {
        "open_prs": [],
        "prs_by_number": {},
        "approved_issues": [],
        "proposals": [],
        "blocked_personas": set(),
        "off_hours": False,
        "weekday": 0,            # Monday
        "proposer_ran_today": False,
        "triage_ran_today": False,
        "drift_ran_this_week": False,
        "reviewer_verdicts": {},      # pr_number -> {cli -> verdict}
        "pending_reviewers": {},      # pr_number -> [cli, ...]
        "pick_reviewer": None,        # str or None
    }

    monkeypatch.setattr(gh_lib, "list_prs",
                        lambda **_: list(state["open_prs"]))
    monkeypatch.setattr(gh_lib, "get_pr",
                        lambda n: state["prs_by_number"][n])

    def list_issues_real(**kwargs):
        labels = kwargs.get("labels") or []
        first = labels[0] if labels else None
        if first == settings.label("approved"):
            return list(state["approved_issues"])
        if first == settings.label("proposal"):
            return list(state["proposals"])
        return []

    monkeypatch.setattr(gh_lib, "list_issues", list_issues_real)

    monkeypatch.setattr(rotation, "reviewer_verdicts",
                        lambda n: dict(state["reviewer_verdicts"].get(n, {})))
    monkeypatch.setattr(rotation, "pending_reviewer_clis",
                        lambda n: list(state["pending_reviewers"].get(n, [])))
    monkeypatch.setattr(rotation, "pick_reviewer_cli",
                        lambda n: state["pick_reviewer"])
    monkeypatch.setattr(rotation, "reviewer_persona_name",
                        lambda cli: f"reviewer-{cli}")

    monkeypatch.setattr(orchestrator, "_persona_blocked",
                        lambda name: name in state["blocked_personas"])
    monkeypatch.setattr(orchestrator, "_is_off_hours",
                        lambda: state["off_hours"])
    monkeypatch.setattr(orchestrator, "_proposer_ran_today",
                        lambda: state["proposer_ran_today"])
    monkeypatch.setattr(orchestrator, "_triage_ran_today",
                        lambda: state["triage_ran_today"])
    monkeypatch.setattr(orchestrator, "_drift_ran_this_week",
                        lambda: state["drift_ran_this_week"])

    class _FakeNow:
        weekday_ = 0
        @classmethod
        def now(cls):
            class Now:
                weekday__ = cls.weekday_
                def weekday(self): return _FakeNow.weekday_
            return Now()
    monkeypatch.setattr(orchestrator.dt, "datetime", _FakeNow)

    monkeypatch.setattr(trust, "filter_trusted_marker_posts", lambda posts: posts)
    return state


def _make_pr(
    *,
    number: int = 42,
    labels: Iterable[str] = ("agent:authored-by-claude",),
    commits: Iterable[str] = (),
    reviews: Iterable[tuple[str, str]] = (),
    comments: Iterable[tuple[str, str]] = (),
    headRefName: str = "feature/x",
    createdAt: str = "2026-01-01T00:00:00Z",
) -> dict:
    return {
        "number": number,
        "createdAt": createdAt,
        "labels": [{"name": n} for n in labels],
        "commits": [{"committedDate": ts} for ts in commits],
        "reviews": [
            {"submittedAt": ts, "body": body,
             "author": {"login": "alexander-matthew"}}
            for ts, body in reviews
        ],
        "comments": [
            {"createdAt": ts, "body": body,
             "author": {"login": "alexander-matthew"}}
            for ts, body in comments
        ],
        "headRefName": headRefName,
    }


def _add_pr(state, pr):
    state["open_prs"].append(pr)
    state["prs_by_number"][pr["number"]] = pr


# ---- noop -----------------------------------------------------------------


def test_no_prs_and_not_off_hours_is_noop(stubs):
    d = orchestrator.plan()
    assert d.phase == "noop"


# ---- pending reviewer dispatch -------------------------------------------


def test_pending_reviewer_dispatches_review_phase(stubs):
    _add_pr(stubs, _make_pr(number=42))
    stubs["pending_reviewers"][42] = ["codex"]
    stubs["pick_reviewer"] = "codex"

    d = orchestrator.plan()
    assert d.phase == "review"
    assert d.target == 42
    assert d.cli == "codex"


def test_pending_reviewer_but_persona_blocked_advances_to_next_pr(stubs):
    _add_pr(stubs, _make_pr(number=42))
    stubs["pending_reviewers"][42] = ["codex"]
    stubs["pick_reviewer"] = "codex"
    stubs["blocked_personas"].add("reviewer-codex")

    d = orchestrator.plan()
    assert d.phase == "noop"


# ---- consensus → security → librarian → merge ----------------------------


def _consensus_approve_pr(state):
    pr = _make_pr(number=42, commits=["2026-01-01T00:00:00Z"])
    _add_pr(state, pr)
    state["reviewer_verdicts"][42] = {"codex": "APPROVE", "gemini": "APPROVE"}


def test_consensus_approve_runs_security_first(stubs):
    _consensus_approve_pr(stubs)
    d = orchestrator.plan()
    assert d.phase == "security"
    assert d.target == 42


def test_security_skipped_when_already_cleared_runs_librarian(stubs):
    pr = _make_pr(
        number=42,
        labels=("agent:authored-by-claude", "agent:security-cleared"),
        commits=["2026-01-01T00:00:00Z"],
    )
    _add_pr(stubs, pr)
    stubs["reviewer_verdicts"][42] = {"codex": "APPROVE", "gemini": "APPROVE"}
    d = orchestrator.plan()
    assert d.phase == "librarian"


def test_both_cleared_runs_merge(stubs):
    pr = _make_pr(
        number=42,
        labels=(
            "agent:authored-by-claude",
            "agent:security-cleared",
            "agent:librarian-cleared",
        ),
        commits=["2026-01-01T00:00:00Z"],
    )
    _add_pr(stubs, pr)
    stubs["reviewer_verdicts"][42] = {"codex": "APPROVE", "gemini": "APPROVE"}
    d = orchestrator.plan()
    assert d.phase == "merge"


# ---- arbiter spoken: REQUEST_FINAL_CHANGES --------------------------------


def test_arbiter_request_final_changes_with_new_commits_re_arbitrates(stubs):
    arb_body = "##ARBITER_VERDICT: REQUEST_FINAL_CHANGES\n##REASONING:\nfix x\n"
    pr = _make_pr(
        number=42,
        commits=["2026-01-02T00:00:00Z"],
        comments=[("2026-01-01T00:00:00Z", arb_body)],
    )
    _add_pr(stubs, pr)
    d = orchestrator.plan()
    assert d.phase == "arbitrate"


def test_arbiter_request_final_changes_no_new_commits_dispatches_respond(stubs):
    arb_body = "##ARBITER_VERDICT: REQUEST_FINAL_CHANGES\n##REASONING:\nfix x\n"
    pr = _make_pr(
        number=42,
        commits=["2026-01-01T00:00:00Z"],
        comments=[("2026-01-02T00:00:00Z", arb_body)],
    )
    _add_pr(stubs, pr)
    d = orchestrator.plan()
    assert d.phase == "respond"


# ---- round cap → arbiter --------------------------------------------------


def test_round_cap_reached_triggers_arbiter(stubs):
    review_body_rc = "##VERDICT: REQUEST_CHANGES\n##SUMMARY: x\n##CHECKLIST:\n- [ ] y\n"
    pr = _make_pr(
        number=42,
        commits=["2026-01-01T00:00:00Z"],
        reviews=[
            ("2026-01-01T01:00:00Z", review_body_rc),
            ("2026-01-01T02:00:00Z", review_body_rc),
            ("2026-01-01T03:00:00Z", review_body_rc),
        ],
    )
    _add_pr(stubs, pr)
    stubs["reviewer_verdicts"][42] = {"codex": "REQUEST_CHANGES", "gemini": "REQUEST_CHANGES"}
    d = orchestrator.plan()
    assert d.phase == "arbitrate"


# ---- engineer responds to REQUEST_CHANGES ---------------------------------


def test_all_reviewers_in_one_request_changes_dispatches_respond(stubs):
    rc = "##VERDICT: REQUEST_CHANGES\n##SUMMARY: x\n##CHECKLIST:\n- [ ] y\n"
    ap = "##VERDICT: APPROVE\n##SUMMARY: x\n##CHECKLIST:\n- [x] y\n"
    pr = _make_pr(
        number=42,
        commits=["2026-01-01T00:00:00Z"],
        reviews=[
            ("2026-01-01T01:00:00Z", rc),
            ("2026-01-01T02:00:00Z", ap),
        ],
    )
    _add_pr(stubs, pr)
    stubs["reviewer_verdicts"][42] = {"codex": "REQUEST_CHANGES", "gemini": "APPROVE"}
    d = orchestrator.plan()
    assert d.phase == "respond"


# ---- off-hours: work / propose / triage / drift ---------------------------


def test_off_hours_with_approved_issue_dispatches_work(stubs):
    stubs["off_hours"] = True
    stubs["approved_issues"] = [{"number": 7}]
    d = orchestrator.plan()
    assert d.phase == "work"


def test_off_hours_no_approved_issue_dispatches_propose(stubs):
    stubs["off_hours"] = True
    d = orchestrator.plan()
    assert d.phase == "propose"


def test_off_hours_proposer_done_triage_runs_if_proposals_exist(stubs):
    stubs["off_hours"] = True
    stubs["proposer_ran_today"] = True
    stubs["proposals"] = [{"number": 9, "title": "spec", "labels": [],
                           "author": {"login": "alexander-matthew"}, "body": ""}]
    d = orchestrator.plan()
    assert d.phase == "triage"


def test_off_hours_sunday_drift_runs(stubs):
    stubs["off_hours"] = True
    stubs["proposer_ran_today"] = True
    stubs["triage_ran_today"] = True
    orchestrator.dt.datetime.weekday_ = 6
    d = orchestrator.plan()
    assert d.phase == "drift"


def test_off_hours_sunday_after_drift_runs_synthesis(stubs, monkeypatch):
    stubs["off_hours"] = True
    stubs["proposer_ran_today"] = True
    stubs["triage_ran_today"] = True
    stubs["drift_ran_this_week"] = True
    monkeypatch.setattr(orchestrator, "_synthesis_ran_this_week", lambda: False)
    orchestrator.dt.datetime.weekday_ = 6
    d = orchestrator.plan()
    assert d.phase == "synthesis"


# ---- phases disabled in config -------------------------------------------


def test_disabled_phase_skipped(tmp_path, monkeypatch):
    """If [phases].enabled excludes a phase, plan() must not return it."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("""
[project]
name = "test"
repo = "x/y"
trusted_authors = []
off_hours_start = 23
off_hours_end   = 6

[phases]
enabled = ["work", "review", "respond", "arbitrate", "merge"]
""")
    monkeypatch.setenv("AGENT_LOOP_CONFIG_ROOT", str(tmp_path))
    config.reset()

    # Wire up consensus-APPROVE PR with neither security nor librarian labels.
    pr = _make_pr(
        number=42,
        commits=["2026-01-01T00:00:00Z"],
    )
    monkeypatch.setattr(gh_lib, "list_prs", lambda **_: [pr])
    monkeypatch.setattr(gh_lib, "get_pr", lambda n: pr)
    monkeypatch.setattr(gh_lib, "list_issues", lambda **_: [])
    monkeypatch.setattr(rotation, "reviewer_verdicts",
                        lambda n: {"codex": "APPROVE", "gemini": "APPROVE"})
    monkeypatch.setattr(rotation, "pending_reviewer_clis", lambda n: [])
    monkeypatch.setattr(rotation, "pick_reviewer_cli", lambda n: None)
    monkeypatch.setattr(orchestrator, "_persona_blocked", lambda name: False)
    monkeypatch.setattr(orchestrator, "_is_off_hours", lambda: False)
    monkeypatch.setattr(trust, "filter_trusted_marker_posts", lambda posts: posts)

    # Security and librarian disabled → plan should skip straight to merge.
    d = orchestrator.plan()
    assert d.phase == "merge"

    config.reset()
