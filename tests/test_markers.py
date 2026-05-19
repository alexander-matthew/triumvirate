"""Tests for the centralized marker-string module."""
from __future__ import annotations

from agent_loop.lib.markers import Marker, Section, extract_block, extract_inline, render


# ---- Marker / Section enums round-trip as strings -------------------------


def test_marker_values_have_leading_hashes():
    assert str(Marker.REVIEW) == "##VERDICT:"
    assert str(Marker.ARBITER) == "##ARBITER_VERDICT:"
    assert str(Marker.SECURITY) == "##SECURITY_VERDICT:"
    assert str(Marker.AUDIT) == "##AUDIT_VERDICT:"
    assert str(Marker.PROPOSAL) == "##PROPOSAL"
    assert str(Marker.DECISIONS) == "##DECISIONS"


def test_section_values_are_bare_keys():
    # Section values compose into both inline and block forms, so they
    # carry no leading ## and no trailing colon.
    assert str(Section.VERDICT) == "VERDICT"
    assert str(Section.SUMMARY) == "SUMMARY"
    assert str(Section.ARBITER_VERDICT) == "ARBITER_VERDICT"


# ---- extract_inline -------------------------------------------------------


def test_extract_inline_simple():
    body = "##VERDICT: APPROVE\n##SUMMARY: looks fine\n"
    assert extract_inline(body, Section.VERDICT) == "APPROVE"
    assert extract_inline(body, Section.SUMMARY) == "looks fine"


def test_extract_inline_missing_section_returns_none():
    body = "##VERDICT: APPROVE\n"
    assert extract_inline(body, Section.SUMMARY) is None


def test_extract_inline_tolerates_trailing_whitespace():
    body = "##VERDICT:   APPROVE   \n"
    assert extract_inline(body, Section.VERDICT) == "APPROVE"


def test_extract_inline_only_matches_at_line_start():
    # A marker mentioned mid-line should not be matched.
    body = "prose mentioning ##VERDICT: NOT_REAL inline\n"
    assert extract_inline(body, Section.VERDICT) is None


# ---- extract_block --------------------------------------------------------


def test_extract_block_runs_until_next_heading():
    body = (
        "##VERDICT: APPROVE\n"
        "##CHECKLIST:\n"
        "- [x] tests pass\n"
        "- [x] no regressions\n"
        "##NOTES:\n"
        "lgtm\n"
    )
    assert extract_block(body, Section.CHECKLIST) == "- [x] tests pass\n- [x] no regressions"
    assert extract_block(body, Section.NOTES) == "lgtm"


def test_extract_block_runs_to_end_of_body():
    body = "##VERDICT: APPROVE\n##NOTES:\nlast section, no trailer\n"
    assert extract_block(body, Section.NOTES) == "last section, no trailer"


def test_extract_block_missing_section_returns_none():
    body = "##VERDICT: APPROVE\n"
    assert extract_block(body, Section.CHECKLIST) is None


def test_extract_block_handles_blank_lines_inside():
    body = (
        "##NOTES:\n"
        "first paragraph\n"
        "\n"
        "second paragraph\n"
        "##END\n"
    )
    # ##END is a valid heading-shaped lookahead terminator.
    assert extract_block(body, Section.NOTES) == "first paragraph\n\nsecond paragraph"


# ---- render ---------------------------------------------------------------


def test_render_inline_pairs():
    out = render([(Section.VERDICT, "APPROVE"), (Section.SUMMARY, "looks good")])
    assert out == "##VERDICT: APPROVE\n##SUMMARY: looks good\n"


def test_render_with_multiline_sections():
    out = render(
        [
            (Section.VERDICT, "APPROVE"),
            (Section.CHECKLIST, "- [x] tests"),
            (Section.NOTES, "lgtm"),
        ],
        multiline={Section.CHECKLIST, Section.NOTES},
    )
    assert out == (
        "##VERDICT: APPROVE\n"
        "##CHECKLIST:\n- [x] tests\n"
        "##NOTES:\nlgtm\n"
    )


def test_render_round_trip_through_extract():
    pairs = [
        (Section.VERDICT, "REQUEST_CHANGES"),
        (Section.SUMMARY, "see checklist"),
        (Section.CHECKLIST, "- [ ] fix the bug\n- [ ] add a test"),
        (Section.NOTES, "happy to re-review"),
    ]
    body = render(pairs, multiline={Section.CHECKLIST, Section.NOTES})
    assert extract_inline(body, Section.VERDICT) == "REQUEST_CHANGES"
    assert extract_inline(body, Section.SUMMARY) == "see checklist"
    assert extract_block(body, Section.CHECKLIST) == "- [ ] fix the bug\n- [ ] add a test"
    assert extract_block(body, Section.NOTES) == "happy to re-review"
