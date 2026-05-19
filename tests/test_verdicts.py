"""Tests for the structured verdict schemas in lib/verdicts."""
from __future__ import annotations

import pytest

from agent_loop.lib.verdicts import (
    ArbiterVerdict,
    AuditVerdict,
    ParseError,
    ReviewVerdict,
    SecurityVerdict,
)


# ---- ReviewVerdict --------------------------------------------------------


class TestReviewVerdict:
    def test_parse_happy(self):
        body = (
            "##VERDICT: APPROVE\n"
            "##SUMMARY: lgtm\n"
            "##CHECKLIST:\n- [x] tests\n"
            "##NOTES:\nminor: prefer foo over bar\n"
        )
        v = ReviewVerdict.parse(body)
        assert v.verdict == "APPROVE"
        assert v.summary == "lgtm"
        assert v.checklist == "- [x] tests"
        assert v.notes == "minor: prefer foo over bar"

    def test_missing_verdict_raises(self):
        with pytest.raises(ParseError, match="missing ##VERDICT"):
            ReviewVerdict.parse("##SUMMARY: x\n")

    def test_invalid_verdict_value_raises(self):
        with pytest.raises(ParseError, match="not in"):
            ReviewVerdict.parse("##VERDICT: MAYBE\n##SUMMARY: x\n")

    def test_summary_required(self):
        with pytest.raises(ParseError, match="SUMMARY"):
            ReviewVerdict.parse("##VERDICT: COMMENT\n##CHECKLIST:\n- [x] x\n")

    def test_checklist_required(self):
        with pytest.raises(ParseError, match="CHECKLIST"):
            ReviewVerdict.parse("##VERDICT: APPROVE\n##SUMMARY: x\n")

    def test_notes_optional_defaults_empty(self):
        v = ReviewVerdict.parse("##VERDICT: APPROVE\n##SUMMARY: x\n##CHECKLIST:\n- [x] y\n")
        assert v.notes == ""

    def test_render_round_trip(self):
        original = ReviewVerdict(
            verdict="REQUEST_CHANGES",
            summary="see checklist",
            checklist="- [ ] fix the bug\n- [ ] add a test",
            notes="happy to re-review",
        )
        round_tripped = ReviewVerdict.parse(original.render())
        assert round_tripped == original


# ---- ArbiterVerdict -------------------------------------------------------


class TestArbiterVerdict:
    def test_parse_happy(self):
        body = (
            "##ARBITER_VERDICT: APPROVE_FOR_MERGE\n"
            "##REASONING:\nreviewer-codex's concern was already addressed\n"
        )
        v = ArbiterVerdict.parse(body)
        assert v.verdict == "APPROVE_FOR_MERGE"
        assert v.reasoning == "reviewer-codex's concern was already addressed"

    def test_invalid_verdict_value_raises(self):
        with pytest.raises(ParseError):
            ArbiterVerdict.parse("##ARBITER_VERDICT: APPROVE\n")

    def test_missing_verdict_raises(self):
        with pytest.raises(ParseError):
            ArbiterVerdict.parse("##REASONING:\nbecause\n")

    def test_render_round_trip(self):
        original = ArbiterVerdict(
            verdict="ESCALATE_TO_HUMAN",
            reasoning="security-relevant disagreement; human should call this",
        )
        assert ArbiterVerdict.parse(original.render()) == original


# ---- SecurityVerdict ------------------------------------------------------


class TestSecurityVerdict:
    def test_parse_happy(self):
        body = (
            "##SECURITY_VERDICT: FLAG\n"
            "##FINDINGS:\n- shell injection in src/foo.rs:42\n"
            "##NOTES:\nrecommend escaping via shlex\n"
        )
        v = SecurityVerdict.parse(body)
        assert v.verdict == "FLAG"
        assert "shell injection" in v.findings
        assert "shlex" in v.notes

    def test_invalid_verdict_value_raises(self):
        with pytest.raises(ParseError):
            SecurityVerdict.parse("##SECURITY_VERDICT: MAYBE\n")

    def test_render_round_trip(self):
        original = SecurityVerdict(verdict="CLEAR", findings="none", notes="all good")
        assert SecurityVerdict.parse(original.render()) == original

    def test_findings_required(self):
        with pytest.raises(ParseError, match="FINDINGS"):
            SecurityVerdict.parse("##SECURITY_VERDICT: CLEAR\n")


# ---- AuditVerdict ---------------------------------------------------------


class TestAuditVerdict:
    def test_parse_happy(self):
        body = (
            "##AUDIT_VERDICT: AUDIT_FAIL\n"
            "##CONSISTENCY_NOTES:\nadds tokio while other crates use async-std\n"
        )
        v = AuditVerdict.parse(body)
        assert v.verdict == "AUDIT_FAIL"
        assert "tokio" in v.consistency_notes

    def test_render_round_trip(self):
        original = AuditVerdict(
            verdict="AUDIT_PASS",
            consistency_notes="no cross-project concerns",
        )
        assert AuditVerdict.parse(original.render()) == original


# ---- frozenness -----------------------------------------------------------


def test_review_verdict_is_frozen():
    v = ReviewVerdict.parse(
        "##VERDICT: APPROVE\n##SUMMARY: x\n##CHECKLIST:\n- [x] y\n"
    )
    with pytest.raises(Exception):
        v.verdict = "REQUEST_CHANGES"  # type: ignore[misc]
