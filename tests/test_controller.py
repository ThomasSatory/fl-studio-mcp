"""The controller script, executed under pytest with fake FL Studio modules."""

from __future__ import annotations

import json

import pytest

from tests.fakes import load_controller, make_fl_modules


@pytest.fixture
def fl(tmp_path, monkeypatch):
    modules = make_fl_modules()
    controller = load_controller(monkeypatch, tmp_path, modules)
    return controller, modules


def test_controller_dispatches_with_fake_modules(fl):
    controller, modules = fl
    modules["transport"].isPlaying.return_value = 1

    result = controller.dispatch_command("transport.getStatus", {})

    assert result["is_playing"] is True
    assert result["loop_mode"] == "pattern"


def test_execute_pending_command_writes_response_file(fl):
    controller, _ = fl
    controller.COMMAND_FILE.write_text(json.dumps({"action": "transport.getStatus", "params": {}}))

    controller.execute_pending_command()

    response = json.loads(controller.RESPONSE_FILE.read_text())
    assert response["success"] is True
    assert response["is_playing"] is False


def test_execute_pending_command_echoes_request_id(fl):
    controller, _ = fl
    controller.COMMAND_FILE.write_text(
        json.dumps({"id": "req42", "action": "transport.getStatus", "params": {}})
    )

    controller.execute_pending_command()

    assert json.loads(controller.RESPONSE_FILE.read_text())["id"] == "req42"


def test_invalid_command_json_still_answers_with_null_id(fl):
    controller, _ = fl
    controller.COMMAND_FILE.write_text("{not json")

    controller.execute_pending_command()

    response = json.loads(controller.RESPONSE_FILE.read_text())
    assert response["success"] is False
    assert response["id"] is None
    assert "Invalid JSON" in response["error"]


def test_write_response_serialises_before_opening_the_file(fl):
    controller, _ = fl
    controller.RESPONSE_FILE.write_text('{"id": "keep-me", "success": true}')

    controller.write_response({"id": "x", "bad": object()})  # not JSON serialisable

    assert json.loads(controller.RESPONSE_FILE.read_text())["id"] == "keep-me"


def test_get_tempo_reads_mixer(fl):
    controller, modules = fl
    modules["mixer"].getCurrentTempo.return_value = 133.5

    assert controller.dispatch_command("transport.getTempo", {}) == {"tempo": 133.5}


def test_set_tempo_uses_rec_event_and_reads_back(fl):
    controller, modules = fl
    midi = modules["midi"]
    modules["mixer"].getCurrentTempo.return_value = 128.0

    result = controller.dispatch_command("transport.setTempo", {"bpm": 128})

    modules["general"].processRECEvent.assert_called_once_with(
        midi.REC_Tempo, 128000, midi.REC_Control | midi.REC_UpdateValue | midi.REC_UpdateControl
    )
    assert result == {"requested": 128.0, "tempo": 128.0, "applied": True}


def test_set_tempo_reports_when_fl_did_not_apply(fl):
    controller, modules = fl
    modules["mixer"].getCurrentTempo.return_value = 120.0

    result = controller.dispatch_command("transport.setTempo", {"bpm": 140})

    assert result["applied"] is False
    assert result["tempo"] == 120.0


@pytest.mark.parametrize("bpm", [9.9, 522.1, 0])
def test_set_tempo_rejects_out_of_range(fl, bpm):
    controller, modules = fl

    result = controller.dispatch_command("transport.setTempo", {"bpm": bpm})

    assert "out of range" in result["error"]
    modules["general"].processRECEvent.assert_not_called()
