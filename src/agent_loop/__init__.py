"""agent-loop — autonomous engineering loop running Claude + Codex + Gemini.

See README.md for the design. Public entry points:

- `agent_loop.cli.main()`            console script (`agent-loop ...`)
- `agent_loop.config.Settings`       per-project configuration
- `agent_loop.orchestrator.one_tick` manual step of the state machine
- `agent_loop.orchestrator.daemon`   nightly long-running loop
"""

__version__ = "0.1.0"
