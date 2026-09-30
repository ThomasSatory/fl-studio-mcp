"""MCP behaviour hints shared by every tool.

Clients use them to auto-approve reads and to warn before destructive calls.
A setter taking an absolute value is idempotent; a toggle (mute/solo/arm
with no explicit value, play/stop) is an ACTION.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True)
IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
