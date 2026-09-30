"""The note tools trust only what the piano roll script reports back."""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools import piano_roll
from fl_studio_mcp.tools.piano_roll import register_piano_roll_tools
from tests.fakes import RecordingMCP


class ScriptedFL:
    """Plays ComposeWithLLM: empties the queue, exports state, writes `response`."""

    is_supported = True
    platform = "TestOS"
    keystroke = "Ctrl+Alt+Y"

    def __init__(self, scripts_dir, response):
        self.scripts_dir = scripts_dir
        self.response = response

    def is_fl_running(self):
        return True

    def activate_window(self):
        return True

    def trigger(self, delay=0.0):
        (self.scripts_dir / "mcp_request.json").write_text("[]")
        (self.scripts_dir / "piano_roll_state.json").write_text(
            json.dumps({"ppq": 96, "noteCount": 0, "notes": []})
        )
        if self.response is not None:
            (self.scripts_dir / "mcp_response.json").write_text(json.dumps(self.response))
        return True


@pytest.fixture
def make_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))
    scripts_dir = tmp_path / "Piano roll scripts"
    monkeypatch.setattr(piano_roll, "_focus_piano_roll", lambda: None)

    def build(response):
        scripts_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(piano_roll, "get_trigger", lambda: ScriptedFL(scripts_dir, response))
        mcp = RecordingMCP()
        register_piano_roll_tools(mcp)
        return mcp.tools

    return build


def v2(**fields):
    return {"status": "success", "requests_processed": 1, "notes_added": 0,
            "notes_deleted": 0, "script_version": 2, "note_count": 0, **fields}


def test_success_names_notes_and_count(make_tools):
    tools = make_tools(v2(notes_added=4, note_count=36))

    text = tools["fl_send_chord"]("Am7", octave=3, duration=4)

    assert "4 note(s) written" in text
    assert "A3 C4 E4 G4" in text
    assert "now has 36 notes" in text
    assert "Warning" not in text


def test_script_error_raises_with_partial_counts(make_tools):
    tools = make_tools(v2(status="error", notes_added=2, notes_deleted=1,
                          message="action 1 (add_notes) failed: 'midi'"))

    with pytest.raises(ToolError, match="applied 2 added and 1 deleted.*action 1 \\(add_notes\\)"):
        tools["fl_send_notes"]([{"midi": 60, "duration": 1}])


def test_added_count_mismatch_raises(make_tools):
    tools = make_tools(v2(notes_added=1))

    with pytest.raises(ToolError, match="Sent 3 note\\(s\\).*reports 1 added"):
        tools["fl_send_chord"]("C")


def test_v1_script_with_extended_props_raises(make_tools):
    tools = make_tools({"status": "success", "notes_added": 1, "notes_deleted": 0})

    with pytest.raises(ToolError, match="outdated.*without pan/color/slide/porta"):
        tools["fl_send_notes"]([{"midi": 60, "duration": 1, "slide": True}])


def test_v1_script_without_extended_props_warns(make_tools):
    tools = make_tools({"status": "success", "notes_added": 1, "notes_deleted": 0})

    text = tools["fl_send_notes"]([{"midi": 60, "duration": 1}])

    assert "1 note(s) written" in text
    assert "Warning: ComposeWithLLM.pyscript in FL Studio is outdated" in text


def test_no_response_for_sent_notes_raises(make_tools, monkeypatch):
    tools = make_tools(None)
    # With a queued request the round-trip waits for a response until the
    # deadline; shorten it so the test does not sleep 5 s.
    original = piano_roll._run_pr_script
    monkeypatch.setattr(piano_roll, "_run_pr_script", lambda: original(timeout=0.3))

    with pytest.raises(ToolError, match="processed nothing"):
        tools["fl_send_notes"]([{"midi": 60, "duration": 1}])


def test_trigger_script_with_empty_queue_has_no_false_warning(make_tools):
    tools = make_tools(None)

    text = tools["fl_trigger_script"]()

    assert text == "FL Studio ran the piano roll script (nothing was queued)."


def test_trigger_script_reports_script_errors(make_tools):
    tools = make_tools(v2(status="error", message="action 0 (transpose) failed: unknown action"))
    piano_roll._write_request({"action": "transpose"})

    with pytest.raises(ToolError, match="unknown action"):
        tools["fl_trigger_script"]()


def test_delete_and_clear_report_deleted_count(make_tools):
    tools = make_tools(v2(notes_deleted=2, note_count=5))

    assert tools["fl_delete_notes"]([{"midi": 60}]) == (
        "2 note(s) deleted. The piano roll now has 5 notes."
    )
    assert tools["fl_clear_piano_roll"]() == (
        "Piano roll cleared (2 note(s) removed). The piano roll now has 5 notes."
    )


def test_notes_queued_earlier_are_counted_not_blamed(make_tools):
    # First call only queued (e.g. the script had not been run by hand yet);
    # the next call's run processes both batches.
    tools = make_tools(v2(notes_added=5, note_count=5))
    tools["fl_send_notes"]([{"midi": n, "duration": 1} for n in (60, 62, 64)], auto_trigger=False)

    text = tools["fl_send_notes"]([{"midi": 65, "duration": 1}, {"midi": 67, "duration": 1}])

    assert "2 note(s) written" in text
    assert "3 note(s) queued earlier were also written" in text


def test_mismatch_accounts_for_notes_queued_earlier(make_tools):
    tools = make_tools(v2(notes_added=3))
    tools["fl_send_notes"]([{"midi": 60, "duration": 1}], auto_trigger=False)

    with pytest.raises(ToolError, match="Sent 1 note\\(s\\) \\(plus 1 queued earlier\\).*3 added"):
        tools["fl_send_notes"]([{"midi": 62, "duration": 1}])
