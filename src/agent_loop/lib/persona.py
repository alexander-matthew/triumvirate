"""Agent personas — declarative config + prompt body per phase.

Persona files live at `<project>/agents/personas/<name>.md` (or wherever
the project's `Settings.personas_dir` points). Each file is a TOML
frontmatter block followed by the prompt body template.

The framework ships generic persona templates in `triumvirate/templates/
personas/` that projects copy and customize. The Persona class itself
doesn't care which version is loaded — it just parses the file.

The triumvirate constitution (``constitution.md``) is loaded once at
process start and prepended to every persona's rendered prompt. See
``load_constitution`` and ``Persona.render`` for the wiring; see
``templates/constitution.md`` for the canonical text.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from string import Template

from ..config import settings


# ---- constitution loading -------------------------------------------------


_CONSTITUTION_FILENAME = "constitution.md"


@lru_cache(maxsize=1)
def load_constitution() -> str:
    """Return the constitution text that every persona prompt is prefixed with.

    Discovery order:
      1. ``<config_root>/constitution.md`` — per-project override.
      2. ``<config_root>/agents/constitution.md`` — co-located layout.
      3. The packaged template at ``templates/constitution.md`` —
         framework default.

    A consumer that wants to opt out of the constitution entirely can
    place an empty file at one of the first two paths; the persona
    layer will then prepend an empty preamble. That is an explicit
    choice (and a constitutional violation per the document itself),
    not a silent default.
    """
    candidates: list[Path] = []
    try:
        s = settings()
        candidates.append(s.config_root / _CONSTITUTION_FILENAME)
        candidates.append(s.config_root / "agents" / _CONSTITUTION_FILENAME)
    except FileNotFoundError:
        # No consumer config present (e.g. running tests against the
        # framework itself). Fall through to the packaged default.
        pass
    # Framework default: relative to this source file → templates/.
    candidates.append(
        Path(__file__).resolve().parents[3] / "templates" / _CONSTITUTION_FILENAME
    )
    for path in candidates:
        if path.exists():
            return path.read_text()
    return ""  # framework misinstalled; degrade open rather than crash


def reset_constitution_cache() -> None:
    """For tests: clear the lru_cache so a fresh constitution is loaded."""
    load_constitution.cache_clear()


ON_RATE_LIMIT = {"skip_until_reset", "fail"}
ON_PARSE_FAIL = {"comment_and_retry", "fail"}
ON_NO_OUTPUT = {"log_and_skip", "fail"}
OUTPUT_FORMAT = {"free", "structured-markers"}


@dataclass(frozen=True)
class Persona:
    name: str
    cli: str
    role: str
    voice: str
    timeout_min: int

    sandbox: str | None = None
    permission_mode: str | None = None
    max_turns: int | None = None
    allowed_tools: tuple[str, ...] | None = None
    disallowed_tools: tuple[str, ...] | None = None

    on_rate_limit: str = "skip_until_reset"
    on_parse_fail: str = "comment_and_retry"
    on_no_output: str = "log_and_skip"
    escalation_label: str = "agent:needs-human"

    output_format: str = "free"
    required_markers: tuple[str, ...] = field(default_factory=tuple)

    # Extra read-only directories the persona may access beyond its worktree.
    extra_include_dirs: tuple[str, ...] = field(default_factory=tuple)

    prompt_template: str = ""

    def render(self, **vars: str | int) -> str:
        """Render the persona's prompt template with the constitution prepended.

        The constitution is the highest-authority document of the loop
        (see ``templates/constitution.md``) and is included as a preamble
        for every persona, every render. Persona prompts may reference
        constitutional concepts (tiers, the three values, the
        unanimous-three rule) without re-stating them.
        """
        body = Template(self.prompt_template).safe_substitute(vars)
        constitution = load_constitution()
        if not constitution:
            return body
        return f"{constitution}\n\n---\n\n{body}"

    @classmethod
    def load(cls, name: str) -> "Persona":
        path = settings().personas_dir / f"{name}.md"
        if not path.exists():
            raise FileNotFoundError(f"no persona at {path}")
        return _load_from_path(path)


def _load_from_path(path: Path) -> Persona:
    text = path.read_text()
    fm, body = _split_frontmatter(text)
    data = tomllib.loads(fm) if fm else {}

    _check_enum("on_rate_limit", data.get("on_rate_limit"), ON_RATE_LIMIT)
    _check_enum("on_parse_fail", data.get("on_parse_fail"), ON_PARSE_FAIL)
    _check_enum("on_no_output", data.get("on_no_output"), ON_NO_OUTPUT)
    _check_enum("output_format", data.get("output_format"), OUTPUT_FORMAT)

    return Persona(
        name=data.get("name", path.stem),
        cli=data["cli"],
        role=data.get("role", ""),
        voice=data.get("voice", ""),
        timeout_min=int(data.get("timeout_min", 30)),
        sandbox=data.get("sandbox"),
        permission_mode=data.get("permission_mode"),
        max_turns=data.get("max_turns"),
        allowed_tools=_tuple_or_none(data.get("allowed_tools")),
        disallowed_tools=_tuple_or_none(data.get("disallowed_tools")),
        on_rate_limit=data.get("on_rate_limit", "skip_until_reset"),
        on_parse_fail=data.get("on_parse_fail", "comment_and_retry"),
        on_no_output=data.get("on_no_output", "log_and_skip"),
        escalation_label=data.get("escalation_label", "agent:needs-human"),
        output_format=data.get("output_format", "free"),
        required_markers=tuple(data.get("required_markers", ())),
        extra_include_dirs=tuple(data.get("extra_include_dirs", ())),
        prompt_template=body,
    )


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("+++\n"):
        return "", text
    try:
        end = text.index("\n+++\n", 4)
    except ValueError:
        raise ValueError("persona frontmatter started with +++ but never closed")
    return text[4:end], text[end + 5:]


def _check_enum(field_name: str, value: str | None, allowed: set[str]) -> None:
    if value is not None and value not in allowed:
        raise ValueError(
            f"persona field {field_name}={value!r} not in {sorted(allowed)}"
        )


def _tuple_or_none(v) -> tuple[str, ...] | None:
    if v is None:
        return None
    return tuple(v)
