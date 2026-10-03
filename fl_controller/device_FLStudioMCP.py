# name=FL Studio MCP Controller
# url=https://github.com/karl-andres/fl-studio-mcp
# supportedDevices=FL Studio MCP

"""
FL Studio MIDI Controller Script for MCP Integration.

This script runs inside FL Studio and receives MIDI trigger messages from the
MCP server. When triggered, it reads a command from a JSON file, executes the
corresponding FL Studio API function, and writes the response to another JSON file.

Communication flow:
1. MCP server writes command to mcp_command.json
2. MCP server sends MIDI note 127 (trigger)
3. This script receives the trigger via OnMidiMsg()
4. Script reads command JSON, executes FL Studio API
5. Script writes response to mcp_response.json
6. MCP server reads response
"""

import json
import os
import sys
from pathlib import Path

# FL Studio API modules (available when running inside FL Studio)
import channels
import midi
import mixer
import plugins
import transport


def _get_script_dir() -> Path:
    """Get the script directory path.

    FL Studio's Python environment doesn't support __file__, so we construct
    the path based on the platform's standard FL Studio settings location.
    """
    if sys.platform == "darwin":
        # macOS
        base = Path.home() / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    elif sys.platform == "win32":
        # Windows
        userprofile = os.environ.get("USERPROFILE", "~")
        base = Path(userprofile) / "Documents" / "Image-Line" / "FL Studio" / "Settings"
    else:
        # Linux (unlikely but handle it)
        base = Path.home() / "Documents" / "Image-Line" / "FL Studio" / "Settings"

    return base / "Hardware" / "FLStudioMCP"


# File paths for JSON communication
SCRIPT_DIR = _get_script_dir()
COMMAND_FILE = SCRIPT_DIR / "mcp_command.json"
RESPONSE_FILE = SCRIPT_DIR / "mcp_response.json"

# MIDI trigger note
TRIGGER_NOTE = 127

# Bumped whenever an action is added or changes; the MCP server reads it via
# system.info to detect an outdated script installed in FL Studio.
CONTROLLER_VERSION = 4

# Project loads seen since the script started (OnProjectLoad), so the server
# can wait for a reload it requested. The instance id changes when FL reloads
# the script itself, which resets the counter.
_load_count = 0
_last_load_status = None
_instance_id = "%x" % id(object())

# PME flags of the MIDI event that triggered the command being executed (see
# OnMidiMsg). FL Studio only allows some operations (saving, among others) from
# an event whose flags say it is safe; calling them without the flags makes FL
# write nothing and crash a few seconds later.
_pme_flags = None


def OnInit():
    """Called when the script is loaded."""
    print("FL Studio MCP Controller initialized")
    print(f"Command file: {COMMAND_FILE}")
    print(f"Response file: {RESPONSE_FILE}")


def OnDeInit():
    """Called when the script is unloaded."""
    print("FL Studio MCP Controller deinitialized")


def OnProjectLoad(status):
    """Count finished project loads (midi.PL_LoadOk = 100, PL_LoadError = 101)."""
    global _load_count, _last_load_status
    if status in (100, 101):
        _load_count += 1
        _last_load_status = "ok" if status == 100 else "error"


def OnMidiMsg(event):
    """Called when a MIDI message is received."""
    # Check for trigger note (Note On, note 127)
    global _pme_flags
    if event.midiId == 0x90 and event.data1 == TRIGGER_NOTE and event.data2 > 0:
        try:
            _pme_flags = event.pmeFlags
        except Exception:
            _pme_flags = None
        try:
            execute_pending_command()
        finally:
            _pme_flags = None
        event.handled = True


def OnIdle():
    """Called periodically when FL Studio is idle."""
    pass  # Required FL Studio API hook; no polling needed


# Id of the last executed command. A MIDI trigger queued while FL Studio was
# busy (the server has already timed out and written the next command) reads
# the current command file; without this guard that command would run twice.
_last_request_id = None


def execute_pending_command():
    """Read command from JSON file, execute it, and write response tagged with its id."""
    global _last_request_id
    response = {"id": None, "success": False, "error": None}

    try:
        if not COMMAND_FILE.exists():
            response["error"] = "No command file found"
            write_response(response)
            return

        command = json.loads(COMMAND_FILE.read_text())
        request_id = command.get("id")
        if request_id is not None and request_id == _last_request_id:
            # Late trigger for a command that already ran: its response is on disk.
            return
        _last_request_id = request_id
        response["id"] = request_id

        action = command.get("action", "")
        params = command.get("params", {})

        result = dispatch_command(action, params)
        # A handler that returns {"error": ...} has failed; do not mark it a success.
        response = {"id": command.get("id"), "success": not result.get("error"), **result}

    except json.JSONDecodeError as e:
        response["error"] = f"Invalid JSON in command file: {e}"
    except Exception as e:
        response["error"] = f"Error executing command: {e}"

    write_response(response)


# ---------------------------------------------------------------------------
# Pattern management (requires newer FL Studio, lazy-imported)
# ---------------------------------------------------------------------------

def _patterns_mod():
    """Lazily import the patterns module (not available on older FL versions)."""
    import patterns  # noqa: PLC0415
    return patterns


def _ui_mod():
    """Lazily import the ui module."""
    import ui  # noqa: PLC0415
    return ui


# FL window indices (midi.wid* constants) by the names the MCP tools accept.
_WINDOWS = {
    "mixer": 0,
    "channel_rack": 1,
    "playlist": 2,
    "piano_roll": 3,
    "browser": 4,
}


def _window_index(params: dict) -> int:
    """Resolve the 'window' parameter (name or index) to an FL window index."""
    window = params.get("window", "piano_roll")
    if isinstance(window, int) and not isinstance(window, bool):
        return window
    index = _WINDOWS.get(str(window).lower())
    if index is None:
        raise ValueError("Unknown window '%s'. Valid: %s" % (window, ", ".join(_WINDOWS)))
    return index


def _window_report(ui, index: int) -> dict:
    """Visibility/focus of one window plus the caption of whatever is focused."""
    name = next((n for n, i in _WINDOWS.items() if i == index), str(index))
    return {
        "window": name,
        "visible": bool(ui.getVisible(index)),
        "focused": bool(ui.getFocused(index)),
        "focused_caption": ui.getFocusedFormCaption(),
    }


def handle_ui_get_focus(params: dict) -> dict:
    """Report which FL windows are visible/focused and the focused window's caption.

    The caption of a focused piano roll is "Piano roll - <channel name>", which
    is the only way to learn which channel an open piano roll targets.
    """
    try:
        ui = _ui_mod()
        return {
            "focused": {n: bool(ui.getFocused(i)) for n, i in _WINDOWS.items()},
            "visible": {n: bool(ui.getVisible(i)) for n, i in _WINDOWS.items()},
            "caption": ui.getFocusedFormCaption(),
            "form_id": ui.getFocusedFormID(),
            "plugin": ui.getFocusedPluginName(),
        }
    except Exception as e:
        return {"error": str(e)}


def handle_ui_show_window(params: dict) -> dict:
    """Show an FL window and (by default) give it FL-internal focus."""
    try:
        ui = _ui_mod()
        index = _window_index(params)
        ui.showWindow(index)
        if params.get("focus", True):
            ui.setFocused(index)
        return _window_report(ui, index)
    except Exception as e:
        return {"error": str(e)}


def handle_ui_hide_window(params: dict) -> dict:
    """Hide an FL window."""
    try:
        ui = _ui_mod()
        index = _window_index(params)
        ui.hideWindow(index)
        return _window_report(ui, index)
    except Exception as e:
        return {"error": str(e)}


def handle_ui_set_focus(params: dict) -> dict:
    """Give an FL window FL-internal focus (without changing its visibility)."""
    try:
        ui = _ui_mod()
        index = _window_index(params)
        ui.setFocused(index)
        return _window_report(ui, index)
    except Exception as e:
        return {"error": str(e)}


def handle_pianoroll_open(params: dict) -> dict:
    """Retarget the piano roll to a channel and make it FL's focused window.

    An open piano roll ignores channels selected via scripting, and merely
    showing/focusing it does not help either. What does work (verified against
    FL 2026): hide the piano roll, select the channel, show it again - a
    freshly shown piano roll picks up the selected channel. Without "index"
    the piano roll is only shown and focused, its target is left alone.
    """
    try:
        ui = _ui_mod()
        pr = _WINDOWS["piano_roll"]
        index = params.get("index")
        if index is not None:
            index = int(index)
            if index < 0 or index >= channels.channelCount(True):
                return {"error": "Channel index %d out of range (0..%d)"
                        % (index, channels.channelCount(True) - 1)}
            ui.hideWindow(pr)
            channels.selectOneChannel(index, True)
        ui.showWindow(pr)
        ui.setFocused(pr)
        report = _window_report(ui, _WINDOWS["piano_roll"])
        if index is not None:
            report["channel"] = {"index": index, "name": channels.getChannelName(index, True)}
        return report
    except Exception as e:
        return {"error": str(e)}


def _pattern_index_error(patterns, index: int) -> dict | None:
    """Return an error dict for indices outside 1..patternMax, else None.

    FL silently creates or selects unexpected patterns for out-of-range
    indices instead of failing, so validate before calling into the API.
    """
    max_index = patterns.patternMax()
    if index < 1 or index > max_index:
        return {"error": f"Pattern index {index} out of range (1..{max_index})"}
    return None


def handle_patterns_get_count(params: dict) -> dict:
    try:
        patterns = _patterns_mod()
        return {
            "count": patterns.patternCount(),
            "current": patterns.patternNumber(),
            "max": patterns.patternMax(),
        }
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_get_all(params: dict) -> dict:
    try:
        patterns = _patterns_mod()
        include_default = params.get("include_default", False)
        max_index = patterns.patternMax()
        out = []
        for i in range(1, max_index + 1):
            is_default = patterns.isPatternDefault(i)
            if is_default and not include_default:
                continue
            out.append({
                "index": i,
                "name": patterns.getPatternName(i),
                "color": patterns.getPatternColor(i),
                "length_beats": patterns.getPatternLength(i),
                "is_default": is_default,
                "is_selected": patterns.isPatternSelected(i),
            })
        return {"patterns": out, "current": patterns.patternNumber()}
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_get_current(params: dict) -> dict:
    try:
        patterns = _patterns_mod()
        i = patterns.patternNumber()
        return {
            "index": i,
            "name": patterns.getPatternName(i),
            "length_beats": patterns.getPatternLength(i),
        }
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_select(params: dict) -> dict:
    try:
        patterns = _patterns_mod()
        index = int(params.get("index", 0))
        error = _pattern_index_error(patterns, index)
        if error:
            return error
        patterns.jumpToPattern(index)
        return {"selected": index, "name": patterns.getPatternName(index)}
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_new_empty(params: dict) -> dict:
    """Select the first/next empty pattern (automation-safe: no name prompt).

    Note: patterns are virtual in FL; this jumps to an unused one, which is
    the closest equivalent to "create new pattern".
    """
    try:
        patterns = _patterns_mod()
        FFNEP_DontPromptName = 1 << 1  # from midi module flags
        patterns.findFirstNextEmptyPat(FFNEP_DontPromptName)
        i = patterns.patternNumber()
        if params.get("name"):
            patterns.setPatternName(i, str(params["name"]))
        return {"selected": i, "name": patterns.getPatternName(i)}
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_clone(params: dict) -> dict:
    """Clone a pattern. NOTE: this closes the piano roll (FL behaviour)."""
    try:
        patterns = _patterns_mod()
        index = params.get("index")
        if index is None:
            patterns.clonePattern()
            new_index = patterns.patternNumber()
        else:
            error = _pattern_index_error(patterns, int(index))
            if error:
                return error
            patterns.clonePattern(int(index))
            new_index = patterns.patternNumber()
        return {"cloned_to": new_index, "name": patterns.getPatternName(new_index)}
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


def handle_patterns_set_name(params: dict) -> dict:
    try:
        patterns = _patterns_mod()
        index = int(params.get("index", 0))
        name = str(params.get("name", ""))
        error = _pattern_index_error(patterns, index)
        if error:
            return error
        patterns.setPatternName(index, name)
        return {"index": index, "name": patterns.getPatternName(index)}
    except ImportError:
        return {"error": "patterns module not available (requires FL Studio 2024+)"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Undo / redo safety net
# ---------------------------------------------------------------------------

def _general_mod():
    import general  # noqa: PLC0415
    return general


def handle_general_undo(params: dict) -> dict:
    try:
        general = _general_mod()
        result = general.undoUp()
        return {"undone": True, "result": result,
                "history_pos": general.getUndoHistoryPos(),
                "history_count": general.getUndoHistoryCount()}
    except ImportError:
        return {"error": "general module not available"}
    except Exception as e:
        return {"error": str(e)}


def handle_general_redo(params: dict) -> dict:
    try:
        general = _general_mod()
        result = general.undoDown()
        return {"redone": True, "result": result,
                "history_pos": general.getUndoHistoryPos(),
                "history_count": general.getUndoHistoryCount()}
    except ImportError:
        return {"error": "general module not available"}
    except Exception as e:
        return {"error": str(e)}


# UF flags (see FL API docs for general.saveUndo)
_UNDO_FLAGS = {
    "none": 0, "ee": 1, "pr": 2, "playlist": 4, "knob": 32,
    "audio_rec": 256, "auto_clip": 512, "pr_marker": 1024,
    "pl_marker": 2048, "plugin": 4096, "ss_looping": 8192, "reset": 65536,
}


def handle_general_save_undo_point(params: dict) -> dict:
    try:
        general = _general_mod()
        name = str(params.get("name", "MCP undo point"))
        flags_in = params.get("flags", ["pr", "playlist", "knob", "ss_looping"])
        if isinstance(flags_in, str):
            flags_in = [flags_in]
        unknown = [str(f) for f in flags_in if str(f).lower() not in _UNDO_FLAGS]
        if unknown:
            # Ignoring a flag silently would create an undo point that does not
            # cover what the caller assumed.
            return {"error": "Unknown undo flag(s): %s. Valid: %s"
                    % (", ".join(unknown), ", ".join(_UNDO_FLAGS))}
        flags = 0
        for f in flags_in:
            flags |= _UNDO_FLAGS[str(f).lower()]
        general.saveUndo(name, flags)
        return {"saved": True, "name": name, "flags": flags,
                "history_pos": general.getUndoHistoryPos(),
                "history_count": general.getUndoHistoryCount()}
    except ImportError:
        return {"error": "general module not available"}
    except Exception as e:
        return {"error": str(e)}


def handle_general_get_undo_status(params: dict) -> dict:
    try:
        general = _general_mod()
        return {
            "history_count": general.getUndoHistoryCount(),
            "history_pos": general.getUndoHistoryPos(),
            "history_last": general.getUndoHistoryLast(),
            "level_hint": general.getUndoLevelHint(),
            "changed_flag": general.getChangedFlag(),
            "safe_to_edit": general.safeToEdit(),
        }
    except ImportError:
        return {"error": "general module not available"}
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# Project snapshot (one round-trip overview)
# =============================================================================


def _snapshot_patterns() -> dict:
    listing = handle_patterns_get_all({"include_default": False})
    if listing.get("error"):
        raise RuntimeError(listing["error"])
    counts = handle_patterns_get_count({})
    if counts.get("error"):
        raise RuntimeError(counts["error"])
    return {"current": listing["current"], "count": counts["count"], "items": listing["patterns"]}


def handle_project_snapshot(params: dict) -> dict:
    """Transport, tempo, patterns, channels and mixer tracks in one response.

    Each section is computed on its own; one failing section becomes
    {"error": ...} without hiding the others.
    """

    def section(compute):
        try:
            return compute()
        except Exception as e:
            return {"error": str(e)}

    return {
        "transport": section(handle_transport_get_status),
        "tempo": section(_current_bpm),
        "patterns": section(_snapshot_patterns),
        "channels": section(lambda: handle_channels_get_all()["channels"]),
        "mixer_tracks": section(
            lambda: handle_mixer_get_all_tracks({"include_empty": False})["tracks"]
        ),
    }


def write_response(response: dict):
    """Write the response file in place.

    Never via a temp file + os.replace: in FL Studio 2026 os.replace returns
    NULL without setting an exception and leaves the embedded interpreter
    failing every later call. Serialise first so a bad value cannot truncate
    the file; the server only accepts a response carrying its own request id.
    """
    try:
        text = json.dumps(response, indent=2)
    except Exception as e:
        print(f"Error serialising response: {e}")
        return
    try:
        RESPONSE_FILE.write_text(text)
    except Exception as e:
        print(f"Error writing response: {e}")


def dispatch_command(action: str, params: dict) -> dict:
    """Route command to appropriate handler and return result."""

    # Transport commands
    if action == "transport.start":
        return handle_transport_start()
    elif action == "transport.stop":
        return handle_transport_stop()
    elif action == "transport.record":
        return handle_transport_record()
    elif action == "transport.getStatus":
        return handle_transport_get_status()
    elif action == "transport.setPosition":
        return handle_transport_set_position(params)
    elif action == "transport.getLength":
        return handle_transport_get_length()
    elif action == "transport.setLoopMode":
        return handle_transport_set_loop_mode(params)
    elif action == "transport.setPlaybackSpeed":
        return handle_transport_set_playback_speed(params)
    elif action == "transport.getTempo":
        return handle_transport_get_tempo()
    elif action == "transport.setTempo":
        return handle_transport_set_tempo(params)

    # Mixer commands
    elif action == "mixer.getTrackCount":
        return handle_mixer_get_track_count()
    elif action == "mixer.getTrackInfo":
        return handle_mixer_get_track_info(params)
    elif action == "mixer.getAllTracks":
        return handle_mixer_get_all_tracks(params)
    elif action == "mixer.setTrackVolume":
        return handle_mixer_set_track_volume(params)
    elif action == "mixer.setTrackPan":
        return handle_mixer_set_track_pan(params)
    elif action == "mixer.muteTrack":
        return handle_mixer_mute_track(params)
    elif action == "mixer.soloTrack":
        return handle_mixer_solo_track(params)
    elif action == "mixer.armTrack":
        return handle_mixer_arm_track(params)
    elif action == "mixer.setTrackName":
        return handle_mixer_set_track_name(params)
    elif action == "mixer.setTrackColor":
        return handle_mixer_set_track_color(params)
    elif action == "mixer.setStereoSep":
        return handle_mixer_set_stereo_sep(params)

    # Channel commands
    elif action == "channels.getCount":
        return handle_channels_get_count(params)
    elif action == "channels.getInfo":
        return handle_channels_get_info(params)
    elif action == "channels.getAll":
        return handle_channels_get_all()
    elif action == "channels.getSelected":
        return handle_channels_get_selected()
    elif action == "channels.select":
        return handle_channels_select(params)
    elif action == "channels.selectOne":
        return handle_channels_select_one(params)
    elif action == "channels.triggerNote":
        return handle_channels_trigger_note(params)
    elif action == "channels.setVolume":
        return handle_channels_set_volume(params)
    elif action == "channels.setPan":
        return handle_channels_set_pan(params)
    elif action == "channels.mute":
        return handle_channels_mute(params)
    elif action == "channels.solo":
        return handle_channels_solo(params)
    elif action == "channels.setName":
        return handle_channels_set_name(params)
    elif action == "channels.setColor":
        return handle_channels_set_color(params)
    elif action == "channels.routeToMixer":
        return handle_channels_route_to_mixer(params)

    # Step sequencer commands
    elif action == "channels.getGridBit":
        return handle_channels_get_grid_bit(params)
    elif action == "channels.setGridBit":
        return handle_channels_set_grid_bit(params)
    elif action == "channels.getStepSequence":
        return handle_channels_get_step_sequence(params)
    elif action == "channels.setStepSequence":
        return handle_channels_set_step_sequence(params)

    # Plugin commands
    elif action == "plugins.isValid":
        return handle_plugins_is_valid(params)
    elif action == "plugins.getName":
        return handle_plugins_get_name(params)
    elif action == "plugins.getParamCount":
        return handle_plugins_get_param_count(params)
    elif action == "plugins.getParams":
        return handle_plugins_get_params(params)
    elif action == "plugins.getParamValue":
        return handle_plugins_get_param_value(params)
    elif action == "plugins.setParamValue":
        return handle_plugins_set_param_value(params)
    elif action == "plugins.getPresetCount":
        return handle_plugins_get_preset_count(params)
    elif action == "plugins.nextPreset":
        return handle_plugins_next_preset(params)
    elif action == "plugins.prevPreset":
        return handle_plugins_prev_preset(params)
    elif action == "patterns.getCount":
        return handle_patterns_get_count(params)
    elif action == "patterns.getAll":
        return handle_patterns_get_all(params)
    elif action == "patterns.getCurrent":
        return handle_patterns_get_current(params)
    elif action == "patterns.select":
        return handle_patterns_select(params)
    elif action == "patterns.newEmpty":
        return handle_patterns_new_empty(params)
    elif action == "patterns.clone":
        return handle_patterns_clone(params)
    elif action == "patterns.setName":
        return handle_patterns_set_name(params)
    elif action == "general.undo":
        return handle_general_undo(params)
    elif action == "general.redo":
        return handle_general_redo(params)
    elif action == "general.saveUndoPoint":
        return handle_general_save_undo_point(params)
    elif action == "general.getUndoStatus":
        return handle_general_get_undo_status(params)

    # UI / window handling
    elif action == "ui.getFocus":
        return handle_ui_get_focus(params)
    elif action == "ui.showWindow":
        return handle_ui_show_window(params)
    elif action == "ui.hideWindow":
        return handle_ui_hide_window(params)
    elif action == "ui.setFocus":
        return handle_ui_set_focus(params)
    elif action == "pianoroll.open":
        return handle_pianoroll_open(params)
    elif action == "plugins.getColor":
        return handle_plugins_get_color(params)

    elif action == "project.snapshot":
        return handle_project_snapshot(params)

    elif action in _EXTRA_HANDLERS:
        return _EXTRA_HANDLERS[action](params)

    else:
        return {"error": f"Unknown action: {action}"}


# =============================================================================
# Transport Handlers
# =============================================================================


def handle_transport_start() -> dict:
    """Toggle play/pause."""
    transport.start()
    return {"is_playing": transport.isPlaying() == 1}


def handle_transport_stop() -> dict:
    """Stop playback."""
    transport.stop()
    return {"stopped": True}


def handle_transport_record() -> dict:
    """Toggle recording."""
    transport.record()
    return {"is_recording": transport.isRecording() == 1}


def handle_transport_get_status() -> dict:
    """Get transport status."""
    loop_mode = transport.getLoopMode()
    return {
        "is_playing": transport.isPlaying() == 1,
        "is_recording": transport.isRecording() == 1,
        "position": transport.getSongPosHint(),
        "loop_mode": "song" if loop_mode == 1 else "pattern",
    }


# transport.getSongLength / getSongPos / setSongPos modes (midi.SONGLENGTH_*).
SONGLENGTH_MS = 0
SONGLENGTH_S = 1
SONGLENGTH_ABSTICKS = 2
SONGLENGTH_BARS = 3
SONGLENGTH_STEPS = 4
SONGLENGTH_TICKS = 5


def _ppq() -> int:
    try:
        return int(_general_mod().getRecPPQ())
    except Exception:
        return 96


def handle_transport_set_position(params: dict) -> dict:
    """Set the playback position from absolute ticks or seconds (both int for FL)."""
    if "ticks" in params:
        transport.setSongPos(int(round(params["ticks"])), SONGLENGTH_ABSTICKS)
    elif "seconds" in params:
        transport.setSongPos(int(round(params["seconds"] * 1000)), SONGLENGTH_MS)
    else:
        return {"error": "setPosition needs 'ticks' or 'seconds'"}
    return {
        "position": transport.getSongPosHint(),
        "ticks": transport.getSongPos(SONGLENGTH_ABSTICKS),
        "ppq": _ppq(),
    }


def handle_transport_get_length() -> dict:
    """Song length in every unit FL reports (song mode arrangement, not the pattern)."""
    return {
        "ticks": transport.getSongLength(SONGLENGTH_ABSTICKS),
        "seconds": transport.getSongLength(SONGLENGTH_S),
        "milliseconds": transport.getSongLength(SONGLENGTH_MS),
        "bars": transport.getSongLength(SONGLENGTH_BARS),
        "steps": transport.getSongLength(SONGLENGTH_STEPS),
        "bar_ticks": transport.getSongLength(SONGLENGTH_TICKS),
        "ppq": _ppq(),
    }


def handle_transport_set_loop_mode(params: dict) -> dict:
    """Set loop mode."""
    mode = params.get("mode", "pattern")
    current_mode = transport.getLoopMode()
    target_mode = 1 if mode.lower() == "song" else 0

    if current_mode != target_mode:
        transport.setLoopMode()

    return {"mode": mode}


def handle_transport_set_playback_speed(params: dict) -> dict:
    """Set playback speed."""
    speed = params.get("speed", 1.0)
    transport.setPlaybackSpeed(speed)
    return {"speed": speed}


TEMPO_MIN = 10.0
TEMPO_MAX = 522.0


def _current_bpm() -> float:
    """Project tempo in BPM.

    FL Studio 2025 answers getCurrentTempo() in thousandths of a BPM (130 BPM
    -> 130000) although the API stubs promise a float BPM. A real tempo never
    exceeds TEMPO_MAX, so anything above 1000 is thousandths.
    """
    raw = mixer.getCurrentTempo()
    return raw / 1000.0 if raw > 1000 else float(raw)


def handle_transport_get_tempo() -> dict:
    """Current project tempo in BPM."""
    return {"tempo": _current_bpm()}


def handle_transport_set_tempo(params: dict) -> dict:
    """Set the project tempo through the REC event (the only scripting route).

    processRECEvent is documented as fragile, so the tempo is read back and
    `applied` tells the caller whether FL Studio really took the value.
    """
    bpm = float(params.get("bpm", 0))
    if not TEMPO_MIN <= bpm <= TEMPO_MAX:
        return {"error": "Tempo %g out of range (%g..%g BPM)" % (bpm, TEMPO_MIN, TEMPO_MAX)}
    general = _general_mod()
    general.processRECEvent(
        midi.REC_Tempo,
        int(round(bpm * 1000)),
        midi.REC_Control | midi.REC_UpdateValue | midi.REC_UpdateControl,
    )
    current = _current_bpm()
    return {"requested": bpm, "tempo": current, "applied": abs(current - bpm) < 0.01}


# =============================================================================
# Mixer Handlers
# =============================================================================


def handle_mixer_get_track_count() -> dict:
    """Get number of mixer tracks."""
    return {"count": mixer.trackCount()}


def handle_mixer_get_track_info(params: dict) -> dict:
    """Get info about a mixer track."""
    track = params.get("track", 0)
    error = _track_error(track)
    if error:
        return {"error": error}
    return {
        "index": track,
        "name": mixer.getTrackName(track),
        "volume": mixer.getTrackVolume(track),
        "volume_db": mixer.getTrackVolume(track, 1),
        "pan": mixer.getTrackPan(track),
        "stereo_separation": mixer.getTrackStereoSep(track),
        "is_muted": mixer.isTrackMuted(track) == 1,
        "is_solo": mixer.isTrackSolo(track) == 1,
        "is_armed": mixer.isTrackArmed(track) == 1,
        "color": hex(mixer.getTrackColor(track)),
    }


def handle_mixer_get_all_tracks(params: dict) -> dict:
    """Get info about all mixer tracks."""
    include_empty = params.get("include_empty", False)
    tracks = []
    track_count = mixer.trackCount()

    for i in range(track_count):
        name = mixer.getTrackName(i)

        # Skip empty tracks if requested
        if not include_empty and (not name or name.startswith("Insert ")):
            if i != 0:  # Always include master
                continue

        tracks.append({
            "index": i,
            "name": name if name else ("Master" if i == 0 else f"Insert {i}"),
            "volume": mixer.getTrackVolume(i),
            "pan": mixer.getTrackPan(i),
            "is_muted": mixer.isTrackMuted(i) == 1,
            "is_solo": mixer.isTrackSolo(i) == 1,
        })

    return {"track_count": track_count, "tracks": tracks}


# A fader position FL reports back differs from the one just set by less than this.
_FADER_TOLERANCE = 0.005


def _stable_volume(track: int, mode: int = 0) -> float:
    """Read a track's volume until two consecutive reads agree (at most 6 reads).

    On an automated fader FL has been seen answering the previous value first.
    """
    last = mixer.getTrackVolume(track, mode)
    for _ in range(5):
        current = mixer.getTrackVolume(track, mode)
        if abs(current - last) < 1e-6:
            return current
        last = current
    return last


def _automation_error(track: int, wanted, reported: float) -> dict:
    return {
        "error": "Mixer track %d's volume did not follow: set to %s, FL Studio reports %.3f. "
                 "The fader is probably automated (an automation clip, or a controller "
                 "linked to it, drives the value; FL keeps an automation clip's last value "
                 "after the clip ends). Edit or delete the automation clip "
                 "(fl_edit_arrangement), or unlink the controller, then retry."
                 % (track, wanted, reported),
        "automated": True,
    }


def _set_fader(track: int, value: float):
    """Set the fader (no pickup); {"got": reported} when FL reports another value, else None."""
    mixer.setTrackVolume(track, value, midi.PIM_None)
    got = _stable_volume(track)
    if abs(got - value) > _FADER_TOLERANCE:
        return {"got": got}
    return None


def _set_track_db(track: int, target_db: float):
    """Bisect the fader position that gives target_db (FL's curve is not linear).

    Each step checks that FL reports the fader position just set; a fader that
    does not follow (automation) stops the search, puts the fader back where
    it was and returns an error instead of walking to the wrong end.
    """
    original = _stable_volume(track)
    lo, hi = 0.0, 1.0
    for _ in range(30):
        mid = (lo + hi) / 2
        mismatch = _set_fader(track, mid)
        if mismatch:
            mixer.setTrackVolume(track, original, midi.PIM_None)
            return _automation_error(track, "%.3f (searching for %g dB)" % (mid, target_db),
                                     mismatch["got"])
        if _stable_volume(track, 1) < target_db:
            lo = mid
        else:
            hi = mid
    mismatch = _set_fader(track, hi)
    if mismatch:
        mixer.setTrackVolume(track, original, midi.PIM_None)
        return _automation_error(track, "%.3f (%g dB)" % (hi, target_db), mismatch["got"])
    return None


def handle_mixer_set_track_volume(params: dict) -> dict:
    """Set mixer track volume (0-1 fader position, or 'db'); pickup mode off."""
    track = params.get("track", 0)
    error = _track_error(track)
    if error:
        return {"error": error}
    if "db" in params:
        failure = _set_track_db(track, float(params["db"]))
    else:
        # Explicit PIM_None: with pickup (takeover) FL ignores a jump in value.
        volume = float(params.get("volume", 0.8))
        mismatch = _set_fader(track, volume)
        failure = _automation_error(track, "%.3f" % volume, mismatch["got"]) if mismatch else None
    if failure:
        return failure
    return {
        "volume": _stable_volume(track),
        "volume_db": _stable_volume(track, 1),
        "track_name": mixer.getTrackName(track),
    }


def handle_mixer_set_track_pan(params: dict) -> dict:
    """Set mixer track pan (-1..1); pickup mode off."""
    track = params.get("track", 0)
    error = _track_error(track)
    if error:
        return {"error": error}
    mixer.setTrackPan(track, float(params.get("pan", 0.0)), midi.PIM_None)
    return {"pan": mixer.getTrackPan(track), "track_name": mixer.getTrackName(track)}


def handle_mixer_mute_track(params: dict) -> dict:
    """Mute/unmute mixer track."""
    track = params.get("track", 0)
    error = _track_error(track)
    if error:
        return {"error": error}
    muted = params.get("muted")  # None = toggle

    if muted is None:
        mixer.muteTrack(track, -1)
    else:
        mixer.muteTrack(track, 1 if muted else 0)

    return {
        "is_muted": mixer.isTrackMuted(track) == 1,
        "track_name": mixer.getTrackName(track),
    }


def handle_mixer_solo_track(params: dict) -> dict:
    """Solo/unsolo mixer track."""
    track = params.get("track", 0)
    error = _track_error(track)
    if error:
        return {"error": error}
    solo = params.get("solo")  # None = toggle
    mode = params.get("mode", 3)

    if solo is None:
        mixer.soloTrack(track, -1, mode)
    else:
        mixer.soloTrack(track, 1 if solo else 0, mode)

    return {
        "is_solo": mixer.isTrackSolo(track) == 1,
        "track_name": mixer.getTrackName(track),
    }


def handle_mixer_arm_track(params: dict) -> dict:
    """Toggle arm state of mixer track."""
    track = params.get("track", 0)
    error = _track_error(track)
    if error:
        return {"error": error}
    mixer.armTrack(track)
    return {
        "is_armed": mixer.isTrackArmed(track) == 1,
        "track_name": mixer.getTrackName(track),
    }


def handle_mixer_set_track_name(params: dict) -> dict:
    """Set mixer track name."""
    track = params.get("track", 0)
    error = _track_error(track)
    if error:
        return {"error": error}
    name = params.get("name", "")
    mixer.setTrackName(track, name)
    return {"name": name}


def handle_mixer_set_track_color(params: dict) -> dict:
    """Set mixer track color."""
    track = params.get("track", 0)
    error = _track_error(track)
    if error:
        return {"error": error}
    r = params.get("r", 0)
    g = params.get("g", 0)
    b = params.get("b", 0)
    # FL Studio's scripting API takes 0xRRGGBB
    color = (r << 16) | (g << 8) | b
    mixer.setTrackColor(track, color)
    return {"color": f"RGB({r}, {g}, {b})"}


def handle_mixer_set_stereo_sep(params: dict) -> dict:
    """Set mixer track stereo separation."""
    track = params.get("track", 0)
    error = _track_error(track)
    if error:
        return {"error": error}
    separation = params.get("separation", 0.0)
    mixer.setTrackStereoSep(track, separation)
    return {"separation": separation}


# =============================================================================
# Channel Handlers
# =============================================================================


def handle_channels_get_count(params: dict) -> dict:
    """Get number of channels."""
    global_count = params.get("global_count", True)
    return {"count": channels.channelCount(global_count)}


def handle_channels_get_info(params: dict) -> dict:
    """Get info about a channel."""
    index = params.get("index", 0)
    use_global = params.get("use_global", True)

    return {
        "index": index,
        "name": channels.getChannelName(index, use_global),
        "color": hex(channels.getChannelColor(index, use_global)),
        "volume": channels.getChannelVolume(index, useGlobalIndex=use_global),
        "pan": channels.getChannelPan(index, use_global),
        "pitch": channels.getChannelPitch(index, useGlobalIndex=use_global),
        **_channel_pitch_detail(index, use_global),
        "is_muted": channels.isChannelMuted(index, use_global) == 1,
        "is_solo": channels.isChannelSolo(index, use_global) == 1,
        "is_selected": channels.isChannelSelected(index, use_global) == 1,
        "target_fx_track": channels.getTargetFxTrack(index, use_global),
        **(_channel_kind(index) if use_global else {}),
    }


def _channel_pitch_detail(index, use_global) -> dict:
    """Pitch knob in cents and its range in semitones (pitch alone is -1..1)."""
    try:
        return {"pitch_cents": channels.getChannelPitch(index, 1, use_global),
                "pitch_range": channels.getChannelPitch(index, 2, use_global)}
    except Exception:  # older API: mode not supported
        return {}


# channels.getChannelType() values (midi.CT_*).
_CHANNEL_TYPES = {0: "sampler", 1: "hybrid", 2: "plugin", 3: "layer", 4: "audio_clip",
                  5: "automation_clip"}


def _channel_kind(index: int) -> dict:
    """Channel type and, for a generator plugin, its name (best effort)."""
    out = {}
    try:
        kind = channels.getChannelType(index, True)
        out["type"] = _CHANNEL_TYPES.get(kind, "unknown")
    except Exception:
        return out
    try:
        if kind in (1, 2) and plugins.isValid(index, -1, True):
            out["plugin"] = plugins.getPluginName(index, -1, False, True)
    except Exception:
        pass
    return out


def handle_channels_get_all() -> dict:
    """Get info about all channels."""
    channels_list = []
    count = channels.channelCount(True)

    for i in range(count):
        channels_list.append({
            **_channel_kind(i),
            "index": i,
            "name": channels.getChannelName(i, True),
            "volume": channels.getChannelVolume(i, useGlobalIndex=True),
            "pan": channels.getChannelPan(i, True),
            "is_muted": channels.isChannelMuted(i, True) == 1,
            "is_solo": channels.isChannelSolo(i, True) == 1,
            "is_selected": channels.isChannelSelected(i, True) == 1,
            "target_fx_track": channels.getTargetFxTrack(i, True),
        })

    return {"channels": channels_list}


def handle_channels_get_selected() -> dict:
    """Get currently selected channel."""
    index = channels.selectedChannel(canBeNone=True, indexGlobal=True)

    if index is None or index < 0:
        return {"channel": None}

    return {
        "channel": {
            "index": index,
            "name": channels.getChannelName(index, True),
            "volume": channels.getChannelVolume(index, useGlobalIndex=True),
            "pan": channels.getChannelPan(index, True),
            "is_muted": channels.isChannelMuted(index, True) == 1,
            "is_solo": channels.isChannelSolo(index, True) == 1,
        }
    }


def handle_channels_select(params: dict) -> dict:
    """Select/deselect a channel."""
    index = params.get("index", 0)
    select = params.get("select", True)
    channels.selectChannel(index, 1 if select else 0, True)
    return {
        "selected": select,
        "channel_name": channels.getChannelName(index, True),
    }


def handle_channels_select_one(params: dict) -> dict:
    """Select only one channel, deselecting others."""
    index = params.get("index", 0)
    channels.selectOneChannel(index, True)
    return {"channel_name": channels.getChannelName(index, True)}


def handle_channels_trigger_note(params: dict) -> dict:
    """Trigger a MIDI note on a channel."""
    channel = params.get("channel", 0)
    note = params.get("note", 60)
    velocity = params.get("velocity", 100)
    midi_channel = params.get("midi_channel", -1)
    channels.midiNoteOn(channel, note, velocity, midi_channel)
    return {"triggered": True, "note": note, "velocity": velocity}


def handle_channels_set_volume(params: dict) -> dict:
    """Set channel volume."""
    index = params.get("index", 0)
    volume = params.get("volume", 0.8)
    # 3rd argument is pickupMode, not useGlobalIndex (same trap as setParamValue).
    channels.setChannelVolume(index, volume, midi.PIM_None, True)
    return {
        "volume": channels.getChannelVolume(index, useGlobalIndex=True),
        "channel_name": channels.getChannelName(index, True),
    }


def handle_channels_set_pan(params: dict) -> dict:
    """Set channel pan."""
    index = params.get("index", 0)
    pan = params.get("pan", 0.0)
    channels.setChannelPan(index, pan, midi.PIM_None, True)
    return {
        "pan": channels.getChannelPan(index, True),
        "channel_name": channels.getChannelName(index, True),
    }


def handle_channels_mute(params: dict) -> dict:
    """Mute/unmute channel."""
    index = params.get("index", 0)
    muted = params.get("muted")  # None = toggle

    if muted is None:
        channels.muteChannel(index, -1, True)
    else:
        channels.muteChannel(index, 1 if muted else 0, True)

    return {
        "is_muted": channels.isChannelMuted(index, True) == 1,
        "channel_name": channels.getChannelName(index, True),
    }


def handle_channels_solo(params: dict) -> dict:
    """Solo/unsolo channel."""
    index = params.get("index", 0)
    solo = params.get("solo")  # None = toggle

    if solo is None:
        channels.soloChannel(index, -1, True)
    else:
        channels.soloChannel(index, 1 if solo else 0, True)

    return {
        "is_solo": channels.isChannelSolo(index, True) == 1,
        "channel_name": channels.getChannelName(index, True),
    }


def handle_channels_set_name(params: dict) -> dict:
    """Set channel name."""
    index = params.get("index", 0)
    name = params.get("name", "")
    channels.setChannelName(index, name, True)
    return {"name": name}


def handle_channels_set_color(params: dict) -> dict:
    """Set channel color."""
    index = params.get("index", 0)
    r = params.get("r", 0)
    g = params.get("g", 0)
    b = params.get("b", 0)
    # FL Studio's scripting API takes 0xRRGGBB
    color = (r << 16) | (g << 8) | b
    channels.setChannelColor(index, color, True)
    return {"color": f"RGB({r}, {g}, {b})"}


def handle_channels_route_to_mixer(params: dict) -> dict:
    """Route channel to mixer track."""
    channel_index = params.get("channel_index", 0)
    mixer_track = params.get("mixer_track", 0)
    channels.setTargetFxTrack(channel_index, mixer_track, True)
    return {
        "channel_name": channels.getChannelName(channel_index, True),
        "mixer_track": mixer_track,
    }


# =============================================================================
# Step Sequencer Handlers
# =============================================================================


def handle_channels_get_grid_bit(params: dict) -> dict:
    """Get whether a step is active."""
    channel = params.get("channel", 0)
    position = params.get("position", 0)
    return {"value": channels.getGridBit(channel, position, True) == 1}


def handle_channels_set_grid_bit(params: dict) -> dict:
    """Set a step on or off."""
    channel = params.get("channel", 0)
    position = params.get("position", 0)
    value = params.get("value", False)
    channels.setGridBit(channel, position, 1 if value else 0, True)
    return {
        "value": value,
        "channel_name": channels.getChannelName(channel, True),
    }


def handle_channels_get_step_sequence(params: dict) -> dict:
    """Get step sequence for a channel."""
    channel = params.get("channel", 0)
    steps = params.get("steps", 16)
    sequence = []

    for i in range(steps):
        sequence.append(channels.getGridBit(channel, i, True) == 1)

    return {"sequence": sequence}


def handle_channels_set_step_sequence(params: dict) -> dict:
    """Set complete step sequence for a channel."""
    channel = params.get("channel", 0)
    pattern = params.get("pattern", [])

    for i, value in enumerate(pattern):
        channels.setGridBit(channel, i, 1 if value else 0, True)

    active_steps = sum(pattern)
    return {
        "active_steps": active_steps,
        "total_steps": len(pattern),
        "channel_name": channels.getChannelName(channel, True),
    }


# =============================================================================
# Plugin Handlers
# =============================================================================


def handle_plugins_is_valid(params: dict) -> dict:
    """Check if plugin exists at location."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        valid = plugins.isValid(index, slot_index, True)
    else:
        valid = plugins.isValid(index, -1, use_global)

    return {"valid": valid == 1}


def handle_plugins_get_name(params: dict) -> dict:
    """Get plugin name."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        name = plugins.getPluginName(index, slot_index, useGlobalIndex=True)
    else:
        name = plugins.getPluginName(index, -1, useGlobalIndex=use_global)

    return {"name": name}


def handle_plugins_get_param_count(params: dict) -> dict:
    """Get number of plugin parameters."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        count = plugins.getParamCount(index, slot_index, True)
    else:
        count = plugins.getParamCount(index, -1, use_global)

    return {"count": count}


def _plugin_target(params: dict) -> tuple:
    """(index, slot_index, use_global) as the plugins module expects them.

    A mixer effect slot always uses global indexing; a channel rack plugin
    (slot_index -1) honours use_global.
    """
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    if slot_index >= 0:
        return index, slot_index, True
    return index, -1, params.get("use_global", True)


def handle_plugins_get_params(params: dict) -> dict:
    """Plugin identity plus its parameters; an empty location is not an error."""
    index, slot_index, use_global = _plugin_target(params)
    max_params = params.get("max_params", 50)

    if not plugins.isValid(index, slot_index, use_global):
        return {"valid": False, "params": []}

    param_count = plugins.getParamCount(index, slot_index, use_global)
    param_list = []
    for i in range(min(param_count, max_params)):
        try:
            param_list.append({
                "index": i,
                "name": plugins.getParamName(i, index, slot_index, use_global),
                "value": plugins.getParamValue(i, index, slot_index, use_global),
                "value_string": plugins.getParamValueString(
                    i, index, slot_index, useGlobalIndex=use_global
                ),
            })
        except Exception as e:
            print(f"Warning: could not read param {i}: {e}")

    return {
        "valid": True,
        "name": plugins.getPluginName(index, slot_index, useGlobalIndex=use_global),
        "param_count": param_count,
        "preset_count": plugins.getPresetCount(index, slot_index, use_global),
        "color": hex(plugins.getColor(index, slot_index, useGlobalIndex=use_global)),
        "params": param_list,
    }


def handle_plugins_get_param_value(params: dict) -> dict:
    """Get specific parameter value."""
    param_index = params.get("param_index", 0)
    plugin_index = params.get("plugin_index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        name = plugins.getParamName(param_index, plugin_index, slot_index, True)
        value = plugins.getParamValue(param_index, plugin_index, slot_index, True)
        value_str = plugins.getParamValueString(
            param_index, plugin_index, slot_index, useGlobalIndex=True
        )
    else:
        name = plugins.getParamName(param_index, plugin_index, -1, use_global)
        value = plugins.getParamValue(param_index, plugin_index, -1, use_global)
        value_str = plugins.getParamValueString(
            param_index, plugin_index, -1, useGlobalIndex=use_global
        )

    return {
        "index": param_index,
        "name": name,
        "value": value,
        "value_string": value_str,
    }


def handle_plugins_set_param_value(params: dict) -> dict:
    """Set plugin parameter value."""
    param_index = params.get("param_index", 0)
    value = params.get("value", 0.0)
    plugin_index = params.get("plugin_index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    # setParamValue's 5th argument is pickupMode, NOT useGlobalIndex:
    # setParamValue(value, paramIndex, index, slotIndex, pickupMode, useGlobalIndex).
    # Passing True there enables pickup mode and the value is silently ignored.
    pim_none = midi.PIM_None
    if slot_index >= 0:
        name = plugins.getParamName(param_index, plugin_index, slot_index, True)
        plugins.setParamValue(value, param_index, plugin_index, slot_index, pim_none, True)
        new_value = plugins.getParamValue(param_index, plugin_index, slot_index, True)
        value_str = plugins.getParamValueString(
            param_index, plugin_index, slot_index, useGlobalIndex=True
        )
    else:
        name = plugins.getParamName(param_index, plugin_index, -1, use_global)
        plugins.setParamValue(value, param_index, plugin_index, -1, pim_none, use_global)
        new_value = plugins.getParamValue(param_index, plugin_index, -1, use_global)
        value_str = plugins.getParamValueString(
            param_index, plugin_index, -1, useGlobalIndex=use_global
        )

    return {
        "name": name,
        "value": new_value,
        "value_string": value_str,
    }


def handle_plugins_get_preset_count(params: dict) -> dict:
    """Get number of plugin presets."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        count = plugins.getPresetCount(index, slot_index, True)
    else:
        count = plugins.getPresetCount(index, -1, use_global)

    return {"count": count}


def handle_plugins_next_preset(params: dict) -> dict:
    """Switch to next preset."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        plugin_name = plugins.getPluginName(index, slot_index, useGlobalIndex=True)
        plugins.nextPreset(index, slot_index, True)
    else:
        plugin_name = plugins.getPluginName(index, -1, useGlobalIndex=use_global)
        plugins.nextPreset(index, -1, use_global)

    return {"plugin_name": plugin_name}


def handle_plugins_prev_preset(params: dict) -> dict:
    """Switch to previous preset."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        plugin_name = plugins.getPluginName(index, slot_index, useGlobalIndex=True)
        plugins.prevPreset(index, slot_index, True)
    else:
        plugin_name = plugins.getPluginName(index, -1, useGlobalIndex=use_global)
        plugins.prevPreset(index, -1, use_global)

    return {"plugin_name": plugin_name}


def handle_plugins_get_color(params: dict) -> dict:
    """Get plugin color."""
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    use_global = params.get("use_global", True)

    if slot_index >= 0:
        color = plugins.getColor(index, slot_index, useGlobalIndex=True)
    else:
        color = plugins.getColor(index, -1, useGlobalIndex=use_global)

    return {"color": hex(color)}


# =============================================================================
# System / project handlers
# =============================================================================


def _playlist_mod():
    import playlist  # noqa: PLC0415
    return playlist


def _arrangement_mod():
    import arrangement  # noqa: PLC0415
    return arrangement


def handle_system_info(params: dict) -> dict:
    """Script version, API/FL versions, window title, changed flag and load counter."""
    general = _general_mod()
    info = {
        "controller_version": CONTROLLER_VERSION,
        "instance_id": _instance_id,
        "load_count": _load_count,
        "last_load_status": _last_load_status,
        "api_version": general.getVersion(),
        "changed": general.getChangedFlag(),
        "ppq": _ppq(),
    }
    try:
        ui = _ui_mod()
        info["fl_version"] = ui.getVersion(4)
        info["title"] = ui.getProgTitle()
    except Exception as e:
        info["ui_error"] = str(e)
    return info


SAVE_BUSY_ERROR = "FL Studio is busy and cannot save right now; retry in a moment"


def _system_safe() -> bool:
    """True when the triggering MIDI event allows critical operations (PME_System_Safe)."""
    return _pme_flags is not None and bool(_pme_flags & midi.PME_System_Safe)


def handle_transport_save(params: dict) -> dict:
    """Save the project (Ctrl+S), or save a new numbered version with new_version.

    FL must get the triggering event's PME flags: without them (or when FL says
    the moment is not safe, e.g. a modal window is open) it often writes
    nothing and crashes seconds later, so the save is refused instead.
    """
    if not _system_safe():
        return {"error": SAVE_BUSY_ERROR, "busy": True}
    general = _general_mod()
    command = midi.FPT_SaveNew if params.get("new_version") else midi.FPT_Save
    transport.globalTransport(command, 1, _pme_flags)
    return {"changed": general.getChangedFlag()}


# =============================================================================
# Mixer routing, effects and levels
# =============================================================================


def _track_error(track) -> str | None:
    if not isinstance(track, int) or isinstance(track, bool):
        return "track must be an integer, got %r" % (track,)
    if track < 0 or track >= mixer.trackCount():
        return "Mixer track %d out of range (0..%d)" % (track, mixer.trackCount() - 1)
    return None


def _routes_from(track: int) -> list:
    routes = []
    for dest in range(mixer.trackCount()):
        if dest != track and mixer.getRouteSendActive(track, dest):
            route = {"to": dest, "name": mixer.getTrackName(dest)}
            try:
                route["level"] = mixer.getRouteToLevel(track, dest)
            except Exception:
                pass  # API < 36
            routes.append(route)
    return routes


def _route_path(start: int, goal: int):
    """Tracks from start to goal following active routes ([start, ..., goal]), or None."""
    count = mixer.trackCount()
    previous = {start: None}
    queue = [start]
    while queue:
        track = queue.pop(0)
        if track == goal:
            path = []
            while track is not None:
                path.append(track)
                track = previous[track]
            return path[::-1]
        for dest in range(count):
            if dest not in previous and dest != track and mixer.getRouteSendActive(track, dest):
                previous[dest] = track
                queue.append(dest)
    return None


def handle_mixer_get_routing(params: dict) -> dict:
    """Where a track sends its signal (with levels on API 36+)."""
    track = params.get("track", 0)
    error = _track_error(track)
    if error:
        return {"error": error}
    return {"track": track, "name": mixer.getTrackName(track), "routes": _routes_from(track)}


def handle_mixer_set_route(params: dict) -> dict:
    """Create/remove a send from one mixer track to another, optionally with a level."""
    source = params.get("source")
    dest = params.get("dest")
    for t in (source, dest):
        error = _track_error(t)
        if error:
            return {"error": error}
    if source == dest:
        return {"error": "A track cannot be routed to itself"}
    enabled = bool(params.get("enabled", True))
    if enabled and not mixer.getRouteSendActive(source, dest):
        path = _route_path(dest, source)
        if path:
            # FL silently refuses a route that would close a loop.
            return {"error": "FL refused the route: insert %d already feeds insert %d "
                             "(feedback loop: %s)" % (dest, source, " -> ".join(
                                 str(t) for t in path + [dest]))}
    # The route only exists once FL has processed the routing change, and a
    # level can only be set on an existing route.
    mixer.setRouteTo(source, dest, enabled)
    mixer.afterRoutingChanged()
    level_applied = None
    active = bool(mixer.getRouteSendActive(source, dest))
    if enabled and not active:
        return {"error": "FL Studio did not create the route from insert %d to insert %d"
                         % (source, dest), "active": False}
    if enabled and params.get("level") is not None:
        try:
            mixer.setRouteToLevel(source, dest, float(params["level"]))
            level_applied = mixer.getRouteToLevel(source, dest)
        except Exception as e:
            return {"error": "Route created, but its level could not be set (needs FL "
                             "Studio 2025 / API 36+): %s" % e}
    return {
        "source": source,
        "dest": dest,
        "active": bool(mixer.getRouteSendActive(source, dest)),
        "level": level_applied,
        "routes": _routes_from(source),
    }


def handle_mixer_get_effects(params: dict) -> dict:
    """The 10 effect slots of a mixer track: plugin name, parameter count."""
    track = params.get("track", 0)
    error = _track_error(track)
    if error:
        return {"error": error}
    slots = []
    for slot in range(10):
        if not plugins.isValid(track, slot, True):
            continue
        slots.append({
            "slot": slot,
            "plugin": plugins.getPluginName(track, slot, False, True),
            "name": plugins.getPluginName(track, slot, True, True),
            "param_count": plugins.getParamCount(track, slot, True),
        })
    enabled = None
    try:
        enabled = bool(mixer.isTrackSlotsEnabled(track))
    except Exception:
        pass
    return {"track": track, "name": mixer.getTrackName(track), "slots_enabled": enabled,
            "effects": slots}


def handle_mixer_get_peaks(params: dict) -> dict:
    """Current peak (max of L/R, 1.0 = 0 dBFS) of the given tracks, or all of them."""
    tracks = params.get("tracks")
    if tracks is None:
        tracks = list(range(mixer.trackCount()))
    for track in tracks:
        error = _track_error(track)
        if error:
            return {"error": error}
    return {"peaks": {str(t): mixer.getTrackPeaks(t, 2) for t in tracks},
            "playing": transport.isPlaying() == 1}


# =============================================================================
# Plugin parameters by name
# =============================================================================


def handle_plugins_set_param_by_name(params: dict) -> dict:
    """Set a plugin parameter found by (case-insensitive) name instead of index."""
    index, slot_index, use_global = _plugin_target(params)
    wanted = str(params.get("name", "")).strip().lower()
    if not wanted:
        return {"error": "name is required"}
    if not plugins.isValid(index, slot_index, use_global):
        return {"error": "No plugin at this location"}
    count = plugins.getParamCount(index, slot_index, use_global)
    names = []
    for i in range(count):
        name = plugins.getParamName(i, index, slot_index, use_global)
        if name:
            names.append((i, name))
    exact = [p for p in names if p[1].strip().lower() == wanted]
    partial = [p for p in names if wanted in p[1].lower()]
    matches = exact or partial
    if not matches:
        sample = ", ".join(n for _, n in names[:40])
        return {"error": "No parameter named %r. Parameters: %s" % (params.get("name"), sample)}
    if len(matches) > 1 and not exact:
        return {"error": "%r is ambiguous: %s" % (
            params.get("name"), ", ".join("%d=%s" % m for m in matches[:20]))}
    param_index = matches[0][0]
    value = float(params.get("value", 0.0))
    plugins.setParamValue(value, param_index, index, slot_index, midi.PIM_None, use_global)
    return {
        "param_index": param_index,
        "name": matches[0][1],
        "value": plugins.getParamValue(param_index, index, slot_index, use_global),
        "value_string": plugins.getParamValueString(
            param_index, index, slot_index, useGlobalIndex=use_global),
        "plugin": plugins.getPluginName(index, slot_index, useGlobalIndex=use_global),
    }


# =============================================================================
# Step sequencer with per-step pitch, velocity, pan...
# =============================================================================

# midi.pPitch ... midi.pShift
_STEP_PARAMS = {"pitch": 0, "velocity": 1, "release": 2, "fine_pitch": 3, "pan": 4,
                "mod_x": 5, "mod_y": 6, "shift": 7}


def _channel_error(index) -> str | None:
    if not isinstance(index, int) or isinstance(index, bool):
        return "channel must be an integer, got %r" % (index,)
    count = channels.channelCount(True)
    if index < 0 or index >= count:
        return "Channel index %d out of range (0..%d)" % (index, count - 1)
    return None


def _select_pattern_param(params: dict):
    """Jump to params['pattern'] when given; returns (pattern, error)."""
    patterns = _patterns_mod()
    pattern = params.get("pattern")
    if pattern is None:
        return patterns.patternNumber(), None
    error = _pattern_index_error(patterns, int(pattern))
    if error:
        return None, error["error"]
    if patterns.patternNumber() != int(pattern):
        patterns.jumpToPattern(int(pattern))
    return int(pattern), None


def handle_channels_set_steps(params: dict) -> dict:
    """Write steps with pitch/velocity/pan... into a channel of any pattern.

    Unlike the piano roll this needs no keystroke: it goes through the step
    sequencer (one note per 16th step and channel).
    """
    channel = params.get("channel")
    error = _channel_error(channel)
    if error:
        return {"error": error}
    pattern, error = _select_pattern_param(params)
    if error:
        return {"error": error}
    steps = params.get("steps") or []
    if params.get("clear", True):
        pattern_steps = int(round(_patterns_mod().getPatternLength(pattern) * 4))
        clear_to = max([pattern_steps] + [int(s["step"]) + 1 for s in steps])
        for i in range(clear_to):
            channels.setGridBit(channel, i, 0, True)
    for s in steps:
        step = int(s["step"])
        channels.setGridBit(channel, step, 1, True)
        for key, param_id in _STEP_PARAMS.items():
            if key in s:
                channels.setStepParameterByIndex(channel, pattern, step, param_id,
                                                 int(s[key]), True)
    try:
        channels.updateGraphEditor()
    except Exception:
        pass
    mismatched = []
    for s in steps:
        step = int(s["step"])
        if not channels.getGridBit(channel, step, True):
            mismatched.append({"step": step, "problem": "step is off"})
            continue
        if "pitch" in s:
            got = channels.getCurrentStepParam(channel, step, 0, True)
            if got != int(s["pitch"]):
                mismatched.append({"step": step, "problem": "pitch %s != %s" % (got, s["pitch"])})
    return {
        "channel": channel,
        "channel_name": channels.getChannelName(channel, True),
        "pattern": pattern,
        "steps_written": len(steps),
        "mismatched": mismatched,
    }


def handle_channels_get_steps(params: dict) -> dict:
    """Active steps of a channel (in a pattern) with their pitch/velocity/pan/shift."""
    channel = params.get("channel")
    error = _channel_error(channel)
    if error:
        return {"error": error}
    pattern, error = _select_pattern_param(params)
    if error:
        return {"error": error}
    count = int(params.get("steps", 0)) or max(
        16, int(round(_patterns_mod().getPatternLength(pattern) * 4)))
    step_ticks = _ppq() // 4
    active = []
    for step in range(count):
        if channels.getGridBit(channel, step, True):
            entry = {"step": step}
            for key in ("pitch", "velocity", "pan", "shift"):
                entry[key] = channels.getCurrentStepParam(channel, step, _STEP_PARAMS[key], True)
            # FL reports the shift as the step's absolute tick position.
            if entry["shift"] >= step * step_ticks:
                entry["shift"] -= step * step_ticks
            active.append(entry)
    return {"channel": channel, "channel_name": channels.getChannelName(channel, True),
            "pattern": pattern, "steps_scanned": count, "active": active}


# =============================================================================
# Patterns (colour, create with name)
# =============================================================================


def handle_patterns_set_color(params: dict) -> dict:
    patterns = _patterns_mod()
    index = int(params.get("index", 0))
    error = _pattern_index_error(patterns, index)
    if error:
        return error
    r, g, b = (int(params.get(k, 0)) for k in ("r", "g", "b"))
    color = (r << 16) | (g << 8) | b
    patterns.setPatternColor(index, color)
    return {"index": index, "color": _rgb(patterns.getPatternColor(index))}


# =============================================================================
# Playlist tracks and markers (live API: clips themselves are not scriptable)
# =============================================================================


def _rgb(color: int) -> str:
    """FL colours come back as signed ints; show them as #RRGGBB."""
    return "#%06X" % (color & 0xFFFFFF)


def _playlist_track_info(playlist, index: int) -> dict:
    return {
        "index": index,
        "name": playlist.getTrackName(index),
        "color": _rgb(playlist.getTrackColor(index)),
        "is_muted": bool(playlist.isTrackMuted(index)),
        "is_solo": bool(playlist.isTrackSolo(index)),
    }


def handle_playlist_get_tracks(params: dict) -> dict:
    """Playlist tracks (1-based). Unnamed tracks are skipped unless include_empty."""
    playlist = _playlist_mod()
    count = playlist.trackCount()
    limit = min(count, int(params.get("limit", count)))
    tracks = []
    for i in range(1, limit + 1):
        info = _playlist_track_info(playlist, i)
        default = info["name"] == "Track %d" % i
        if default and not params.get("include_empty") and not info["is_muted"]:
            continue
        tracks.append(info)
    return {"track_count": count, "tracks": tracks}


def handle_playlist_set_track(params: dict) -> dict:
    """Rename / colour / mute / solo one playlist track (1-based index)."""
    playlist = _playlist_mod()
    index = params.get("index")
    if not isinstance(index, int) or index < 1 or index > playlist.trackCount():
        return {"error": "Playlist track %r out of range (1..%d)" % (index, playlist.trackCount())}
    if params.get("name") is not None:
        playlist.setTrackName(index, str(params["name"]))
    if params.get("color") is not None:
        r, g, b = params["color"]
        playlist.setTrackColor(index, (int(r) << 16) | (int(g) << 8) | int(b))
    if params.get("muted") is not None:
        playlist.muteTrack(index, 1 if params["muted"] else 0)
    if params.get("solo") is not None:
        playlist.soloTrack(index, 1 if params["solo"] else 0)
    return _playlist_track_info(playlist, index)


def handle_arrangement_add_marker(params: dict) -> dict:
    """Add a time marker at an absolute tick position."""
    arrangement = _arrangement_mod()
    ticks = int(params.get("ticks", 0))
    name = str(params.get("name", ""))
    arrangement.addAutoTimeMarker(ticks, name)
    return {"ticks": ticks, "name": name}


_EXTRA_HANDLERS = {
    "system.info": handle_system_info,
    "transport.save": handle_transport_save,
    "mixer.getRouting": handle_mixer_get_routing,
    "mixer.setRoute": handle_mixer_set_route,
    "mixer.getEffects": handle_mixer_get_effects,
    "mixer.getPeaks": handle_mixer_get_peaks,
    "plugins.setParamByName": handle_plugins_set_param_by_name,
    "channels.setSteps": handle_channels_set_steps,
    "channels.getSteps": handle_channels_get_steps,
    "patterns.setColor": handle_patterns_set_color,
    "playlist.getTracks": handle_playlist_get_tracks,
    "playlist.setTrack": handle_playlist_set_track,
    "arrangement.addMarker": handle_arrangement_add_marker,
}


# =============================================================================
# Automation targets (REC event ids, as stored in automation clips)
# =============================================================================

# Offsets inside a mixer track's REC range (see midi.REC_Mixer_*).
_EQ_KINDS = {"gain": 0, "freq": 8, "q": 16}


def _find_param(index: int, slot: int, param) -> tuple:
    """(param index, name) of a plugin parameter given by index or name."""
    use_global = True
    if not plugins.isValid(index, slot, use_global):
        raise ValueError("No plugin at index %d slot %d" % (index, slot))
    count = plugins.getParamCount(index, slot, use_global)
    if isinstance(param, int) and not isinstance(param, bool):
        if not 0 <= param < count:
            raise ValueError("Parameter %d out of range (0..%d)" % (param, count - 1))
        return param, plugins.getParamName(param, index, slot, use_global)
    wanted = str(param).strip().lower()
    names = [(i, plugins.getParamName(i, index, slot, use_global)) for i in range(count)]
    exact = [n for n in names if n[1] and n[1].strip().lower() == wanted]
    partial = [n for n in names if n[1] and wanted in n[1].lower()]
    matches = exact or partial
    if not matches:
        raise ValueError("No parameter named %r" % (param,))
    if len(matches) > 1 and not exact:
        raise ValueError("%r is ambiguous: %s" % (param, ", ".join(
            "%d=%s" % m for m in matches[:15])))
    return matches[0]


def _automation_target(target: dict) -> tuple:
    """(REC event id, label) for a target description."""
    kind = target.get("type")
    if kind == "tempo":
        return midi.REC_Tempo, "Tempo"
    if kind in ("mixer_volume", "mixer_pan", "mixer_stereo", "mixer_eq", "effect_mix",
                "effect_param"):
        track = int(target.get("track", 0))
        error = _track_error(track)
        if error:
            raise ValueError(error)
        slot = int(target.get("slot", 0))
        plugin_id = mixer.getTrackPluginId(track, slot if kind.startswith("effect") else 0)
        track_name = mixer.getTrackName(track)
        if kind == "mixer_volume":
            return midi.REC_Mixer_Vol + plugin_id, "%s - Volume" % track_name
        if kind == "mixer_pan":
            return midi.REC_Mixer_Pan + plugin_id, "%s - Panning" % track_name
        if kind == "mixer_stereo":
            return midi.REC_Mixer_SS + plugin_id, "%s - Stereo separation" % track_name
        if kind == "mixer_eq":
            band = int(target.get("band", 0))
            what = str(target.get("eq", "gain"))
            if what not in _EQ_KINDS or not 0 <= band < 3:
                raise ValueError("mixer_eq needs band 0-2 and eq gain|freq|q")
            return (midi.REC_Mixer_EQ_Gain + _EQ_KINDS[what] + band + plugin_id,
                    "%s - EQ band %d %s" % (track_name, band + 1, what))
        if not plugins.isValid(track, slot, True):
            raise ValueError("No effect in track %d slot %d" % (track, slot))
        plugin_name = plugins.getPluginName(track, slot, True, True)
        if kind == "effect_mix":
            return midi.REC_Plug_MixLevel + plugin_id, "%s (%s) - Mix level" % (
                plugin_name, track_name)
        param, name = _find_param(track, slot, target.get("param", 0))
        return (midi.REC_Plug_First + plugin_id + midi.REC_PluginBase + param,
                "%s (%s) - %s" % (plugin_name, track_name, name))
    if kind in ("channel_volume", "channel_pan", "channel_pitch", "channel_param"):
        channel = target.get("channel")
        error = _channel_error(channel)
        if error:
            raise ValueError(error)
        base = channels.getRecEventId(channel, True)
        name = channels.getChannelName(channel, True)
        if kind == "channel_volume":
            return base + midi.REC_Chan_Vol, "%s - Channel volume" % name
        if kind == "channel_pan":
            return base + midi.REC_Chan_Pan, "%s - Channel panning" % name
        if kind == "channel_pitch":
            return base + midi.REC_Chan_Pitch, "%s - Channel pitch" % name
        param, pname = _find_param(channel, -1, target.get("param", 0))
        return base + midi.REC_Chan_Plugin_First + param, "%s - %s" % (name, pname)
    raise ValueError("unknown target type %r" % (kind,))


def handle_automation_resolve(params: dict) -> dict:
    """REC event ids and default clip names for a list of automation targets."""
    out = []
    for target in params.get("targets") or []:
        try:
            rec_id, label = _automation_target(target)
        except Exception as e:
            return {"error": "target %r: %s" % (target, e)}
        entry = {"rec_id": rec_id, "label": label}
        try:
            entry["fl_name"] = mixer.getEventIDName(rec_id)
            entry["current"] = mixer.remoteFindEventValue(rec_id)
        except Exception:
            pass
        out.append(entry)
    return {"targets": out}


def handle_automation_describe(params: dict) -> dict:
    """FL's name for REC event ids read from automation clips."""
    names = {}
    for rec_id in params.get("ids") or []:
        try:
            names[str(rec_id)] = mixer.getEventIDName(int(rec_id))
        except Exception:
            names[str(rec_id)] = None
    return {"names": names}


_EXTRA_HANDLERS["automation.resolve"] = handle_automation_resolve
_EXTRA_HANDLERS["automation.describe"] = handle_automation_describe
