"""Channel rack control tools for FL Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.annotations import ACTION, DESTRUCTIVE, IDEMPOTENT, READ_ONLY
from fl_studio_mcp.utils import project_files
from fl_studio_mcp.utils.errors import run
from fl_studio_mcp.utils.midi_connection import OUTDATED_CONTROLLER_HINT
from fl_studio_mcp.utils.music import midi_to_note_name, note_to_midi

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
            **{k: result[k] for k in ("pitch_cents", "pitch_range") if k in result},
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
        channels = result.get("channels", [])
        if channels and "volume" not in channels[0]:
            raise ToolError(OUTDATED_CONTROLLER_HINT)

        return channels

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
            note: MIDI note number (0-127, 60 = middle C, "C4" here, "C5" in FL's UI)
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

    @mcp.tool(annotations=DESTRUCTIVE)
    def fl_write_steps(
        channel: int,
        steps: list[dict],
        pattern: int | None = None,
        clear: bool = True,
    ) -> dict:
        """Write step-sequencer notes with pitch, velocity and pan into any pattern.

        Works without the piano roll and without a keystroke: pick the channel
        and pattern by number. One note per 16th step and channel, the length
        is the channel's default (fine for drums, 808s and one-shots; use
        fl_send_notes for chords, long notes or off-grid timing).

        Args:
            channel: channel index (global)
            steps: [{"step": 0, "note": "C3" or "midi": 48, "velocity": 0-127,
                     "pan": -1..1, "shift": 0-1 (fraction of a step, swing/nudge;
                     needs controller script version 5)}]
                   step 0 = first 16th of the pattern (16 steps per 4/4 bar).
                   Pitch defaults to 60 (C4 here; FL's UI calls it C5), which
                   plays a sample at its original pitch.
            pattern: pattern number (1-based); default the active pattern.
                     FL switches to that pattern.
            clear: first switch off every step of this channel (default True)

        Example: 808 on bar 1: [{"step": 0, "note": "F1", "velocity": 120},
                                 {"step": 6, "note": "F1"}, {"step": 10, "note": "Ab1"}]
        """
        if not isinstance(steps, list):
            raise ToolError("steps must be a list")
        conn = get_connection()
        info = project_files.controller_info(conn)
        ppq = int(info.get("ppq") or 96)
        normalized = []
        seen = set()
        for i, step in enumerate(steps):
            normalized.append(_normalize_step(i, step, ppq))
            if normalized[-1]["step"] in seen:
                raise ToolError(f"list item {i}: step {normalized[-1]['step']} is given twice")
            seen.add(normalized[-1]["step"])
        if pattern is not None and (isinstance(pattern, bool) or pattern < 1):
            raise ToolError(f"pattern numbers start at 1, got {pattern!r}")
        if (any("shift" in step for step in normalized)
                and int(info.get("controller_version") or 0) < SHIFT_CONTROLLER_VERSION):
            raise ToolError(project_files.OUTDATED_FOR_FEATURE.format(
                action="safe step shift (older scripts leave a shifted step without its "
                       "note; leave 'shift' out or update the script)"))
        params = {"channel": channel, "steps": normalized, "clear": clear}
        if pattern is not None:
            params["pattern"] = pattern
        result = run(conn, "channels.setSteps", params)
        if result.get("mismatched"):
            raise ToolError(
                f"FL Studio did not apply {len(result['mismatched'])} step(s) as asked "
                f"(read back after writing): {result['mismatched'][:5]}. Check them with "
                "fl_get_steps."
            )
        return result

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_get_steps(channel: int, pattern: int | None = None, steps: int = 0) -> dict:
        """Read a channel's active step-sequencer steps with pitch, velocity, pan and shift.

        pan (-1..1) and shift (fraction of a step) use fl_write_steps' scales;
        FL's raw values are in pan_raw (0-127, 64 = centre) and shift_ticks.
        A step FL holds no note data for is marked "broken".

        Args:
            channel: channel index (global)
            pattern: pattern number (default: the active one; FL switches to it)
            steps: how many steps to scan (default: the pattern's length)
        """
        conn = get_connection()
        params: dict = {"channel": channel, "steps": steps}
        if pattern is not None:
            params["pattern"] = pattern
        info = project_files.controller_info(conn)
        step_ticks = max(1, int(info.get("ppq") or 96) // 4)
        result = run(conn, "channels.getSteps", params)
        for entry in result.get("active", []):
            _readable_step(entry, step_ticks)
        return result


# Controller version whose setSteps writes and verifies the shift safely
# (version 6 verifies it more strictly, but 5 is safe).
SHIFT_CONTROLLER_VERSION = 5


def _readable_step(entry: dict, step_ticks: int) -> None:
    """FL's raw step values -> the scales fl_write_steps takes (raw ones kept)."""
    if any(entry.get(key) == -1 for key in ("pitch", "velocity", "pan")):
        entry["broken"] = True
        entry["problem"] = ("FL holds no note data for this step (its values read -1): "
                            "rewrite it with fl_write_steps")
        return
    if isinstance(entry.get("pitch"), int):
        entry["note"] = midi_to_note_name(entry["pitch"])
    if isinstance(entry.get("pan"), int):
        entry["pan_raw"] = entry["pan"]
        entry["pan"] = round(entry["pan"] / 64 - 1, 3)
    if isinstance(entry.get("shift"), int):
        entry["shift_ticks"] = entry["shift"]
        entry["shift"] = round(entry["shift"] / step_ticks, 3)


def _normalize_step(i: int, step: object, ppq: int) -> dict:
    """One step for channels.setSteps, with FL's raw parameter values."""
    if not isinstance(step, dict):
        raise ToolError(f"list item {i}: expected an object, got {step!r}")
    allowed = {"step", "note", "midi", "velocity", "pan", "shift"}
    unknown = sorted(set(step) - allowed)
    if unknown:
        raise ToolError(f"list item {i}: unknown field(s) {', '.join(unknown)}; allowed: "
                        f"{', '.join(sorted(allowed))}")
    index = step.get("step")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 512:
        raise ToolError(f"list item {i}: 'step' must be an integer 0-511, got {index!r}")
    where = f"step {index} (list item {i})"  # the step number, and where it is in the list
    out: dict = {"step": index}
    if "note" in step and "midi" in step:
        raise ToolError(f"{where}: give note or midi, not both")
    if "note" in step:
        try:
            out["pitch"] = note_to_midi(step["note"])
        except ValueError as e:
            raise ToolError(f"{where}: {e}") from e
    elif "midi" in step:
        midi = step["midi"]
        if isinstance(midi, bool) or not isinstance(midi, int) or not 0 <= midi <= 127:
            raise ToolError(f"{where}: midi must be 0-127, got {midi!r}")
        out["pitch"] = midi
    if "velocity" in step:
        velocity = step["velocity"]
        if isinstance(velocity, bool) or not isinstance(velocity, int) or not 0 <= velocity <= 127:
            raise ToolError(f"{where}: velocity must be an integer 0-127, got {velocity!r}")
        out["velocity"] = velocity
    if "pan" in step:
        pan = step["pan"]
        if isinstance(pan, bool) or not isinstance(pan, (int, float)) or not -1 <= pan <= 1:
            raise ToolError(f"{where}: pan must be -1..1, got {pan!r}")
        out["pan"] = min(127, int(round((pan + 1) * 64)))
    if "shift" in step:
        shift = step["shift"]
        if isinstance(shift, bool) or not isinstance(shift, (int, float)) or not 0 <= shift < 1:
            raise ToolError(f"{where}: shift must be 0 <= shift < 1 (fraction of a step)")
        step_ticks = max(1, ppq // 4)
        out["shift"] = min(step_ticks - 1, int(round(shift * step_ticks)))
    return out
