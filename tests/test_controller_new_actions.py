"""Controller actions added for the arrangement, steps, mixer and automation tools."""

from __future__ import annotations

import json

import pytest

from tests.fakes import load_controller, make_fl_modules


@pytest.fixture
def fl(tmp_path, monkeypatch):
    modules = make_fl_modules()
    controller = load_controller(monkeypatch, tmp_path, modules)
    return controller, modules


def test_song_length_uses_fl_modes(fl):
    controller, modules = fl
    lengths = {0: 60000, 1: 60, 2: 11520, 3: 30, 4: 0, 5: 0}
    modules["transport"].getSongLength.side_effect = lambda mode: lengths[mode]
    modules["general"].getRecPPQ.return_value = 96
    result = controller.dispatch_command("transport.getLength", {})
    assert (result["ticks"], result["seconds"], result["milliseconds"], result["bars"]) == (
        11520, 60, 60000, 30)


def test_set_position_by_ticks_and_seconds(fl):
    controller, modules = fl
    controller.dispatch_command("transport.setPosition", {"ticks": 1536})
    modules["transport"].setSongPos.assert_called_with(1536, 2)
    controller.dispatch_command("transport.setPosition", {"seconds": 1.5})
    modules["transport"].setSongPos.assert_called_with(1500, 0)


def test_system_info_reports_version_and_loads(fl):
    controller, modules = fl
    modules["general"].getVersion.return_value = 36
    modules["general"].getChangedFlag.return_value = 0
    before = controller.dispatch_command("system.info", {})
    controller.OnProjectLoad(100)
    after = controller.dispatch_command("system.info", {})
    assert before["controller_version"] == controller.CONTROLLER_VERSION
    assert after["load_count"] == before["load_count"] + 1
    assert after["last_load_status"] == "ok"


def _trigger(controller, pme_flags):
    """Run the pending command the way FL does: from OnMidiMsg with the note's flags."""
    from types import SimpleNamespace

    controller.OnMidiMsg(SimpleNamespace(midiId=0x90, data1=127, data2=100,
                                         pmeFlags=pme_flags, handled=False))
    return json.loads(controller.RESPONSE_FILE.read_text())


def test_save_passes_the_trigger_events_pme_flags(fl):
    controller, modules = fl
    modules["general"].getChangedFlag.return_value = 0
    controller.COMMAND_FILE.write_text(json.dumps(
        {"id": "s1", "action": "transport.save", "params": {}}))
    response = _trigger(controller, 111)  # what FL 2025 gives the trigger note
    assert response["success"] is True
    modules["transport"].globalTransport.assert_called_once_with(92, 1, 111)
    assert controller._pme_flags is None  # not kept for a later command


def test_save_new_version_passes_flags_too(fl):
    controller, modules = fl
    modules["general"].getChangedFlag.return_value = 0
    controller.COMMAND_FILE.write_text(json.dumps(
        {"id": "s2", "action": "transport.save", "params": {"new_version": True}}))
    _trigger(controller, 111)
    modules["transport"].globalTransport.assert_called_once_with(93, 1, 111)


@pytest.mark.parametrize("flags", [0, 2, 1 | 2 | 8])  # no PME_System_Safe bit
def test_save_without_safe_flags_is_refused_without_calling_fl(fl, flags):
    controller, modules = fl
    controller.COMMAND_FILE.write_text(json.dumps(
        {"id": "s3-%d" % flags, "action": "transport.save", "params": {}}))
    response = _trigger(controller, flags)
    assert response["success"] is False
    assert "busy and cannot save" in response["error"]
    modules["transport"].globalTransport.assert_not_called()


def test_save_outside_a_midi_event_is_refused(fl):
    controller, modules = fl
    result = controller.dispatch_command("transport.save", {})
    assert "busy and cannot save" in result["error"]
    modules["transport"].globalTransport.assert_not_called()


def test_controller_version_is_4(fl):
    controller, _ = fl
    assert controller.CONTROLLER_VERSION == 4


def test_track_volume_in_db_bisects_the_fader(fl):
    controller, modules = fl
    mixer = modules["mixer"]
    state = {"v": 0.8}
    mixer.setTrackVolume.side_effect = lambda t, v, pickup: state.update(v=v)
    # A fake curve: dB rises monotonically with the fader.
    mixer.getTrackVolume.side_effect = lambda t, mode=0: (
        state["v"] if mode == 0 else 40 * (state["v"] - 0.8))
    result = controller.dispatch_command("mixer.setTrackVolume", {"track": 1, "db": -6.0})
    assert abs(result["volume_db"] + 6.0) < 0.01
    assert all(call.args[2] == 0 for call in mixer.setTrackVolume.call_args_list)  # PIM_None


def test_set_steps_writes_pitch_in_the_given_pattern(fl):
    controller, modules = fl
    channels, patterns = modules["channels"], modules["patterns"]
    bits = {}
    channels.setGridBit.side_effect = lambda ch, pos, val, g: bits.__setitem__(pos, val)
    channels.getGridBit.side_effect = lambda ch, pos, g: bits.get(pos, 0)
    pitches = {}
    channels.setStepParameterByIndex.side_effect = (
        lambda ch, pat, step, param, value, g: pitches.__setitem__((step, param), value))
    channels.getCurrentStepParam.side_effect = lambda ch, step, param, g: pitches.get(
        (step, param), 60)
    result = controller.dispatch_command("channels.setSteps", {
        "channel": 1, "pattern": 2,
        "steps": [{"step": 0, "pitch": 41, "velocity": 120}, {"step": 6, "pitch": 41}]})
    patterns.jumpToPattern.assert_called_with(2)
    assert result["mismatched"] == []
    assert bits[0] == 1 and bits[6] == 1 and bits[3] == 0
    assert pitches[(0, 0)] == 41 and pitches[(0, 1)] == 120


def test_set_route_with_level(fl):
    controller, modules = fl
    mixer = modules["mixer"]
    routes = set()
    mixer.setRouteTo.side_effect = lambda s, d, on, *a: (routes.add((s, d)) if on
                                                          else routes.discard((s, d)))
    mixer.getRouteSendActive.side_effect = lambda s, d: (s, d) in routes
    mixer.getRouteToLevel.return_value = 0.5
    result = controller.dispatch_command("mixer.setRoute", {"source": 1, "dest": 2,
                                                            "level": 0.5})
    assert result["active"] is True and result["level"] == 0.5
    mixer.afterRoutingChanged.assert_called()


def test_route_to_itself_is_refused(fl):
    controller, _ = fl
    assert "itself" in controller.dispatch_command(
        "mixer.setRoute", {"source": 1, "dest": 1})["error"]


def test_automation_targets_resolve_to_rec_ids(fl):
    controller, modules = fl
    modules["mixer"].getTrackPluginId.side_effect = lambda t, s: ((t << 6) + s) << 16
    modules["channels"].getRecEventId.side_effect = lambda ch, g=False: ch << 16
    modules["plugins"].isValid.return_value = True
    modules["plugins"].getParamCount.return_value = 3
    modules["plugins"].getParamName.side_effect = lambda i, *a: ["Mix", "Cutoff", "Res"][i]
    modules["plugins"].getPluginName.return_value = "Filter"
    result = controller.dispatch_command("automation.resolve", {"targets": [
        {"type": "tempo"},
        {"type": "mixer_volume", "track": 1},
        {"type": "effect_param", "track": 1, "slot": 2, "param": "cutoff"},
        {"type": "channel_pitch", "channel": 1},
    ]})
    ids = [t["rec_id"] for t in result["targets"]]
    assert ids == [1073741829, 0x20401FC0, 0x20000000 + (((1 << 6) + 2) << 16) + 0x8001,
                   (1 << 16) + 4]


def test_automation_unknown_target(fl):
    controller, _ = fl
    assert "unknown target" in controller.dispatch_command(
        "automation.resolve", {"targets": [{"type": "warp"}]})["error"]


def test_playlist_track_is_one_based(fl):
    controller, modules = fl
    playlist = modules.get("playlist")
    assert playlist is not None
    playlist.trackCount.return_value = 500
    playlist.getTrackName.return_value = "Drums"
    playlist.getTrackColor.return_value = 0
    playlist.isTrackMuted.return_value = 0
    playlist.isTrackSolo.return_value = 0
    result = controller.dispatch_command("playlist.setTrack", {"index": 1, "name": "Drums",
                                                               "color": [255, 0, 0]})
    playlist.setTrackName.assert_called_with(1, "Drums")
    playlist.setTrackColor.assert_called_with(1, 0xFF0000)
    assert result["name"] == "Drums"
    assert "out of range" in controller.dispatch_command("playlist.setTrack",
                                                         {"index": 0})["error"]


# --- mixer volume on automated faders, track validation, feedback loops ------

def _lagging_fader(mixer, start=0.0):
    """A fader whose readback answers the value set one call earlier (automated track)."""
    state = {"set": start, "shown": start}

    def set_volume(t, v, pickup):
        state["shown"], state["set"] = state["set"], v

    mixer.setTrackVolume.side_effect = set_volume
    mixer.getTrackVolume.side_effect = lambda t, mode=0: (
        state["shown"] if mode == 0 else 40 * (state["shown"] - 0.8))
    return state


def test_db_on_a_lagging_fader_returns_an_automation_error(fl):
    controller, modules = fl
    mixer = modules["mixer"]
    _lagging_fader(mixer, start=0.0)
    result = controller.dispatch_command("mixer.setTrackVolume", {"track": 0, "db": 0.0})
    assert result.get("automated") is True
    assert "automat" in result["error"]
    # Stopped at the first mismatch and put the fader back where it was.
    assert mixer.setTrackVolume.call_args_list[-1].args[1] == 0.0
    assert mixer.setTrackVolume.call_count == 2


def test_fader_position_on_a_lagging_fader_returns_an_automation_error(fl):
    controller, modules = fl
    _lagging_fader(modules["mixer"], start=0.0)
    result = controller.dispatch_command("mixer.setTrackVolume", {"track": 0, "volume": 0.8})
    assert "automat" in result["error"]


def test_volume_readback_waits_for_two_equal_reads(fl):
    controller, modules = fl
    mixer = modules["mixer"]
    reads = iter([0.1, 0.5, 0.5, 0.5, 0.5])  # stale first answer, then the new value
    mixer.getTrackVolume.side_effect = lambda t, mode=0: next(reads) if mode == 0 else -1.9
    result = controller.dispatch_command("mixer.setTrackVolume", {"track": 1, "volume": 0.5})
    assert "error" not in result and result["volume"] == 0.5


@pytest.mark.parametrize("action, extra", [
    ("mixer.setTrackVolume", {"db": -6.0}),
    ("mixer.setTrackVolume", {"volume": 0.5}),
    ("mixer.setTrackPan", {"pan": 0.2}),
    ("mixer.getTrackInfo", {}),
    ("mixer.muteTrack", {"muted": True}),
    ("mixer.soloTrack", {"solo": True}),
    ("mixer.armTrack", {}),
    ("mixer.setTrackName", {"name": "X"}),
    ("mixer.setTrackColor", {"r": 1, "g": 2, "b": 3}),
    ("mixer.setStereoSep", {"separation": 0.1}),
])
def test_mixer_handlers_reject_an_out_of_range_track(fl, action, extra):
    controller, modules = fl
    result = controller.dispatch_command(action, {"track": 999, **extra})
    assert result["error"] == "Mixer track 999 out of range (0..2)"
    mixer = modules["mixer"]
    for setter in (mixer.setTrackVolume, mixer.setTrackPan, mixer.muteTrack, mixer.soloTrack,
                   mixer.armTrack, mixer.setTrackName, mixer.setTrackColor,
                   mixer.setTrackStereoSep):
        setter.assert_not_called()


def test_peaks_reject_an_out_of_range_track(fl):
    controller, _ = fl
    assert "out of range" in controller.dispatch_command(
        "mixer.getPeaks", {"tracks": [0, 7]})["error"]


def _routes(mixer, existing, count=5):
    routes = set(existing)
    mixer.trackCount.return_value = count
    mixer.setRouteTo.side_effect = lambda s, d, on, *a: (routes.add((s, d)) if on
                                                          else routes.discard((s, d)))
    mixer.getRouteSendActive.side_effect = lambda s, d: (s, d) in routes
    return routes


def test_set_route_refuses_a_feedback_loop(fl):
    controller, modules = fl
    mixer = modules["mixer"]
    _routes(mixer, {(3, 4), (4, 1)})
    result = controller.dispatch_command("mixer.setRoute", {"source": 1, "dest": 3})
    assert result["error"] == ("FL refused the route: insert 3 already feeds insert 1 "
                               "(feedback loop: 3 -> 4 -> 1 -> 3)")
    mixer.setRouteTo.assert_not_called()


def test_set_route_not_created_does_not_blame_the_api_version(fl):
    controller, modules = fl
    mixer = modules["mixer"]
    routes = _routes(mixer, set())
    mixer.setRouteTo.side_effect = lambda *a: None  # FL ignores it
    result = controller.dispatch_command("mixer.setRoute", {"source": 1, "dest": 2,
                                                            "level": 0.5})
    assert "did not create the route" in result["error"]
    assert "API" not in result["error"]
    mixer.setRouteToLevel.assert_not_called()
    assert routes == set()


def test_existing_route_is_not_treated_as_a_loop(fl):
    controller, modules = fl
    mixer = modules["mixer"]
    _routes(mixer, {(1, 2), (2, 1)})
    mixer.getRouteToLevel.return_value = 0.3
    result = controller.dispatch_command("mixer.setRoute", {"source": 1, "dest": 2,
                                                            "level": 0.3})
    assert result["active"] is True and result["level"] == 0.3
