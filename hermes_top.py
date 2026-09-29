#!/usr/bin/env python3
"""Read-only curses monitor for the Hermes Agent runtime."""
from __future__ import annotations

import argparse
import curses
import datetime as datetime_module
import json
import os
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

HERMES_HOME = Path(os.environ.get("HERMES_MONITOR_HOME", "~/.hermes")).expanduser()
STATE_DB = HERMES_HOME / "state.db"
REFRESH_SECONDS = 1.0
COMPLETED_RETENTION_SECONDS = 2 * 24 * 60 * 60
EXECUTING_STATES = {"running", "started", "active", "in_progress"}
QUEUED_STATES = {"pending", "queued", "assigned"}
KNOWN_STATES = EXECUTING_STATES | QUEUED_STATES | {"completed", "failed", "cancelled", "canceled"}
PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
DATA_CACHE_SECONDS = 5.0


@dataclass
class _TimedCache:
    value: Any = None
    stored_at: float = 0.0


_PROFILE_CACHE = _TimedCache()
_KANBAN_CACHE = _TimedCache()


def _cached(cache: _TimedCache, loader: Any) -> Any:
    """Reuse slow CLI results briefly while keeping manual refresh available."""
    now = time.monotonic()
    if cache.value is not None and now - cache.stored_at < DATA_CACHE_SECONDS:
        return cache.value
    value = loader()
    cache.value, cache.stored_at = value, time.monotonic()
    return value


def invalidate_data_caches() -> None:
    """Force the next refresh to reread CLI-backed data."""
    _PROFILE_CACHE.value = None
    _PROFILE_CACHE.stored_at = 0.0
    _KANBAN_CACHE.value = None
    _KANBAN_CACHE.stored_at = 0.0


@dataclass
class ProcessInfo:
    pid: int
    parent_pid: int
    cpu_percent: float
    memory_percent: float
    rss_kb: int
    elapsed: str
    command: str
    summary: str
    profile: str
    role: str
    session_tokens: tuple[str, ...]


@dataclass
class ProfileInfo:
    name: str
    gateway_state: str
    state: str
    cpu_percent: float
    memory_percent: float
    rss_kb: int
    process_count: int
    task_count: int
    queued_count: int


@dataclass
class ProcessGroup:
    profile: str
    processes: list[ProcessInfo]
    cpu_percent: float
    memory_percent: float
    rss_kb: int
    task_count: int = 0
    queued_count: int = 0

    @property
    def process_count(self) -> int:
        return len(self.processes)

    @property
    def roles(self) -> tuple[str, ...]:
        return tuple(sorted({process.role for process in self.processes}))


@dataclass
class DelegationInfo:
    delegation_id: str
    status: str
    age: str
    owner: str
    owner_profile: str
    task_summary: str
    updated_at: float
    executing: bool
    queued: bool


@dataclass
class KanbanTask:
    task_id: str
    title: str
    status: str
    assignee: str


@dataclass
class KanbanBoard:
    slug: str
    name: str
    current: bool
    tasks: list[KanbanTask]


def run_command(arguments: list[str], timeout: float = 3.0) -> tuple[str, int]:
    """Run a read-only command and return stdout plus its exit code."""
    try:
        completed = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)
        return completed.stdout, completed.returncode
    except (OSError, subprocess.SubprocessError):
        return "", 1


def _read_kanban_boards() -> list[KanbanBoard]:
    """Read every Hermes board and its tasks using read-only CLI commands."""
    output, return_code = run_command(["hermes", "kanban", "boards", "list"], timeout=5.0)
    if return_code != 0:
        return []
    boards: list[KanbanBoard] = []
    for line in output.splitlines():
        match = re.match(r"\s*(●\s*)?([A-Za-z0-9_.-]+)\s{2,}(.+?)\s{2,}.*", line)
        if not match or match.group(2) == "SLUG":
            continue
        slug, name = match.group(2), match.group(3).strip()
        tasks_output, tasks_code = run_command(["hermes", "kanban", "--board", slug, "list", "--json"], timeout=5.0)
        tasks: list[KanbanTask] = []
        if tasks_code == 0:
            try:
                payload = json.loads(tasks_output)
            except json.JSONDecodeError:
                payload = []
            if isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    tasks.append(KanbanTask(
                        clean_text(item.get("id", ""), 14),
                        clean_text(item.get("title", "Untitled"), 54),
                        str(item.get("status", "unknown") or "unknown").lower(),
                        clean_text(item.get("assignee") or "unassigned", 18),
                    ))
        boards.append(KanbanBoard(slug, clean_text(name, 38), bool(match.group(1)), tasks))
    return boards


def read_kanban_boards() -> list[KanbanBoard]:
    """Read boards from a short-lived cache to avoid per-frame CLI fan-out."""
    return _cached(_KANBAN_CACHE, _read_kanban_boards)


def clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def parse_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _parse_profiles() -> tuple[list[tuple[str, str]], bool]:
    """Read the roster; directory discovery remains a valid fallback."""
    output, return_code = run_command(["hermes", "profile", "list"])
    profiles: list[tuple[str, str]] = []
    for line in output.splitlines():
        stripped = line.strip().lstrip("◆").strip()
        if not stripped or stripped.startswith(("Profile ", "─")):
            continue
        match = re.match(r"(?P<name>[A-Za-z0-9_.-]+)(?:\s+\([^)]*\))?\s+\S+\s+(?P<state>\S+)", stripped)
        if match:
            profiles.append((match.group("name"), match.group("state").lower()))
    if profiles:
        return sorted(dict(profiles).items()), return_code == 0
    profile_directory = HERMES_HOME / "profiles"
    if profile_directory.is_dir():
        names = sorted(path.name for path in profile_directory.iterdir() if path.is_dir() and PROFILE_PATTERN.match(path.name))
        return [(name, "unknown") for name in names], False
    return [], False


def parse_profiles() -> tuple[list[tuple[str, str]], bool]:
    """Read the roster from a short-lived cache; refresh invalidates it."""
    return _cached(_PROFILE_CACHE, _parse_profiles)


def process_profile(command: str) -> str:
    for pattern in (r"(?:--profile|-p)\s+([A-Za-z0-9_.-]+)", r"--profile=([A-Za-z0-9_.-]+)", r"/profiles/([A-Za-z0-9_.-]+)(?:/|\s)"):
        match = re.search(pattern, command)
        if match:
            return match.group(1)
    return ""


def process_role(command: str) -> str:
    lowered = command.lower()
    if "tui_gateway.entry" in lowered or "ui-tui/dist/entry.js" in lowered:
        return "ui gateway"
    if "hermes_cli.main gateway" in lowered or "gateway run" in lowered:
        return "global gateway"
    if "hermes_cli.main" in lowered and "--cli" in lowered:
        return "delegated agent"
    return "hermes runtime"


def session_tokens(command: str) -> tuple[str, ...]:
    patterns = (r"(?:--session|--session-key|--ui-session-id|--origin-session-id)\s+([^\s]+)", r"(?:session_key|origin_session_id)=([^\s]+)")
    tokens: set[str] = set()
    for pattern in patterns:
        tokens.update(match.group(1).strip("'\"") for match in re.finditer(pattern, command))
    return tuple(tokens)


def is_hermes_process(command: str) -> bool:
    lowered = command.lower()
    basename = Path(command.split(None, 1)[0]).name.lower() if command.split() else ""
    excluded = ("hermes_top.py", "hermes_kernel", "death_supervisor", "hermes-snap", " /bin/bash", " /bin/sh", " /bin/zsh", "osascript", "grep ")
    if basename in {"bash", "sh", "zsh", "osascript", "grep", "env"} or any(item in lowered for item in excluded):
        return False
    markers = ("hermes_cli.main", "tui_gateway.entry", "ui-tui/dist/entry.js")
    return any(marker in lowered for marker in markers) and "hermes-agent-monitor" not in lowered


def process_snapshot() -> list[ProcessInfo]:
    output, _ = run_command(["ps", "-axo", "pid=,ppid=,pcpu=,pmem=,rss=,etime=,command="])
    processes: list[ProcessInfo] = []
    for line in output.splitlines():
        fields = line.strip().split(None, 6)
        if len(fields) != 7 or not is_hermes_process(fields[6]):
            continue
        try:
            pid, parent_pid, rss_kb = int(fields[0]), int(fields[1]), int(fields[4])
        except ValueError:
            continue
        command = fields[6]
        processes.append(ProcessInfo(pid, parent_pid, max(0.0, parse_number(fields[2])), max(0.0, parse_number(fields[3])), max(0, rss_kb), fields[5], command, clean_text(command, 96), process_profile(command), process_role(command), session_tokens(command)))
    return processes


def decode_json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def timestamp(value: Any) -> float | None:
    parsed = parse_number(value, -1.0)
    return parsed if parsed > 0 else None


def format_age(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown age"
    value = int(seconds)
    if value < 60:
        return f"{value}s"
    if value < 3600:
        return f"{value // 60}m {value % 60:02d}s"
    if value < 86400:
        return f"{value // 3600}h {(value % 3600) // 60:02d}m"
    return f"{value // 86400}d {(value % 86400) // 3600:02d}h"


def payload_value(payload: dict[str, Any], keys: Iterable[str], default: str = "") -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", []):
            return clean_text(value[0] if isinstance(value, list) else value, 100)
    return default


def proven_profile(payloads: Iterable[dict[str, Any]], row: sqlite3.Row, processes: list[ProcessInfo], profiles: set[str]) -> str:
    candidates: list[str] = []
    owner_pid = row["owner_pid"]
    if owner_pid not in (None, ""):
        try:
            candidates.extend(process.profile for process in processes if process.pid == int(owner_pid) and process.profile)
        except (TypeError, ValueError):
            pass
    for payload in payloads:
        for key in ("profile", "owner_profile", "origin_profile", "agent_profile"):
            value = payload.get(key)
            if isinstance(value, str) and value in profiles:
                candidates.append(value)
    origin_values = [str(row[key] or "") for key in ("origin_session", "parent_session_id", "origin_session_id")]
    for process in processes:
        if process.profile and any(token and token in process.session_tokens for token in origin_values):
            candidates.append(process.profile)
    unique = set(candidates)
    return unique.pop() if len(unique) == 1 else ""


def read_delegations(processes: list[ProcessInfo] | None = None, profiles: set[str] | None = None, limit: int = 200) -> tuple[list[DelegationInfo], str]:
    processes = processes or []
    profiles = profiles or set()
    if not STATE_DB.exists():
        return [], "missing"
    try:
        with sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=0.2) as connection:
            connection.row_factory = sqlite3.Row
            table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='async_delegations'").fetchone()
            if not table:
                return [], "table missing"
            rows = connection.execute("SELECT delegation_id, origin_session, parent_session_id, origin_session_id, owner_pid, state, dispatched_at, updated_at, event_json, task_json FROM async_delegations ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
    except (sqlite3.Error, OSError):
        return [], "unavailable"
    delegations: list[DelegationInfo] = []
    for row in rows:
        event, task = decode_json(row["event_json"]), decode_json(row["task_json"])
        payloads = (task, event)
        payload = {**task, **event}
        raw_state = str(row["state"] or "").lower()
        status = clean_text(raw_state, 14).upper() or "UNKNOWN"
        dispatched = timestamp(row["dispatched_at"])
        updated = timestamp(row["updated_at"])
        owner_profile = proven_profile(payloads, row, processes, profiles)
        owner = owner_profile or "UNKNOWN OWNER"
        summary = payload_value(payload, ("goal", "prompt", "task", "description")) or clean_text(row["delegation_id"], 100)
        delegations.append(DelegationInfo(clean_text(row["delegation_id"], 24), status, format_age(None if dispatched is None else time.time() - dispatched), owner, owner_profile, summary, updated or 0.0, raw_state in EXECUTING_STATES, raw_state in QUEUED_STATES))
    delegations.sort(key=lambda item: (not item.executing, not item.queued, -item.updated_at))
    return delegations, "ok"


def build_profiles(roster: list[tuple[str, str]], processes: list[ProcessInfo], delegations: list[DelegationInfo], roster_ok: bool) -> list[ProfileInfo]:
    by_profile: dict[str, list[ProcessInfo]] = {}
    for process in processes:
        if process.profile:
            by_profile.setdefault(process.profile, []).append(process)
    executing: dict[str, int] = {}
    queued: dict[str, int] = {}
    for delegation in delegations:
        if delegation.owner_profile:
            target = executing if delegation.executing else queued if delegation.queued else None
            if target is not None:
                target[delegation.owner_profile] = target.get(delegation.owner_profile, 0) + 1
    result: list[ProfileInfo] = []
    for name, gateway_state in roster:
        matches = by_profile.get(name, [])
        task_count, queued_count = executing.get(name, 0), queued.get(name, 0)
        normalized = gateway_state.lower()
        if task_count:
            state = "WORKING"
        elif queued_count:
            state = "QUEUED"
        elif matches:
            state = "IDLE"
        elif normalized in {"stopped", "disabled", "offline"}:
            state = "OFFLINE"
        elif normalized == "unknown" and not roster_ok:
            state = "UNKNOWN"
        else:
            state = "AVAILABLE"
        result.append(ProfileInfo(name, normalized, state, sum(p.cpu_percent for p in matches), sum(p.memory_percent for p in matches), sum(p.rss_kb for p in matches), len(matches), task_count, queued_count))
    return result


def group_processes(
    processes: list[ProcessInfo],
    delegations: list[DelegationInfo],
    profile_names: Iterable[str] = (),
) -> list[ProcessGroup]:
    """Aggregate processes and keep a card for every configured bot profile.

    Hermes commonly runs profiles behind shared gateway processes whose command
    lines do not contain a profile name.  Empty configured-profile groups are
    therefore intentional: they make the bot roster visible without guessing
    ownership of a shared PID.
    """
    grouped: dict[str, list[ProcessInfo]] = {}
    for process in processes:
        grouped.setdefault(process.profile or "UNASSIGNED/GLOBAL", []).append(process)
    for profile in profile_names:
        grouped.setdefault(profile, [])
    active: dict[str, int] = {}
    queued: dict[str, int] = {}
    for delegation in delegations:
        if delegation.owner_profile:
            target = active if delegation.executing else queued if delegation.queued else None
            if target is not None:
                target[delegation.owner_profile] = target.get(delegation.owner_profile, 0) + 1
    groups: list[ProcessGroup] = []
    for profile, members in grouped.items():
        groups.append(ProcessGroup(
            profile=profile,
            processes=members,
            cpu_percent=sum(process.cpu_percent for process in members),
            memory_percent=sum(process.memory_percent for process in members),
            rss_kb=sum(process.rss_kb for process in members),
            task_count=active.get(profile, 0),
            queued_count=queued.get(profile, 0),
        ))
    return groups


def format_rss(rss_kb: int) -> str:
    if rss_kb < 1024:
        return f"{rss_kb}K"
    if rss_kb < 1024 * 1024:
        return f"{rss_kb / 1024:.1f}M"
    return f"{rss_kb / (1024 * 1024):.2f}G"


def safe_add(window: Any, y: int, x: int, text: str, attributes: int = 0) -> None:
    height, width = window.getmaxyx()
    if y < 0 or y >= height or x < 0 or x >= width - 1:
        return
    try:
        window.addnstr(y, x, str(text), max(0, width - x - 1), attributes)
    except curses.error:
        pass


def draw_bar(window: Any, y: int, x: int, width: int, value: float, attributes: int = 0) -> None:
    if width < 5:
        return
    inner = width - 2
    filled = min(inner, max(0, int(inner * min(100.0, value) / 100.0)))
    color = curses.color_pair(4) if value > 0 else curses.A_DIM
    safe_add(window, y, x, "[", attributes)
    safe_add(window, y, x + 1, "█" * filled, attributes | color)
    safe_add(window, y, x + 1 + filled, "░" * (inner - filled), attributes | curses.A_DIM)
    safe_add(window, y, x + width - 1, "]", attributes)


def group_usage_score(group: ProcessGroup) -> float:
    """Return a bounded score for aggregate card emphasis."""
    return min(100.0, max(0.0, group.cpu_percent + group.memory_percent + group.process_count * 2.0))


def process_group_card_size(group: ProcessGroup, available_width: int) -> tuple[int, int]:
    """Scale a profile card using aggregate usage and process count."""
    score = group_usage_score(group)
    card_width = min(available_width, 42 + int(score / 10.0) + min(12, group.process_count * 2))
    card_height = 8 + (1 if score >= 25 else 0) + (1 if score >= 60 else 0)
    return max(available_width, card_width) if available_width < 44 else card_width, card_height


def usage_score(process: ProcessInfo) -> float:
    """Return a bounded score used for individual process metrics."""
    return min(100.0, max(0.0, process.cpu_percent + process.memory_percent))


def process_card_size(process: ProcessInfo, available_width: int) -> tuple[int, int]:
    """Scale a card without allowing one busy process to crowd out the view."""
    score = usage_score(process)
    card_width = min(available_width, 34 + int(score / 12.0))
    card_height = 6 + (1 if score >= 25 else 0) + (1 if score >= 60 else 0)
    return max(available_width, card_width) if available_width < 36 else card_width, card_height


def status_color(state: str) -> int:
    return {"WORKING": 2, "IDLE": 3, "AVAILABLE": 5, "QUEUED": 5, "OFFLINE": 4}.get(state, 1)


def kanban_counts(boards: list[KanbanBoard]) -> dict[str, int]:
    """Summarize task states across every visible Kanban board."""
    counts = {state: 0 for state in ("TODO", "IN PROGRESS", "BLOCKED", "IN REVIEW", "QA", "DONE")}
    for board in boards:
        for task in board.tasks:
            state = kanban_status(task.status)
            counts[state] = counts.get(state, 0) + 1
    return counts


def draw_header(window: Any, view_name: str, profile_count: int, process_count: int, active_count: int, queued_count: int, db_state: str) -> None:
    _, width = window.getmaxyx()
    now = datetime_module.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = f" HERMES MONITOR | {view_name} | {now} | profiles {profile_count} | processes {process_count} | working {active_count} | queued {queued_count} | DB {db_state}"
    safe_add(window, 0, 0, title.ljust(max(0, width - 1)), curses.color_pair(1) | curses.A_BOLD)
    safe_add(window, 1, 0, " Read-only telemetry. Process presence is not task activity.", curses.A_DIM)


def draw_dashboard(window: Any, profiles: list[ProfileInfo], processes: list[ProcessInfo], delegations: list[DelegationInfo], db_state: str, sort_mode: int) -> None:
    height, width = window.getmaxyx()
    draw_header(window, "DASHBOARD", len(profiles), len(processes), sum(d.executing for d in delegations), sum(d.queued for d in delegations), db_state)
    counts = {state: sum(p.state == state for p in profiles) for state in ("WORKING", "QUEUED", "IDLE", "AVAILABLE", "OFFLINE", "UNKNOWN")}
    boards = read_kanban_boards()
    task_counts = kanban_counts(boards)
    total_tasks = sum(task_counts.values())
    active_delegations = sum(delegation.executing for delegation in delegations)
    queued_delegations = sum(delegation.queued for delegation in delegations)
    completed_delegations = sum(delegation.status in {"COMPLETED", "DONE"} for delegation in delegations)
    totals = (sum(p.cpu_percent for p in processes), sum(p.memory_percent for p in processes), sum(p.rss_kb for p in processes))

    if width < 90:
        safe_add(window, 3, 1, "AGENTS", curses.A_BOLD)
        safe_add(window, 4, 1, f"{len(profiles)} total  WORK {counts['WORKING']}  QUEUED {counts['QUEUED']}  IDLE {counts['IDLE']}  OFFLINE {counts['OFFLINE']}")
        safe_add(window, 6, 1, "RUNTIME", curses.A_BOLD)
        safe_add(window, 7, 1, f"{len(processes)} processes  CPU {totals[0]:.1f}%  MEM {totals[1]:.1f}%  RSS {format_rss(totals[2])}")
        safe_add(window, 9, 1, "SUBAGENTS", curses.A_BOLD)
        safe_add(window, 10, 1, f"{len(delegations)} tracked  {active_delegations} executing  {queued_delegations} queued  {completed_delegations} done")
        safe_add(window, 12, 1, "KANBAN", curses.A_BOLD)
        safe_add(window, 13, 1, f"{len(boards)} boards  {total_tasks} tasks  TODO {task_counts['TODO']}  ACTIVE {task_counts['IN PROGRESS']}  BLOCKED {task_counts['BLOCKED']}  DONE {task_counts['DONE']}")
        safe_add(window, 15, 1, f"AGENTS (sort: {['name', 'state', 'cpu'][sort_mode]})", curses.A_BOLD)
        for index, profile in enumerate(sorted(profiles, key=lambda p: p.name.lower())[: max(0, height - 18)]):
            y = 16 + index
            attributes = curses.color_pair(status_color(profile.state)) if profile.state != "IDLE" else 0
            safe_add(window, y, 1, f"{profile.name[:16]:<16} {profile.state:<9}", attributes | curses.A_BOLD)
            safe_add(window, y, 30, f"CPU {profile.cpu_percent:4.1f}% MEM {profile.memory_percent:4.1f}% {format_rss(profile.rss_kb):>7}", attributes)
        return

    panel_width = max(20, (width - 4) // 3)
    panel_x = (1, 2 + panel_width, 3 + panel_width * 2)
    safe_add(window, 3, panel_x[0], "AGENTS", curses.A_BOLD)
    safe_add(window, 4, panel_x[0], f"{len(profiles)} total  WORK {counts['WORKING']}  QUEUED {counts['QUEUED']}")
    safe_add(window, 5, panel_x[0], f"IDLE {counts['IDLE']}  AVAILABLE {counts['AVAILABLE']}  OFFLINE {counts['OFFLINE']}")
    safe_add(window, 3, panel_x[1], "RUNTIME", curses.A_BOLD)
    safe_add(window, 4, panel_x[1], f"{len(processes)} processes  RSS {format_rss(totals[2])}")
    safe_add(window, 5, panel_x[1], f"CPU {totals[0]:.1f}%  MEM {totals[1]:.1f}%")
    safe_add(window, 3, panel_x[2], "SUBAGENTS", curses.A_BOLD)
    safe_add(window, 4, panel_x[2], f"{len(delegations)} tracked  {active_delegations} executing")
    safe_add(window, 5, panel_x[2], f"{queued_delegations} queued  {completed_delegations} done")
    safe_add(window, 7, 1, "KANBAN SUMMARY", curses.A_BOLD)
    safe_add(window, 8, 1, f"{len(boards)} boards  {total_tasks} tasks", curses.A_BOLD)
    safe_add(window, 9, 1, f"TODO {task_counts['TODO']}   ACTIVE {task_counts['IN PROGRESS']}   BLOCKED {task_counts['BLOCKED']}   REVIEW {task_counts['IN REVIEW']}   QA {task_counts['QA']}   DONE {task_counts['DONE']}")
    safe_add(window, 11, 1, f"AGENT HEALTH (sort: {['name', 'state', 'cpu'][sort_mode]})", curses.A_BOLD)
    safe_add(window, 12, 1, "PROFILE                 STATE       CPU BAR                 MEM BAR                 RSS     PROC TASKS QUEUED", curses.A_DIM)
    ordered = sorted(profiles, key=lambda p: p.name.lower()) if sort_mode == 0 else sorted(profiles, key=lambda p: (p.state != "WORKING", p.name.lower())) if sort_mode == 1 else sorted(profiles, key=lambda p: p.cpu_percent, reverse=True)
    for index, profile in enumerate(ordered[: max(0, height - 15)]):
        y = 13 + index
        safe_add(window, y, 1, f"{profile.name[:22]:<22} {profile.state:<10}", curses.color_pair(status_color(profile.state)) | curses.A_BOLD)
        draw_bar(window, y, 36, 22, profile.cpu_percent)
        draw_bar(window, y, 60, 22, profile.memory_percent)
        safe_add(window, y, 84, f"{format_rss(profile.rss_kb):>7} {profile.process_count:>4} {profile.task_count:>5} {profile.queued_count:>6}")


def draw_processes(window: Any, processes: list[ProcessInfo], profiles: list[ProfileInfo], delegations: list[DelegationInfo], db_state: str, sort_mode: int) -> None:
    height, width = window.getmaxyx()
    draw_header(window, "AGENTS", len(profiles), len(processes), sum(d.executing for d in delegations), sum(d.queued for d in delegations), db_state)
    groups = group_processes(processes, delegations, (profile.name for profile in profiles))
    ordered = sorted(groups, key=lambda group: group.cpu_percent, reverse=True) if sort_mode == 2 else sorted(groups, key=lambda group: group.profile.lower())
    if not groups:
        safe_add(window, 5, 2, "No Hermes processes detected.", curses.A_DIM)
        return

    total_processes = sum(group.process_count for group in ordered)
    total_active = sum(group.task_count for group in ordered)
    total_queued = sum(group.queued_count for group in ordered)
    safe_add(
        window,
        3,
        1,
        f"BENTO PROFILE MAP  |  TOTAL PROCESSES {total_processes}  ACTIVE TASKS {total_active}  QUEUED TASKS {total_queued}",
        curses.A_BOLD,
    )
    if width >= 90:
        # Keep the roster dense: three compact cards per row makes the full
        # bot roster visible on normal terminal sizes.  Detailed telemetry is
        # intentionally left to the dashboard; this view is for at-a-glance
        # process/profile presence.
        columns = 3
        gap = 1
        card_width = max(28, (width - 2 - gap * (columns - 1)) // columns)
        card_height = 6
        row_gap = 0
        for index, group in enumerate(ordered):
            row, column = divmod(index, columns)
            x = 1 + column * (card_width + gap)
            y = 5 + row * (card_height + row_gap)
            if y + card_height >= height - 1:
                break
            draw_process_group_card(window, y, x, card_width, group, compact=True)
        return

    if width < 64:
        y = 5
        for group in ordered:
            if y >= height - 1:
                break
            _, card_height = process_group_card_size(group, max(44, width - 3))
            if y + card_height >= height - 1:
                break
            draw_process_group_card(window, y, 1, width - 2, group)
            y += card_height
        return

    x, y, row_height = 1, 5, 0
    for group in ordered:
        card_width, card_height = process_group_card_size(group, width - 2)
        if x > 1 and x + card_width > width - 1:
            x, y, row_height = 1, y + row_height, 0
        if y + card_height >= height - 1:
            break
        draw_process_group_card(window, y, x, card_width, group)
        x += card_width + 1
        row_height = max(row_height, card_height)


def draw_process_group_card(window: Any, y: int, x: int, card_width: int, group: ProcessGroup, compact: bool = False) -> None:
    """Draw one aggregate profile card and its compact process inventory."""
    score = group_usage_score(group)
    # No per-agent palette: idle cards are dim gray, cards with a mapped
    # process are plain white.  CPU/memory heat bars are the only alert color.
    border_attributes = curses.color_pair(1)
    if group.process_count == 0:
        border_attributes |= curses.A_DIM
    if score >= 25:
        border_attributes |= curses.A_BOLD
    card_height = 6 if compact else 8 + (1 if score >= 25 else 0) + (1 if score >= 60 else 0)
    right, bottom = x + card_width - 1, y + card_height - 1
    safe_add(window, y, x, "┌" + "─" * max(0, card_width - 2) + "┐", border_attributes)
    for line in range(1, card_height - 1):
        safe_add(window, y + line, x, "│", border_attributes)
        safe_add(window, y + line, right, "│", border_attributes)
    safe_add(window, bottom, x, "└" + "─" * max(0, card_width - 2) + "┘", border_attributes)
    profile = clean_text(group.profile, max(16, card_width - 4))
    safe_add(window, y + 1, x + 2, profile, border_attributes | curses.A_BOLD)
    roles = ", ".join(group.roles) or "configured profile"
    process_list = ", ".join(f"{process.role}/{process.pid}" for process in sorted(group.processes, key=lambda item: item.pid))
    if not process_list:
        process_list = "no mapped PID (shared gateway)"
    if compact:
        bar_width = max(8, card_width - 17)
        safe_add(window, y + 2, x + 2, "CPU")
        draw_bar(window, y + 2, x + 6, bar_width, group.cpu_percent)
        safe_add(window, y + 2, x + card_width - 9, f"{group.cpu_percent:4.1f}%")
        safe_add(window, y + 3, x + 2, "MEM")
        draw_bar(window, y + 3, x + 6, bar_width, group.memory_percent)
        safe_add(window, y + 3, x + card_width - 9, f"{group.memory_percent:4.1f}%")
        safe_add(window, y + 4, x + 2, f"PROC {group.process_count}  ACTIVE {group.task_count}  QUEUED {group.queued_count}")
        return

    safe_add(window, y + 2, x + 2, f"ROLES {clean_text(roles, max(10, card_width - 9))}")
    safe_add(window, y + 3, x + 2, f"CPU {group.cpu_percent:5.1f}% MEM {group.memory_percent:5.1f}% RSS {format_rss(group.rss_kb)}")
    safe_add(window, y + 4, x + 2, f"PROCESSES {group.process_count:>2}  ACTIVE {group.task_count:>2}  QUEUED {group.queued_count:>2}")
    safe_add(window, y + 5, x + 2, clean_text(process_list, max(10, card_width - 4)), curses.A_DIM)
    draw_bar(window, y + 6, x + 2, max(5, card_width - 4), group.cpu_percent)
    if card_height >= 9:
        draw_bar(window, y + 7, x + 2, max(5, card_width - 4), group.memory_percent)


def draw_subagents(window: Any, delegations: list[DelegationInfo], profiles: list[ProfileInfo], processes: list[ProcessInfo], db_state: str, sort_mode: int) -> None:
    height, width = window.getmaxyx()
    draw_header(window, "SUBAGENTS", len(profiles), len(processes), sum(d.executing for d in delegations), sum(d.queued for d in delegations), db_state)

    now = time.time()
    visible_delegations = [
        delegation
        for delegation in delegations
        if not (
            delegation.status in {"COMPLETED", "DONE"}
            and delegation.updated_at > 0
            and now - delegation.updated_at > COMPLETED_RETENTION_SECONDS
        )
    ]

    def section_key(delegation: DelegationInfo) -> tuple[int, float]:
        if delegation.executing:
            bucket = 0
        elif delegation.queued:
            bucket = 1
        elif delegation.status in {"COMPLETED", "DONE"}:
            bucket = 3
        else:
            bucket = 2
        return bucket, -delegation.updated_at

    ordered = sorted(visible_delegations, key=section_key)
    section_names = {
        0: "EXECUTING",
        1: "QUEUED",
        2: "RUNNING TASK",
        3: "COMPLETED",
    }
    current_bucket: int | None = None
    y = 2
    for delegation in ordered:
        bucket = section_key(delegation)[0]
        if bucket != current_bucket:
            # Give each section breathing room.  Completed work gets an
            # additional blank line so the historical boundary is obvious.
            spacing = 2 if bucket == 3 else 1
            y += spacing
            if y >= height - 1:
                break
            safe_add(window, y, 1, f"── {section_names[bucket]} ──", curses.A_BOLD | curses.A_UNDERLINE)
            y += 1
            current_bucket = bucket
        if y >= height - 1:
            break
        if width < 100:
            safe_add(window, y, 1, f"{delegation.status:<11} {delegation.age:<11} {clean_text(delegation.task_summary, max(10, width - 28))}", curses.color_pair(2) if delegation.executing else 0)
        else:
            safe_add(window, y, 1, f"{delegation.status:<12} {delegation.age:>11} {delegation.delegation_id:<24} {delegation.task_summary}", curses.color_pair(2) if delegation.executing else 0)
        y += 1
    if not visible_delegations:
        safe_add(window, 5, 2, "No delegations recorded." if db_state == "ok" else f"Delegation data unavailable ({db_state}).", curses.A_DIM)


def kanban_status(status: str) -> str:
    return {
        "running": "IN PROGRESS",
        "in_progress": "IN PROGRESS",
        "ready": "TODO",
        "todo": "TODO",
        "triage": "BACKLOG",
        "blocked": "BLOCKED",
        "review": "IN REVIEW",
        "qa": "QA",
        "done": "DONE",
        "completed": "DONE",
        "cancelled": "CANCELLED",
        "canceled": "CANCELLED",
    }.get(status.lower(), status.upper()[:12] or "UNKNOWN")


def draw_kanban_card(window: Any, y: int, x: int, card_width: int, board: KanbanBoard, card_height: int) -> None:
    """Draw a compact board card, similar to the process-view bento cards."""
    right, bottom = x + card_width - 1, y + card_height - 1
    attributes = curses.color_pair(2) if board.current else curses.color_pair(1)
    if not board.tasks:
        attributes |= curses.A_DIM
    safe_add(window, y, x, "┌" + "─" * max(0, card_width - 2) + "┐", attributes)
    for line in range(1, card_height - 1):
        safe_add(window, y + line, x, "│", attributes)
        safe_add(window, y + line, right, "│", attributes)
    safe_add(window, bottom, x, "└" + "─" * max(0, card_width - 2) + "┘", attributes)
    title = f"BOARD: {board.name}{'  ●' if board.current else ''}"
    safe_add(window, y + 1, x + 2, clean_text(title, max(10, card_width - 4)), attributes | curses.A_BOLD)

    counts: dict[str, int] = {}
    for task in board.tasks:
        state = kanban_status(task.status)
        counts[state] = counts.get(state, 0) + 1
    count_text = " ".join(f"{state}:{counts[state]}" for state in ("TODO", "IN PROGRESS", "BLOCKED", "IN REVIEW", "QA", "DONE") if counts.get(state))
    safe_add(window, y + 2, x + 2, clean_text(f"{len(board.tasks)} tasks  {count_text}", max(10, card_width - 4)), curses.A_DIM)

    ordered = sorted(board.tasks, key=lambda task: (kanban_status(task.status) == "DONE", kanban_status(task.status), task.title.lower()))
    visible = ordered[:5]
    for index, task in enumerate(visible):
        state = kanban_status(task.status)
        task_text = f"{index + 1:>2}. {state[:5]:<5} {task.title}"
        task_attributes = curses.color_pair(4) if state == "BLOCKED" else curses.A_DIM if state == "DONE" else 0
        safe_add(window, y + 3 + index, x + 2, clean_text(task_text, max(10, card_width - 4)), task_attributes)
    if len(ordered) > len(visible):
        safe_add(window, y + 3 + len(visible), x + 2, f"+{len(ordered) - len(visible)} more", curses.A_DIM)



def draw_board(window: Any, profiles: list[ProfileInfo], processes: list[ProcessInfo], delegations: list[DelegationInfo], db_state: str, scroll_row: int = 0) -> None:
    height, width = window.getmaxyx()
    boards = read_kanban_boards()
    draw_header(window, "KANBAN", len(profiles), len(processes), sum(d.executing for d in delegations), sum(d.queued for d in delegations), db_state)
    if not boards:
        safe_add(window, 4, 2, "Kanban data unavailable (hermes kanban CLI returned no boards).", curses.A_DIM)
        return
    task_total = sum(len(board.tasks) for board in boards)
    safe_add(window, 3, 1, f"KANBAN BOARD MAP  |  BOARDS {len(boards)}  TASKS {task_total}  |  read-only", curses.A_BOLD)
    safe_add(window, 4, 1, "↑/↓ scroll board rows  •  BLOCKED red  •  DONE gray", curses.A_DIM)

    columns = 2 if width >= 80 else 1
    gap = 1
    card_width = max(34, (width - 2 - gap * (columns - 1)) // columns)
    board_rows: list[list[KanbanBoard]] = []
    for index in range(0, len(boards), columns):
        board_rows.append(boards[index:index + columns])
    row_heights = [max(5, max(4 + min(5, len(board.tasks)) + (1 if len(board.tasks) > 5 else 0) for board in row)) for row in board_rows]
    y = 5
    for row_index, row_boards in enumerate(board_rows):
        row_height = row_heights[row_index]
        if row_index < scroll_row:
            continue
        if y + row_height > height - 1:
            break
        for column, board in enumerate(row_boards):
            x = 1 + column * (card_width + gap)
            draw_kanban_card(window, y, x, card_width, board, row_height)
        y += row_height
    if len(board_rows) > 1:
        safe_add(window, max(5, height - 2), 2, f"Board rows {scroll_row + 1}-{min(len(board_rows), scroll_row + 1 + 3)} / {len(board_rows)}  (↑/↓ scroll)", curses.A_DIM)


def draw(window: Any, view: int, sort_mode: int, profile_filter: str = "", board_scroll: int = 0) -> None:
    window.erase()
    height, width = window.getmaxyx()
    if height < 8 or width < 40:
        safe_add(window, 0, 0, "Terminal too small (minimum 40x8).", curses.A_BOLD)
        safe_add(window, 2, 0, "Keys: 1 dashboard 2 agents 3 subagents 4 kanban r refresh s sort q quit")
        window.refresh()
        return
    roster, roster_ok = parse_profiles()
    if profile_filter:
        roster = [(name, state) for name, state in roster if name == profile_filter]
    processes = process_snapshot()
    if profile_filter:
        processes = [p for p in processes if p.profile in {profile_filter, ""}]
    profiles = {name for name, _ in roster}
    delegations, db_state = read_delegations(processes, profiles)
    profiles_data = build_profiles(roster, processes, delegations, roster_ok)
    if view == 1:
        draw_processes(window, processes, profiles_data, delegations, db_state, sort_mode)
    elif view == 2:
        draw_subagents(window, delegations, profiles_data, processes, db_state, sort_mode)
    elif view == 3:
        draw_board(window, profiles_data, processes, delegations, db_state, board_scroll)
    else:
        draw_dashboard(window, profiles_data, processes, delegations, db_state, sort_mode)
    safe_add(window, height - 1, 0, " 1 dashboard  2 agents  3 subagents  4 kanban  ↑/↓ board rows  r refresh  s sort  q quit ", curses.color_pair(1) | curses.A_BOLD)
    window.refresh()


def main(window: Any, profile_filter: str = "") -> None:
    curses.curs_set(0)
    curses.use_default_colors()
    color_pairs = {
        1: curses.COLOR_WHITE,
        2: curses.COLOR_GREEN,
        3: curses.COLOR_YELLOW,
        4: curses.COLOR_RED,
        5: curses.COLOR_CYAN,
    }
    for number, foreground in color_pairs.items():
        curses.init_pair(number, foreground, -1)
    window.keypad(True)
    window.nodelay(True)
    view, sort_mode, board_scroll, next_refresh = 0, 0, 0, 0.0
    while True:
        now = time.monotonic()
        if now >= next_refresh:
            draw(window, view, sort_mode, profile_filter, board_scroll)
            next_refresh = now + REFRESH_SECONDS
        key = window.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if key in (ord("1"), ord("2"), ord("3"), ord("4")):
            view, board_scroll, next_refresh = int(chr(key)) - 1, 0, 0.0
        elif view == 3 and key in (curses.KEY_UP, curses.KEY_PPAGE):
            board_scroll, next_refresh = max(0, board_scroll - 1), 0.0
        elif view == 3 and key in (curses.KEY_DOWN, curses.KEY_NPAGE):
            board_scroll, next_refresh = board_scroll + 1, 0.0
        elif key in (ord("r"), ord("R")):
            invalidate_data_caches()
            next_refresh = 0.0
        elif key in (ord("s"), ord("S")):
            sort_mode, next_refresh = (sort_mode + 1) % 3, 0.0
        time.sleep(0.05)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Hermes Agent runtime monitor")
    parser.add_argument("--profile", "-p", dest="profile", default="", help="Show one configured profile")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    try:
        curses.wrapper(lambda window: main(window, arguments.profile))
    except KeyboardInterrupt:
        # Ctrl+C is a normal way to leave the monitor; curses.wrapper()
        # already restores the terminal before this handler runs.
        pass
