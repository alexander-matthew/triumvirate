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
import re
import signal
import sys
import time

from .config import settings
from .lib import db, gh, kill_switch, quota, rotation
from .lib.paths import ensure_state_dir
from .lib.persona import Persona

from .phases import (
    arbitrate_pr, drift_watcher, librarian_check, merge_gate,
    propose_issues, respond_to_review, review_pr, security_check,
    triage_proposals, work_issue,
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


# ---- PR classification ----------------------------------------------------


def _is_arbiter_override(post: dict) -> bool:
    return "Arbiter override" in post["body"]


def _latest_codex_verdict(pr: dict) -> str | None:
    posts = [p for p in gh.marker_posts(pr) if not _is_arbiter_override(p)]
    if not posts:
        return None
    m = re.search(r"##VERDICT:\s*(\S+)", posts[-1]["body"])
    return m.group(1) if m else None


def _latest_arbiter_verdict(pr: dict) -> tuple[str | None, str | None]:
    posts = gh.marker_posts(pr, marker="##ARBITER_VERDICT:")
    if not posts:
        return None, None
    m = re.search(r"##ARBITER_VERDICT:\s*(\S+)", posts[-1]["body"])
    return (m.group(1) if m else None), posts[-1]["ts"]


def _reviewer_rounds(pr: dict) -> int:
    return len([p for p in gh.marker_posts(pr) if not _is_arbiter_override(p)])


def _commits_since_review(pr: dict) -> bool:
    posts = [p for p in gh.marker_posts(pr) if not _is_arbiter_override(p)]
    if not posts:
        return True
    last_ts = posts[-1]["ts"]
    commits = pr.get("commits") or []
    if not commits:
        return False
    return (commits[-1].get("committedDate") or "") > last_ts


def _commits_since_arbiter(pr: dict) -> bool:
    _, arb_ts = _latest_arbiter_verdict(pr)
    if not arb_ts:
        return False
    commits = pr.get("commits") or []
    if not commits:
        return False
    return (commits[-1].get("committedDate") or "") > arb_ts


def _is_agent_pr(pr: dict) -> bool:
    return any(l["name"].startswith("agent:") for l in pr.get("labels", []))


def _is_stalled(pr: dict) -> bool:
    s = settings()
    bad = {s.label("needs_human"), s.label("veto"), s.label("protected_violation")}
    return any(l["name"] in bad for l in pr.get("labels", []))


# ---- the state machine ----------------------------------------------------


def _persona_blocked(persona_name: str) -> bool:
    persona = Persona.load(persona_name)
    blocked, _ = quota.is_blocked(persona.cli)
    return blocked


def _dispatch() -> tuple[str, int | None]:
    s = settings()

    open_prs = gh.list_prs(state="open", limit=50)
    candidates = [p for p in open_prs if _is_agent_pr(p) and not _is_stalled(p)]
    candidates.sort(key=lambda p: p["createdAt"])
    agent_prs = [gh.get_pr(p["number"]) for p in candidates]

    for pr in agent_prs:
        pr_number = pr["number"]
        verdicts = rotation.reviewer_verdicts(pr_number)
        pending = rotation.pending_reviewer_clis(pr_number)
        verdict = _latest_codex_verdict(pr)
        arb_verdict, _ = _latest_arbiter_verdict(pr)

        # ---- arbiter has spoken ----
        if arb_verdict is not None:
            if arb_verdict == "REQUEST_FINAL_CHANGES":
                if _commits_since_arbiter(pr):
                    rc = arbitrate_pr.arbitrate(pr_number)
                    return ("arbitrate", pr_number) if rc == 0 else ("arbitrate.skip", pr_number)
                if _persona_blocked("engineer"):
                    continue
                rc = respond_to_review.respond(pr_number)
                return ("respond", pr_number) if rc == 0 else ("respond.skip", pr_number)

        # ---- reviewer round-cap reached without convergence → invoke arbiter ----
        has_request_changes = any(v == "REQUEST_CHANGES" for v in verdicts.values())
        if (has_request_changes
                and _reviewer_rounds(pr) >= s.max_review_rounds
                and not pending
                and arb_verdict is None):
            rc = arbitrate_pr.arbitrate(pr_number)
            return ("arbitrate", pr_number) if rc == 0 else ("arbitrate.skip", pr_number)

        # ---- normal flow ----

        # 1. Pending reviewers → dispatch one.
        if pending:
            chosen = rotation.pick_reviewer_cli(pr_number)
            if chosen and not _persona_blocked(rotation.reviewer_persona_name(chosen)):
                rc = review_pr.review(pr_number, cli=chosen)
                return ("review", pr_number) if rc == 0 else ("review.skip", pr_number)
            continue

        # 2. All required reviews are in for the latest commit.

        # 2a. Any agent requested changes → engineer responds.
        if has_request_changes:
            if _persona_blocked("engineer"):
                continue
            rc = respond_to_review.respond(pr_number)
            return ("respond", pr_number) if rc == 0 else ("respond.skip", pr_number)

        # 2b. Consensus APPROVE → security + librarian → merge.
        if all(v == "APPROVE" for v in verdicts.values()) and verdicts:
            labels = {l["name"] for l in pr.get("labels", [])}
            if (s.label("security_cleared") not in labels
                    and s.label("security_flag") not in labels):
                if _persona_blocked("security"):
                    continue
                rc = security_check.check(pr_number)
                return ("security", pr_number) if rc == 0 else ("security.skip", pr_number)
            if (s.label("librarian_cleared") not in labels
                    and s.label("librarian_flag") not in labels):
                if _persona_blocked("librarian-gemini"):
                    continue
                rc = librarian_check.check(pr_number)
                return ("librarian", pr_number) if rc == 0 else ("librarian.skip", pr_number)
            rc = merge_gate.evaluate(pr_number)
            return ("merge", pr_number) if rc == 0 else ("merge.skip", pr_number)

    # No PR work pending.

    # Worker (off-hours).
    if _is_off_hours() and not _persona_blocked("engineer"):
        approved = gh.list_issues(labels=[s.label("approved")], state="open", limit=5)
        if approved:
            rc = work_issue.run()
            return ("work", None) if rc == 0 else ("work.skip", None)

    # Proposer (once per day, off-hours).
    if (_is_off_hours()
            and not _proposer_ran_today()
            and not _persona_blocked("proposer")):
        rc = propose_issues.run()
        return ("propose", None) if rc == 0 else ("propose.skip", None)

    # Triage (once per day, off-hours, after proposer).
    if (_is_off_hours()
            and not _triage_ran_today()
            and _proposer_ran_today()
            and not _persona_blocked("triage")):
        proposals = gh.list_issues(labels=[s.label("proposal")], state="open", limit=5)
        if proposals:
            rc = triage_proposals.run()
            return ("triage", None) if rc == 0 else ("triage.skip", None)

    # Drift watcher (Sundays, off-hours).
    if (_is_off_hours()
            and dt.datetime.now().weekday() == 6
            and not _drift_ran_this_week()
            and not _persona_blocked("drift_watcher")):
        rc = drift_watcher.run()
        return ("drift", None) if rc == 0 else ("drift.skip", None)

    return ("noop", None)


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
                         if _is_agent_pr(p) and not _is_stalled(p)]
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
