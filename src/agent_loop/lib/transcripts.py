"""Per-PR transcript artifacts.

When a PR closes (typically via auto-merge), the loop's full review chain
becomes the most useful artifact for human auditing and future persona
tuning. This module writes that chain as a markdown file under
``<state_dir>/transcripts/PR-N.md`` so it survives independently of
GitHub's UI.

The transcript is composed entirely from local DB rows
(:func:`db.verdicts_for_pr` + :func:`db.events_for_pr`), so it is fast
and works even if GitHub is unreachable at merge time.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from ..config import settings
from . import db


_MARKER_LABELS = {
    "review": "Reviewer",
    "arbiter": "Arbiter",
    "security": "Security",
    "audit": "Librarian",
}


def transcripts_dir() -> Path:
    """``<state_dir>/transcripts/``, created on first access."""
    p = settings().state_dir / "transcripts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def write(pr_number: int, *, title: str | None = None) -> Path:
    """Write (or overwrite) the transcript for ``pr_number``.

    Returns the file path. Title is the PR title if known — used as the
    h1; otherwise just ``PR #N``.
    """
    path = transcripts_dir() / f"PR-{pr_number}.md"

    verdicts = db.verdicts_for_pr(pr_number)
    events = db.events_for_pr(pr_number)

    h1 = f"# PR #{pr_number}"
    if title:
        h1 += f": {title}"

    lines: list[str] = [
        h1,
        "",
        f"_Transcript generated {dt.datetime.now().isoformat(timespec='seconds')}_",
        "",
    ]

    # ---- verdict summary ----
    counts: dict[tuple[str, str], int] = {}
    for v in verdicts:
        key = (v["marker_type"], v["cli"])
        counts[key] = counts.get(key, 0) + 1
    if counts:
        lines.append("## Verdict summary")
        lines.append("")
        for (marker, cli), c in sorted(counts.items()):
            lines.append(f"- **{_MARKER_LABELS.get(marker, marker)}** ({cli}): {c} verdict(s)")
        lines.append("")

    # ---- chronological verdict log ----
    if verdicts:
        lines.append("## Verdict log")
        lines.append("")
        for v in verdicts:
            ts = dt.datetime.fromtimestamp(v["ts"]).isoformat(timespec="seconds")
            head = (v.get("head_sha") or "")[:8]
            round_n = v.get("round_n")
            label = _MARKER_LABELS.get(v["marker_type"], v["marker_type"])
            heading = f"### {label} — {v['cli']} — {v['verdict']}"
            extras = []
            if round_n is not None:
                extras.append(f"round {round_n}")
            if head:
                extras.append(f"head {head}")
            extras.append(ts)
            heading += " · " + " · ".join(extras)
            lines.append(heading)
            lines.append("")
            body = (v.get("raw_body") or "").strip()
            if body:
                # Fence the raw body so its own ## headings don't blow up the
                # transcript's outline.
                lines.append("```")
                lines.append(body)
                lines.append("```")
            lines.append("")

    # ---- phase event log ----
    if events:
        lines.append("## Phase timeline")
        lines.append("")
        lines.append("| time | phase | action | agent | outcome | duration |")
        lines.append("|------|-------|--------|-------|---------|----------|")
        for ev in events:
            ts = dt.datetime.fromtimestamp(ev["ts"]).strftime("%H:%M:%S")
            dur = f"{ev['duration_s']:.1f}s" if ev.get("duration_s") else ""
            lines.append(
                f"| {ts} | {ev['phase']} | {ev['action']} | "
                f"{ev.get('agent') or ''} | {ev.get('outcome') or ''} | {dur} |"
            )
        lines.append("")

    path.write_text("\n".join(lines))
    return path
