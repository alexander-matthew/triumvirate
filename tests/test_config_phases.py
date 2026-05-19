"""Tests for [phases].enabled config knob and Settings.phase_enabled()."""
from __future__ import annotations

from pathlib import Path

from agent_loop import config


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(body)
    return cfg


def _load(tmp_path: Path, body: str, monkeypatch) -> config.Settings:
    """Load Settings from a synthesized config_root in tmp_path."""
    monkeypatch.setenv("AGENT_LOOP_CONFIG_ROOT", str(tmp_path))
    config.reset()
    _write(tmp_path, body)
    return config.load()


def test_phases_default_to_all_enabled(tmp_path, monkeypatch):
    s = _load(tmp_path, """
[project]
name = "test"
repo = "x/y"
trusted_authors = []
""", monkeypatch)
    for phase in ("work", "review", "respond", "arbitrate", "security",
                  "librarian", "merge", "propose", "triage", "drift"):
        assert s.phase_enabled(phase), f"{phase} should default-enabled"


def test_phases_enabled_list_restricts_dispatch(tmp_path, monkeypatch):
    s = _load(tmp_path, """
[project]
name = "test"
repo = "x/y"
trusted_authors = []

[phases]
enabled = ["work", "review", "respond", "arbitrate", "merge"]
""", monkeypatch)
    assert s.phase_enabled("work")
    assert s.phase_enabled("review")
    assert s.phase_enabled("merge")
    assert not s.phase_enabled("security")
    assert not s.phase_enabled("librarian")
    assert not s.phase_enabled("propose")
    assert not s.phase_enabled("drift")


def test_phase_enabled_unknown_phase_is_false(tmp_path, monkeypatch):
    s = _load(tmp_path, """
[project]
name = "test"
repo = "x/y"
trusted_authors = []
""", monkeypatch)
    assert not s.phase_enabled("nonexistent_phase")
