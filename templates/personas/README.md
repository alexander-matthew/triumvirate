# Persona starter templates

These persona files are **starting points** for new projects. Copy them
into your project's `agents/personas/` directory and customize the voice,
project-specific conventions, and reading lists for your project.

```bash
mkdir -p agents/personas
cp /path/to/triumvirate/templates/personas/*.md agents/personas/
```

These templates were extracted from
[`personal-site`](https://github.com/alexander-matthew/personal-site)'s
agent loop, so a few project-specific references remain (e.g. the engineer
persona mentions `palantir_page.html`). Edit those out and replace with
your own project's conventions before turning the loop on.

## What each persona is for

| File                     | Role           | CLI    | Triggered when                                      |
|--------------------------|----------------|--------|-----------------------------------------------------|
| `engineer.md`            | Writes code    | Claude | An `agent:approved` issue exists; or a review asks for changes |
| `proposer.md`            | PM, spec-driven| Claude | Once per night (off-hours)                          |
| `drift_watcher.md`       | Weekly audit   | Claude | Sundays only                                        |
| `reviewer-codex.md`      | Critic         | Codex  | PR has unreviewed commits (one of two consensus reviewers) |
| `reviewer-gemini.md`     | Critic         | Gemini | Same as above, alternates with codex                |
| `arbiter-codex.md`       | Tiebreaker     | Codex  | reviewer-gemini stuck after max rounds              |
| `arbiter-gemini.md`      | Tiebreaker     | Gemini | reviewer-codex stuck after max rounds               |
| `triage.md`              | Auto-approve   | Gemini | After proposer files specs; gates by label/effort   |
| `security.md`            | AppSec pass    | Gemini | PR touches `sensitive_path_prefixes`                |
| `librarian-gemini.md`    | Cross-project audit | Gemini | PR touches `librarian_trigger_paths`           |

## What to customize per project

For each persona file, look for:

- **Template references** (e.g., `palantir_page.html`) — replace with your
  project's template names.
- **Service paths** (e.g., `app/services/oauth.py`) — replace with your
  project's auth/service paths.
- **Project-specific examples** in the prompt body (e.g., "Spotify dashboard"
  mentions in `proposer.md`) — replace with examples relevant to your project.
- **Voice / role** — adjust the persona's voice to match your project's
  tone if you want. The starter values are reasonable but not sacred.

What you should NOT change:

- **Output format markers** (`##VERDICT:`, `##ARBITER_VERDICT:`,
  `##DECISIONS`, etc.) — the framework's parsers depend on these literal strings.
- **Frontmatter field names** — the persona loader uses them.
- **The hard rules / guard rails** that map to wrapper-enforced behavior
  (protected paths, diff cap, round cap). Customize values via
  `agents/config.toml`, not by rewriting the persona prompt.
