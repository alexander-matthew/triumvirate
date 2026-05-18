"""`agent-loop` console script — single-entry CLI for the agent loop.

Subcommands:
  daemon   long-running off-hours process (used by systemd)
  tick     force one state-machine step now (foreground, easy to ^C)
  status   show current pipeline state at a glance
  halt     engage all three kill switches
  resume   reverse halt
  journal  pretty-print recent runs.sqlite events
  list     list open loop-labeled issues + PRs
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys

from .config import settings
from .lib import db, gh, kill_switch, quota
from .lib.paths import ensure_state_dir


# ---- helpers ---------------------------------------------------------------


def _fmt_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")
    print("─" * max(40, len(title)))


def _ok(s: str) -> str:    return f"\033[32m{s}\033[0m"
def _warn(s: str) -> str:  return f"\033[33m{s}\033[0m"
def _err(s: str) -> str:   return f"\033[31m{s}\033[0m"
def _dim(s: str) -> str:   return f"\033[2m{s}\033[0m"


# ---- subcommands -----------------------------------------------------------


def cmd_status(_args: argparse.Namespace) -> int:
    s = settings()
    ensure_state_dir()
    now = dt.datetime.now()
    is_off_hours_now = (
        now.hour >= s.off_hours_start or now.hour < s.off_hours_end
        if s.off_hours_start >= s.off_hours_end
        else s.off_hours_start <= now.hour < s.off_hours_end
    )

    _section("Loop state")
    ks = kill_switch.status()
    halted = any(ks.values())
    print(f"  Project      : {s.project_name} ({s.project_root})")
    print(f"  Current time : {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"  Off-hours    : {_ok('YES') if is_off_hours_now else _dim('no (worker gated)')}")
    print(f"  Kill: STOP   : {_err('ENGAGED') if ks['stop_file'] else _ok('clear')}  ({s.stop_path})")
    print(f"  Kill: label  : {_err('ENGAGED') if ks['halt_label'] else _ok('clear')}  ({s.label('halt')})")
    for cli in ("claude", "codex", "gemini"):
        blocked_, ts_ = quota.is_blocked(cli)
        label = f"  Quota: {cli:6s}: "
        if blocked_:
            print(label + _warn(dt.datetime.fromtimestamp(ts_).strftime("rate-limited until %H:%M")))
        else:
            print(label + _ok("armed"))
    print(f"  Overall      : {_err('HALTED') if halted else _ok('armed')}")

    _section(f"Open issues — {s.label('approved')}")
    issues = gh.list_issues(labels=[s.label("approved")], state="open", limit=20)
    if not issues:
        print(_dim("  (none — engineer has nothing to do)"))
    for i in issues:
        labels = ",".join(l["name"] for l in i.get("labels", []))
        print(f"  #{i['number']:>4}  {i['title'][:60]}")
        print(_dim(f"        labels: {labels}"))

    _section(f"Open issues — {s.label('proposal')} (awaiting triage)")
    props = gh.list_issues(labels=[s.label("proposal")], state="open", limit=20)
    if not props:
        print(_dim("  (none)"))
    for i in props:
        print(f"  #{i['number']:>4}  {i['title'][:60]}")

    _section("Open agent-authored PRs")
    prs = gh.list_prs(state="open", limit=30)
    agent_prs = [p for p in prs if any(l["name"].startswith("agent:") for l in p.get("labels", []))]
    if not agent_prs:
        print(_dim("  (none)"))
    for p in agent_prs:
        labels = ",".join(l["name"] for l in p.get("labels", []))
        decision = p.get("reviewDecision") or "—"
        ci = "—"
        rolls = p.get("statusCheckRollup") or []
        if rolls:
            states = {r.get("conclusion") or r.get("status") for r in rolls}
            ci = "green" if states <= {"SUCCESS", "COMPLETED"} else "/".join(sorted(st for st in states if st))
        adds, dels = p.get("additions", 0), p.get("deletions", 0)
        print(f"  PR#{p['number']:<4} {p['title'][:55]}")
        print(_dim(f"        review={decision}  ci={ci}  diff=+{adds}/-{dels}  labels={labels}"))

    _section("Today's loop events")
    summary = db.today_summary()
    if not summary:
        print(_dim("  (no events today)"))
    else:
        width = max(len(k) for k in summary)
        for k, c in sorted(summary.items()):
            print(f"  {k:<{width}}  {c}")
    return 0


def cmd_journal(args: argparse.Namespace) -> int:
    events = db.recent(args.limit)
    for ev in reversed(events):
        ts = _fmt_ts(ev["ts"])
        phase = ev["phase"].ljust(10)
        agent = (ev.get("agent") or "").ljust(8)
        target = []
        if ev.get("issue_number"):
            target.append(f"#{ev['issue_number']}")
        if ev.get("pr_number"):
            target.append(f"PR#{ev['pr_number']}")
        tgt = " ".join(target).ljust(12)
        outcome = ev.get("outcome") or ""
        dur = f"{ev['duration_s']:.1f}s" if ev.get("duration_s") else ""
        print(f"{_dim(ts)}  {phase}  {agent}  {tgt}  {ev['action']:<8} {outcome:<24} {_dim(dur)}")
    return 0


def cmd_tick(args: argparse.Namespace) -> int:
    if getattr(args, "force", False):
        os.environ["LOOP_FORCE_OFF_HOURS"] = "1"
        print(_warn("  · forcing off-hours gate open for this tick"))
    from .orchestrator import one_tick
    return one_tick()


def cmd_daemon(_args: argparse.Namespace) -> int:
    from .orchestrator import daemon
    return daemon()


def cmd_halt(_args: argparse.Namespace) -> int:
    s = settings()
    s.stop_path.parent.mkdir(parents=True, exist_ok=True)
    s.stop_path.write_text(f"halted at {dt.datetime.now().isoformat()}\n")
    print(_warn(f"  ✓ touched {s.stop_path}"))

    proc = subprocess.run(
        ["systemctl", "stop", f"{s.project_name}-loop.service"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        print(_warn(f"  ✓ systemctl stop {s.project_name}-loop.service"))
    else:
        print(_dim(f"  · systemctl stop: {proc.stderr.strip() or 'no-op'}"))

    print(_dim(f"  · to add {s.label('halt')} label:  gh issue edit N --add-label {s.label('halt')}"))

    db.append(phase="halt", action="engaged", outcome="manual")
    print(_warn("\nLoop halted. Run `agent-loop resume` to re-arm."))
    return 0


def cmd_resume(_args: argparse.Namespace) -> int:
    s = settings()
    if s.stop_path.exists():
        s.stop_path.unlink()
        print(_ok(f"  ✓ removed {s.stop_path}"))
    else:
        print(_dim(f"  · {s.stop_path} was not present"))

    ks = kill_switch.status()
    if ks["halt_label"]:
        print(_warn(f"  · {s.label('halt')} label is still set somewhere; remove it manually"))

    db.append(phase="halt", action="cleared", outcome="manual")
    print(_ok("\nLoop re-armed."))
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    s = settings()
    label_names = [s.label(k) for k in (
        "approved", "proposal", "in_progress", "needs_human", "veto"
    )]
    for label in label_names:
        items = gh.list_issues(labels=[label], state="open", limit=20)
        prs = [p for p in gh.list_prs(state="open", limit=50)
               if any(l["name"] == label for l in p.get("labels", []))]
        if not items and not prs:
            continue
        _section(label)
        for i in items:
            print(f"  #{i['number']:>4}  {i['title']}")
        for p in prs:
            print(f"  PR#{p['number']:>4}  {p['title']}")
    return 0


# ---- entry -----------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(prog="agent-loop", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daemon", help="long-running nightly process").set_defaults(fn=cmd_daemon)
    t = sub.add_parser("tick", help="force one state-machine step now")
    t.add_argument("--force", action="store_true",
                   help="bypass the off-hours gate (for daytime testing)")
    t.set_defaults(fn=cmd_tick)
    sub.add_parser("status", help="show pipeline state").set_defaults(fn=cmd_status)
    j = sub.add_parser("journal", help="recent runs.sqlite events")
    j.add_argument("--limit", type=int, default=30)
    j.set_defaults(fn=cmd_journal)
    sub.add_parser("halt", help="engage all kill switches").set_defaults(fn=cmd_halt)
    sub.add_parser("resume", help="reverse halt").set_defaults(fn=cmd_resume)
    sub.add_parser("list", help="list open loop-labeled issues + PRs").set_defaults(fn=cmd_list)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
