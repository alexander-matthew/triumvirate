"""Tests for the verdicts table + helpers in lib/db."""
from __future__ import annotations

import pytest

from agent_loop import config
from agent_loop.lib import db


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Each test gets a fresh state_dir with its own runs.sqlite."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("""
[project]
name = "test"
repo = "x/y"
trusted_authors = []
""")
    monkeypatch.setenv("AGENT_LOOP_CONFIG_ROOT", str(tmp_path))
    config.reset()
    yield
    config.reset()


def test_record_and_read_back_single_verdict():
    db.record_verdict(
        pr_number=42, marker_type="review", cli="codex",
        verdict="APPROVE", head_sha="abc123", round_n=1, raw_body="body",
    )
    rows = db.verdicts_for_pr(42)
    assert len(rows) == 1
    r = rows[0]
    assert r["pr_number"] == 42
    assert r["marker_type"] == "review"
    assert r["cli"] == "codex"
    assert r["verdict"] == "APPROVE"
    assert r["head_sha"] == "abc123"
    assert r["round_n"] == 1
    assert r["raw_body"] == "body"


def test_verdicts_for_pr_orders_by_ts():
    for i, v in enumerate(["REQUEST_CHANGES", "REQUEST_CHANGES", "APPROVE"]):
        db.record_verdict(pr_number=7, marker_type="review", cli="codex", verdict=v)
    verdicts = [r["verdict"] for r in db.verdicts_for_pr(7)]
    assert verdicts == ["REQUEST_CHANGES", "REQUEST_CHANGES", "APPROVE"]


def test_verdict_distribution_groups_by_cli_and_value():
    for cli, v in [("codex", "APPROVE"), ("codex", "APPROVE"),
                   ("codex", "REQUEST_CHANGES"),
                   ("gemini", "APPROVE"), ("gemini", "REQUEST_CHANGES"),
                   ("gemini", "REQUEST_CHANGES")]:
        db.record_verdict(pr_number=1, marker_type="review", cli=cli, verdict=v)

    dist = db.verdict_distribution("review")
    assert dist == {
        "codex":  {"APPROVE": 2, "REQUEST_CHANGES": 1},
        "gemini": {"APPROVE": 1, "REQUEST_CHANGES": 2},
    }


def test_verdict_distribution_filters_by_marker_type():
    db.record_verdict(pr_number=1, marker_type="review", cli="codex", verdict="APPROVE")
    db.record_verdict(pr_number=1, marker_type="security", cli="gemini", verdict="CLEAR")
    db.record_verdict(pr_number=1, marker_type="security", cli="gemini", verdict="FLAG")

    assert db.verdict_distribution("review") == {"codex": {"APPROVE": 1}}
    assert db.verdict_distribution("security") == {"gemini": {"CLEAR": 1, "FLAG": 1}}
    assert db.verdict_distribution("audit") == {}


def test_consensus_rounds_empty_when_no_data():
    assert db.consensus_rounds(("codex", "gemini")) == []


def test_consensus_rounds_reports_when_required_clis_approved():
    # PR 1: both required CLIs approve at round 2 → consensus at round 2.
    db.record_verdict(pr_number=1, marker_type="review", cli="codex",
                      verdict="REQUEST_CHANGES", round_n=1)
    db.record_verdict(pr_number=1, marker_type="review", cli="gemini",
                      verdict="REQUEST_CHANGES", round_n=1)
    db.record_verdict(pr_number=1, marker_type="review", cli="codex",
                      verdict="APPROVE", round_n=2)
    db.record_verdict(pr_number=1, marker_type="review", cli="gemini",
                      verdict="APPROVE", round_n=2)
    # PR 2: only codex approved — no consensus yet.
    db.record_verdict(pr_number=2, marker_type="review", cli="codex",
                      verdict="APPROVE", round_n=1)
    # PR 3: codex r1, gemini r3 → consensus at round 3 (the later one).
    db.record_verdict(pr_number=3, marker_type="review", cli="codex",
                      verdict="APPROVE", round_n=1)
    db.record_verdict(pr_number=3, marker_type="review", cli="gemini",
                      verdict="APPROVE", round_n=3)

    rounds = db.consensus_rounds(("codex", "gemini"))
    assert rounds == [2, 3]
