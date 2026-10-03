"""Project overview tool: everything an assistant needs before acting, in one call."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fl_studio_mcp.tools.annotations import READ_ONLY
from fl_studio_mcp.tools.patterns import with_lengths
from fl_studio_mcp.utils.errors import run

if TYPE_CHECKING:
    from fastmcp import FastMCP

# The controller walks every pattern, channel and mixer track: allow more than
# the 2 s used for single commands.
SNAPSHOT_TIMEOUT = 5.0

SECTIONS = ("transport", "tempo", "patterns", "channels", "mixer_tracks")


def register_project_tools(mcp: FastMCP) -> None:
    """Register project-level tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_project_snapshot() -> dict:
        """Get a one-call overview of the open project.

        Returns transport state, tempo, the current pattern and the list of
        used patterns, all channels (name, volume 0-1, pan -1..1, mute, solo,
        selection, mixer target) and the mixer tracks in use (name, volume,
        pan, mute, solo). A section FL Studio
        could not read comes back as {"error": ...} while the others stay
        valid. The piano roll is not included: use fl_get_pr_context.
        """
        result = run(get_connection(), "project.snapshot", timeout=SNAPSHOT_TIMEOUT)
        patterns = result.get("patterns")
        if isinstance(patterns, dict):
            for item in patterns.get("items") or []:
                with_lengths(item)
        return {key: result[key] for key in SECTIONS if key in result}
