# Hermes Agent Monitor

A production-oriented, read-only curses TUI for observing the live Hermes Agent runtime. It does not stop, kill, signal, dispatch, or modify Hermes processes or state.

## Run

```bash
cd /Users/andy/hermes-agent-monitor
python3 hermes_top.py
python3 hermes_top.py --profile=coder-reviewer
```

The monitor uses only standard-library Python modules, the macOS `ps` command, the Hermes CLI on `PATH`, and read-only SQLite access.

## Views and keyboard controls

- `1`: Dashboard
- `2`: Hermes agent view
- `3`: Subagent and delegation view
- `4`: Kanban board view (all Hermes boards as read-only cards; use `↑`/`↓` to scroll board rows)
- `r`: Refresh immediately
- `s`: Cycle sorting (name/state/CPU)
- `q` or `Esc`: Quit
- `Ctrl+C`: Quit cleanly without a Python traceback

The `--profile=NAME` option limits the roster and profile-associated processes to one configured profile. Global and unassigned processes remain distinguishable in the agent view. A small terminal shows `Terminal too small (minimum 40x8).` without raising a curses exception.

## Dashboard semantics

The dashboard shows configured profiles from `hermes profile list`. If the CLI roster cannot be read, valid directories under `~/.hermes/profiles` are used as a fallback and are not automatically marked offline.

- `WORKING`: at least one delegation owned by that profile is in an executing state: `running`, `started`, `active`, or `in_progress`.
- `QUEUED`: at least one delegation is owned by that profile and is `pending`, `queued`, or `assigned`, with no executing delegation.
- `IDLE`: a live, profile-associated process exists, but no owned delegation is executing or queued.
- `AVAILABLE`: the profile is configured and has no local profile-associated process.
- `OFFLINE`: the roster explicitly reports `stopped`, `disabled`, or `offline`.
- `UNKNOWN`: directory fallback is in use and no stronger runtime state is available.

A matching process alone never produces `WORKING`. Process presence and task activity are separate signals. The dashboard shows aggregate CPU and memory progress bars plus paired CPU and memory bars for every visible profile, along with RSS, process counts, executing task counts, and queued task counts.

## Agent view

The process view is a responsive bento map of the configured Hermes bot roster plus actual Hermes runtime processes collected from `ps`. On terminals at least 90 columns wide it uses three compact columns so the full roster fits vertically; nonessential detail is omitted from these cards and remains available on the dashboard. Each configured profile has exactly one card, even when Hermes runs it behind a shared gateway whose command line cannot prove a profile-specific PID. Processes without a proven profile share a separate `UNASSIGNED/GLOBAL` card. Empty profile cards explicitly show `no mapped PID (shared gateway)` rather than inventing process ownership. Compact cards show CPU and memory heat bars with numeric values, plus explicit `PROC`, `ACTIVE`, and `QUEUED` counts; the bento heading also shows totals across all cards. Idle cards use dim gray, cards with mapped processes use plain white, and CPU/memory values above 0% use red heat bars. Detailed PID/role inventory remains on the larger layout. Shell wrappers, `bash`, `sh`, `zsh`, `osascript`, the monitor itself, kernel helpers, death supervisors, and broad `/hermes` substring matches are excluded.

Processes without a proven profile are shown as `UNASSIGNED/GLOBAL`; they are not assigned to the default `coder` profile. Dashboard aggregate and per-profile CPU/memory progress bars remain independent of the grouped process cards.

## Subagent view and ownership

The subagent view reads `async_delegations` from `~/.hermes/state.db` in SQLite read-only mode. It groups the display into `EXECUTING`, `QUEUED`, `RUNNING TASK`, and `COMPLETED` sections, with completed records always rendered at the bottom. Completed records older than two days are hidden from this view only; the SQLite database is not modified. Within sections, the newest updated records appear first. Ownership is accepted only when it can be proven from an explicit known profile in task/event JSON, a matching `owner_pid`, or a session-token match against a profile-associated process using `origin_session`, `parent_session_id`, or `origin_session_id`.

If ownership cannot be proven, the row displays `UNKNOWN OWNER / UNATTRIBUTED`. The monitor never guesses `coder` from a missing or generic session value. Invalid or missing timestamps display `unknown age`, never `0s`.

Missing databases, missing tables, locked/unavailable databases, malformed JSON, and failed roster commands degrade the display without terminating the UI. All database access uses SQLite read-only mode.

## Data sources and safety

- Profile roster: `hermes profile list`
- Profile fallback: directories under `~/.hermes/profiles`
- Process metrics: macOS `ps -axo pid=,ppid=,pcpu=,pmem=,rss=,etime=,command=`
- Delegations: read-only SQLite access to `~/.hermes/state.db`, table `async_delegations`
- Optional monitor home override: `HERMES_MONITOR_HOME=/path/to/.hermes`

The program performs no write operations against Hermes, the database, or process state.

## Refresh cost control

Process metrics and SQLite delegation data are sampled on every refresh so CPU, memory, and active-task state remain responsive. The slower CLI-backed profile roster and Kanban data use a five-second in-memory cache; this avoids repeatedly launching `hermes profile list` and one `hermes kanban ... list --json` subprocess per board on every one-second screen refresh. Press `r` to invalidate those caches and force an immediate reread. Failed CLI reads are cached for the same short interval to avoid a subprocess retry storm when a command is unavailable.

## Verification

From the project directory:

```bash
python3 -m py_compile hermes_top.py
```

For a terminal smoke test, run the program in a real PTY and send `1`, `2`, `3`, then `q`. A small-terminal PTY should show the minimum-size message. The monitor is read-only throughout these checks.
