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
    register_arrangement_tools,
    register_channel_tools,
    register_history_tools,
    register_mixer_tools,
    register_pattern_tools,
    register_piano_roll_tools,
    register_playlist_tools,
    register_plugin_tools,
    register_project_tools,
    register_render_tools,
    register_sample_tools,
    register_transport_tools,
    register_ui_tools,
)
from fl_studio_mcp.tools.annotations import IDEMPOTENT
from fl_studio_mcp.utils import errors, project_files, render
from fl_studio_mcp.utils.connection import get_connection, reset_connection

# Create the MCP server
mcp = FastMCP(
    name="fl-studio-mcp",
    version=__version__,
    instructions="""
FL Studio MCP Server - control a running FL Studio from an AI assistant.

Start with fl_get_project_snapshot: transport, tempo, patterns, channels and
mixer tracks in one call. If a tool reports no connection, call
fl_connection_status(reconnect=True); it also says when the controller script
installed in FL Studio is outdated.

Conventions: bars are 1-based like FL's ruler (bar 1 = song start), playlist
tracks 1-based, patterns 1-based, channels and mixer tracks 0-based (0 =
Master). Note times inside a pattern are in quarter notes. Pitch names use
C4 = 60; FL's UI labels MIDI 60 as "C5".

Domains:
- Transport and project: play/stop/record, fl_set_song_position(bar=...),
  loop mode, tempo (fl_set_tempo), fl_get_song_length, fl_save_project
- Arrangement (playlist clips): fl_get_arrangement lists every clip and
  marker; fl_edit_arrangement places / deletes / copies / moves pattern
  clips and creates automation clips (tempo, volume, pan, pitch, EQ, effect
  mix, any plugin parameter) in ONE batch. Both save the project; edits
  make FL reopen it (a few seconds, undo history reset, backup kept - undo
  with fl_open_project(backup)). Batch all edits into one call.
- Playlist tracks and markers: fl_get_playlist_tracks,
  fl_set_playlist_track (name, colour, mute), fl_add_marker
- Patterns: list, select, fl_new_pattern(name), clone, rename, colour
- Notes: fl_send_notes / fl_send_chord write into the piano roll of
  `channel` in `pattern` (they retarget it themselves); they need the
  ComposeWithLLM keystroke. fl_write_steps writes step-sequencer notes with
  pitch/velocity/pan into any channel and pattern with no keystroke at all -
  prefer it for drums, 808s and one-shot patterns.
- Channels: properties, mixer routing, fl_add_sample_channels (load samples
  from disk; find them with fl_find_samples, measure them - level, pitch
  for tuning 808s - with fl_analyze_audio)
- Mixer: volume (fader or dB), pan, mute, solo, names, colours, sends
  (fl_set_send for reverb buses), fl_get_mixer_routing, fl_list_effects,
  fl_get_mixer_levels (peak dBFS per track while playing)
- Plugins: fl_get_plugin_params, fl_set_plugin_param(name=...) or by
  index, presets. Loading new plugins or effects is impossible.
- Undo: fl_save_undo_point before a batch of live edits, fl_undo, fl_redo
- Windows: show, hide, focus; fl_open_piano_roll(channel)

Listening to the mix:
- fl_render_mix saves the project, renders the saved file to WAV with FL's
  command-line renderer (~20 s, plus ~20 s per stem; one render at a time)
  and measures it: LUFS, true peak, crest, band balance, stereo, silence,
  per-section figures (playlist markers or 8-bar blocks) and warnings. Run it
  after mix or arrangement changes to check the result.
- stems=[3, 5] / "buses" / "all" (max 8 per call) renders mixer inserts
  alone and compares them (masking, dominant or silent tracks). The WAVs can
  be played to the user with fl_preview_sample.

Piano roll rules:
- ComposeWithLLM must have been run once by hand in this FL Studio session
  (piano roll > Tools > Scripting); on macOS the app running this server
  needs Accessibility permission for the keystroke.
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

    fl_crashed is set when the running FL Studio wrote a crash log (macOS): its
    crash window is open even if FL still answers.
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
    out = {
        "connected": status.get("connected", False),
        "port_name": status.get("port_name"),
        "available_ports": status.get("available_ports", []),
        "error": status.get("error"),
    }
    if out["connected"]:
        log = errors.crash_window_log(force=True)
        if log is not None:
            out["fl_crashed"] = errors.crash_window_message(log)
        try:
            info = project_files.controller_info(conn)
            out["controller_version"] = info.get("controller_version")
            out["fl_version"] = info.get("fl_version")
            out["api_version"] = info.get("api_version")
            version = int(info.get("controller_version") or 0)
            if version < project_files.SAVE_CONTROLLER_VERSION:
                out["controller_warning"] = project_files.OUTDATED_FOR_SAVING.format(
                    version=version)
            elif version < project_files.LATEST_CONTROLLER_VERSION:
                if version < 5:
                    need = "step shift in fl_write_steps needs version 5"
                elif version < 6:
                    need = "version 6 checks written steps more strictly"
                else:
                    need = ("version 7 removes a new mixer route again when its level "
                            "cannot be set")
                out["controller_note"] = (
                    f"The controller script is at version {version}, the latest is "
                    f"{project_files.LATEST_CONTROLLER_VERSION}: {need}. Copy "
                    "fl_controller/device_FLStudioMCP.py into FL Studio again (setup.sh) and "
                    "restart FL Studio.")
        except Exception as e:  # noqa: BLE001 - a status report must not fail
            out["controller_warning"] = str(e)
    return out


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
register_arrangement_tools(mcp)
register_playlist_tools(mcp)
register_sample_tools(mcp)
register_render_tools(mcp)


def main():
    """Run the FL Studio MCP server."""
    # A render left unfinished by a server that died: put FL's shared files back.
    render.recover_at_startup()
    render.install_exit_handlers()
    mcp.run()


if __name__ == "__main__":
    main()
