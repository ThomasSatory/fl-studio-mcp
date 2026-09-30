"""Note validation happens before anything is queued (no FL Studio needed)."""

from __future__ import annotations

import copy
import json

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools import piano_roll
from fl_studio_mcp.tools.piano_roll import _validate_notes, register_piano_roll_tools
from tests.fakes import RecordingMCP


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))
    mcp = RecordingMCP()
    register_piano_roll_tools(mcp)
    return mcp.tools


def queued(tmp_path):
    request_file = tmp_path / "Piano roll scripts" / "mcp_request.json"
    return json.loads(request_file.read_text()) if request_file.exists() else []


def test_defaults_are_filled_and_names_resolved():
    assert _validate_notes([{"note": "A3", "duration": 2}]) == [
        {"midi": 57, "time": 0.0, "duration": 2.0, "velocity": 0.8,
         "pan": 0.5, "color": 0, "slide": False, "porta": False}
    ]


def test_input_list_is_not_modified():
    notes = [{"midi": 60, "duration": 1}]
    before = copy.deepcopy(notes)
    _validate_notes(notes)
    assert notes == before


@pytest.mark.parametrize(
    ("note", "message"),
    [
        ({"duration": 1}, "exactly one of 'midi'"),
        ({"midi": 60, "note": "C4", "duration": 1}, "exactly one of 'midi'"),
        ({"midi": 128, "duration": 1}, "midi must be an integer 0-127"),
        ({"midi": True, "duration": 1}, "midi must be an integer 0-127"),
        ({"midi": "60", "duration": 1}, "midi must be an integer 0-127"),
        ({"note": "H4", "duration": 1}, "invalid note name"),
        ({"midi": 60}, "missing 'duration'"),
        ({"midi": 60, "duration": 0}, "duration must be a number > 0"),
        ({"midi": 60, "duration": "1"}, "duration must be a number > 0"),
        ({"midi": 60, "duration": 1, "time": -1}, "time must be a number >= 0"),
        ({"midi": 60, "duration": 1, "velocity": 1.4}, "velocity must be a number 0-1"),
        ({"midi": 60, "duration": 1, "pan": -0.1}, "pan must be a number 0-1"),
        ({"midi": 60, "duration": 1, "color": 16}, "color must be an integer 0-15"),
        ({"midi": 60, "duration": 1, "color": 1.5}, "color must be an integer 0-15"),
        ({"midi": 60, "duration": 1, "slide": 1}, "slide must be true or false"),
        ({"midi": 60, "duration": 1, "porta": "yes"}, "porta must be true or false"),
        ({"midi": 60, "duration": 1, "veloctiy": 0.5}, "unknown field\\(s\\) veloctiy"),
        ("C4", "expected an object"),
    ],
)
def test_each_rule_names_the_note_index(note, message):
    notes = [{"midi": 60, "duration": 1}, note]
    with pytest.raises(ToolError, match=f"note 1: .*{message}"):
        _validate_notes(notes)


def test_empty_list_is_an_error():
    with pytest.raises(ToolError, match="No notes provided"):
        _validate_notes([])


def test_delete_mode_needs_no_duration_and_refuses_add_fields():
    assert _validate_notes([{"note": "C4", "time": 2}], require_duration=False) == [
        {"midi": 60, "time": 2.0}
    ]
    with pytest.raises(ToolError, match="unknown field\\(s\\) duration"):
        _validate_notes([{"midi": 60, "duration": 1}], require_duration=False)


def test_invalid_note_queues_nothing(tools, tmp_path):
    with pytest.raises(ToolError):
        tools["fl_send_notes"]([{"midi": 60, "duration": 1}, {"midi": 60}], auto_trigger=False)
    assert queued(tmp_path) == []


def test_replace_with_invalid_note_queues_nothing(tools, tmp_path):
    with pytest.raises(ToolError):
        tools["fl_send_notes"]([{"midi": 999, "duration": 1}], mode="replace", auto_trigger=False)
    assert queued(tmp_path) == []


def test_unknown_mode_is_rejected(tools, tmp_path):
    with pytest.raises(ToolError, match="mode must be 'add' or 'replace'"):
        tools["fl_send_notes"]([{"midi": 60, "duration": 1}], mode="overwrite", auto_trigger=False)
    assert queued(tmp_path) == []


def test_send_notes_queues_normalized_midi_only(tools, tmp_path):
    text = tools["fl_send_notes"](
        [{"note": "C4", "duration": 1}], mode="replace", auto_trigger=False
    )
    requests = queued(tmp_path)
    assert requests[0] == {"action": "clear"}
    assert requests[1]["action"] == "add_notes"
    assert requests[1]["notes"][0]["midi"] == 60
    assert "note" not in requests[1]["notes"][0]
    assert "C4@0" in text and "Not verified" in text


def test_send_chord_symbol(tools, tmp_path):
    text = tools["fl_send_chord"]("Am7", octave=3, time=4, duration=2, auto_trigger=False)
    (request,) = queued(tmp_path)
    assert request["action"] == "add_notes"
    assert [n["midi"] for n in request["notes"]] == [57, 60, 64, 67]
    assert all(n["time"] == 4.0 and n["duration"] == 2.0 for n in request["notes"])
    assert "Am7 → A3 C4 E4 G4" in text


def test_send_chord_mixed_list(tools, tmp_path):
    tools["fl_send_chord"]([57, "C4", "E4"], pan=0.2, color=3, auto_trigger=False)
    (request,) = queued(tmp_path)
    assert [n["midi"] for n in request["notes"]] == [57, 60, 64]
    assert all(n["pan"] == 0.2 and n["color"] == 3 for n in request["notes"])


@pytest.mark.parametrize(
    ("chord", "message"),
    [("Cmaj13", "unknown chord quality"), ([], "No notes provided"), ([60, True], "note 1")],
)
def test_send_chord_errors(tools, tmp_path, chord, message):
    with pytest.raises(ToolError, match=message):
        tools["fl_send_chord"](chord, auto_trigger=False)
    assert queued(tmp_path) == []


def test_delete_notes_accepts_names(tools, tmp_path):
    tools["fl_delete_notes"]([{"note": "C4", "time": 0}, {"midi": 64}], auto_trigger=False)
    (request,) = queued(tmp_path)
    assert request == {
        "action": "delete_notes",
        "notes": [{"midi": 60, "time": 0.0}, {"midi": 64, "time": 0.0}],
    }


def test_state_notes_get_names(tools, tmp_path, monkeypatch):
    monkeypatch.setattr(
        piano_roll, "_read_state", lambda: {"ppq": 96, "notes": [{"midi": 61}]}
    )
    assert tools["fl_get_piano_roll_state"]()["notes"][0]["note_name"] == "C#4"
