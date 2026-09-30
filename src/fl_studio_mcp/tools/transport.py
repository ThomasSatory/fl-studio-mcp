"""Transport control tools for FL Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.annotations import ACTION, IDEMPOTENT, READ_ONLY
from fl_studio_mcp.utils.errors import run

if TYPE_CHECKING:
    from fastmcp import FastMCP

# FL Studio's tempo limits in BPM.
TEMPO_MIN = 10.0
TEMPO_MAX = 522.0


def register_transport_tools(mcp: FastMCP) -> None:
    """Register transport control tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool(annotations=ACTION)
    def fl_play() -> str:
        """Start or pause FL Studio playback.

        Toggles between play and pause states. If stopped, starts playback.
        If playing, pauses playback.
        """
        result = run(get_connection(), "transport.start")

        is_playing = result.get("is_playing", False)
        return f"Playback {'started' if is_playing else 'paused'}"

    @mcp.tool(annotations=ACTION)
    def fl_stop() -> str:
        """Stop FL Studio playback completely.

        Stops playback and resets the playback position.
        """
        run(get_connection(), "transport.stop")

        return "Playback stopped"

    @mcp.tool(annotations=ACTION)
    def fl_record() -> str:
        """Toggle recording mode in FL Studio.

        When enabled, incoming MIDI and audio will be recorded.
        """
        result = run(get_connection(), "transport.record")

        is_recording = result.get("is_recording", False)
        return f"Recording {'enabled' if is_recording else 'disabled'}"

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_transport_status() -> dict:
        """Get current transport status including playback and recording state.

        Returns information about whether FL Studio is playing, recording,
        the current position, and loop mode.
        """
        result = run(get_connection(), "transport.getStatus")

        return {
            "is_playing": result.get("is_playing", False),
            "is_recording": result.get("is_recording", False),
            "position": result.get("position", ""),
            "loop_mode": result.get("loop_mode", "pattern"),
        }

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_song_position(position: float, mode: int = 2) -> str:
        """Set the playback position in FL Studio.

        Args:
            position: The position value. Interpretation depends on mode.
            mode: Position format:
                  0 = Percentage (0.0 to 1.0)
                  1 = Time in milliseconds
                  2 = Time in seconds (default)
                  3 = Position in ticks
                  4 = Position as bars:steps:ticks (encoded)
        """
        result = run(get_connection(), "transport.setPosition", {
            "position": position,
            "mode": mode,
        })

        new_pos = result.get("position", "")
        return f"Position set to {new_pos}"

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_song_length() -> dict:
        """Get the total length of the current song/pattern.

        Returns the length in multiple formats for convenience.
        """
        result = run(get_connection(), "transport.getLength")

        return {
            "ticks": result.get("ticks", 0),
            "seconds": result.get("seconds", 0),
            "milliseconds": result.get("milliseconds", 0),
        }

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_loop_mode(mode: str) -> str:
        """Set the loop mode between pattern and song.

        Args:
            mode: Either "pattern" or "song"
        """
        if mode not in ("pattern", "song"):
            raise ToolError("mode must be 'pattern' or 'song'")

        run(get_connection(), "transport.setLoopMode", {"mode": mode})

        return f"Loop mode set to {mode}"

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_playback_speed(speed: float) -> str:
        """Set the playback speed multiplier.

        Args:
            speed: Speed multiplier from 0.25 (quarter speed) to 4.0 (4x speed).
                   1.0 is normal speed.
        """
        if not 0.25 <= speed <= 4.0:
            raise ToolError("Speed must be between 0.25 and 4.0")

        run(get_connection(), "transport.setPlaybackSpeed", {"speed": speed})

        return f"Playback speed set to {speed}x"

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_tempo() -> dict:
        """Get the project tempo in BPM."""
        result = run(get_connection(), "transport.getTempo")
        return {"tempo": result.get("tempo")}

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_tempo(bpm: float) -> dict:
        """Set the project tempo in BPM (10 to 522).

        FL Studio only exposes tempo through an automation event, so the value
        is read back afterwards; the call fails if FL Studio did not apply it.
        """
        if isinstance(bpm, bool) or not isinstance(bpm, (int, float)):
            raise ToolError(f"Tempo must be a number between 10 and 522 BPM, got {bpm!r}")
        if not TEMPO_MIN <= bpm <= TEMPO_MAX:
            raise ToolError(f"Tempo must be between 10 and 522 BPM, got {bpm!r}")
        result = run(get_connection(), "transport.setTempo", {"bpm": float(bpm)})
        if not result.get("applied"):
            raise ToolError(
                f"Requested tempo {bpm:g} BPM but FL Studio reports {result.get('tempo')} BPM"
            )
        return {"tempo": result.get("tempo")}
