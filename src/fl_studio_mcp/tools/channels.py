"""Channel rack control tools for FL Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.annotations import ACTION, DESTRUCTIVE, IDEMPOTENT, READ_ONLY
from fl_studio_mcp.utils.errors import run

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_channel_tools(mcp: FastMCP) -> None:
    """Register channel rack tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_channel_info(index: int, use_global_index: bool = True) -> dict:
        """Get detailed information about a channel.

        Args:
            index: Channel index
            use_global_index: Whether to use global channel indexing
        """
        result = run(get_connection(), "channels.getInfo", {
            "index": index,
            "use_global": use_global_index,
        })

        return {
            "index": result.get("index", index),
            "name": result.get("name", ""),
            "color": result.get("color", "0x0"),
            "volume": result.get("volume", 0),
            "pan": result.get("pan", 0),
            "pitch": result.get("pitch", 0),
            "is_muted": result.get("is_muted", False),
            "is_solo": result.get("is_solo", False),
            "is_selected": result.get("is_selected", False),
            "target_fx_track": result.get("target_fx_track", 0),
        }

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_all_channels() -> list[dict]:
        """List every channel in the channel rack.

        Each channel: index (global), name, volume, pan, is_muted, is_solo,
        is_selected, target_fx_track. The channel count is the list length;
        the selected channel(s) have is_selected true.
        """
        result = run(get_connection(), "channels.getAll")

        return result.get("channels", [])

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_select_channel(index: int, select: bool = True, exclusive: bool = False) -> str:
        """Select or deselect a channel.

        Args:
            index: Channel index (global)
            select: True to select, False to deselect
            exclusive: True to select only this channel, deselecting all others
        """
        if exclusive:
            if not select:
                raise ToolError("exclusive=True requires select=True")
            result = run(get_connection(), "channels.selectOne", {"index": index})
            channel_name = result.get("channel_name", f"Channel {index}")
            return f"Channel '{channel_name}' selected exclusively"

        result = run(get_connection(), "channels.select", {
            "index": index,
            "select": select,
        })

        channel_name = result.get("channel_name", f"Channel {index}")
        return f"Channel '{channel_name}' {'selected' if select else 'deselected'}"

    @mcp.tool(annotations=ACTION)
    def fl_trigger_note(
        channel: int,
        note: int,
        velocity: int = 100,
        midi_channel: int = -1
    ) -> str:
        """Trigger a MIDI note on a channel (real-time, does NOT persist to pattern).

        This triggers the note in real-time. To persist notes in a pattern,
        FL Studio must be in record mode, or use the step sequencer functions.

        Args:
            channel: Channel index (global)
            note: MIDI note number (0-127, where 60 = C5/Middle C)
            velocity: Note velocity (1-127, 0 = note off)
            midi_channel: MIDI channel (-1 for default)
        """
        if not 0 <= note <= 127:
            raise ToolError("Note must be between 0 and 127")
        if not 0 <= velocity <= 127:
            raise ToolError("Velocity must be between 0 and 127")

        run(get_connection(), "channels.triggerNote", {
            "channel": channel,
            "note": note,
            "velocity": velocity,
            "midi_channel": midi_channel,
        })

        note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        note_name = note_names[note % 12]
        octave = (note // 12) - 1

        if velocity == 0:
            return f"Note {note_name}{octave} (MIDI {note}) released on channel {channel}"
        return (
            f"Note {note_name}{octave} (MIDI {note}) triggered with velocity {velocity}"
            f" on channel {channel}"
        )

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_channel_volume(index: int, volume: float) -> str:
        """Set the volume of a channel.

        Args:
            index: Channel index (global)
            volume: Volume level from 0.0 (silence) to 1.0 (full)
        """
        if not 0.0 <= volume <= 1.0:
            raise ToolError("Volume must be between 0.0 and 1.0")

        result = run(get_connection(), "channels.setVolume", {
            "index": index,
            "volume": volume,
        })

        channel_name = result.get("channel_name", f"Channel {index}")
        new_volume = result.get("volume", volume)
        return f"Channel '{channel_name}' volume set to {new_volume:.2f}"

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_channel_pan(index: int, pan: float) -> str:
        """Set the pan position of a channel.

        Args:
            index: Channel index (global)
            pan: Pan from -1.0 (full left) to 1.0 (full right), 0.0 = center
        """
        if not -1.0 <= pan <= 1.0:
            raise ToolError("Pan must be between -1.0 and 1.0")

        result = run(get_connection(), "channels.setPan", {
            "index": index,
            "pan": pan,
        })

        channel_name = result.get("channel_name", f"Channel {index}")
        new_pan = result.get("pan", pan)
        return f"Channel '{channel_name}' pan set to {new_pan:.2f}"

    @mcp.tool(annotations=ACTION)
    def fl_mute_channel(index: int, muted: bool | None = None) -> str:
        """Mute or unmute a channel.

        Args:
            index: Channel index (global)
            muted: True to mute, False to unmute, None to toggle
        """
        result = run(get_connection(), "channels.mute", {
            "index": index,
            "muted": muted,
        })

        is_muted = result.get("is_muted", False)
        channel_name = result.get("channel_name", f"Channel {index}")
        return f"Channel '{channel_name}' {'muted' if is_muted else 'unmuted'}"

    @mcp.tool(annotations=ACTION)
    def fl_solo_channel(index: int, solo: bool | None = None) -> str:
        """Solo or unsolo a channel.

        Args:
            index: Channel index (global)
            solo: True to solo, False to unsolo, None to toggle
        """
        result = run(get_connection(), "channels.solo", {
            "index": index,
            "solo": solo,
        })

        is_solo = result.get("is_solo", False)
        channel_name = result.get("channel_name", f"Channel {index}")
        return f"Channel '{channel_name}' {'soloed' if is_solo else 'unsoloed'}"

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_channel_name(index: int, name: str) -> str:
        """Set the name of a channel.

        Args:
            index: Channel index (global)
            name: New name for the channel
        """
        run(get_connection(), "channels.setName", {
            "index": index,
            "name": name,
        })

        return f"Channel {index} renamed to '{name}'"

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_channel_color(index: int, red: int, green: int, blue: int) -> str:
        """Set the color of a channel.

        Args:
            index: Channel index (global)
            red: Red component (0-255)
            green: Green component (0-255)
            blue: Blue component (0-255)
        """
        run(get_connection(), "channels.setColor", {
            "index": index,
            "r": red,
            "g": green,
            "b": blue,
        })

        return f"Channel {index} color set to RGB({red}, {green}, {blue})"

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_route_channel_to_mixer(channel_index: int, mixer_track: int) -> str:
        """Route a channel to a specific mixer track.

        Args:
            channel_index: Channel index (global)
            mixer_track: Mixer track index to route to
        """
        result = run(get_connection(), "channels.routeToMixer", {
            "channel_index": channel_index,
            "mixer_track": mixer_track,
        })

        channel_name = result.get("channel_name", f"Channel {channel_index}")
        return f"Channel '{channel_name}' routed to mixer track {mixer_track}"

    # Step Sequencer Tools

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_grid_bit(channel: int, position: int, value: bool) -> str:
        """Set a step in the step sequencer on or off.

        This allows basic pattern programming for drum-style instruments.

        Args:
            channel: Channel index (global)
            position: Step position (0-based)
            value: True to enable the step, False to disable
        """
        result = run(get_connection(), "channels.setGridBit", {
            "channel": channel,
            "position": position,
            "value": value,
        })

        channel_name = result.get("channel_name", f"Channel {channel}")
        return f"Channel '{channel_name}' step {position} {'enabled' if value else 'disabled'}"

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_step_sequence(channel: int, steps: int = 16) -> list[bool]:
        """Get the step sequence pattern for a channel.

        Args:
            channel: Channel index (global)
            steps: Number of steps to retrieve (default 16)
        """
        result = run(get_connection(), "channels.getStepSequence", {
            "channel": channel,
            "steps": steps,
        })

        return result.get("sequence", [])

    @mcp.tool(annotations=DESTRUCTIVE)
    def fl_set_step_sequence(channel: int, pattern: list[bool]) -> str:
        """Set a complete step sequence pattern for a channel.

        Args:
            channel: Channel index (global)
            pattern: List of boolean values for each step (True = on, False = off)
        """
        result = run(get_connection(), "channels.setStepSequence", {
            "channel": channel,
            "pattern": pattern,
        })

        channel_name = result.get("channel_name", f"Channel {channel}")
        active_steps = result.get("active_steps", sum(pattern))
        total_steps = result.get("total_steps", len(pattern))
        return (
            f"Channel '{channel_name}' pattern set with {active_steps}/{total_steps} steps active"
        )
