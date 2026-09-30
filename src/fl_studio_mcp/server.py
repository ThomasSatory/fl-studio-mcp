"""FL Studio MCP Server - Control FL Studio via Model Context Protocol.

This MCP server provides tools to control FL Studio through two mechanisms:
1. MIDI + JSON - Real-time control of mixer, transport, channels, and plugins
2. Piano Roll Scripts - Persistent note placement via JSON + keystroke triggering

Requirements:
- FL Studio 20.7+ running
- FLStudioMCP controller script installed (run: ./scripts/setup_mac.sh)
- On Mac: IAC Driver enabled in Audio MIDI Setup
- On Windows: loopMIDI virtual MIDI ports configured
- For piano roll: ComposeWithLLM.pyscript installed in FL Studio

Limitations:
- Cannot load new plugins (only control existing ones)
- Cannot create new patterns programmatically
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
from fl_studio_mcp.tools.annotations import READ_ONLY
from fl_studio_mcp.utils.connection import get_connection, reset_connection

# Create the MCP server
mcp = FastMCP(
    name="fl-studio-mcp",
    version=__version__,
    instructions="""
FL Studio MCP Server - Control FL Studio from AI assistants.

This server provides tools to control FL Studio through its Python scripting API.
FL Studio must be running with the FLStudioMCP MIDI controller enabled.

Available tool categories:
- Transport: Play, stop, record, tempo, position control
- Mixer: Volume, pan, mute, solo, track management
- Channels: Channel info, note triggering, step sequencer
- Plugins: Parameter control, preset navigation (cannot load new plugins)
- Project: fl_get_project_snapshot for a one-call overview (transport, tempo,
  patterns, channels, mixer tracks)

Important limitations:
1. Cannot load new VST/AU plugins - only control existing ones
2. Cannot create new patterns programmatically
3. Note triggering (fl_trigger_note) is real-time only - notes won't persist
   unless FL Studio is recording. Use step sequencer (fl_set_grid_bit) for
   persistent drum patterns.
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
@mcp.tool(annotations=READ_ONLY)
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
