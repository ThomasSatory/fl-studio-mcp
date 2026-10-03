"""Playlist tracks and markers through FL Studio's live API.

Clips themselves are handled by the arrangement tools (fl_get_arrangement,
fl_edit_arrangement) because the API cannot reach them.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.annotations import ACTION, IDEMPOTENT, READ_ONLY
from fl_studio_mcp.utils import project_files
from fl_studio_mcp.utils.errors import run

if TYPE_CHECKING:
    from fastmcp import FastMCP


def parse_color(color: str | list[int] | None) -> list[int] | None:
    """[r, g, b] from "#RRGGBB", "RRGGBB" or a 3-item list; None stays None."""
    if color is None:
        return None
    if isinstance(color, str):
        match = re.fullmatch(r"#?([0-9a-fA-F]{6})", color.strip())
        if not match:
            raise ToolError(f"color must be '#RRGGBB' or [r, g, b], got {color!r}")
        value = int(match.group(1), 16)
        return [value >> 16, (value >> 8) & 0xFF, value & 0xFF]
    if (
        isinstance(color, list)
        and len(color) == 3
        and all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in color)
    ):
        return list(color)
    raise ToolError(f"color must be '#RRGGBB' or [r, g, b] with 0-255 values, got {color!r}")


def register_playlist_tools(mcp: FastMCP) -> None:
    """Register live playlist tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_playlist_tracks(include_empty: bool = False, limit: int = 100) -> dict:
        """List playlist tracks (1-based): name, colour, mute, solo.

        Args:
            include_empty: also list unnamed "Track N" tracks (default False)
            limit: scan at most this many tracks from the top (default 100, max 500)
        """
        if not 1 <= limit <= 500:
            raise ToolError("limit must be 1-500")
        conn = get_connection()
        project_files.controller_info(conn)
        return run(conn, "playlist.getTracks", {"include_empty": include_empty, "limit": limit})

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_playlist_track(
        track: int,
        name: str | None = None,
        color: str | list[int] | None = None,
        muted: bool | None = None,
        solo: bool | None = None,
    ) -> dict:
        """Rename, colour, mute or solo a playlist track (1-based, like the playlist).

        Args:
            track: playlist track 1-500
            name: new name
            color: "#RRGGBB" or [r, g, b]
            muted, solo: True/False (None leaves it unchanged)
        """
        if isinstance(track, bool) or not isinstance(track, int) or not 1 <= track <= 500:
            raise ToolError(f"track must be 1-500, got {track!r}")
        if name is None and color is None and muted is None and solo is None:
            raise ToolError("nothing to change: give name, color, muted or solo")
        conn = get_connection()
        project_files.controller_info(conn)
        return run(conn, "playlist.setTrack", {
            "index": track, "name": name, "color": parse_color(color),
            "muted": muted, "solo": solo,
        })

    @mcp.tool(annotations=ACTION)
    def fl_add_marker(bar: float, name: str, beats_per_bar: int = 4) -> dict:
        """Add a playlist time marker (e.g. "Verse 1") at a 1-based bar.

        Args:
            bar: 1-based bar (9 = start of bar 9; 9.5 = half-way through it)
            name: marker label
            beats_per_bar: time signature numerator (default 4)
        """
        if isinstance(bar, bool) or not isinstance(bar, (int, float)) or not math.isfinite(bar):
            raise ToolError(f"bar must be a number >= 1, got {bar!r}")
        if bar < 1:
            raise ToolError(f"bar must be >= 1, got {bar!r}")
        conn = get_connection()
        info = project_files.controller_info(conn)
        ppq = int(info.get("ppq") or 96)
        ticks = int(round((bar - 1) * beats_per_bar * ppq))
        run(conn, "arrangement.addMarker", {"ticks": ticks, "name": name})
        return {"bar": bar, "name": name, "ticks": ticks}
