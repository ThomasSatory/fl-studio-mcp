"""Piano Roll tools for FL Studio - persistent note placement.

This module provides tools for creating, editing, and deleting notes in
FL Studio's piano roll. Unlike MIDI real-time note triggering, these
tools create persistent notes by communicating with FL Studio's Piano Roll
scripting API via JSON files.

Communication flow:
1. MCP server writes requests to mcp_request.json
2. Keystroke trigger (Cmd+Opt+Y) executes FL Studio's ComposeWithLLM script
3. Script reads JSON, modifies piano roll, exports state to piano_roll_state.json
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from fl_studio_mcp.utils.fl_trigger import FLProbeError, get_trigger
from fl_studio_mcp.utils.midi_connection import get_fl_settings_base
from fl_studio_mcp.utils.music import chord_to_midi, midi_to_note_name, note_to_midi

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _get_fl_scripts_dir() -> Path:
    """Get the FL Studio Piano Roll scripts directory."""
    base = get_fl_settings_base()

    scripts_dir = base / "Piano roll scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    return scripts_dir


def _get_request_file() -> Path:
    """Get the path to the MCP request JSON file."""
    return _get_fl_scripts_dir() / "mcp_request.json"


def _get_response_file() -> Path:
    """Get the path to the MCP response JSON file."""
    return _get_fl_scripts_dir() / "mcp_response.json"


def _get_state_file() -> Path:
    """Get the path to the piano roll state JSON file."""
    return _get_fl_scripts_dir() / "piano_roll_state.json"


def _write_request(request: dict | list) -> None:
    """Write a request to the MCP request file."""
    request_file = _get_request_file()

    # Read existing requests if any
    existing = []
    if request_file.exists():
        try:
            with open(request_file) as f:
                data = json.load(f)
                if isinstance(data, list):
                    existing = data
                elif isinstance(data, dict):
                    existing = [data]
        except (json.JSONDecodeError, IOError):
            existing = []

    # Append new request(s)
    if isinstance(request, list):
        existing.extend(request)
    else:
        existing.append(request)

    # Write back
    with open(request_file, "w") as f:
        json.dump(existing, f, indent=2)


def _clear_request_file() -> None:
    """Clear the request file."""
    request_file = _get_request_file()
    if request_file.exists():
        request_file.unlink()


def _read_state() -> dict | None:
    """Read the current piano roll state."""
    state_file = _get_state_file()
    if not state_file.exists():
        return None

    try:
        with open(state_file) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


# How long to wait for the piano roll script to consume a request and answer.
PR_SCRIPT_TIMEOUT = 5.0

# Shown whenever the trigger keystroke was sent but the script did not run.
PR_SCRIPT_NOT_RUN_HINT = (
    "The piano roll script did not run. Make sure a piano roll window is open in "
    "FL Studio and that ComposeWithLLM has been run once in this FL Studio session "
    "from the piano roll's Tools > Scripting menu (the trigger keystroke only "
    "re-runs the last script)."
)
PR_SCRIPT_QUEUED_HINT = (
    " The request is still queued and will be processed on the next successful "
    "trigger; cancel it with fl_clear_request_queue."
)
PR_FL_NOT_RUNNING = (
    "FL Studio is not running. Start it, open a piano roll and run ComposeWithLLM once "
    "from the piano roll's Tools > Scripting menu, then retry."
)
PR_FL_PROBE_FAILED = (
    "Could not determine whether FL Studio is running ({detail}). On macOS, grant "
    "Automation access to System Events for this app in System Settings > Privacy & "
    "Security > Automation, then retry."
)

PR_SCRIPT_OUTDATED = (
    "ComposeWithLLM.pyscript in FL Studio is outdated (its answer has no "
    "script_version). Reinstall it with scripts/setup.sh, then run it once from "
    "the piano roll's Tools > Scripting menu."
)

# Serialises the whole note round-trip (queue write, focus, keystroke, wait).
_PR_LOCK = threading.Lock()


def _not_run_error() -> str:
    """Error text for a trigger that FL Studio ignored, mentioning queued requests."""
    if _load_request_queue():
        return PR_SCRIPT_NOT_RUN_HINT + PR_SCRIPT_QUEUED_HINT
    return PR_SCRIPT_NOT_RUN_HINT


def _load_request_queue() -> list[dict]:
    """Return the queued requests as a list (empty when the file is missing/invalid)."""
    request_file = _get_request_file()
    if not request_file.exists():
        return []
    try:
        with open(request_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return []
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict) and data.get("action"):
        return [data]
    return []


def _remove_queued_actions(action: str) -> int:
    """Drop every queued request with the given action; returns how many were removed."""
    queue = _load_request_queue()
    kept = [r for r in queue if r.get("action") != action]
    removed = len(queue) - len(kept)
    if removed:
        with open(_get_request_file(), "w") as f:
            json.dump(kept, f, indent=2)
    return removed


def _state_mtime() -> int | None:
    """Modification time of the state file in ns, or None when it does not exist."""
    try:
        return _get_state_file().stat().st_mtime_ns
    except OSError:
        return None


def _try_read_response() -> dict | None:
    """Read and remove the response file; None while it is absent or still being written."""
    response_file = _get_response_file()
    if not response_file.exists():
        return None
    try:
        data = json.loads(response_file.read_text())
    except (json.JSONDecodeError, IOError):
        return None  # partial write or transient error - the caller retries
    try:
        response_file.unlink()
    except OSError:
        pass
    return data if isinstance(data, dict) else {"raw_response": data}


def _focus_piano_roll() -> str | None:
    """Make the piano roll FL's focused window before sending the trigger keystroke.

    Ctrl+Alt+Y only reaches the piano roll script while the piano roll is the
    focused window inside FL Studio; the OS-level window activation done by
    the trigger cannot guarantee that. Best effort: a missing MIDI bridge must
    not break the keystroke path. Returns the focused window's caption when
    the controller answered, else None.
    """
    from fl_studio_mcp.utils.connection import get_connection

    try:
        result = get_connection().send_command(
            "ui.showWindow", {"window": "piano_roll", "focus": True}, timeout=1.0
        )
    except Exception:  # noqa: BLE001 - best effort only
        return None
    if not result.get("success"):
        return None
    return result.get("focused_caption")


def _run_pr_script(timeout: float = PR_SCRIPT_TIMEOUT) -> dict:
    """Trigger the piano roll script and report what actually happened.

    The old behaviour slept for a fixed delay and reported success whenever the
    keystroke could be sent, even if nothing in FL Studio reacted. This waits
    for evidence instead: the script's response file or, failing that, the
    state export it always writes.

    Returns a dict with:
    - triggered: the keystroke was sent
    - ran: the script executed in FL Studio
    - response: the script's response dict (None when it processed nothing)
    - error: human-readable problem description, or None
    """
    trigger = get_trigger()
    if not trigger.is_supported:
        return {
            "triggered": False,
            "ran": False,
            "response": None,
            "error": (
                f"Auto-trigger not supported on {trigger.platform}. "
                f"Press {trigger.keystroke} in the piano roll manually."
            ),
        }

    try:
        fl_running = trigger.is_fl_running()
    except FLProbeError as e:
        error = PR_FL_PROBE_FAILED.format(detail=e)
        if _load_request_queue():
            error += PR_SCRIPT_QUEUED_HINT
        return {"triggered": False, "ran": False, "response": None, "error": error}
    if not fl_running:
        error = PR_FL_NOT_RUNNING
        if _load_request_queue():
            error += PR_SCRIPT_QUEUED_HINT
        return {"triggered": False, "ran": False, "response": None, "error": error}

    # Remove a stale response BEFORE triggering: the script answers within
    # milliseconds, so deleting it after the keystroke would discard the
    # fresh answer.
    response_file = _get_response_file()
    if response_file.exists():
        try:
            response_file.unlink()
        except OSError:
            pass

    state_before = _state_mtime()
    # With a queued request the script always writes a response, so keep waiting
    # for it until the deadline; only an empty queue is proven by the state export.
    expect_response = bool(_load_request_queue())

    # FL applies ui.setFocused only while its window is the active OS window:
    # activate first, then focus the piano roll, then send the keystroke.
    if trigger.activate_window():
        _focus_piano_roll()

    if not trigger.trigger(delay=0.2):
        return {
            "triggered": False,
            "ran": False,
            "response": None,
            "error": (
                "Could not send the trigger keystroke to FL Studio (window not found?). "
                f"Press {trigger.keystroke} in the piano roll manually."
            ),
        }

    deadline = time.time() + timeout
    state_changed_at: float | None = None
    while time.time() < deadline:
        response = _try_read_response()
        if response is not None:
            return {"triggered": True, "ran": True, "response": response, "error": None}
        if state_changed_at is None and _state_mtime() != state_before:
            # The script exports the state before it writes the response;
            # give the response a short grace period, then accept the export
            # alone as proof (the script writes no response for an empty queue).
            state_changed_at = time.time()
        elif (
            state_changed_at is not None
            and not expect_response
            and time.time() - state_changed_at > 0.5
        ):
            return {"triggered": True, "ran": True, "response": None, "error": None}
        time.sleep(0.05)

    if state_changed_at is not None:
        return {"triggered": True, "ran": True, "response": None, "error": None}
    return {"triggered": True, "ran": False, "response": None, "error": _not_run_error()}


def _describe_run(run: dict) -> str:
    """Human-readable outcome of a _run_pr_script() result, as a sentence."""
    if not run["ran"]:
        return f"Warning: {run['error']}"
    response = run.get("response") or {}
    if response.get("status") == "error":
        return f"FL Studio ran the script but it reported an error: {response.get('message')}"
    added = response.get("notes_added")
    deleted = response.get("notes_deleted")
    if added is None and deleted is None:
        return "FL Studio ran the piano roll script."
    return f"FL Studio processed the request ({added or 0} note(s) added, {deleted or 0} deleted)."


def _enrich_pr_context(context: dict) -> dict:
    """Fill in what the piano roll runtime cannot report itself.

    The `channels` and `patterns` modules are normally not importable from a
    piano roll script, so the active pattern and the selected channel are
    fetched from the controller script over MIDI instead. A failing MIDI bridge
    must not hide the context the script did deliver.
    """
    from fl_studio_mcp.utils.connection import get_connection

    context.pop("selected_channel_error", None)
    context.pop("current_pattern_error", None)
    try:
        conn = get_connection()
        if "current_pattern" not in context:
            result = conn.send_command("patterns.getCurrent")
            if result.get("success") and result.get("index") is not None:
                context["current_pattern"] = {
                    "index": result.get("index"),
                    "name": result.get("name"),
                    "length_beats": result.get("length_beats"),
                }
        if "selected_channel" not in context:
            result = conn.send_command("channels.getSelected")
            channel = result.get("channel") if result.get("success") else None
            if channel:
                context["selected_channel"] = {
                    "index": channel.get("index"),
                    "name": channel.get("name"),
                }
    except Exception as e:  # noqa: BLE001 - keep the script's context regardless
        context["controller_error"] = str(e)
    try:
        focus = conn.send_command("ui.getFocus")
        if focus.get("success"):
            context["piano_roll_focused"] = bool(focus.get("focused", {}).get("piano_roll"))
            context["focused_caption"] = focus.get("caption")
    except Exception as e:  # noqa: BLE001
        context.setdefault("controller_error", str(e))
    context["note"] = (
        "selected_channel is the channel rack selection; an open piano roll does "
        "not follow it when selected via scripting, and FL does not report the "
        "piano roll's target channel. Call fl_open_piano_roll(channel) to make "
        "the piano roll target a specific channel before writing notes."
    )
    return context


# Defaults of the optional note fields; the script receives every field.
NOTE_DEFAULTS = {
    "time": 0.0, "velocity": 0.8, "pan": 0.5, "color": 0, "slide": False, "porta": False,
}
# Fields a v1 ComposeWithLLM.pyscript silently ignores.
EXTENDED_PROPS = ("pan", "color", "slide", "porta")
_ADD_FIELDS = frozenset({"midi", "note", "duration", *NOTE_DEFAULTS})
_DELETE_FIELDS = frozenset({"midi", "note", "time"})

NOT_VERIFIED_HINT = (
    "Not verified: nothing reaches FL Studio until fl_trigger_script runs the queue."
)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_note(i: int, note: dict, require_duration: bool) -> dict:
    """One validated note with every field filled in; raises ToolError naming note i."""

    def fail(message: str) -> ToolError:
        return ToolError(f"note {i}: {message}")

    if ("midi" in note) == ("note" in note):
        raise fail("give exactly one of 'midi' (0-127) or 'note' (e.g. \"C4\", C4 = 60)")
    if "midi" in note:
        midi = note["midi"]
        if not _is_int(midi) or not 0 <= midi <= 127:
            raise fail(f"midi must be an integer 0-127, got {midi!r}")
    else:
        try:
            midi = note_to_midi(note["note"])
        except ValueError as e:
            raise fail(str(e)) from e

    time = note.get("time", NOTE_DEFAULTS["time"])
    if not _is_number(time) or time < 0:
        raise fail(f"time must be a number >= 0 (quarter notes), got {time!r}")
    if not require_duration:
        return {"midi": midi, "time": float(time)}

    if "duration" not in note:
        raise fail("missing 'duration' (quarter notes, > 0)")
    duration = note["duration"]
    if not _is_number(duration) or duration <= 0:
        raise fail(f"duration must be a number > 0 (quarter notes), got {duration!r}")
    out = {"midi": midi, "time": float(time), "duration": float(duration)}
    for key in ("velocity", "pan"):
        value = note.get(key, NOTE_DEFAULTS[key])
        if not _is_number(value) or not 0 <= value <= 1:
            raise fail(f"{key} must be a number 0-1, got {value!r}")
        out[key] = float(value)
    color = note.get("color", NOTE_DEFAULTS["color"])
    if not _is_int(color) or not 0 <= color <= 15:
        raise fail(f"color must be an integer 0-15, got {color!r}")
    out["color"] = color
    for key in ("slide", "porta"):
        value = note.get(key, NOTE_DEFAULTS[key])
        if not isinstance(value, bool):
            raise fail(f"{key} must be true or false, got {value!r}")
        out[key] = value
    return out


def _validate_notes(notes: object, *, require_duration: bool = True) -> list[dict]:
    """Validate every note before anything is queued; returns new, normalized dicts."""
    if not isinstance(notes, list) or not notes:
        raise ToolError("No notes provided")
    allowed = _ADD_FIELDS if require_duration else _DELETE_FIELDS
    normalized = []
    for i, note in enumerate(notes):
        if not isinstance(note, dict):
            raise ToolError(f"note {i}: expected an object, got {note!r}")
        unknown = sorted(set(note) - allowed)
        if unknown:
            raise ToolError(
                f"note {i}: unknown field(s) {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )
        normalized.append(_normalize_note(i, note, require_duration))
    return normalized


def _summarize_notes(notes: list[dict], limit: int = 8) -> str:
    """'C4@0, E4@0, ... (3 more)' for a tool answer."""
    text = ", ".join(f"{midi_to_note_name(n['midi'])}@{n['time']:g}" for n in notes[:limit])
    if len(notes) > limit:
        text += f", ... ({len(notes) - limit} more)"
    return text


def _uses_extended_props(notes: list[dict]) -> bool:
    return any(
        n.get(p, NOTE_DEFAULTS[p]) != NOTE_DEFAULTS[p] for n in notes for p in EXTENDED_PROPS
    )


def _note_count_text(response: dict) -> str:
    count = response.get("note_count")
    return f" The piano roll now has {count} notes." if count is not None else ""


def _checked_response(outcome: dict, notes_sent: list[dict] | None = None) -> tuple[dict, str]:
    """Raise ToolError unless the script provably did what was asked.

    Returns the script's response and a warning suffix ("" when none). With
    `notes_sent`, the number of notes FL added must match it.
    """
    if not outcome["ran"]:
        raise ToolError(outcome["error"])
    response = outcome.get("response") or {}
    added = response.get("notes_added") or 0
    deleted = response.get("notes_deleted") or 0
    if not response:
        if notes_sent is not None:
            raise ToolError(
                "FL Studio ran the piano roll script but it processed nothing: the "
                "request was not in its queue. Check fl_get_piano_roll_info and retry."
            )
        return response, ""
    if response.get("status") == "error":
        raise ToolError(
            f"FL Studio applied {added} added and {deleted} deleted note(s), then "
            f"failed: {response.get('message')}"
        )
    suffix = ""
    if "script_version" not in response:
        if notes_sent is not None and _uses_extended_props(notes_sent):
            raise ToolError(
                f"{PR_SCRIPT_OUTDATED} The notes were written without pan/color/slide/porta."
            )
        suffix = f" Warning: {PR_SCRIPT_OUTDATED}"
    if notes_sent is not None and added != len(notes_sent):
        raise ToolError(
            f"Sent {len(notes_sent)} note(s) but FL Studio reports {added} added."
        )
    return response, suffix


def register_piano_roll_tools(mcp: FastMCP) -> None:
    """Register piano roll tools with the MCP server."""

    @mcp.tool()
    def fl_send_notes(notes: list[dict], mode: str = "add", auto_trigger: bool = True) -> str:
        """Add or replace notes in the open FL Studio piano roll.

        Times and durations are in quarter notes. Pitch convention: C4 = 60
        (FL Studio's own UI labels MIDI 60 as "C5").

        Args:
            notes: note objects. Each needs exactly one of:
                   - midi (int 0-127), or
                   - note (str, e.g. "C4", "F#3", "Bb2")
                   plus:
                   - duration (float > 0, quarter notes) - required
                   - time (float >= 0, quarter notes, default 0)
                   - velocity (0-1, default 0.8)
                   - pan (0-1, 0.5 = centre, default 0.5)
                   - color (int 0-15, FL note colour group, default 0)
                   - slide (bool, default false), porta (bool, default false)
                   Unknown fields are rejected.
            mode: "add" keeps existing notes, "replace" clears the piano roll first
            auto_trigger: run the piano roll script now and verify the result
                          (default True); False only queues the request

        Example:
            [{"note": "C4", "time": 0, "duration": 1},
             {"midi": 64, "time": 1, "duration": 0.5, "velocity": 0.6}]
        """
        if mode not in ("add", "replace"):
            raise ToolError(f"mode must be 'add' or 'replace', got {mode!r}")
        normalized = _validate_notes(notes)
        requests = [{"action": "clear"}] if mode == "replace" else []
        requests.append({"action": "add_notes", "notes": normalized})
        summary = _summarize_notes(normalized)

        with _PR_LOCK:
            _write_request(requests)
            if not auto_trigger:
                return f"Queued {len(normalized)} note(s): {summary}. {NOT_VERIFIED_HINT}"
            outcome = _run_pr_script()
        response, warning = _checked_response(outcome, normalized)
        return (
            f"{len(normalized)} note(s) written: {summary}."
            f"{_note_count_text(response)}{warning}"
        )

    @mcp.tool()
    def fl_send_chord(
        chord: str | list[int | str],
        octave: int = 4,
        inversion: int = 0,
        time: float = 0,
        duration: float = 1.0,
        velocity: float = 0.8,
        pan: float = 0.5,
        color: int = 0,
        auto_trigger: bool = True,
    ) -> str:
        """Add a chord to the open FL Studio piano roll.

        Args:
            chord: a chord symbol such as "Am7", "Cmaj9/E", "F#m7b5", "Dsus4",
                   or a list of MIDI numbers and/or note names ([57, "C4", "E4"]).
                   Qualities: maj, m, dim, aug, sus2, sus4, 5, 6, m6, 7, maj7, m7,
                   mM7, m7b5, dim7, 9, maj9, m9, add9, 11, 13. "/X" adds X as bass.
            octave: octave of the chord root for a symbol (C4 = 60; FL's UI shows
                    MIDI 60 as "C5"). Ignored for a list.
            inversion: raise the n lowest notes an octave (symbol only)
            time: start in quarter notes (default 0)
            duration: length in quarter notes (default 1.0)
            velocity, pan: 0-1 (defaults 0.8 and 0.5)
            color: FL note colour group 0-15 (default 0)
            auto_trigger: run the piano roll script now and verify the result

        Example: fl_send_chord("Am7", octave=3, time=0, duration=4)
        """
        if isinstance(chord, str):
            try:
                pitches: list[int | str] = list(chord_to_midi(chord, octave, inversion))
            except ValueError as e:
                raise ToolError(str(e)) from e
        else:
            pitches = list(chord)
        shared = {"time": time, "duration": duration, "velocity": velocity,
                  "pan": pan, "color": color}
        normalized = _validate_notes([
            {"note": p, **shared} if isinstance(p, str) else {"midi": p, **shared}
            for p in pitches
        ])
        names = " ".join(midi_to_note_name(n["midi"]) for n in normalized)
        label = f"{chord} → {names}" if isinstance(chord, str) else names
        summary = f"{label} at {time:g}, duration {duration:g}"

        with _PR_LOCK:
            _write_request({"action": "add_notes", "notes": normalized})
            if not auto_trigger:
                return f"Queued chord {summary}. {NOT_VERIFIED_HINT}"
            outcome = _run_pr_script()
        response, warning = _checked_response(outcome, normalized)
        return (
            f"{len(normalized)} note(s) written: {summary}."
            f"{_note_count_text(response)}{warning}"
        )

    @mcp.tool()
    def fl_delete_notes(notes: list[dict], auto_trigger: bool = True) -> str:
        """Delete notes from the open piano roll, matched by pitch and start time.

        Args:
            notes: objects with exactly one of midi (0-127) or note ("C4", C4 = 60),
                   and time (quarter notes, default 0).
            auto_trigger: run the piano roll script now and verify the result

        Example: [{"note": "C4", "time": 0}, {"midi": 64, "time": 0}]
        """
        normalized = _validate_notes(notes, require_duration=False)
        with _PR_LOCK:
            _write_request({"action": "delete_notes", "notes": normalized})
            if not auto_trigger:
                return f"Queued deletion of {len(normalized)} note(s). {NOT_VERIFIED_HINT}"
            outcome = _run_pr_script()
        response, warning = _checked_response(outcome)
        return (
            f"{response.get('notes_deleted') or 0} note(s) deleted."
            f"{_note_count_text(response)}{warning}"
        )

    @mcp.tool()
    def fl_clear_piano_roll(auto_trigger: bool = True) -> str:
        """Delete every note in the open piano roll.

        Args:
            auto_trigger: run the piano roll script now and verify the result
        """
        with _PR_LOCK:
            _write_request({"action": "clear"})
            if not auto_trigger:
                return f"Queued clear of all notes. {NOT_VERIFIED_HINT}"
            outcome = _run_pr_script()
        response, warning = _checked_response(outcome)
        return (
            f"Piano roll cleared ({response.get('notes_deleted') or 0} note(s) removed)."
            f"{_note_count_text(response)}{warning}"
        )

    @mcp.tool()
    def fl_get_piano_roll_state() -> dict:
        """Get the current state of notes in the FL Studio piano roll.

        Returns a dictionary containing:
        - ppq: Pulses per quarter note (ticks per beat)
        - notes: List of all notes with their properties

        Note: This reads from the last exported state. Trigger FL Studio
        (Cmd+Opt+Y on macOS) to refresh the state file after making changes.
        """
        state = _read_state()

        if state is None:
            raise ToolError(
                "No piano roll state available. Make sure FL Studio's "
                "ComposeWithLLM script has been run at least once."
            )

        # Add human-readable note names
        if "notes" in state:
            for note in state["notes"]:
                if "midi" in note:
                    note["note_name"] = midi_to_note_name(note["midi"])

        return state

    @mcp.tool()
    def fl_clear_request_queue() -> str:
        """Clear any pending note requests without executing them.

        Use this if you want to cancel queued changes before triggering FL Studio.
        """
        _clear_request_file()
        return "Request queue cleared."

    @mcp.tool()
    def fl_trigger_script() -> str:
        """Run the ComposeWithLLM piano roll script on the queued requests.

        Sends the keystroke (Cmd+Opt+Y on macOS, Ctrl+Alt+Y on Windows/Linux)
        and reports what the script did; raises if it failed or did not run.
        """
        with _PR_LOCK:
            outcome = _run_pr_script()
        response, warning = _checked_response(outcome)
        if not response:
            return "FL Studio ran the piano roll script (nothing was queued)."
        return f"{_describe_run(outcome)}{_note_count_text(response)}{warning}"

    @mcp.tool()
    def fl_get_pr_context() -> dict:
        """Get context of the currently open piano roll (read-only).

        Returns PPQ, time signature, note/marker counts, snap-to-scale info,
        timeline selection, the active pattern and the channel selected in the
        channel rack. Useful to verify WHERE notes would be written before
        using fl_send_notes - but note that an open piano roll does not follow
        channel selection made via scripting, only selection in the FL UI.

        Requires an open piano roll and, once per FL Studio session, a manual
        run of ComposeWithLLM from the piano roll's Tools > Scripting menu.
        """
        with _PR_LOCK:
            _write_request({"action": "get_context"})
            outcome = _run_pr_script()
            if not outcome["ran"]:
                # Do not let context requests pile up for the next successful trigger.
                _remove_queued_actions("get_context")
        if not outcome["ran"]:
            if outcome["triggered"]:
                # The generic hint mentions the queue; this request is gone.
                raise ToolError(PR_SCRIPT_NOT_RUN_HINT)
            raise ToolError(outcome["error"])

        response = outcome.get("response") or {}
        if response.get("status") == "error":
            raise ToolError(f"Piano roll script error: {response.get('message')}")
        context = response.get("context")
        if not isinstance(context, dict):
            raise ToolError(
                "The piano roll script answered without context data. Update "
                "ComposeWithLLM.pyscript in FL Studio's 'Piano roll scripts' "
                f"folder from this repository. Raw response: {response!r}"
            )
        return _enrich_pr_context(context)

    @mcp.tool()
    def fl_get_piano_roll_info() -> dict:
        """Get information about the Piano Roll integration status.

        Returns platform info, file paths, and whether auto-triggering is supported.
        """
        trigger = get_trigger()

        return {
            "platform": trigger.platform,
            "auto_trigger_supported": trigger.is_supported,
            "trigger_keystroke": trigger.keystroke,
            "scripts_dir": str(_get_fl_scripts_dir()),
            "request_file": str(_get_request_file()),
            "state_file": str(_get_state_file()),
            "request_file_exists": _get_request_file().exists(),
            "state_file_exists": _get_state_file().exists(),
        }
