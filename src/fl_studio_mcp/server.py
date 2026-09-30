"""FL Studio MCP Server - Control FL Studio via Model Context Protocol.

Two channels to FL Studio:
1. MIDI + JSON files - transport, mixer, channels, plugins, patterns, undo,
   windows (controller script device_FLStudioMCP.py)
2. Piano roll script - persistent notes via JSON files + a trigger keystroke
   (ComposeWithLLM.pyscript)

Requirements: FL Studio 20.7+ running with the FLStudioMCP controller enabled
on a virtual MIDI port (IAC Driver on macOS, loopMIDI on Windows); both
scripts installed by scripts/setup.sh.
"""

from __future__ import annotations

from fastmcp import FastMCP

from fl_studio_mcp import __version__
from fl_studio_mcp.tools import (
    register_channel_tools,
    register_history_tools,
    register_mixer_tools,
    register_pattern_tools,
    register_piano_roll_tools,
    register_plugin_tools,
    register_project_tools,
    register_transport_tools,
    register_ui_tools,
)
from fl_studio_mcp.tools.annotations import IDEMPOTENT
from fl_studio_mcp.utils.connection import get_connection, reset_connection

# Create the MCP server
mcp = FastMCP(
    name="fl-studio-mcp",
    version=__version__,
    instructions="""
FL Studio MCP Server - control a running FL Studio from an AI assistant.

Start with fl_get_project_snapshot: transport, tempo, patterns, channels and
mixer tracks in one call. If a tool reports no connection, call
fl_connection_status(reconnect=True).

Domains:
- Transport and tempo: play/stop/record, position, loop mode, speed, tempo
- Mixer: track volume, pan, mute, solo, arm, name, colour, stereo separation
- Channels: channel rack properties, mixer routing, step sequencer
  (fl_set_step_sequence / fl_set_grid_bit for persistent drum steps)
- Plugins: fl_get_plugin_params describes a plugin and its parameters;
  parameter values and preset navigation. Loading new plugins is impossible.
- Piano roll: fl_send_notes, fl_send_chord ("Am7", "Cmaj9/E"),
  fl_delete_notes, fl_clear_piano_roll, fl_get_piano_roll_state,
  fl_get_pr_context
- Patterns: list, select, create empty, clone, rename (FL Studio 2024+)
- Undo: fl_save_undo_point before a batch of edits, fl_undo, fl_redo
- Windows: show, hide, focus; fl_open_piano_roll(channel) targets a channel

Piano roll rules:
- A piano roll window must be open, and ComposeWithLLM must have been run
  once by hand in this FL Studio session (piano roll > Tools > Scripting).
- Call fl_open_piano_roll(channel) first to choose which channel the notes
  go to; the piano roll does not follow channel selection made by scripting.
- Times and durations are in quarter notes (1.0 = one beat in 4/4).
- Pitch names use C4 = 60. FL Studio's own UI labels MIDI 60 as "C5", so a
  note written as "A3" appears as "A4" in FL.
- The note tools verify what FL Studio applied and raise on any failure.

Limits:
- fl_trigger_note plays a note in real time only; it is not recorded unless
  FL Studio is recording.
- FL's undo does not track every scripting change (pattern renames).
""",
)


# Register connection status resource
@mcp.resource("fl://status")
def get_fl_status() -> str:
    """Get FL Studio connection status."""
    conn = get_connection()
    if conn.is_connected:
        return "Connected to FL Studio via MIDI"
    else:
        return f"Not connected: {conn.connection_error}"


@mcp.resource("fl://project")
def get_project_info() -> dict:
    """Get current FL Studio project information."""
    conn = get_connection()

    if not conn.is_connected:
        return {"error": conn.connection_error}

    try:
        result = conn.send_command("transport.getStatus")
        if not result.get("success", False) and "error" in result:
            return {"error": result["error"]}

        return {
            "is_playing": result.get("is_playing", False),
            "is_recording": result.get("is_recording", False),
            "position": result.get("position", ""),
            "loop_mode": result.get("loop_mode", "pattern"),
        }
    except Exception as e:
        return {"error": str(e)}


# Connection management tools
# Not read-only: reconnect=True drops and reopens the MIDI connection.
@mcp.tool(annotations=IDEMPOTENT)
def fl_connection_status(reconnect: bool = False) -> dict:
    """Get the FL Studio MIDI connection status.

    Args:
        reconnect: drop the current connection and connect again first - use
                   after starting FL Studio or when commands stopped arriving.
    """
    if reconnect:
        reset_connection()
    conn = get_connection()
    # Eagerly attempt a connection so the reported status reflects actual
    # reachability rather than the lazily-initialized flag.
    try:
        conn.ensure_connected()
    except RuntimeError:
        pass
    status = conn.get_status()
    return {
        "connected": status.get("connected", False),
        "port_name": status.get("port_name"),
        "available_ports": status.get("available_ports", []),
        "error": status.get("error"),
    }


# Register all tools
register_transport_tools(mcp)
register_mixer_tools(mcp)
register_channel_tools(mcp)
register_plugin_tools(mcp)
register_piano_roll_tools(mcp)
register_history_tools(mcp)
register_pattern_tools(mcp)
register_ui_tools(mcp)
register_project_tools(mcp)


def main():
    """Run the FL Studio MCP server."""
    mcp.run()


if __name__ == "__main__":
    main()
