"""Pre-merge security check: Gemini scans sensitive PRs for findings.

Sensitive paths come from `Settings.sensitive_path_prefixes`.
"""
from __future__ import annotations

import re
import time

from ..config import settings
from ..lib import agent_run, db, gh, kill_switch, quota
from ..lib.persona import Persona
from ..lib.phase_runtime import isolated_worktree
from ..lib.verdicts import ParseError, SecurityVerdict


def _is_sensitive_pr(pr: dict) -> tuple[bool, list[str]]:
    s = settings()
    files = pr.get("files") or []
    paths = [f.get("path", "") for f in files if f.get("path")]
    hits = set()
    for p in paths:
        for prefix in s.sensitive_path_prefixes:
            if p == prefix or p.startswith(prefix + "/"):
                hits.add(p)
        # Heuristic: newly-added file under app/routes/ counts as a new endpoint.
        if p.startswith("app/routes/") and any(
            f.get("path") == p and (f.get("additions", 0) > 0)
            and (f.get("deletions", 0) == 0) for f in files
        ):
            hits.add(p)
        # Dependency files always count.
        if p in ("pyproject.toml", "package.json", "uv.lock", "package-lock.json"):
            hits.add(p)
    return (bool(hits), sorted(hits))


def _already_scanned(pr: dict) -> bool:
    s = settings()
    labels = {l["name"] for l in pr.get("labels", [])}
    return s.label("security_flag") in labels or s.label("security_cleared") in labels


def _extract_issue_ref(pr_body: str) -> int | None:
    m = re.search(r"Closes\s+#(\d+)", pr_body)
    return int(m.group(1)) if m else None


def check(pr_number: int) -> int:
    s = settings()
    kill_switch.check(reason="security_check start")
    persona = Persona.load("security")

    blocked, retry_after = quota.is_blocked(persona.cli)
    if blocked:
        db.append(phase="security", action="skip", agent=persona.cli,
                  pr_number=pr_number, outcome="rate_limited",
                  notes={"retry_after_ts": retry_after})
        return 1

    pr = gh.get_pr(pr_number)
    if _already_scanned(pr):
        db.append(phase="security", action="skip", pr_number=pr_number,
                  outcome="already_scanned")
        return 1

    sensitive, paths = _is_sensitive_pr(pr)
    if not sensitive:
        gh.add_label(kind="pr", number=pr_number, label=s.label("security_cleared"), as_cli=persona.cli)
        db.append(phase="security", action="finish", pr_number=pr_number,
                  outcome="not_sensitive",
                  notes={"label_applied": s.label("security_cleared")})
        return 0

    db.append(phase="security", action="start", agent=persona.cli,
              pr_number=pr_number,
              notes={"persona": persona.name, "sensitive_paths": paths})

    branch = pr.get("headRefName", "")
    try:
        with isolated_worktree(
            f"security-{pr_number}",
            as_cli=persona.cli,
            checkout_branch=branch,
        ) as worktree:
            prompt = persona.render(
                PR_NUMBER=pr["number"],
                PR_TITLE=pr["title"],
                ISSUE_NUMBER=_extract_issue_ref(pr.get("body") or "") or "?",
                ADDITIONS=pr.get("additions", 0),
                DELETIONS=pr.get("deletions", 0),
                CHANGED_FILES=pr.get("changedFiles", 0),
                PR_BODY=pr.get("body") or "",
                SENSITIVE_PATHS="\n".join(f"- {p}" for p in paths),
            )

            run_ = agent_run.run_persona(persona, prompt=prompt, cwd=worktree)
            duration = run_.duration_s

            if run_.rate_limited:
                db.append(phase="security", action="error", agent=persona.cli,
                          pr_number=pr_number, duration_s=duration,
                          outcome="rate_limited",
                          notes={"retry_after_ts": run_.retry_after_ts})
                return 1
            if run_.timed_out:
                db.append(phase="security", action="error", agent=persona.cli,
                          pr_number=pr_number, duration_s=duration,
                          exit_code=run_.returncode, outcome="timed_out")
                return 2

            try:
                parsed = SecurityVerdict.parse(run_.final_message)
            except ParseError as e:
                db.append(phase="security", action="error", agent=persona.cli,
                          pr_number=pr_number, duration_s=duration,
                          outcome="parse_failed",
                          notes={"stdout_tail": run_.stdout[-1500:],
                                 "stderr_tail": run_.stderr[-1500:],
                                 "parse_error": str(e)})
                return 2

            body = parsed.render() + (
                f"\n---\n*security: {persona.cli} · "
                f"{time.strftime('%Y-%m-%d %H:%M')}*"
            )
            gh.comment(kind="pr", number=pr_number, body=body, as_cli=persona.cli)

            db.record_verdict(
                pr_number=pr_number,
                marker_type="security",
                cli=persona.cli,
                verdict=parsed.verdict,
                raw_body=body,
            )

            if parsed.verdict == "FLAG":
                gh.add_label(kind="pr", number=pr_number, label=s.label("security_flag"), as_cli=persona.cli)
                gh.add_label(kind="pr", number=pr_number, label=s.label("needs_human"), as_cli=persona.cli)
            else:
                gh.add_label(kind="pr", number=pr_number, label=s.label("security_cleared"), as_cli=persona.cli)

            db.append(phase="security", action="finish", agent=persona.cli,
                      pr_number=pr_number, duration_s=duration,
                      outcome=parsed.verdict.lower(),
                      notes={"persona": persona.name, "sensitive_paths": paths,
                             "findings": parsed.findings[:1500]})
            return 0

    except kill_switch.HaltRequested as e:
        db.append(phase="security", action="halted", agent=persona.cli,
                  pr_number=pr_number, notes={"reason": str(e)})
        return 2
    except Exception as e:
        db.append(phase="security", action="error", agent=persona.cli,
                  pr_number=pr_number, notes={"error": repr(e)})
        return 2
