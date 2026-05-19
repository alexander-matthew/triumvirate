"""Centralized marker strings for structured agent comments.

Agents post structured comments to GitHub using ``##KEY: value`` markers so
the orchestrator (and future automation) can read their verdicts without
having to interpret natural language. Until now, the marker strings have
lived as literals in every phase + the orchestrator + lib/rotation; this
module is the single source of truth.

Use:

- ``Marker.*`` — leading markers that identify which kind of post a
  comment is. Pass to ``gh.marker_posts(pr, marker=...)`` to filter.
- ``Section.*`` — the section keys that appear inside a structured body.
- ``extract_inline(body, Section.X)`` — read a single-line value
  (``##KEY: value``).
- ``extract_block(body, Section.X)`` — read a multi-line section that
  runs until the next ``##HEADING`` or end of body.
- ``render(pairs, multiline=...)`` — format an ordered sequence of
  (section, value) pairs as a body.

The shape of a *whole* persona output (which sections are required,
allowed values for verdicts, etc.) belongs in the verdict schemas
(``lib/verdicts.py``); this module is intentionally string-level.
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Iterable


class Marker(StrEnum):
    """Leading marker that identifies the kind of structured post.

    These are the strings to pass to ``gh.marker_posts(pr, marker=...)``
    when listing comments of a particular kind.
    """

    REVIEW = "##VERDICT:"
    ARBITER = "##ARBITER_VERDICT:"
    SECURITY = "##SECURITY_VERDICT:"
    AUDIT = "##AUDIT_VERDICT:"
    PROPOSAL = "##PROPOSAL"
    DECISIONS = "##DECISIONS"


class Section(StrEnum):
    """Section keys inside a structured post body.

    Values are the bare key (no leading ``##``, no trailing ``:``) so they
    compose cleanly into both inline and block forms.
    """

    VERDICT = "VERDICT"
    ARBITER_VERDICT = "ARBITER_VERDICT"
    SECURITY_VERDICT = "SECURITY_VERDICT"
    AUDIT_VERDICT = "AUDIT_VERDICT"
    SUMMARY = "SUMMARY"
    CHECKLIST = "CHECKLIST"
    NOTES = "NOTES"
    FINDINGS = "FINDINGS"
    REASONING = "REASONING"
    CONSISTENCY_NOTES = "CONSISTENCY_NOTES"
    TITLE = "TITLE"
    LABELS = "LABELS"
    BODY = "BODY"


# Lookahead used by extract_block to stop at the next section heading.
# Matches a line starting with ``##`` followed by uppercase letters /
# underscores (our section-name convention), with or without a trailing
# colon — or end of string.
_NEXT_HEADING = r"(?=^##[A-Z_]+:?\s*$|\Z)"


def extract_inline(body: str, section: Section | str) -> str | None:
    """Extract a single-line section value (``##KEY: value``).

    Returns the trimmed value, or None if the section is absent.
    Tolerant of trailing whitespace on the marker line.
    """
    m = re.search(rf"^##{section}:\s*(.+?)\s*$", body, re.M)
    return m.group(1) if m else None


def extract_block(body: str, section: Section | str) -> str | None:
    """Extract a multi-line section body that runs until the next ``##KEY``
    line (or end of body).

    Returns the trimmed block, or None if the section header is absent.
    """
    m = re.search(
        rf"^##{section}:\s*\n(.*?){_NEXT_HEADING}",
        body,
        re.M | re.S,
    )
    if not m:
        return None
    return m.group(1).rstrip()


def render(
    pairs: Iterable[tuple[Section | str, str]],
    multiline: set[Section | str] | None = None,
) -> str:
    """Render ordered (section, value) pairs into a marker-formatted body.

    By default every pair is rendered inline (``##KEY: value``). Pass
    ``multiline={Section.X, ...}`` for sections whose value should appear
    on the next line and span multiple lines.

    Always ends with a trailing newline so concatenation is safe.
    """
    multi = multiline or set()
    out: list[str] = []
    for sec, val in pairs:
        key = str(sec)
        if sec in multi or key in multi:
            out.append(f"##{key}:\n{val}")
        else:
            out.append(f"##{key}: {val}")
    return "\n".join(out) + "\n"
