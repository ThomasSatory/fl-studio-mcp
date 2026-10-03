"""Turn controller failures into MCP tool errors.

The controller answers {"success": false, "error": "..."} (or {"error": ...}
from an older handler). Returning that as a tool result makes a failure look
like data; raising ToolError marks the MCP response as an error instead.
"""

from __future__ import annotations

import time
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


class FLBusyError(ToolError):
    """FL refused a command because a modal window (maybe its crash window) is open."""


class FLCrashedError(FLBusyError):
    """FL Studio crashed: its crash window is open (a new crash log was written)."""


def crash_logs_dir() -> Path:
    """FL's crash logs: <FL user data>/Support/Logs/Crash (next to Settings)."""
    return get_fl_settings_base().parent / "Support" / "Logs" / "Crash"


def crash_logs_since(since: float) -> list[Path]:
    """Crash logs FL wrote at or after `since` (epoch seconds), newest first."""
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
        if path.is_file() and mtime >= since:
            found.append((mtime, path))
    return [p for _, p in sorted(found, reverse=True)]


def note_answer(action: str, result: Any) -> None:
    """Called with every answer from FL: remember when FL last carried out a command."""
    global _last_ok
    if action != "system.info" and isinstance(result, dict) and result.get("success"):
        _last_ok = time.time()


def last_ok() -> float:
    """When FL last carried out a command (or when this server started)."""
    return _last_ok if _last_ok is not None else _loaded_at


def unsafe_error(text: str, since: float | None = None) -> FLBusyError:
    """The explained error for FL's bare "Operation unsafe at current time"."""
    logs = crash_logs_since(last_ok() if since is None else since)
    if logs:
        return FLCrashedError(
            f"FL Studio crashed: its crash window is open (crash log {logs[0]}), so it "
            f"refuses every command ({text}). Close the crash window, relaunch FL Studio "
            "and reopen the project; MCP backups are in the Backup folder next to it."
        )
    return FLBusyError(
        f"FL Studio refused the command ({text}): FL is busy because a modal window is "
        "open (a dialog, a plugin window waiting for an answer, or FL's crash window). "
        "Close it in FL Studio and retry; if it is a crash window, relaunch FL Studio and "
        "reopen the project."
    )


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
