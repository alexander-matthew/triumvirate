# triumvirate

> Autonomous engineering loop — Claude + Codex + Gemini consensus PR review,
> running on the host that runs systemd. Subscription-only, no API keys.

`agent-loop` is the Python package; **triumvirate** is the name of the
overall design: three subscription-driven LLM CLIs collaborating as
producer, critics, and tiebreaker against a single git repository.

## What it does

Each night between 23:00 and 06:00 local time, a daemon walks a state
machine against your repo:

```
                  ┌──────────────┐
[approved issue]─▶│   engineer   │─▶ PR opens (Claude writes code)
                  └──────────────┘
                         │
                         ▼
                  ┌──────────────┐
                  │  reviewers   │  ← both Codex AND Gemini independently
                  │ (consensus)  │
                  └──────────────┘
                         │
              ┌──────────┴──────────┐
              │                     │
        all APPROVE              any REQUEST_CHANGES
              │                     │
              ▼                     ▼
         pre-merge:           engineer iterates
         security scan        (max 3 rounds, then arbiter
         librarian scan        — the third leg of the stool — decides)
              │
              ▼
         merge gate → auto-merge
```

Personas and their CLIs:

| Persona       | Role                                       | CLI            | Mode       |
|---------------|--------------------------------------------|----------------|------------|
| engineer      | Writes code (producer + responder)         | Claude         | write      |
| proposer      | PM, spec-driven                            | Claude         | read-only  |
| drift_watcher | Weekly architectural audit                 | Claude         | read-only  |
| reviewer-codex| One of the two consensus critics           | Codex          | read-only  |
| reviewer-gemini| One of the two consensus critics          | Gemini         | read-only  |
| arbiter-codex | Tiebreaker (when reviewer-gemini stuck)    | Codex          | read-only  |
| arbiter-gemini| Tiebreaker (when reviewer-codex stuck)     | Gemini         | read-only  |
| triage        | Auto-approves low-risk proposals           | Gemini         | read-only  |
| security      | Adversarial pre-merge pass                 | Gemini         | read-only  |
| librarian     | Cross-project consistency audit            | Gemini         | read-only  |

## Why a separate repo

Each consuming project (e.g., `personal-site`) keeps only:

- `agents/config.toml` — project-specific protected paths, sensitive paths, trusted authors, off-hours window, etc.
- `agents/personas/` — project-customized persona prompts (start from this repo's `templates/personas/` and edit voice/conventions for your project).
- `agents/state/` — per-host runtime (gitignored): `runs.sqlite`, lock files, worktrees.

The framework (lib, phase scripts, orchestrator, CLI) lives here.

## Installation

In a project that wants the loop:

```bash
uv add agent-loop --git https://github.com/alexander-matthew/triumvirate
```

Then bootstrap the project's `agents/` directory:

```bash
mkdir -p agents/personas agents/state
cp -r path/to/triumvirate/templates/personas/* agents/personas/
cp path/to/triumvirate/templates/config.toml.example agents/config.toml
```

Edit `agents/config.toml` for your project (repo name, protected paths, etc).

Customize `agents/personas/*.md` to reference your project's conventions.

Run the CLI:

```sh
uv run agent-loop status      # see pipeline state
uv run agent-loop tick        # manually step the state machine once
uv run agent-loop daemon      # the nightly process (run via systemd)
uv run agent-loop halt        # engage all three kill switches
uv run agent-loop journal     # tail recent runs.sqlite events
```

## Bot identities (recommended)

By default the loop runs under whatever `gh` login the host has on disk —
which means commits, PRs, reviews, and labels all attribute to the human
owner. Promote each CLI to its own GitHub App bot to fix that:

```sh
agent-loop bots add claude --app-id 123456 --pem-file ~/Downloads/claude-bot.pem
agent-loop bots add codex  --app-id 234567 --pem-file ~/Downloads/codex-bot.pem
agent-loop bots add gemini --app-id 345678 --pem-file ~/Downloads/gemini-bot.pem
agent-loop bots list
agent-loop bots test claude     # mint a token + probe the API
```

Full walk-through (create the three apps in the GitHub UI, choose
permissions, install on the repo, rotate keys) is in
[BOTS.md](BOTS.md). Without bots configured the loop falls back to host
`gh` auth, so this is fully opt-in.

## Guard rails

Designed to be **fully autonomous on merge**, so the guard rails matter.

- **Protected paths.** Listed per-project. Touch one → auto-rejected.
- **Diff cap.** 400 LOC by default. Configurable.
- **Round cap.** Max 3 reviewer rounds. Past that, the arbiter decides.
- **Consensus.** Both `reviewer-codex` and `reviewer-gemini` must `APPROVE` the *latest* commit before merge.
- **CI green** required.
- **Security pass** required on PRs touching sensitive paths.
- **Librarian pass** required on PRs touching cross-project concerns.
- **Three kill switches**: `systemctl stop`, `touch agents/STOP`, `agent:halt` label.

## Threat model

Designed to run safely against **public repositories**. Defenses, in order:

1. GitHub `collaborators_only` interaction limit (set per consumer repo; outside users can read but cannot comment or open issues/PRs).
2. Trusted-author filter on `##VERDICT:` posts (`lib/trust.py`).
3. PRs require `agent:authored-by-claude` label to enter the pipeline.
4. Issues require both `agent:approved` label and a trusted author to be picked up.
5. External content in prompts is wrapped with `trust.wrap_untrusted()` so injection attempts read as data, not instructions.

## Status

🚧 **Alpha** — extracted from
[`personal-site`](https://github.com/alexander-matthew/personal-site)'s
agent loop. Working but not yet stable API.

## License

MIT
