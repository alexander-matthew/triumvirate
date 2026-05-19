"""Tests for the constitutional fidelity layer of merge_gate.

Covers the three new checks introduced in B6-post-ratification:
  - tier-3 detection (hardcoded surfaces + config augmentation + label)
  - tier-3 unanimous-three approval requirement
  - hard-limit human-author enforcement
"""
from __future__ import annotations

from typing import Iterable

import pytest

from agent_loop import config
from agent_loop.lib import bots, rotation, trust
from agent_loop.phases import merge_gate


@pytest.fixture(autouse=True)
def _stub_trust(monkeypatch):
    """Bypass trust filtering in gh.marker_posts — tests synthesize their
    own posts with the standard ``alexander-matthew`` author and don't
    need the real trust pipeline."""
    monkeypatch.setattr(trust, "filter_trusted_marker_posts", lambda posts: posts)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("""
[project]
name = "test"
repo = "x/y"
trusted_authors = ["alexander-matthew"]

[guards]
protected_paths = [".github/workflows/"]
requires_full_consensus_paths = ["agents/extra.md"]

[reviewers]
required_clis = ["codex", "gemini"]
""")
    monkeypatch.setenv("AGENT_LOOP_CONFIG_ROOT", str(tmp_path))
    config.reset()
    yield config.settings()
    config.reset()


def _pr(*, files: Iterable[str], labels: Iterable[str] = (),
        commits: Iterable[dict] | None = None,
        reviews: Iterable[dict] | None = None,
        comments: Iterable[dict] | None = None,
        number: int = 7) -> dict:
    return {
        "number": number,
        "files": [{"path": p} for p in files],
        "labels": [{"name": n} for n in labels],
        "commits": list(commits or []),
        "reviews": list(reviews or []),
        "comments": list(comments or []),
    }


def _review_post(*, cli: str, verdict: str, ts: str, round_n: int = 1) -> dict:
    """A synthesized reviewer review-comment, anchored to the trailer
    format that ``_explicit_approves_for_latest_commit`` parses."""
    body = (
        f"##VERDICT: {verdict}\n"
        f"##SUMMARY: synth\n"
        f"##CHECKLIST:\n- [x] y\n"
        f"\n---\n*Round {round_n}/3 · reviewer: {cli} · 2026-05-18 22:00*"
    )
    return {
        "submittedAt": ts,
        "body": body,
        "author": {"login": "alexander-matthew"},
    }


# ---- _is_tier3_pr ---------------------------------------------------------


class TestTier3Detection:
    def test_constitution_file_triggers_hardcoded_tier3(self, settings):
        is3, reasons = merge_gate._is_tier3_pr(_pr(files=["templates/constitution.md"]))
        assert is3
        assert any("hardcoded" in r and "constitution" in r for r in reasons)

    def test_persona_file_triggers_hardcoded_tier3(self, settings):
        is3, reasons = merge_gate._is_tier3_pr(_pr(files=["templates/personas/reviewer-codex.md"]))
        assert is3
        assert any("persona prompt" in r for r in reasons)

    def test_agents_persona_dir_also_hardcoded(self, settings):
        is3, _ = merge_gate._is_tier3_pr(_pr(files=["agents/personas/engineer.md"]))
        assert is3

    def test_unrelated_file_is_not_tier3(self, settings):
        is3, reasons = merge_gate._is_tier3_pr(_pr(files=["src/agent_loop/lib/db.py"]))
        assert not is3
        assert reasons == []

    def test_requires_full_consensus_paths_config_augments(self, settings):
        # agents/extra.md was put in the config above.
        is3, reasons = merge_gate._is_tier3_pr(_pr(files=["agents/extra.md"]))
        assert is3
        assert any("config" in r for r in reasons)

    def test_label_elevation_triggers_tier3(self, settings):
        is3, reasons = merge_gate._is_tier3_pr(
            _pr(files=["src/main.py"], labels=["requires:full-consensus"])
        )
        assert is3
        assert any("label" in r for r in reasons)


# ---- _tier3_missing_approvals --------------------------------------------


class TestTier3Approvals:
    """Tests for _tier3_missing_approvals.

    The constitution requires explicit ##VERDICT: APPROVE from each of
    {claude, codex, gemini}. Critically, an arbiter APPROVE_FOR_MERGE
    must NOT synthesize approval for the CLI it overrode — that would
    let the arbiter satisfy unanimous-three single-handedly. The
    implementation bypasses rotation.reviewer_verdicts (which DOES
    synthesize) and reads raw posts from PRView. (Reported by gemini
    on PR #3 R1 review.)
    """

    def test_missing_when_no_verdicts(self, settings):
        pr = _pr(files=[], commits=[{"committedDate": "2026-05-18T10:00:00Z"}])
        missing = merge_gate._tier3_missing_approvals(pr)
        assert set(missing) == {"claude", "codex", "gemini"}

    def test_missing_one_cli(self, settings):
        pr = _pr(
            files=[],
            commits=[{"committedDate": "2026-05-18T10:00:00Z"}],
            reviews=[
                _review_post(cli="codex", verdict="APPROVE", ts="2026-05-18T11:00:00Z"),
                _review_post(cli="gemini", verdict="APPROVE", ts="2026-05-18T11:30:00Z"),
            ],
        )
        missing = merge_gate._tier3_missing_approvals(pr)
        assert missing == ["claude"]

    def test_request_changes_counts_as_missing(self, settings):
        pr = _pr(
            files=[],
            commits=[{"committedDate": "2026-05-18T10:00:00Z"}],
            reviews=[
                _review_post(cli="claude", verdict="APPROVE", ts="2026-05-18T11:00:00Z"),
                _review_post(cli="codex", verdict="REQUEST_CHANGES", ts="2026-05-18T11:30:00Z"),
                _review_post(cli="gemini", verdict="APPROVE", ts="2026-05-18T12:00:00Z"),
            ],
        )
        missing = merge_gate._tier3_missing_approvals(pr)
        assert missing == ["codex"]

    def test_all_three_approve_returns_empty(self, settings):
        pr = _pr(
            files=[],
            commits=[{"committedDate": "2026-05-18T10:00:00Z"}],
            reviews=[
                _review_post(cli="claude", verdict="APPROVE", ts="2026-05-18T11:00:00Z"),
                _review_post(cli="codex", verdict="APPROVE", ts="2026-05-18T11:30:00Z"),
                _review_post(cli="gemini", verdict="APPROVE", ts="2026-05-18T12:00:00Z"),
            ],
        )
        assert merge_gate._tier3_missing_approvals(pr) == []

    def test_arbiter_override_does_NOT_satisfy_unanimous_three(self, settings):
        """Critical: an arbiter APPROVE_FOR_MERGE posts a synthetic
        ##VERDICT: APPROVE wrapper on behalf of the overridden CLI.
        rotation.reviewer_verdicts would count that. _tier3_missing_approvals
        must NOT, because the constitution says the arbiter cannot
        substitute for unanimous-three on tier-3 changes."""
        arbiter_wrapper_body = (
            "##VERDICT: APPROVE\n"
            "##SUMMARY: Arbiter override — see arbiter verdict above.\n"
            "##CHECKLIST:\n- [x] Arbiter approved for merge\n"
            "##NOTES:\nThis APPROVE is posted by the arbiter wrapper.\n"
            "[wrapper:arbiter-override]\n"
            "\n---\n*Round 1/3 · reviewer: claude · 2026-05-18 22:00*"
        )
        pr = _pr(
            files=[],
            commits=[{"committedDate": "2026-05-18T10:00:00Z"}],
            reviews=[
                _review_post(cli="codex", verdict="APPROVE", ts="2026-05-18T11:00:00Z"),
                _review_post(cli="gemini", verdict="APPROVE", ts="2026-05-18T11:30:00Z"),
            ],
            comments=[{
                "createdAt": "2026-05-18T12:00:00Z",
                "body": arbiter_wrapper_body,
                "author": {"login": "alexander-matthew"},
            }],
        )
        # The synthetic claude APPROVE in the wrapper must NOT count.
        missing = merge_gate._tier3_missing_approvals(pr)
        assert "claude" in missing


# ---- _is_hard_limit_pr ----------------------------------------------------


class TestHardLimitDetection:
    def test_live_config_edit_triggers_hard_limit(self, settings):
        ok, reasons = merge_gate._is_hard_limit_pr(_pr(files=["config.toml"]))
        assert ok
        assert any("live config" in r for r in reasons)

    def test_agents_config_edit_triggers_hard_limit(self, settings):
        ok, reasons = merge_gate._is_hard_limit_pr(_pr(files=["agents/config.toml"]))
        assert ok

    def test_template_config_example_does_not_trigger(self, settings):
        # Editing the example template is tier 2, not hard-limit — it's not
        # the live config that governs an actual project.
        ok, _ = merge_gate._is_hard_limit_pr(_pr(files=["templates/config.toml.example"]))
        assert not ok

    def test_protected_path_violations_handled_by_pre_existing_check(self, settings):
        # Per codex R1: protected-path file edits are *categorically*
        # refused by the pre-existing protected.violations() check in
        # _gate_reasons, regardless of authorship. _is_hard_limit_pr
        # therefore does NOT also flag them — that would duplicate the
        # reason. This test documents the design.
        ok, reasons = merge_gate._is_hard_limit_pr(
            _pr(files=[".github/workflows/ci.yml"])
        )
        assert not ok
        assert reasons == []

    def test_unrelated_file_is_not_hard_limit(self, settings):
        ok, _ = merge_gate._is_hard_limit_pr(_pr(files=["README.md"]))
        assert not ok


# ---- _all_commits_human_authored ------------------------------------------


class TestHumanAuthorship:
    def test_refuses_when_no_bots_configured(self, settings, monkeypatch):
        monkeypatch.setattr(bots, "configured_bot_logins", lambda: set())
        ok, offender = merge_gate._all_commits_human_authored(
            _pr(files=[], commits=[{"author": {"login": "alexander-matthew"}}])
        )
        assert not ok
        assert offender is None  # could not prove, not because of a specific bot

    def test_refuses_when_no_commit_authors_present(self, settings, monkeypatch):
        monkeypatch.setattr(bots, "configured_bot_logins",
                            lambda: {"claude-bot[bot]"})
        ok, _ = merge_gate._all_commits_human_authored(_pr(files=[]))
        assert not ok

    def test_rejects_when_any_commit_is_bot_authored(self, settings, monkeypatch):
        monkeypatch.setattr(bots, "configured_bot_logins",
                            lambda: {"claude-bot[bot]", "codex-bot[bot]"})
        ok, offender = merge_gate._all_commits_human_authored(
            _pr(files=[], commits=[
                {"author": {"login": "alexander-matthew"}},
                {"author": {"login": "claude-bot[bot]"}},
            ])
        )
        assert not ok
        assert offender == "claude-bot[bot]"

    def test_accepts_when_all_commits_human(self, settings, monkeypatch):
        monkeypatch.setattr(bots, "configured_bot_logins",
                            lambda: {"claude-bot[bot]"})
        ok, offender = merge_gate._all_commits_human_authored(
            _pr(files=[], commits=[
                {"author": {"login": "alexander-matthew"}},
                {"author": {"login": "alexander-matthew"}},
            ])
        )
        assert ok
        assert offender is None


# ---- end-to-end via _gate_reasons -----------------------------------------


class TestGateReasonsIntegration:
    @pytest.fixture(autouse=True)
    def common_stubs(self, monkeypatch):
        """Stub out the tier-2 reviewer check so we can isolate the
        constitutional behaviour. rotation.reviewer_verdicts is still
        used by the tier-2 consensus + 'every required reviewer
        APPROVE'd' branch — not by tier-3 (which reads raw posts)."""
        monkeypatch.setattr(rotation, "reviewer_verdicts",
                            lambda n: {"codex": "APPROVE", "gemini": "APPROVE"})

    def _base_pr(self, files, **kwargs):
        return _pr(
            files=files,
            labels=kwargs.get("labels", ["agent:security-cleared", "agent:librarian-cleared"]),
            commits=kwargs.get("commits") or [
                {"author": {"login": "alexander-matthew"},
                 "committedDate": "2026-05-18T00:00:00Z"}
            ],
            reviews=kwargs.get("reviews") or [],
        )

    def test_tier3_pr_with_only_two_approves_is_blocked(self, settings):
        # codex + gemini APPROVE explicitly; claude missing.
        pr = self._base_pr(
            ["templates/constitution.md"],
            reviews=[
                _review_post(cli="codex", verdict="APPROVE", ts="2026-05-18T11:00:00Z"),
                _review_post(cli="gemini", verdict="APPROVE", ts="2026-05-18T11:30:00Z"),
            ],
        )
        reasons = merge_gate._gate_reasons(pr)
        assert any("tier-3" in r and "missing ['claude']" in r for r in reasons)

    def test_tier3_pr_with_all_three_approves_passes_constitutional_check(
            self, settings):
        pr = self._base_pr(
            ["templates/personas/synthesis.md"],
            reviews=[
                _review_post(cli="claude", verdict="APPROVE", ts="2026-05-18T11:00:00Z"),
                _review_post(cli="codex", verdict="APPROVE", ts="2026-05-18T11:30:00Z"),
                _review_post(cli="gemini", verdict="APPROVE", ts="2026-05-18T12:00:00Z"),
            ],
        )
        reasons = merge_gate._gate_reasons(pr)
        assert not any("tier-3" in r for r in reasons)

    def test_live_config_edit_is_tier3_hardcoded(self, settings):
        """codex + gemini both R1-flagged that editing config.toml must
        be tier-3, since [reviewers].required_clis and
        [guards].requires_full_consensus_paths live there."""
        pr = self._base_pr(
            ["config.toml"],
            commits=[{"author": {"login": "alexander-matthew"},
                      "committedDate": "2026-05-18T00:00:00Z"}],
            reviews=[
                _review_post(cli="codex", verdict="APPROVE", ts="2026-05-18T11:00:00Z"),
                _review_post(cli="gemini", verdict="APPROVE", ts="2026-05-18T11:30:00Z"),
            ],
        )
        reasons = merge_gate._gate_reasons(pr)
        # Both: tier-3 (hardcoded, missing claude) AND hard-limit (human
        # author satisfied, so no hard-limit reason).
        assert any("tier-3" in r and "live config" in r and "missing ['claude']" in r
                   for r in reasons)

    def test_hard_limit_pr_with_bot_author_is_blocked(self, settings, monkeypatch):
        monkeypatch.setattr(bots, "configured_bot_logins",
                            lambda: {"claude-bot[bot]"})
        pr = self._base_pr(
            ["config.toml"],
            commits=[{"author": {"login": "claude-bot[bot]"},
                      "committedDate": "2026-05-18T00:00:00Z"}],
        )
        reasons = merge_gate._gate_reasons(pr)
        assert any("hard-limit" in r and "claude-bot[bot]" in r for r in reasons)

    def test_hard_limit_pr_with_human_author_passes_hard_limit_check(
            self, settings, monkeypatch):
        monkeypatch.setattr(bots, "configured_bot_logins",
                            lambda: {"claude-bot[bot]"})
        pr = self._base_pr(
            ["config.toml"],
            commits=[{"author": {"login": "alexander-matthew"},
                      "committedDate": "2026-05-18T00:00:00Z"}],
        )
        reasons = merge_gate._gate_reasons(pr)
        assert not any("hard-limit" in r for r in reasons)
