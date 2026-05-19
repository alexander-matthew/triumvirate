"""Triage phase: Gemini auto-approves low-risk proposer-filed specs."""
from __future__ import annotations

import re
import time

from ..config import settings
from ..lib import agent_run, db, gh, kill_switch, quota, trust
from ..lib.persona import Persona
from ..lib.phase_runtime import isolated_worktree


_DECISION = re.compile(
    r"^-\s*#(\d+)\s*:\s*(approve|leave_for_human|reject)\b\s*[-—]?\s*(.*)$",
    re.M | re.I,
)


def _parse_decisions(text: str) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    block = re.search(r"##DECISIONS\s*\n(.*?)\n##END", text or "", re.S)
    if not block:
        return out
    for m in _DECISION.finditer(block.group(1)):
        n = int(m.group(1))
        decision = m.group(2).lower()
        reason = (m.group(3) or "").strip()
        out[n] = (decision, reason)
    return out


def _format_issue_for_triage(issue: dict) -> str:
    labels = ", ".join(l["name"] for l in issue.get("labels", []))
    author = (issue.get("author") or {}).get("login", "?")
    body = issue.get("body") or ""
    return (
        f"### Issue #{issue['number']}: {issue['title']}\n"
        f"author: {author}  ·  labels: {labels}\n\n"
        + trust.wrap_untrusted(f"issue #{issue['number']} body", body)
        + "\n"
    )


def run() -> int:
    s = settings()
    kill_switch.check(reason="triage_proposals start")
    persona = Persona.load("triage")

    blocked, retry_after = quota.is_blocked(persona.cli)
    if blocked:
        db.append(phase="triage", action="skip", agent=persona.cli,
                  outcome="rate_limited", notes={"retry_after_ts": retry_after})
        return 1

    proposals = gh.list_issues(labels=[s.label("proposal")], state="open", limit=20)
    if not proposals:
        db.append(phase="triage", action="skip", outcome="no_proposals")
        return 1

    issue_list = "\n".join(_format_issue_for_triage(i) for i in proposals)
    db.append(phase="triage", action="start", agent=persona.cli,
              notes={"persona": persona.name, "n_proposals": len(proposals)})

    started = time.time()
    try:
        with isolated_worktree(f"triage-{int(started)}", as_cli=persona.cli) as worktree:
            prompt = persona.render(ISSUE_LIST=issue_list)
            run_ = agent_run.run_persona(persona, prompt=prompt, cwd=worktree)
            duration = run_.duration_s

            if run_.rate_limited:
                db.append(phase="triage", action="error", agent=persona.cli,
                          duration_s=duration, outcome="rate_limited",
                          notes={"retry_after_ts": run_.retry_after_ts})
                return 1
            if run_.timed_out or run_.returncode != 0:
                db.append(phase="triage", action="error", agent=persona.cli,
                          exit_code=run_.returncode, duration_s=duration,
                          outcome="timed_out" if run_.timed_out else "nonzero_exit",
                          notes={"stderr": run_.stderr[-1500:]})
                return 2

            decisions = _parse_decisions(run_.final_message)
            if not decisions:
                db.append(phase="triage", action="error", agent=persona.cli,
                          duration_s=duration, outcome="parse_failed",
                          notes={"stdout_tail": run_.stdout[-500:]})
                return 2

            applied: list[dict] = []
            for issue in proposals:
                n = issue["number"]
                if n not in decisions:
                    continue
                decision, reason = decisions[n]
                if decision == "approve" and not trust.issue_is_trusted(issue):
                    applied.append({"issue": n, "decision": "leave_for_human",
                                    "reason": "untrusted author; wrapper override",
                                    "model_decision": decision})
                    continue
                try:
                    if decision == "approve":
                        gh.remove_label(kind="issue", number=n, label=s.label("proposal"), as_cli=persona.cli)
                        gh.add_label(kind="issue", number=n, label=s.label("approved"), as_cli=persona.cli)
                        gh.comment(kind="issue", number=n, as_cli=persona.cli,
                                   body=f"🤖 Auto-approved by triage agent. Reason: {reason}")
                    elif decision == "reject":
                        gh.comment(kind="issue", number=n, as_cli=persona.cli,
                                   body=f"🤖 Rejected by triage agent. Reason: {reason}")
                        from ..lib.gh import _run  # type: ignore
                        _run(["issue", "close", str(n)], as_cli=persona.cli)
                    applied.append({"issue": n, "decision": decision, "reason": reason})
                except Exception as e:
                    applied.append({"issue": n, "decision": "error",
                                    "reason": repr(e), "model_decision": decision})

            db.append(phase="triage", action="finish", agent=persona.cli,
                      duration_s=duration, outcome="processed",
                      notes={"count": len(applied), "decisions": applied})
            return 0

    except kill_switch.HaltRequested as e:
        db.append(phase="triage", action="halted", agent=persona.cli,
                  notes={"reason": str(e)})
        return 2
    except Exception as e:
        db.append(phase="triage", action="error", agent=persona.cli,
                  notes={"error": repr(e)})
        return 2
