"""Protected-path enforcement. Agents must not modify these files.

The list comes from `Settings.protected_paths` (project-supplied).
"""
from __future__ import annotations

from ..config import settings


def violations(changed_paths: list[str]) -> list[str]:
    """Return the subset of changed paths matching a project-configured protected prefix."""
    hits = []
    protected_paths = settings().protected_paths
    for p in changed_paths:
        for prefix in protected_paths:
            if p == prefix.rstrip("/") or p.startswith(prefix):
                hits.append(p)
                break
    return hits
