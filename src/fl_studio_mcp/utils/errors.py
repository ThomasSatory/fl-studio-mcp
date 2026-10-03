"""Turn controller failures into MCP tool errors.

The controller answers {"success": false, "error": "..."} (or {"error": ...}
from an older handler). Returning that as a tool result makes a failure look
like data; raising ToolError marks the MCP response as an error instead.
"""

from __future__ import annotations

import os
import platform
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastmcp.exceptions import ToolError

from fl_studio_mcp.utils.midi_connection import get_fl_settings_base

# Envelope keys the controller adds to every answer (request id, status). They
# mean nothing to a tool's caller and are dropped from successful results.
ENVELOPE_KEYS = ("id", "success")

# What FL's API raises while a modal window is open. FL's own crash window is
# one: after a crash FL keeps running, system.info still answers and every
# other command fails with this text.
UNSAFE_MARKER = "Operation unsafe at current time"

# When FL last carried out a command other than system.info (which still
# answers after a crash); a crash log written after it is this session's.
_loaded_at = time.time()
_last_ok: float | None = None
# When FL last carried out a command that changes something (not a read): FL
# keeps answering some reads behind its crash window, but not these.
_last_change_ok: float | None = None

# Actions that only read (see is_read_action).
READ_ACTIONS = {"system.info", "automation.describe", "automation.resolve", "project.snapshot"}

# fl_started_at is cached this long; the crash-window check for reads runs at most this often.
STARTED_CACHE_SECONDS = 5.0
CRASH_CHECK_INTERVAL = 5.0
_started_cache: tuple[float, float | None] | None = None
_crash_check: tuple[float, Path | None] | None = None
_warned_no_process = False

FL_PROCESS_NAME = "OsxFL"  # FL Studio's main process on macOS (the CLI renderer too)

# (start, end) of each command-line render (end None while it runs). The
# renderer is a second FL Studio that writes its crash logs into the same
# folder; a log written inside a window is the renderer's, not the main FL's.
_render_windows: list[tuple[float, float | None]] = []
RENDER_WINDOW_MARGIN = 5.0  # seconds after the renderer exited
_MAX_RENDER_WINDOWS = 50


@contextmanager
def render_window() -> Iterator[None]:
    """Mark the time a command-line render runs (see crash_logs_since)."""
    entry: tuple[float, float | None] = (time.time(), None)
    _render_windows.append(entry)
    try:
        yield
    finally:
        try:
            index = _render_windows.index(entry)
            _render_windows[index] = (entry[0], time.time())
        except ValueError:
            pass
        del _render_windows[:-_MAX_RENDER_WINDOWS]


def in_render_window(when: float) -> bool:
    """Whether `when` (epoch seconds) falls while a command-line render ran."""
    now = time.time()
    return any(start <= when <= (now if end is None else end + RENDER_WINDOW_MARGIN)
               for start, end in _render_windows)


class FLBusyError(ToolError):
    """FL refused a command because a modal window (maybe its crash window) is open."""


class FLCrashedError(FLBusyError):
    """FL Studio crashed: its crash window is open (a new crash log was written)."""

    log: Path | None = None


def crash_logs_dir() -> Path:
    """FL's crash logs: <FL user data>/Support/Logs/Crash (next to Settings)."""
    return get_fl_settings_base().parent / "Support" / "Logs" / "Crash"


def crash_logs_since(since: float) -> list[Path]:
    """Crash logs FL wrote at or after `since` (epoch seconds), newest first.

    Logs written while a command-line render ran are left out: they are the
    renderer's (a separate FL Studio process), not the FL the user works in.
    """
    found = []
    try:
        entries = list(crash_logs_dir().iterdir())
    except OSError:
        return []
    for path in entries:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if path.is_file() and mtime >= since and not in_render_window(mtime):
            found.append((mtime, path))
    return [p for _, p in sorted(found, reverse=True)]


def is_read_action(action: str) -> bool:
    """Whether a controller action only reads (get*/is*, system.info, ...)."""
    method = action.rsplit(".", 1)[-1]
    return action in READ_ACTIONS or method.startswith(("get", "is"))


def note_answer(action: str, result: Any) -> None:
    """Called with every answer from FL: remember when FL last carried out a command."""
    global _last_ok, _last_change_ok, _crash_check
    if action != "system.info" and isinstance(result, dict) and result.get("success"):
        _last_ok = time.time()
        if not is_read_action(action):
            _last_change_ok = _last_ok
            _crash_check = None  # FL carried out a change: no crash window


def last_ok() -> float:
    """When FL last carried out a command (or when this server started)."""
    return _last_ok if _last_ok is not None else _loaded_at


def fl_started_at() -> float | None:
    """When the oldest running FL Studio process started (macOS); None when unknown."""
    if platform.system() != "Darwin":
        return None
    try:
        pids = subprocess.run(["pgrep", "-x", FL_PROCESS_NAME], capture_output=True,
                              text=True, timeout=3).stdout.split()
        if not pids:
            _warn_no_process()
            return None
        out = subprocess.run(["ps", "-o", "lstart=", "-p", ",".join(pids)],
                             capture_output=True, text=True, timeout=3,
                             env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    starts = []
    for line in out.splitlines():
        try:
            starts.append(time.mktime(time.strptime(" ".join(line.split()),
                                                    "%a %b %d %H:%M:%S %Y")))
        except ValueError:
            continue
    return min(starts) if starts else None


def _warn_no_process() -> None:
    """One stderr line (never stdout: MCP) when pgrep finds no FL while FL answers."""
    global _warned_no_process
    if _warned_no_process:
        return
    _warned_no_process = True
    try:
        import sys

        print(f"fl-studio-mcp: pgrep finds no {FL_PROCESS_NAME} process although FL Studio "
              "answers; crash logs are dated from the last change FL carried out instead.",
              file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def fl_started_cached() -> float | None:
    """fl_started_at, cached for STARTED_CACHE_SECONDS."""
    global _started_cache
    now = time.monotonic()
    if _started_cache is None or now - _started_cache[0] >= STARTED_CACHE_SECONDS:
        _started_cache = (now, fl_started_at())
    return _started_cache[1]


def crash_since() -> float:
    """From when a crash log is the running FL's: after the last change FL carried
    out (reads keep answering behind the crash window), never before FL started
    (macOS; else before this server started)."""
    started = fl_started_cached()
    floor = started if started is not None else _loaded_at
    return floor if _last_change_ok is None else max(floor, _last_change_ok)


def fresh_crash_log() -> Path | None:
    """The newest crash log of the running FL Studio written since crash_since()."""
    logs = crash_logs_since(crash_since())
    return logs[0] if logs else None


def crash_window_log(force: bool = False) -> Path | None:
    """The running FL's fresh crash log, checked at most every CRASH_CHECK_INTERVAL.

    Only where FL's start time is known (macOS): elsewhere a log from before a
    relaunch could not be told apart.
    """
    global _crash_check
    now = time.monotonic()
    if not force and _crash_check is not None and now - _crash_check[0] < CRASH_CHECK_INTERVAL:
        return _crash_check[1]
    log = fresh_crash_log() if fl_started_cached() is not None else None
    _crash_check = (now, log)
    return log


def crash_window_message(log: Path) -> str:
    return (f"FL Studio crashed (crash log {log}); close its crash window, relaunch FL "
            "Studio and reopen the project. MCP backups are in the Backup folder next to it.")


def check_crash_window(action: str) -> None:
    """Raise FLCrashedError for a read while the running FL has a fresh crash log.

    FL keeps answering some reads behind its crash window, so their answers
    would look fine. A change FL carries out clears it (see note_answer).
    """
    if not is_read_action(action):
        return
    log = crash_window_log()
    if log is not None:
        error = FLCrashedError(crash_window_message(log))
        error.log = log
        raise error


def crashed_error(text: str, log: Path) -> FLCrashedError:
    """The error for FL's crash window, found through crash log `log`."""
    error = FLCrashedError(
        f"FL Studio crashed: its crash window is open (crash log {log}), so it "
        f"refuses every command ({text}). Close the crash window, relaunch FL Studio "
        "and reopen the project; MCP backups are in the Backup folder next to it."
    )
    error.log = log
    return error


def unsafe_error(text: str, since: float | None = None) -> FLBusyError:
    """The explained error for FL's bare "Operation unsafe at current time"."""
    if since is None:
        log = fresh_crash_log()
    else:
        logs = crash_logs_since(since)
        log = logs[0] if logs else None
    if log is not None:
        return crashed_error(text, log)
    return FLBusyError(
        f"FL Studio refused the command ({text}): FL is busy because a modal window is "
        "open (a dialog, a plugin window waiting for an answer, or FL's crash window). "
        "Close it in FL Studio and retry; if it is a crash window, relaunch FL Studio and "
        "reopen the project."
    )


def check_unsafe(result: Any) -> None:
    """Raise the crash error when FL refused a command as unsafe behind its crash window.

    Called by the connection layer on every answer, so read-only tools notice
    FL's crash window too. A refusal without a fresh crash log is left to the
    caller (a modal window; see unsafe_error).
    """
    text = str(result.get("error") or "") if isinstance(result, dict) else str(result)
    if UNSAFE_MARKER in text:
        log = fresh_crash_log()
        if log is not None:
            raise crashed_error(text, log)


def unwrap(result: dict[str, Any]) -> dict[str, Any]:
    """Return `result` without its envelope keys if it is a success, else raise ToolError."""
    if not isinstance(result, dict):
        raise ToolError(f"Unexpected response from FL Studio: {result!r}")
    failed = result.get("success") is False or (
        result.get("error") and not result.get("success")
    )
    if failed:
        text = str(result.get("error") or "FL Studio reported a failure")
        if UNSAFE_MARKER in text:
            raise unsafe_error(text)
        raise ToolError(text)
    return {key: value for key, value in result.items() if key not in ENVELOPE_KEYS}


def run(
    conn: Any,
    action: str,
    params: dict[str, Any] | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Send one command through `conn` and unwrap its response.

    Connection problems (no MIDI port, port not found) arrive as RuntimeError
    from the connection layer and become ToolError too.
    """
    try:
        result = conn.send_command(action, params, timeout)
    except RuntimeError as e:
        if UNSAFE_MARKER in str(e):
            raise unsafe_error(str(e)) from e
        raise ToolError(str(e)) from e
    return unwrap(result)
