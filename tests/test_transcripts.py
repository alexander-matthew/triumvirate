"""Tests for lib/transcripts: per-PR markdown artifact builder."""
from __future__ import annotations

import pytest

from agent_loop import config
from agent_loop.lib import db, transcripts


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
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


def test_write_empty_pr_produces_skeleton():
    path = transcripts.write(99, title="empty PR")
    text = path.read_text()
    assert path.name == "PR-99.md"
    assert "# PR #99: empty PR" in text
    # No verdicts → no summary section
    assert "Verdict summary" not in text


def test_write_includes_verdict_summary_and_log():
    db.record_verdict(pr_number=42, marker_type="review", cli="codex",
                      verdict="REQUEST_CHANGES", round_n=1, raw_body="##VERDICT: REQUEST_CHANGES")
    db.record_verdict(pr_number=42, marker_type="review", cli="codex",
                      verdict="APPROVE", round_n=2, raw_body="##VERDICT: APPROVE")
    db.record_verdict(pr_number=42, marker_type="review", cli="gemini",
                      verdict="APPROVE", round_n=2, raw_body="##VERDICT: APPROVE")
    db.record_verdict(pr_number=42, marker_type="security", cli="gemini",
                      verdict="CLEAR", raw_body="##SECURITY_VERDICT: CLEAR")
    db.append(phase="merge", action="finish", pr_number=42, outcome="merged",
              duration_s=1.5)

    path = transcripts.write(42, title="real PR")
    text = path.read_text()

    assert "# PR #42: real PR" in text
    assert "## Verdict summary" in text
    assert "Reviewer** (codex): 2 verdict(s)" in text
    assert "Reviewer** (gemini): 1 verdict(s)" in text
    assert "Security** (gemini): 1 verdict(s)" in text
    assert "## Verdict log" in text
    assert "### Reviewer — codex — REQUEST_CHANGES" in text
    assert "### Reviewer — codex — APPROVE" in text
    assert "### Security — gemini — CLEAR" in text
    assert "## Phase timeline" in text
    assert "merge" in text
    assert "merged" in text


def test_write_overwrites_existing_file():
    transcripts.write(7)
    db.record_verdict(pr_number=7, marker_type="review", cli="codex",
                      verdict="APPROVE", raw_body="x")
    path = transcripts.write(7)
    assert "APPROVE" in path.read_text()


def test_transcripts_dir_is_under_state_dir():
    path = transcripts.transcripts_dir()
    s = config.settings()
    assert path.parent == s.state_dir
    assert path.name == "transcripts"
    assert path.exists()
