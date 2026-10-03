"""Pattern management tools for FL Studio.

Requires FL Studio 2024+ (the `patterns` scripting module). On older versions
the tools return a clear error message instead of failing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.annotations import ACTION, IDEMPOTENT, READ_ONLY
from fl_studio_mcp.utils.errors import run

if TYPE_CHECKING:
    from fastmcp import FastMCP


# FL's patterns.getPatternLength counts step-sequencer steps (16ths), not beats
# as its API docs say: verified live, a 2-bar pattern reports 32.
STEPS_PER_BEAT = 4


def with_lengths(pattern: dict) -> dict:
    """Give a controller pattern entry both length_steps and length_beats.

    Controller scripts before version 5 passed FL's step count on as
    "length_beats" (4x too big); version 5 sends both.
    """
    if not isinstance(pattern, dict) or "length_steps" in pattern:
        return pattern
    steps = pattern.get("length_beats")
    if isinstance(steps, (int, float)) and not isinstance(steps, bool):
        pattern["length_steps"] = steps
        pattern["length_beats"] = steps / STEPS_PER_BEAT
    return pattern


def _index_error(index: object) -> str | None:
    """Reject pattern indices FL Studio would silently turn into new patterns.

    Pattern indices are 1-based; jumping to index 0 or a negative index makes
    FL create or select unexpected patterns instead of failing.
    """
    if isinstance(index, bool) or not isinstance(index, int) or index < 1:
        return f"pattern index must be a positive integer (1-based), got {index!r}"
    return None


def register_pattern_tools(mcp: FastMCP) -> None:
    """Register pattern management tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool(annotations=READ_ONLY)
    def fl_list_patterns(include_default: bool = False) -> dict:
        """List all patterns in the project.

        By default only patterns that were modified from their default state
        are listed. Set include_default=True to list all 999 potential slots
        (slow, rarely useful).

        Returns each pattern's index (1-based), name, color, length
        (length_beats = quarter notes, length_steps = 16th steps; 16 beats =
        4 bars of 4/4) and selection state, plus the currently active pattern.
        `current` is the index of the active pattern (the one the channel rack
        shows); `is_selected` only means highlighted in FL's pattern picker.
        """
        result = run(get_connection(), "patterns.getAll", {"include_default": include_default})
        for pattern in result.get("patterns", []):
            with_lengths(pattern)
        return result

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_select_pattern(index: int) -> str:
        """Select and activate a pattern (1-based index).

        Note: jumping to a non-existent pattern index creates it, so stay
        within sane bounds (use fl_new_pattern for a fresh pattern).
        """
        if error := _index_error(index):
            raise ToolError(error)
        result = run(get_connection(), "patterns.select", {"index": index})
        return f"Selected pattern {result.get('selected')} ({result.get('name')})"

    @mcp.tool(annotations=ACTION)
    def fl_new_pattern(name: str | None = None) -> dict:
        """Create a pattern: switch to the next empty one and optionally name it.

        FL patterns are virtual; this finds and activates the next unused
        pattern, which is the practical equivalent of creating one.

        Returns {"pattern": its number, "name", "message"}.

        Args:
            name: name for the new pattern (e.g. "Drums verse")

        Note: FL reports the length of an empty pattern as the project's
        current default (often the previous pattern's length), not 0.
        """
        params = {"name": name} if name else {}
        result = run(get_connection(), "patterns.newEmpty", params)
        return {"pattern": result.get("selected"), "name": result.get("name"),
                "message": f"Switched to empty pattern {result.get('selected')} "
                           f"({result.get('name')})"}

    @mcp.tool(annotations=ACTION)
    def fl_clone_pattern(index: int | None = None) -> str:
        """Clone a pattern (default: the currently active one).

        Warning: FL Studio closes the piano roll when cloning, to protect you
        from editing the wrong pattern. Reopen the piano roll afterwards if
        needed.
        """
        if index is not None and (error := _index_error(index)):
            raise ToolError(error)
        params = {"index": index} if index is not None else {}
        result = run(get_connection(), "patterns.clone", params)
        return f"Cloned to pattern {result.get('cloned_to')} ({result.get('name')})"

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_rename_pattern(index: int, name: str) -> str:
        """Rename the pattern at index (1-based). Empty name resets to default."""
        if error := _index_error(index):
            raise ToolError(error)
        result = run(get_connection(), "patterns.setName", {"index": index, "name": name})
        return f"Pattern {result.get('index')} is now named '{result.get('name')}'"

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_pattern_color(index: int, color: str | list[int]) -> str:
        """Set a pattern's colour ("#RRGGBB" or [r, g, b]); index is 1-based."""
        from fl_studio_mcp.tools.playlist import parse_color

        if error := _index_error(index):
            raise ToolError(error)
        r, g, b = parse_color(color)
        params = {"index": index, "r": r, "g": g, "b": b}
        result = run(get_connection(), "patterns.setColor", params)
        return f"Pattern {index} colour set to {result.get('color')}"
