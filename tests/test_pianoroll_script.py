"""ComposeWithLLM.pyscript executed under pytest with a fake flpianoroll."""

from __future__ import annotations

import json

import pytest

from tests.fakes import load_piano_roll_script


@pytest.fixture
def pr(tmp_path, monkeypatch):
    return load_piano_roll_script(monkeypatch, tmp_path)


def queue(script, requests):
    with open(script.REQUEST_FILE, "w") as f:
        json.dump(requests, f)


def note(midi, **extra):
    return {"midi": midi, "time": 0.0, "duration": 1.0, "velocity": 0.8, **extra}


def test_extended_properties_are_applied(pr):
    script, flp = pr
    queue(script, [{"action": "add_notes", "notes": [
        note(60, pan=0.2, color=5, slide=True, porta=True, time=1.5)
    ]}])

    result = script.process_mcp_request()

    (added,) = flp.score.notes
    assert (added.number, added.time, added.length) == (60, 144, 96)
    assert (added.pan, added.color, added.slide, added.porta) == (0.2, 5, True, True)
    assert result["status"] == "success"
    assert result["notes_added"] == 1
    assert result["script_version"] == 2
    assert result["note_count"] == 1


def test_partial_failure_reports_counts_and_failures(pr):
    script, flp = pr
    queue(script, [
        {"action": "add_notes", "notes": [note(60), note(62)]},
        {"action": "add_notes", "notes": [note(64), {"time": 0, "duration": 1}]},
        {"action": "delete_notes", "notes": [{"midi": 60, "time": 0}]},
    ])

    result = script.process_mcp_request()

    assert result["status"] == "error"
    assert result["notes_added"] == 3  # 60, 62, then 64 before the broken note
    assert result["notes_deleted"] == 1  # later actions still run
    assert result["failures"] == [
        {"index": 1, "action": "add_notes", "message": result["failures"][0]["message"]}
    ]
    assert "action 1 (add_notes)" in result["message"]
    assert result["note_count"] == len(flp.score.notes) == 2


def test_unknown_action_is_a_failure(pr):
    script, _ = pr
    queue(script, [{"action": "transpose", "semitones": 2}])

    result = script.process_mcp_request()

    assert result["status"] == "error"
    assert result["failures"][0]["action"] == "transpose"
    assert "unknown action" in result["failures"][0]["message"]


def test_queue_is_emptied_even_after_a_failure(pr):
    script, _ = pr
    queue(script, [{"action": "transpose"}])

    script.process_mcp_request()

    with open(script.REQUEST_FILE) as f:
        assert json.load(f) == []


def test_clear_then_add_counts_both(pr):
    script, flp = pr
    queue(script, [{"action": "add_notes", "notes": [note(60), note(64)]}])
    script.process_mcp_request()
    queue(script, [{"action": "clear"}, {"action": "add_notes", "notes": [note(67)]}])

    result = script.process_mcp_request()

    assert (result["notes_deleted"], result["notes_added"], result["note_count"]) == (2, 1, 1)
    assert [n.number for n in flp.score.notes] == [67]


def test_empty_queue_answers_nothing(pr):
    script, _ = pr
    queue(script, [])
    assert script.process_mcp_request() is None


def test_apply_writes_response_with_version(pr):
    script, _ = pr
    queue(script, [{"action": "add_notes", "notes": [note(60)]}])

    script.apply()

    with open(script.RESPONSE_FILE) as f:
        response = json.load(f)
    with open(script.STATE_FILE) as f:
        state = json.load(f)
    assert response["script_version"] == 2
    assert state["noteCount"] == 1
