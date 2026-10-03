"""Transport control tools for FL Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.annotations import ACTION, IDEMPOTENT, READ_ONLY
from fl_studio_mcp.utils.errors import run
from fl_studio_mcp.utils.midi_connection import OUTDATED_CONTROLLER_HINT

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
    def fl_set_song_position(
        bar: float | None = None,
        seconds: float | None = None,
        ticks: int | None = None,
        beats_per_bar: int = 4,
    ) -> dict:
        """Move the playhead, by 1-based bar, by seconds or by absolute ticks.

        In pattern mode the position is inside the pattern, in song mode in the song.

        Args:
            bar: 1-based bar (1 = start, 9.5 = half-way through bar 9)
            seconds: time from the start
            ticks: absolute ticks (PPQ ticks per quarter note)
            beats_per_bar: time signature numerator used for `bar` (default 4)
        """
        given = [v for v in (bar, seconds, ticks) if v is not None]
        if len(given) != 1:
            raise ToolError("give exactly one of bar, seconds or ticks")
        conn = get_connection()
        if bar is not None:
            if bar < 1:
                raise ToolError("bar must be >= 1")
            ppq = run(conn, "transport.getLength").get("ppq") or 96
            params = {"ticks": int(round((bar - 1) * beats_per_bar * ppq))}
        elif seconds is not None:
            if seconds < 0:
                raise ToolError("seconds must be >= 0")
            params = {"seconds": float(seconds)}
        else:
            if ticks < 0:
                raise ToolError("ticks must be >= 0")
            params = {"ticks": int(ticks)}
        result = run(conn, "transport.setPosition", params)
        return {"position": result.get("position"), "ticks": result.get("ticks")}

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_song_length() -> dict:
        """Length of the song (playlist arrangement, not the current pattern).

        Returns seconds, milliseconds, ticks, ppq, ticks_per_bar and
        length_bars (the length in bars, e.g. 80.0 for an 80-bar song).
        last_position is where the song's last tick sits, as FL's B:S:T
        display shows it ({"bar": 80, "step": 16, "tick": 23} for an 80-bar
        song): a position, not a length.
        """
        result = run(get_connection(), "transport.getLength")
        if "ppq" not in result:
            raise ToolError(OUTDATED_CONTROLLER_HINT)
        ticks, ppq = result.get("ticks"), result.get("ppq")
        per_bar = result.get("ticks_per_bar")
        out = {key: result.get(key) for key in ("seconds", "milliseconds", "ticks", "ppq")}
        if not per_bar and ppq:
            per_bar = ppq * 4
            if "ticks_per_bar" not in result:
                out["note"] = "length_bars assumes 4/4 (the controller script is outdated)"
            else:
                out["note"] = "FL did not report ticks per bar; length_bars assumes 4/4"
        out["ticks_per_bar"] = per_bar
        out["length_bars"] = (round(ticks / per_bar, 3)
                              if isinstance(ticks, (int, float)) and per_bar else None)
        out["last_position"] = {"bar": result.get("bars"), "step": result.get("steps"),
                                "tick": result.get("bar_ticks")}
        return out

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
