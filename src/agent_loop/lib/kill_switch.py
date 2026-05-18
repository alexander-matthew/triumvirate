"""Three independent halt mechanisms. Any one engaged → halt requested.

1. `systemctl stop personal-site-loop.service` (or whatever systemd unit
   wraps the daemon) — kills the running process.
2. `touch <project>/agents/STOP` — checked at every phase boundary.
3. `agent:halt` label on any open issue or PR — checked at every tick.
"""
from __future__ import annotations

from ..config import settings
from . import gh


class HaltRequested(RuntimeError):
    """Raised when any kill switch is engaged. Catch at phase boundaries."""


def _stop_file_engaged() -> bool:
    return settings().stop_path.exists()


def _halt_label_engaged() -> bool:
    try:
        hits = gh.search(f"is:open label:{settings().label('halt')}")
        return bool(hits)
    except Exception:
        return False


def check(*, reason: str = "phase boundary") -> None:
    """Raise HaltRequested if any kill switch is engaged."""
    if _stop_file_engaged():
        raise HaltRequested(f"STOP file present at {settings().stop_path} ({reason})")
    if _halt_label_engaged():
        raise HaltRequested(
            f"{settings().label('halt')} label is set on an open issue/PR ({reason})"
        )


def status() -> dict:
    return {
        "stop_file": _stop_file_engaged(),
        "halt_label": _halt_label_engaged(),
    }
