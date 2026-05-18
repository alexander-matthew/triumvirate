"""Subprocess wrappers around `claude`, `codex`, and `gemini` CLIs in headless mode.

Each CLI auths via the user's subscription. The wrappers normalize:
- binary location (resolves via PATH or common ~/.local/bin / ~/.npm-global/bin)
- working directory
- timeout (hard SIGKILL)
- tool / sandbox restrictions
- structured stdout capture
- quota detection (rate-limit signals in stderr → recorded as gates)

Phase scripts should call `run_persona(persona, prompt=..., cwd=...)`.
The `run_claude` / `run_codex` / `run_gemini` primitives are kept for
ad-hoc invocations.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from . import quota

if TYPE_CHECKING:
    from .persona import Persona


class AgentRunError(RuntimeError):
    pass


@dataclass
class PersonaRun:
    persona_name: str
    cli: str
    proc: subprocess.CompletedProcess
    final_message: str
    duration_s: float
    rate_limited: bool
    retry_after_ts: Optional[float]
    timed_out: bool

    @property
    def stdout(self) -> str:
        return self.proc.stdout

    @property
    def stderr(self) -> str:
        return self.proc.stderr

    @property
    def returncode(self) -> int:
        return self.proc.returncode


def _home() -> str:
    return os.environ.get("HOME") or str(Path.home())


def _local_bin_candidates(cli_name: str) -> tuple[str, ...]:
    home = _home()
    return (
        f"{home}/.local/bin/{cli_name}",
        f"{home}/.npm-global/bin/{cli_name}",
    )


def _resolve(name: str) -> str:
    p = shutil.which(name)
    if p:
        return p
    for c in _local_bin_candidates(name):
        if Path(c).is_file() and os.access(c, os.X_OK):
            return c
    raise AgentRunError(f"{name!r} not found on PATH or in known locations")


def _base_env() -> dict[str, str]:
    """systemd may strip PATH; ensure both CLI bin dirs are present."""
    env = os.environ.copy()
    home = _home()
    extra = [f"{home}/.local/bin", f"{home}/.npm-global/bin"]
    env["PATH"] = ":".join([*extra, env.get("PATH", "/usr/bin:/bin")])
    env.setdefault("HOME", home)
    return env


def run_claude(
    *,
    prompt: str,
    cwd: Path,
    timeout_min: int,
    permission_mode: str = "bypassPermissions",
    allowed_tools: Optional[list[str]] = None,
    disallowed_tools: Optional[list[str]] = None,
    max_turns: int = 80,
    extra_env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Invoke `claude -p` headlessly.

    Defaults to `bypassPermissions` because the worktree is the blast radius:
    `--add-dir` is NOT passed, so filesystem writes outside `cwd` are blocked
    by Claude Code's permission layer even with bypass.
    """
    bin_ = _resolve("claude")
    args: list[str] = [
        bin_, "-p", prompt,
        "--permission-mode", permission_mode,
        "--max-turns", str(max_turns),
    ]
    if allowed_tools is not None:
        args += ["--allowed-tools", ",".join(allowed_tools)]
    if disallowed_tools is not None:
        args += ["--disallowed-tools", ",".join(disallowed_tools)]

    env = _base_env()
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        args, cwd=cwd, env=env,
        capture_output=True, text=True,
        timeout=timeout_min * 60,
    )


def run_codex(
    *,
    prompt: str,
    cwd: Path,
    timeout_min: int,
    sandbox: str = "read-only",
    model: Optional[str] = None,
    extra_env: Optional[dict[str, str]] = None,
) -> tuple[subprocess.CompletedProcess, str]:
    """Invoke `codex exec` headlessly. Returns (CompletedProcess, last_message).

    last_message comes from --output-last-message (a temp file) to avoid stdout
    parsing fragility.
    """
    bin_ = _resolve("codex")
    with tempfile.NamedTemporaryFile(
        "r+", prefix="codex-final-", suffix=".txt", delete=False
    ) as last_msg_file:
        last_msg_path = Path(last_msg_file.name)

    args: list[str] = [
        bin_, "exec",
        "-C", str(cwd),
        "--sandbox", sandbox,
        "--skip-git-repo-check",
        "--ephemeral",
        "--color", "never",
        "--output-last-message", str(last_msg_path),
    ]
    if model:
        args += ["-m", model]
    args.append(prompt)

    env = _base_env()
    if extra_env:
        env.update(extra_env)

    try:
        proc = subprocess.run(
            args, cwd=cwd, env=env,
            capture_output=True, text=True,
            timeout=timeout_min * 60,
        )
        last_msg = last_msg_path.read_text(errors="replace") if last_msg_path.exists() else ""
        return proc, last_msg
    finally:
        if last_msg_path.exists():
            last_msg_path.unlink(missing_ok=True)


def run_gemini(
    *,
    prompt: str,
    cwd: Path,
    timeout_min: int,
    approval_mode: str = "plan",
    model: Optional[str] = None,
    extra_include_dirs: Optional[tuple[str, ...]] = None,
    extra_env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    """Invoke `gemini -p` headlessly.

    `approval_mode` ∈ {'default','auto_edit','yolo','plan'}. We use 'plan' for
    read-only personas. `extra_include_dirs` widens the read sandbox beyond
    the worktree (e.g. librarian needs `~/code` for cross-project audits).
    """
    bin_ = _resolve("gemini")
    args: list[str] = [
        bin_, "-p", prompt,
        "--approval-mode", approval_mode,
        "--include-directories", str(cwd),
        "--output-format", "text",
        "--skip-trust",
    ]
    for d in (extra_include_dirs or ()):
        args += ["--include-directories", d]
    if model:
        args += ["-m", model]

    env = _base_env()
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        args, cwd=cwd, env=env,
        capture_output=True, text=True,
        timeout=timeout_min * 60,
    )


def run_persona(persona: "Persona", *, prompt: str, cwd: Path) -> PersonaRun:
    """Dispatch a persona's prompt to its configured CLI.

    All of {timeout, sandbox/permission, tool list, max_turns, extra_include_dirs}
    comes from the persona — phase scripts don't pass any of this. Detects
    subscription rate-limit errors and surfaces them as `rate_limited=True`;
    persists the gate so subsequent ticks skip until the quota resets.
    """
    started = time.time()
    timed_out = False
    try:
        if persona.cli == "claude":
            proc = run_claude(
                prompt=prompt, cwd=cwd, timeout_min=persona.timeout_min,
                permission_mode=persona.permission_mode or "bypassPermissions",
                allowed_tools=list(persona.allowed_tools) if persona.allowed_tools else None,
                disallowed_tools=list(persona.disallowed_tools) if persona.disallowed_tools else None,
                max_turns=persona.max_turns or 80,
            )
            final_message = proc.stdout
        elif persona.cli == "codex":
            proc, final_message = run_codex(
                prompt=prompt, cwd=cwd, timeout_min=persona.timeout_min,
                sandbox=persona.sandbox or "read-only",
            )
        elif persona.cli == "gemini":
            approval = "plan" if (persona.sandbox or "read-only") == "read-only" else "yolo"
            proc = run_gemini(
                prompt=prompt, cwd=cwd, timeout_min=persona.timeout_min,
                approval_mode=approval,
                extra_include_dirs=persona.extra_include_dirs,
            )
            final_message = proc.stdout
        else:
            raise AgentRunError(f"persona {persona.name!r} has unknown cli {persona.cli!r}")
    except subprocess.TimeoutExpired as e:
        proc = subprocess.CompletedProcess(
            args=e.cmd, returncode=124,
            stdout=(e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, (bytes, bytearray)) else (e.stdout or ""),
            stderr=(e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, (bytes, bytearray)) else (e.stderr or ""),
        )
        final_message = ""
        timed_out = True

    duration = time.time() - started
    rl = quota.detect(proc.stderr, cli=persona.cli)
    if rl is not None and persona.on_rate_limit == "skip_until_reset":
        quota.record(rl)

    return PersonaRun(
        persona_name=persona.name,
        cli=persona.cli,
        proc=proc,
        final_message=final_message,
        duration_s=duration,
        rate_limited=rl is not None,
        retry_after_ts=rl.retry_after_ts if rl else None,
        timed_out=timed_out,
    )
