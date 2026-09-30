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


def test_snapshot_assembles_all_sections(fl):
    controller, modules = fl
    modules["mixer"].getCurrentTempo.return_value = 124.0

    snapshot = controller.dispatch_command("project.snapshot", {})

    assert snapshot["transport"]["is_playing"] is False
    assert snapshot["tempo"] == 124.0
    assert snapshot["patterns"] == {
        "current": 1,
        "count": 1,
        "items": [
            {
                "index": 1,
                "name": "Pattern 1",
                "color": 0,
                "length_beats": 16,
                "is_default": False,
                "is_selected": False,
            }
        ],
    }
    assert [c["name"] for c in snapshot["channels"]] == ["Kick", "Bass"]
    assert [t["name"] for t in snapshot["mixer_tracks"]] == ["Master", "Drums"]


def test_snapshot_isolates_a_failing_section(fl):
    controller, modules = fl
    modules["patterns"].patternMax.side_effect = RuntimeError("patterns API missing")

    snapshot = controller.dispatch_command("project.snapshot", {})

    assert snapshot["patterns"] == {"error": "patterns API missing"}
    assert snapshot["tempo"] == 120.0
    assert len(snapshot["channels"]) == 2


def test_duplicate_trigger_for_same_id_is_ignored(fl):
    """A queued trigger from a timed-out command must not run the current command twice."""
    controller, modules = fl
    controller.COMMAND_FILE.write_text(
        json.dumps({"id": "req7", "action": "transport.start", "params": {}})
    )

    controller.execute_pending_command()
    controller.RESPONSE_FILE.unlink()
    controller.execute_pending_command()  # the late trigger reads the same file again

    modules["transport"].start.assert_called_once()
    assert not controller.RESPONSE_FILE.exists(), "the first response must not be replaced"

    controller.COMMAND_FILE.write_text(
        json.dumps({"id": "req8", "action": "transport.start", "params": {}})
    )
    controller.execute_pending_command()

    assert modules["transport"].start.call_count == 2
    assert json.loads(controller.RESPONSE_FILE.read_text())["id"] == "req8"


def test_channels_get_all_includes_mix_fields(fl):
    controller, _ = fl

    (kick, _bass) = controller.dispatch_command("channels.getAll", {})["channels"]

    assert kick == {"index": 0, "name": "Kick", "volume": 0.78, "pan": 0.0,
                    "is_muted": False, "is_solo": False, "is_selected": False,
                    "target_fx_track": 1}


def test_mixer_get_all_tracks_reports_total_count(fl):
    controller, _ = fl

    result = controller.dispatch_command("mixer.getAllTracks", {"include_empty": False})

    assert result["track_count"] == 3
    assert [t["name"] for t in result["tracks"]] == ["Master", "Drums"]


def test_plugins_get_params_bundles_plugin_info(fl):
    controller, modules = fl
    plugins = modules["plugins"]
    plugins.isValid.return_value = True
    plugins.getPluginName.return_value = "Sytrus"
    plugins.getParamCount.return_value = 2
    plugins.getPresetCount.return_value = 12
    plugins.getColor.return_value = 0xFF8800
    plugins.getParamName.side_effect = lambda i, *_: ["Cutoff", "Res"][i]
    plugins.getParamValue.return_value = 0.5
    plugins.getParamValueString.return_value = "50%"

    result = controller.dispatch_command("plugins.getParams", {"index": 3, "slot_index": 2})

    assert {k: v for k, v in result.items() if k != "params"} == {
        "valid": True, "name": "Sytrus", "param_count": 2, "preset_count": 12,
        "color": "0xff8800",
    }
    assert [p["name"] for p in result["params"]] == ["Cutoff", "Res"]
    plugins.getParamCount.assert_called_with(3, 2, True)


def test_plugins_get_params_invalid_plugin_is_not_an_error(fl):
    controller, modules = fl
    modules["plugins"].isValid.return_value = False

    result = controller.dispatch_command("plugins.getParams", {"index": 9})

    assert result == {"valid": False, "params": []}
    modules["plugins"].getParamCount.assert_not_called()


@pytest.mark.parametrize(
    ("action", "params"),
    [("channels.getAll", {}), ("channels.getInfo", {"index": 0}),
     ("channels.getSelected", {}), ("channels.setVolume", {"index": 0, "volume": 0.5})],
)
def test_channel_volume_is_read_normalized_with_global_index(fl, action, params):
    controller, modules = fl
    modules["channels"].selectedChannel.return_value = 0

    controller.dispatch_command(action, params)

    for call in modules["channels"].getChannelVolume.call_args_list:
        # The 2nd positional argument is `mode` (True = dB), not the index mode.
        assert len(call.args) == 1 and call.kwargs == {"useGlobalIndex": True}, call


@pytest.mark.parametrize(
    ("action", "params", "setter", "value"),
    [
        ("channels.setVolume", {"index": 1, "volume": 0.5}, "setChannelVolume", 0.5),
        ("channels.setPan", {"index": 1, "pan": -0.25}, "setChannelPan", -0.25),
    ],
)
def test_channel_setters_disable_pickup_and_use_global_index(fl, action, params, setter, value):
    controller, modules = fl

    controller.dispatch_command(action, params)

    # The 3rd positional argument is pickupMode: True (= 1) would enable pickup
    # and FL could ignore the value; the index must be global, like every tool.
    getattr(modules["channels"], setter).assert_called_once_with(
        1, value, modules["midi"].PIM_None, True
    )
