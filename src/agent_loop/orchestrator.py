"""The daemon: polls GitHub state every settings().tick_seconds, dispatches one phase per tick.

Single-process state machine. Phases the orchestrator can dispatch:
  - work       (engineer implements an approved issue → opens PR)
  - review     (reviewer-codex / reviewer-gemini reviews a PR)
  - respond    (engineer addresses reviewer REQUEST_CHANGES)
  - arbitrate  (third leg of the stool; tiebreaker when consensus fails)
  - security   (Gemini adversarial scan before merge)
  - librarian  (Gemini cross-project consistency before merge)
  - merge      (PR has Consensus + Security clear + Librarian clear + CI green → auto-merge)
  - propose    (Claude files 1-3 new proposal issues)
  - triage     (Gemini auto-approves low-risk proposals)
  - drift      (Claude weekly architectural audit)

Exactly one phase runs per tick. Daemon exits when kill switch engaged, past
OFF_HOURS_END with nothing in flight, or SIGTERM.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import os
import signal
import sys
import time
from dataclasses import dataclass

from .config import Settings, settings
from .lib import db, gh, kill_switch, quota, rotation
from .lib.paths import ensure_state_dir
from .lib.persona import Persona
from .lib.pr_view import PRView

from .phases import (
    arbitrate_pr, librarian_check, merge_gate,
    respond_to_review, review_pr, security_check, spec_writer,
    synthesis as synthesis_phase, triage_proposals, work_issue,
)


# ---- signal handling ------------------------------------------------------

_should_stop = False


def _on_sigterm(signum, frame):  # noqa: D401
    global _should_stop
    _should_stop = True


signal.signal(signal.SIGTERM, _on_sigterm)
signal.signal(signal.SIGINT, _on_sigterm)


# ---- time gates -----------------------------------------------------------


def _is_off_hours() -> bool:
    if os.environ.get("LOOP_FORCE_OFF_HOURS") == "1":
        return True
    s = settings()
    h = dt.datetime.now().hour
    if s.off_hours_start < s.off_hours_end:
        return s.off_hours_start <= h < s.off_hours_end
    return h >= s.off_hours_start or h < s.off_hours_end


def _phase_ran_today(phase: str) -> bool:
    today_start = time.time() - (time.time() % 86400)
    for ev in db.recent(200):
        if (ev["phase"] == phase and ev["action"] in {"finish", "skip"}
                and ev["ts"] >= today_start):
            return True
    return False


def _proposer_ran_today() -> bool:
    return _phase_ran_today("propose")


def _triage_ran_today() -> bool:
    return _phase_ran_today("triage")


def _drift_ran_this_week() -> bool:
    week_ago = time.time() - 7 * 86400
    for ev in db.recent(500):
        if (ev["phase"] == "drift" and ev["action"] in {"finish", "skip"}
                and ev["ts"] >= week_ago):
            return True
    return False


def _synthesis_ran_this_week() -> bool:
    week_ago = time.time() - 7 * 86400
    for ev in db.recent(500):
        if (ev["phase"] == "synthesis" and ev["action"] in {"finish", "skip"}
                and ev["ts"] >= week_ago):
            return True
    return False


# ---- the state machine ----------------------------------------------------


def _stalled_labels() -> frozenset[str]:
    s = settings()
    return frozenset({s.label("needs_human"), s.label("veto"), s.label("protected_violation")})


def _is_agent_pr_listing(pr: dict) -> bool:
    """Cheap label-only check against the list_prs() payload (no commits/comments)."""
    return any(l["name"].startswith("agent:") for l in pr.get("labels", []))


def _is_stalled_listing(pr: dict) -> bool:
    bad = _stalled_labels()
    return any(l["name"] in bad for l in pr.get("labels", []))


def _persona_blocked(persona_name: str) -> bool:
    persona = Persona.load(persona_name)
    blocked, _ = quota.is_blocked(persona.cli)
    return blocked


# ---- plan + execute -------------------------------------------------------


@dataclass(frozen=True)
class PhaseDecision:
    """What the orchestrator would do next.

    Returned by :func:`plan` and consumed by :func:`execute`. Splitting the
    state machine into a pure decision step + an effect step makes the
    state machine unit-testable and lets the CLI offer a ``--dry-run``
    that prints what *would* happen without burning agent budget.

    ``phase`` matches the dispatch tag (``review``, ``arbitrate``, ...,
    or ``noop`` when nothing actionable). ``target`` is the PR or issue
    number when phase-relevant. ``reason`` is a human-readable
    explanation suitable for ``status`` output. ``cli`` is set for phases
    that pick one of several reviewer/arbiter CLIs upfront so execute
    doesn't redo the choice.
    """

    phase: str
    target: int | None
    reason: str
    cli: str | None = None


def plan() -> PhaseDecision:
    """Decide what one tick would do, without taking any action.

    Pure with respect to side effects on GitHub or local state. Still
    reads GitHub state and rate-limit DB. Returns a single
    :class:`PhaseDecision`; ``phase == "noop"`` means nothing actionable.
    """
    s = settings()

    open_prs = gh.list_prs(state="open", limit=50)
    candidates = [p for p in open_prs
                  if _is_agent_pr_listing(p) and not _is_stalled_listing(p)]
    candidates.sort(key=lambda p: p["createdAt"])
    views = [PRView.from_pr(gh.get_pr(p["number"])) for p in candidates]

    for view in views:
        decision = _plan_pr(view, s)
        if decision is not None:
            return decision

    return _plan_off_hours(s)


def _plan_pr(view: PRView, s: Settings) -> PhaseDecision | None:
    """Decide what (if anything) to do for one candidate PR.

    Returns None if this PR has no actionable phase — caller should
    advance to the next candidate.
    """
    pr_number = view.number
    verdicts = rotation.reviewer_verdicts(pr_number)
    pending = rotation.pending_reviewer_clis(pr_number)
    has_request_changes = any(v == "REQUEST_CHANGES" for v in verdicts.values())

    # ---- arbiter has spoken ----
    if view.latest_arbiter_verdict == "REQUEST_FINAL_CHANGES":
        if view.commits_since_arbiter and s.phase_enabled("arbitrate"):
            return PhaseDecision("arbitrate", pr_number,
                                 "new commits since arbiter REQUEST_FINAL_CHANGES")
        if s.phase_enabled("respond") and not _persona_blocked("engineer"):
            return PhaseDecision("respond", pr_number,
                                 "arbiter REQUEST_FINAL_CHANGES; engineer addresses")
        return None

    # ---- reviewer round-cap reached without convergence → invoke arbiter ----
    if (has_request_changes
            and view.reviewer_rounds >= s.max_review_rounds
            and not pending
            and view.latest_arbiter_verdict is None
            and s.phase_enabled("arbitrate")):
        return PhaseDecision("arbitrate", pr_number,
                             f"round cap reached ({view.reviewer_rounds}/{s.max_review_rounds})")

    # ---- normal flow ----

    # 1. Pending reviewers → dispatch one.
    if pending and s.phase_enabled("review"):
        chosen = rotation.pick_reviewer_cli(pr_number)
        if chosen and not _persona_blocked(rotation.reviewer_persona_name(chosen)):
            return PhaseDecision("review", pr_number,
                                 f"pending reviewer: {chosen}", cli=chosen)
        return None

    # 2a. Any agent requested changes → engineer responds.
    if has_request_changes and s.phase_enabled("respond"):
        if _persona_blocked("engineer"):
            return None
        return PhaseDecision("respond", pr_number,
                             "reviewer(s) REQUEST_CHANGES; engineer addresses")

    # 2b. Consensus APPROVE → security + librarian → merge.
    if verdicts and all(v == "APPROVE" for v in verdicts.values()):
        if s.phase_enabled("security") and not (
                view.has_label(s.label("security_cleared"))
                or view.has_label(s.label("security_flag"))):
            if _persona_blocked("security"):
                return None
            return PhaseDecision("security", pr_number,
                                 "consensus APPROVE; pre-merge security scan")
        if s.phase_enabled("librarian") and not (
                view.has_label(s.label("librarian_cleared"))
                or view.has_label(s.label("librarian_flag"))):
            if _persona_blocked("librarian-gemini"):
                return None
            return PhaseDecision("librarian", pr_number,
                                 "consensus APPROVE; pre-merge librarian audit")
        if s.phase_enabled("merge"):
            return PhaseDecision("merge", pr_number, "consensus APPROVE; gates green")

    return None


def _plan_off_hours(s: Settings) -> PhaseDecision:
    """Decide which off-hours phase to run when no PR work is pending."""
    if (s.phase_enabled("work")
            and _is_off_hours()
            and not _persona_blocked("engineer")):
        approved = gh.list_issues(labels=[s.label("approved")], state="open", limit=5)
        if approved:
            return PhaseDecision("work", None,
                                 f"{len(approved)} approved issue(s) pending engineer")

    if (s.phase_enabled("propose")
            and _is_off_hours()
            and not _proposer_ran_today()
            and not _persona_blocked("proposer")):
        return PhaseDecision("propose", None, "daily proposer hasn't run yet today")

    if (s.phase_enabled("triage")
            and _is_off_hours()
            and not _triage_ran_today()
            and _proposer_ran_today()
            and not _persona_blocked("triage")):
        proposals = gh.list_issues(labels=[s.label("proposal")], state="open", limit=5)
        if proposals:
            return PhaseDecision("triage", None,
                                 f"{len(proposals)} proposal(s) await triage")

    if (s.phase_enabled("drift")
            and _is_off_hours()
            and dt.datetime.now().weekday() == 6
            and not _drift_ran_this_week()
            and not _persona_blocked("drift_watcher")):
        return PhaseDecision("drift", None, "weekly drift sweep not yet run")

    if (s.phase_enabled("synthesis")
            and _is_off_hours()
            and dt.datetime.now().weekday() == 6
            and not _synthesis_ran_this_week()
            and not _persona_blocked("synthesis")):
        return PhaseDecision("synthesis", None,
                             "weekly self-improvement pass not yet run")

    return PhaseDecision("noop", None, "no actionable work pending")


def execute(decision: PhaseDecision) -> int:
    """Run the phase named by ``decision`` and return its exit code.

    The orchestrator dispatcher: ``one_tick`` calls ``plan()`` then
    ``execute(...)``. Phases the wrapper invokes directly are pure
    pass-through; ``noop`` is a no-op success.
    """
    target = decision.target
    match decision.phase:
        case "review":
            assert target is not None and decision.cli is not None
            return review_pr.review(target, cli=decision.cli)
        case "arbitrate":
            assert target is not None
            return arbitrate_pr.arbitrate(target)
        case "respond":
            assert target is not None
            return respond_to_review.respond(target)
        case "security":
            assert target is not None
            return security_check.check(target)
        case "librarian":
            assert target is not None
            return librarian_check.check(target)
        case "merge":
            assert target is not None
            return merge_gate.evaluate(target)
        case "work":
            return work_issue.run()
        case "propose":
            return spec_writer.propose()
        case "triage":
            return triage_proposals.run()
        case "drift":
            return spec_writer.drift()
        case "synthesis":
            return synthesis_phase.run()
        case "noop":
            return 0
        case _:
            raise ValueError(f"unknown phase: {decision.phase!r}")


def _dispatch() -> tuple[str, int | None]:
    """Back-compat shim: legacy ``(phase, target)`` tuple from plan+execute.

    The daemon and ``one_tick`` still log via this shape. New callers
    should use ``plan()`` / ``execute()`` directly.
    """
    decision = plan()
    rc = execute(decision)
    suffix = "" if rc == 0 else ".skip"
    return (f"{decision.phase}{suffix}", decision.target)


def one_tick() -> int:
    """Run a single dispatch step under the loop lock."""
    ensure_state_dir()
    lock_fp = open(settings().lock_path, "w")
    try:
        fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        db.append(phase="tick", action="skip", outcome="lock_held")
        print("loop.lock held by another process; skipping", file=sys.stderr)
        return 1
    try:
        try:
            kill_switch.check(reason="tick top")
        except kill_switch.HaltRequested as e:
            db.append(phase="tick", action="halted", notes={"reason": str(e)})
            print(f"Halted: {e}", file=sys.stderr)
            return 2

        started = time.time()
        phase, target = _dispatch()
        duration = time.time() - started
        db.append(
            phase="tick", action="finish",
            outcome=phase, duration_s=duration,
            notes={"target": target} if target else None,
        )
        return 0
    finally:
        try:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fp.close()


def daemon() -> int:
    """Long-running process started by systemd at off-hours start."""
    ensure_state_dir()
    db.append(phase="tick", action="daemon_start")
    s = settings()

    while not _should_stop:
        if not _is_off_hours():
            agent_prs = [p for p in gh.list_prs(state="open", limit=50)
                         if _is_agent_pr_listing(p) and not _is_stalled_listing(p)]
            if not agent_prs:
                db.append(phase="tick", action="daemon_wrap", outcome="off_hours_done")
                break

        one_tick()

        sleep_target = _next_sleep_seconds(s.tick_seconds)
        slept = 0
        while slept < sleep_target and not _should_stop:
            time.sleep(min(5, sleep_target - slept))
            slept += 5

    db.append(phase="tick", action="daemon_exit",
              notes={"sigterm": _should_stop})
    return 0


# All CLIs the loop ever dispatches. If all three are quota-blocked, the next
# `_dispatch` call will return noop on every tick — extend the sleep instead.
_KNOWN_CLIS = ("claude", "codex", "gemini")
_MAX_ADAPTIVE_SLEEP_S = 30 * 60  # cap a single sleep window at 30min so
                                  # kill-switch / sigterm stay responsive.


def _next_sleep_seconds(default_s: int) -> int:
    """If every persona is quota-blocked, sleep until the earliest unblock
    (capped at 30min). Otherwise the normal tick interval.

    Costs nothing when CLIs are armed — `earliest_retry_after` returns None
    and we fall through to `default_s`.
    """
    soonest = quota.earliest_retry_after(list(_KNOWN_CLIS))
    if soonest is None:
        return default_s
    # All three blocked? If even one is armed, default tick is fine — there
    # may be useful work this tick.
    if not all(quota.is_blocked(cli)[0] for cli in _KNOWN_CLIS):
        return default_s
    wait = int(soonest - time.time())
    if wait <= default_s:
        return default_s
    capped = min(wait, _MAX_ADAPTIVE_SLEEP_S)
    db.append(phase="tick", action="adaptive_sleep",
              outcome="all_quota_blocked",
              notes={"sleep_s": capped, "retry_after_ts": soonest})
    return capped
