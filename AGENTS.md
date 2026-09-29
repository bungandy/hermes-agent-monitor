# AGENTS.md

## Project

`hermes-agent-monitor` is a read-only Python curses TUI for observing Hermes Agent runtime processes, profiles, delegations, and Kanban boards.

## Scope and safety

- Keep the monitor read-only: do not stop, signal, dispatch, or mutate Hermes processes or state.
- Keep SQLite access to `~/.hermes/state.db` read-only.
- Do not expose credentials, tokens, session contents, or other secrets in the UI, logs, commits, or documentation.
- Do not infer profile ownership when the runtime data cannot prove it; use neutral labels such as `UNASSIGNED/GLOBAL` or `UNKNOWN OWNER`.

## Implementation conventions

- Use Python standard-library modules unless a dependency is clearly necessary.
- Preserve macOS compatibility and the existing curses terminal behavior.
- Keep visible TUI labels and code comments in English.
- Bound every curses write through the existing safe rendering helpers.
- Keep slow CLI-backed discovery cached briefly; keep live process and delegation telemetry responsive.
- Preserve clean exits through `q`, `Esc`, and `Ctrl+C`.

## Verification

Run these checks from the project directory before committing changes:

```bash
python3 -m py_compile hermes_top.py
TERM=xterm-256color printf 'q' | script -q /dev/null python3 hermes_top.py
```

For changes involving data collection or layout, also run targeted runtime checks against the current Hermes roster, process list, SQLite state, and Kanban data. Confirm there is no `Traceback` or `curses.error` in PTY smoke-test output.
