"""Structured schemas for the verdict bodies agents post to GitHub.

Each persona's structured output (reviewer, arbiter, security, librarian)
follows a small marker-based grammar. Until now, every phase parsed its
own grammar with bespoke ``re`` calls and rendered the body via
f-string. This module owns both ends of the contract: a frozen
dataclass per verdict type with ``.parse(body)`` and ``.render()``
methods built on top of ``lib.markers``.

Design notes:

- Stdlib-only (no pydantic). The project pyproject keeps runtime deps
  intentionally minimal; a 60-line dataclass per verdict is cheaper than
  the pydantic import.
- ``parse`` raises ``ParseError`` on malformed input rather than
  returning ``None`` — callers that need tolerant behavior catch the
  exception explicitly. This makes the "I tried, here's why it failed"
  path visible at call sites.
- ``render()`` is the inverse of ``parse()``: ``X.parse(X(...).render())``
  round-trips. Phases that previously hand-formatted bodies should use
  ``render()`` so the contract stays in one place.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .markers import Section, extract_block, extract_inline, render as render_pairs


class ParseError(ValueError):
    """Raised when a verdict body fails to parse against its schema."""


def _require_verdict(body: str, section: Section, allowed: tuple[str, ...]) -> str:
    raw = extract_inline(body, section)
    if raw is None:
        raise ParseError(f"missing ##{section}: line")
    if raw not in allowed:
        raise ParseError(f"##{section}: {raw!r} not in {allowed}")
    return raw


def _require_inline(body: str, section: Section) -> str:
    raw = extract_inline(body, section)
    if raw is None:
        raise ParseError(f"missing ##{section}: line")
    return raw


def _require_block(body: str, section: Section) -> str:
    raw = extract_block(body, section)
    if raw is None:
        raise ParseError(f"missing ##{section}: section")
    return raw


# ---- reviewer (review_pr) -------------------------------------------------


@dataclass(frozen=True)
class ReviewVerdict:
    verdict: str
    summary: str
    checklist: str
    notes: str

    ALLOWED: ClassVar[tuple[str, ...]] = ("APPROVE", "REQUEST_CHANGES", "COMMENT")
    _MULTILINE: ClassVar[set[Section]] = {Section.CHECKLIST, Section.NOTES}

    @classmethod
    def parse(cls, body: str) -> "ReviewVerdict":
        # Reviewer contract requires both SUMMARY and CHECKLIST; NOTES optional.
        return cls(
            verdict=_require_verdict(body, Section.VERDICT, cls.ALLOWED),
            summary=_require_inline(body, Section.SUMMARY),
            checklist=_require_block(body, Section.CHECKLIST),
            notes=extract_block(body, Section.NOTES) or "",
        )

    def render(self) -> str:
        return render_pairs(
            [
                (Section.VERDICT, self.verdict),
                (Section.SUMMARY, self.summary),
                (Section.CHECKLIST, self.checklist),
                (Section.NOTES, self.notes),
            ],
            multiline=self._MULTILINE,
        )


# ---- arbiter (arbitrate_pr) -----------------------------------------------


@dataclass(frozen=True)
class ArbiterVerdict:
    verdict: str
    reasoning: str

    ALLOWED: ClassVar[tuple[str, ...]] = (
        "APPROVE_FOR_MERGE",
        "REQUEST_FINAL_CHANGES",
        "ESCALATE_TO_HUMAN",
    )
    _MULTILINE: ClassVar[set[Section]] = {Section.REASONING}

    @classmethod
    def parse(cls, body: str) -> "ArbiterVerdict":
        return cls(
            verdict=_require_verdict(body, Section.ARBITER_VERDICT, cls.ALLOWED),
            reasoning=extract_block(body, Section.REASONING) or "",
        )

    def render(self) -> str:
        return render_pairs(
            [
                (Section.ARBITER_VERDICT, self.verdict),
                (Section.REASONING, self.reasoning),
            ],
            multiline=self._MULTILINE,
        )


# ---- security (security_check) --------------------------------------------


@dataclass(frozen=True)
class SecurityVerdict:
    verdict: str
    findings: str
    notes: str

    ALLOWED: ClassVar[tuple[str, ...]] = ("CLEAR", "FLAG")
    _MULTILINE: ClassVar[set[Section]] = {Section.FINDINGS, Section.NOTES}

    @classmethod
    def parse(cls, body: str) -> "SecurityVerdict":
        # Security contract requires FINDINGS; NOTES optional.
        return cls(
            verdict=_require_verdict(body, Section.SECURITY_VERDICT, cls.ALLOWED),
            findings=_require_block(body, Section.FINDINGS),
            notes=extract_block(body, Section.NOTES) or "",
        )

    def render(self) -> str:
        return render_pairs(
            [
                (Section.SECURITY_VERDICT, self.verdict),
                (Section.FINDINGS, self.findings),
                (Section.NOTES, self.notes),
            ],
            multiline=self._MULTILINE,
        )


# ---- librarian (librarian_check) ------------------------------------------


@dataclass(frozen=True)
class AuditVerdict:
    verdict: str
    consistency_notes: str

    ALLOWED: ClassVar[tuple[str, ...]] = ("AUDIT_PASS", "AUDIT_FAIL")
    _MULTILINE: ClassVar[set[Section]] = {Section.CONSISTENCY_NOTES}

    @classmethod
    def parse(cls, body: str) -> "AuditVerdict":
        return cls(
            verdict=_require_verdict(body, Section.AUDIT_VERDICT, cls.ALLOWED),
            consistency_notes=extract_block(body, Section.CONSISTENCY_NOTES) or "",
        )

    def render(self) -> str:
        return render_pairs(
            [
                (Section.AUDIT_VERDICT, self.verdict),
                (Section.CONSISTENCY_NOTES, self.consistency_notes),
            ],
            multiline=self._MULTILINE,
        )
