"""Merge gate: pure logic, no agent. Decides whether to auto-merge a PR.

Constitutional fidelity (per templates/constitution.md, ratified
2026-05-18): in addition to the standard tier-2 checks (consensus +
security + librarian + CI green), this gate enforces:

  - **Tier 3 hardcoded detection.** A PR touching the constitution,
    any persona prompt, the requires_full_consensus_paths setting, or
    [reviewers].required_clis is treated as tier 3 regardless of
    config. Tier-3 PRs require ``##VERDICT: APPROVE`` from each of
    {claude, codex, gemini} on the latest commit — not just from the
    configured ``required_clis``. The label
    ``requires:full-consensus`` and the config knob
    ``[guards].requires_full_consensus_paths`` are augmentation only;
    the hardcoded list cannot be bypassed by edits to either.

  - **Hard-limit human authorship.** A PR modifying
    ``[guards].protected_paths``, ``[project].trusted_authors``,
    ``[project].repo``, or any file already in ``protected_paths`` is
    refused unless every commit on the PR is human-authored — i.e. no
    commit was authored by one of the loop's bot logins
    (see lib.bots.configured_bot_logins).
"""
from __future__ import annotations

import re
import time

from ..config import settings
from ..lib import bots, db, gh, kill_switch, markers, pr_view, protected, review_baton, rotation, transcripts


# Hardcoded tier-3 surface. Cannot be edited by tier-3 consensus alone —
# this list is the floor of what the constitution treats as
# constitutional. Operators may *add* more via the config knob
# [guards].requires_full_consensus_paths, but cannot subtract from this
# set without also editing this file (which itself touches lib code →
# tier 2 → does not allow removing hardcoded constitutional surfaces).
_TIER3_HARDCODED_PATHS: frozenset[str] = frozenset({
    "templates/constitution.md",
    "agents/constitution.md",
})
_TIER3_HARDCODED_PREFIXES: tuple[str, ...] = (
    "templates/personas/",
    "agents/personas/",
)
# Live config files whose modification is tier-3-eligible because they may
# (and routinely do) contain settings the constitution defines as tier-3:
# [reviewers].required_clis and [guards].requires_full_consensus_paths.
# A future improvement could parse the TOML diff and only flag if those
# specific keys changed; the current implementation conservatively
# escalates any live config edit to tier 3, which is the safer default.
# Reported by codex + gemini on PR #3 R1 review.
_TIER3_LIVE_CONFIG_FILES: frozenset[str] = frozenset({
    "config.toml",
    "agents/config.toml",
})
_TIER3_CONFIG_KEYS: tuple[str, ...] = (
    "[reviewers].required_clis",
    "[guards].requires_full_consensus_paths",
)


# CLI identities that must all APPROVE for tier-3 merges. These three are
# the triumvirate itself.
_TRIUMVIRATE = ("claude", "codex", "gemini")


# Label that elevates any PR to tier 3 regardless of touched paths.
_TIER3_LABEL = "requires:full-consensus"


# Settings whose modification triggers the hard-limit "human-authored
# commits only" rule. Paths in protected_paths are checked separately via
# protected.violations(); these are the *configuration* surfaces.
_HARD_LIMIT_CONFIG_KEYS: tuple[str, ...] = (
    "[guards].protected_paths",
    "[project].trusted_authors",
    "[project].repo",
)


# Files whose modification is treated as potentially touching a hard-limit
# config key. We can't reliably parse the TOML diff without reading both
# revisions, so we conservatively treat any edit to a live config.toml as
# requiring human authorship. Templates / example files are exempted.
_HARD_LIMIT_CONFIG_FILES: frozenset[str] = frozenset({
    "config.toml",
    "agents/config.toml",
})


def _ci_state(pr: dict) -> str:
    rolls = pr.get("statusCheckRollup") or []
    if not rolls:
        return "unknown"
    states = set()
    for r in rolls:
        c = r.get("conclusion") or r.get("state")
        sst = r.get("status")
        if sst and sst != "COMPLETED":
            states.add("pending")
        if c:
            states.add(c.upper())
    if "pending" in states or {"IN_PROGRESS", "QUEUED", "WAITING"} & states:
        return "pending"
    bad = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
    if bad & states:
        return "failing"
    if states <= {"SUCCESS", "COMPLETED", "NEUTRAL", "SKIPPED"}:
        return "green"
    return "unknown"


# ---- constitutional fidelity ----------------------------------------------


def _matches_path(path: str, candidate: str) -> bool:
    """True if `path` equals `candidate` (file) or sits under `candidate/` (dir)."""
    if not path or not candidate:
        return False
    if path == candidate:
        return True
    return path.startswith(candidate.rstrip("/") + "/")


def _is_tier3_pr(pr: dict) -> tuple[bool, list[str]]:
    """True if `pr` touches any tier-3 surface; returns the reasons.

    Detection layers (per the constitution):
      1. Hardcoded: constitution + every persona file. Cannot be bypassed.
      2. Config-driven augmentation: [guards].requires_full_consensus_paths.
      3. Label-driven: `requires:full-consensus` set on the PR.
    """
    reasons: list[str] = []
    files = [f.get("path", "") for f in (pr.get("files") or [])]
    s = settings()

    # 1. Hardcoded — constitution + personas.
    for path in files:
        if path in _TIER3_HARDCODED_PATHS:
            reasons.append(f"tier-3 (hardcoded): touches {path}")
        else:
            for prefix in _TIER3_HARDCODED_PREFIXES:
                if path.startswith(prefix):
                    reasons.append(f"tier-3 (hardcoded): touches persona prompt {path}")
                    break

    # 2. Hardcoded — live config files (may carry tier-3 settings).
    for path in files:
        if path in _TIER3_LIVE_CONFIG_FILES:
            reasons.append(
                f"tier-3 (hardcoded): touches live config {path}; "
                f"may modify any of {_TIER3_CONFIG_KEYS}"
            )

    # 3. Config-driven augmentation.
    for path in files:
        for prefix in s.requires_full_consensus_paths:
            if _matches_path(path, prefix):
                reasons.append(
                    f"tier-3 (config): touches requires_full_consensus_paths entry {prefix!r}"
                )
                break

    # 4. Label-driven elevation.
    labels = {l["name"] for l in (pr.get("labels") or [])}
    if _TIER3_LABEL in labels:
        reasons.append(f"tier-3 (label): {_TIER3_LABEL!r} label set")

    return (bool(reasons), reasons)


def _explicit_approves_for_latest_commit(pr: dict) -> set[str]:
    """Set of CLIs that posted an explicit ##VERDICT: APPROVE on the latest commit.

    Unlike ``rotation.reviewer_verdicts``, this does NOT count synthetic
    APPROVEs that the arbiter wrapper posts on behalf of an overridden
    CLI when it issues ``APPROVE_FOR_MERGE``. PRView already filters
    those synthetic posts out of ``reviewer_posts`` via the
    ``[wrapper:arbiter-override]`` sentinel + the line-anchored legacy
    fallback. The constitution requires raw, explicit signal from each
    of {claude, codex, gemini} for tier-3 merges — using the
    rotation-level helper would let the arbiter satisfy the unanimous-
    three rule, which is exactly what the constitution forbids.
    Reported by gemini on PR #3 R1 review.
    """
    commits = pr.get("commits") or []
    if not commits:
        return set()
    latest_ts = (commits[-1].get("committedDate") or "")

    view = pr_view.PRView.from_pr(pr)
    approved: set[str] = set()
    # Anchored to the wrapper's own trailer so that a reviewer who
    # mentions a CLI name in prose can't be misattributed.
    trailer_re = re.compile(r"\*Round\s+\d+/\d+\s*·\s*reviewer:\s*(\w+)")
    for post in view.reviewer_posts:
        if post["ts"] <= latest_ts:
            continue
        if markers.extract_inline(post["body"], markers.Section.VERDICT) != "APPROVE":
            continue
        m = trailer_re.search(post["body"])
        if m:
            approved.add(m.group(1))
    return approved


def _tier3_missing_approvals(pr: dict) -> list[str]:
    """Returns the list of triumvirate CLIs that have NOT posted APPROVE.

    For a tier-3 PR to merge, every CLI in :data:`_TRIUMVIRATE` must have
    posted an *explicit* ``##VERDICT: APPROVE`` on the latest commit. See
    :func:`_explicit_approves_for_latest_commit` for why we bypass the
    rotation helper here.
    """
    approved = _explicit_approves_for_latest_commit(pr)
    return [cli for cli in _TRIUMVIRATE if cli not in approved]


def _is_hard_limit_pr(pr: dict) -> tuple[bool, list[str]]:
    """True if ``pr`` modifies a hard-limit *config* surface.

    Protected-path file edits are handled separately and unconditionally
    by the pre-existing ``protected.violations()`` check in
    :func:`_gate_reasons` — that check adds a refusal reason regardless
    of authorship, which is the strict reading of "files listed in
    protected_paths cannot be modified by the loop at all" (codex R1
    note). So this function focuses on *config* edits that may carry
    a hard-limit setting change (``protected_paths``,
    ``trusted_authors``, ``repo``) and require human authorship.
    """
    reasons: list[str] = []
    files = [f.get("path", "") for f in (pr.get("files") or [])]

    for path in files:
        if path in _HARD_LIMIT_CONFIG_FILES:
            reasons.append(
                f"hard-limit: modifies live config file {path}; possible touch of "
                f"{_HARD_LIMIT_CONFIG_KEYS}"
            )

    return (bool(reasons), reasons)


def _commit_authors(pr: dict) -> list[str]:
    """Return each commit's author login on this PR (best-effort)."""
    out: list[str] = []
    for commit in (pr.get("commits") or []):
        author = (commit.get("author") or {})
        login = author.get("login") or ""
        if login:
            out.append(login)
    return out


def _all_commits_human_authored(pr: dict) -> tuple[bool, str | None]:
    """True if no commit on this PR was authored by one of the loop's bot logins.

    Returns (ok, offending_login) — if any commit is bot-authored, ok is
    False and offending_login is the first bot login encountered.
    """
    bot_logins = bots.configured_bot_logins()
    if not bot_logins:
        # No bots configured on this host → we can't distinguish bot from
        # human commits, so we cannot prove human authorship. Be strict:
        # refuse, with a reason that tells the operator to configure bots
        # or merge the hard-limit PR manually outside the loop.
        return (False, None)
    authors = _commit_authors(pr)
    if not authors:
        # No commit author metadata available → cannot prove → refuse.
        return (False, None)
    for login in authors:
        if login in bot_logins:
            return (False, login)
    return (True, None)


# ---- the gate -------------------------------------------------------------


def _gate_reasons(pr: dict) -> list[str]:
    s = settings()
    reasons: list[str] = []

    labels = {l["name"] for l in pr.get("labels", [])}
    if s.label("veto") in labels:
        reasons.append(f"{s.label('veto')} label set")
    if s.label("needs_human") in labels:
        reasons.append(f"{s.label('needs_human')} label set")
    if s.label("protected_violation") in labels:
        reasons.append(f"{s.label('protected_violation')} label set")
    if s.label("too_large") in labels:
        reasons.append(f"{s.label('too_large')} label set")
    if s.label("security_flag") in labels:
        reasons.append(f"{s.label('security_flag')} label set")
    if s.label("librarian_flag") in labels:
        reasons.append(f"{s.label('librarian_flag')} label set")
    # Both pre-merge clearances must be present.
    if s.label("security_cleared") not in labels:
        reasons.append(f"{s.label('security_cleared')} label missing")
    if s.label("librarian_cleared") not in labels:
        reasons.append(f"{s.label('librarian_cleared')} label missing")
    if pr.get("isDraft"):
        reasons.append("PR is draft")

    # Consensus check: every required cli must have APPROVE'd the latest commit.
    pr_number = pr["number"]
    verdicts = rotation.reviewer_verdicts(pr_number)
    missing = [c for c in s.required_reviewer_clis if c not in verdicts]
    if missing:
        reasons.append(f"missing reviews from required agents: {missing}")
    for cli, v in verdicts.items():
        if v != "APPROVE":
            reasons.append(f"latest {cli} verdict is {v}")

    files = pr.get("files") or []
    bad = protected.violations([f.get("path") for f in files if f.get("path")])
    if bad:
        reasons.append(f"diff touches protected paths: {bad}")

    adds = pr.get("additions", 0)
    dels = pr.get("deletions", 0)
    if (adds + dels) > s.max_diff_loc:
        reasons.append(f"diff is {adds + dels} LOC, cap is {s.max_diff_loc}")

    state = _ci_state(pr)
    if state != "green":
        reasons.append(f"CI state is {state}")

    mergeable = (pr.get("mergeable") or "").upper()
    if mergeable == "CONFLICTING":
        reasons.append("PR has merge conflicts")

    # ---- constitutional fidelity ----------------------------------------
    # Tier-3: require APPROVE from each of {claude, codex, gemini}.
    is_tier3, tier3_reasons = _is_tier3_pr(pr)
    if is_tier3:
        missing = _tier3_missing_approvals(pr)
        if missing:
            reasons.append(
                f"{tier3_reasons[0]}; tier-3 requires APPROVE from each of "
                f"{list(_TRIUMVIRATE)} — missing {missing}"
            )

    # Hard-limit: PRs touching protected_paths setting / trusted_authors /
    # repo / files in protected_paths must be human-authored end-to-end.
    is_hard_limit, hl_reasons = _is_hard_limit_pr(pr)
    if is_hard_limit:
        ok, offender = _all_commits_human_authored(pr)
        if not ok:
            if offender:
                reasons.append(
                    f"{hl_reasons[0]}; hard-limit requires human-authored commits, "
                    f"but commit author {offender!r} is a configured bot"
                )
            else:
                reasons.append(
                    f"{hl_reasons[0]}; hard-limit requires human-authored commits, "
                    f"but commit author identity could not be verified "
                    f"(no bots configured, or no commit author metadata)"
                )

    return reasons


def evaluate(pr_number: int) -> int:
    """Returns 0 on merge, 1 on hold, 2 on error."""
    kill_switch.check(reason="merge_gate start")
    try:
        pr = gh.get_pr(pr_number)
    except Exception as e:
        db.append(phase="merge", action="error", pr_number=pr_number,
                  notes={"error": repr(e)})
        return 2

    reasons = _gate_reasons(pr)
    if reasons:
        db.append(phase="merge", action="skip", pr_number=pr_number,
                  outcome="held", notes={"reasons": reasons})
        return 1

    started = time.time()
    db.append(phase="merge", action="start", pr_number=pr_number)
    try:
        # Merge as the engineer's bot (claude) — the PR is theirs.
        gh.merge_pr(number=pr_number, method="squash", as_cli="claude")
    except Exception as e:
        db.append(phase="merge", action="error", pr_number=pr_number,
                  duration_s=time.time() - started,
                  notes={"error": repr(e)})
        return 2

    db.append(phase="merge", action="finish", pr_number=pr_number,
              outcome="merged", duration_s=time.time() - started)
    review_baton.clear(pr_number)
    try:
        transcripts.write(pr_number, title=pr.get("title"))
    except Exception as e:
        # Transcript failure must not fail a successful merge.
        db.append(phase="merge", action="error", pr_number=pr_number,
                  outcome="transcript_failed", notes={"error": repr(e)})
    return 0
