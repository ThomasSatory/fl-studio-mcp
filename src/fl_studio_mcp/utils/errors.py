"""Turn controller failures into MCP tool errors.

The controller answers {"success": false, "error": "..."} (or {"error": ...}
from an older handler). Returning that as a tool result makes a failure look
like data; raising ToolError marks the MCP response as an error instead.
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError

# Envelope keys the controller adds to every answer (request id, status). They
# mean nothing to a tool's caller and are dropped from successful results.
ENVELOPE_KEYS = ("id", "success")


def unwrap(result: dict[str, Any]) -> dict[str, Any]:
    """Return `result` without its envelope keys if it is a success, else raise ToolError."""
    if not isinstance(result, dict):
        raise ToolError(f"Unexpected response from FL Studio: {result!r}")
    failed = result.get("success") is False or (
        result.get("error") and not result.get("success")
    )
    if failed:
        raise ToolError(str(result.get("error") or "FL Studio reported a failure"))
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
        raise ToolError(str(e)) from e
    return unwrap(result)
