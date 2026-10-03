"""Failures surface as ToolError, never as a return value that looks like a result."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.channels import register_channel_tools
from fl_studio_mcp.tools.transport import register_transport_tools
from fl_studio_mcp.utils.errors import run, unwrap
from tests.fakes import RecordingMCP


def test_unwrap_returns_successful_results_without_the_envelope():
    result = {"id": "a1b2", "success": True, "count": 3}
    assert unwrap(result) == {"count": 3}
    assert result == {"id": "a1b2", "success": True, "count": 3}  # not modified in place


def test_run_results_do_not_leak_id_and_success():
    class Answer:
        def send_command(self, action, params=None, timeout=2.0):
            return {"id": "r9", "success": True, "track": 1, "routes": []}

    assert run(Answer(), "mixer.getRouting", {"track": 1}) == {"track": 1, "routes": []}


def test_routing_and_effects_tools_do_not_leak_envelope_keys(monkeypatch):
    import fl_studio_mcp.utils.connection as connection
    from fl_studio_mcp.tools.mixer import register_mixer_tools

    class Conn:
        def send_command(self, action, params=None, timeout=2.0):
            if action == "system.info":
                return {"id": "s", "success": True, "controller_version": 4}
            return {"id": "q", "success": True, "track": 1, "routes": [], "effects": []}

    monkeypatch.setattr(connection, "get_connection", lambda: Conn())
    mcp = RecordingMCP()
    register_mixer_tools(mcp)
    for name in ("fl_get_mixer_routing", "fl_list_effects"):
        result = mcp.tools[name](track=1)
        assert "id" not in result and "success" not in result


@pytest.mark.parametrize(
    "result",
    [
        {"success": False, "error": "Timeout waiting for FL Studio"},
        {"error": "patterns module not available"},
        {"success": False, "error": None},
    ],
)
def test_unwrap_raises_on_failures(result):
    with pytest.raises(ToolError):
        unwrap(result)


def test_unwrap_message_is_the_controller_error():
    with pytest.raises(ToolError, match="Timeout waiting"):
        unwrap({"success": False, "error": "Timeout waiting for FL Studio"})


def test_run_converts_connection_errors():
    class Broken:
        def send_command(self, action, params=None, timeout=2.0):
            raise RuntimeError("No virtual MIDI port found")

    with pytest.raises(ToolError, match="No virtual MIDI port"):
        run(Broken(), "transport.stop")


def test_run_forwards_action_params_and_timeout():
    sent = []

    class Recorder:
        def send_command(self, action, params=None, timeout=2.0):
            sent.append((action, params, timeout))
            return {"success": True}

    run(Recorder(), "mixer.muteTrack", {"track": 1}, timeout=4.0)

    assert sent == [("mixer.muteTrack", {"track": 1}, 4.0)]


class FailingConnection:
    def send_command(self, action, params=None, timeout=2.0):
        return {"success": False, "error": f"boom on {action}"}


@pytest.fixture
def tools(monkeypatch):
    import fl_studio_mcp.utils.connection as connection

    monkeypatch.setattr(connection, "get_connection", lambda: FailingConnection())
    mcp = RecordingMCP()
    register_transport_tools(mcp)
    register_channel_tools(mcp)
    return mcp.tools


def test_string_returning_tool_raises(tools):
    with pytest.raises(ToolError, match="boom on transport.stop"):
        tools["fl_stop"]()


def test_list_returning_tool_raises_instead_of_empty_list(tools):
    with pytest.raises(ToolError, match="boom on channels.getAll"):
        tools["fl_get_all_channels"]()


def test_dict_returning_tool_raises(tools):
    with pytest.raises(ToolError, match="boom on transport.getStatus"):
        tools["fl_get_transport_status"]()


def test_set_track_volume_refuses_a_fader_above_1(monkeypatch):
    import fl_studio_mcp.utils.connection as connection
    from fl_studio_mcp.tools.mixer import register_mixer_tools

    sent = []

    class Conn:
        def send_command(self, action, params=None, timeout=2.0):
            sent.append(action)
            return {"success": True, "volume": 1.0, "volume_db": 5.6}

    monkeypatch.setattr(connection, "get_connection", lambda: Conn())
    mcp = RecordingMCP()
    register_mixer_tools(mcp)
    with pytest.raises(ToolError, match="between 0.0 and 1.0"):
        mcp.tools["fl_set_track_volume"](track=1, volume=1.1)
    assert sent == []
    assert "volume set to 1.000" in mcp.tools["fl_set_track_volume"](track=1, volume=1.0)


def test_operation_unsafe_is_explained(monkeypatch, tmp_path):
    from fl_studio_mcp.utils import errors

    class Conn:
        def send_command(self, action, params=None, timeout=2.0):
            return {"success": False, "error": "Operation unsafe at current time"}

    with pytest.raises(errors.FLBusyError) as e:
        errors.run(Conn(), "mixer.setTrackVolume", {})
    assert "modal window" in str(e.value) and "crash window" in str(e.value)
    assert not isinstance(e.value, errors.FLCrashedError)

    folder = errors.crash_logs_dir()
    folder.mkdir(parents=True)
    (folder / "Crash 1.log").write_text("x")
    with pytest.raises(errors.FLCrashedError, match="FL Studio crashed: its crash window"):
        errors.unwrap({"success": False, "error": "Operation unsafe at current time"})


def test_crash_logs_before_the_last_successful_command_are_old(monkeypatch):
    import os
    import time

    from fl_studio_mcp.utils import errors

    folder = errors.crash_logs_dir()
    folder.mkdir(parents=True)
    log = folder / "Crash old.log"
    log.write_text("x")
    os.utime(log, (time.time() - 100, time.time() - 100))
    errors.note_answer("mixer.setTrackVolume", {"success": True})  # a change FL carried out
    errors.note_answer("system.info", {"success": True})  # answers even after a crash
    assert errors.crash_logs_since(errors.last_ok()) == []
    with pytest.raises(errors.FLBusyError) as e:
        errors.unwrap({"success": False, "error": "Operation unsafe at current time"})
    assert not isinstance(e.value, errors.FLCrashedError)


class _Midi:
    def __init__(self, answer):
        self.answer = answer

    def send_command(self, action, params=None, timeout=2.0):
        if isinstance(self.answer, Exception):
            raise self.answer
        return dict(self.answer)


def _connection(monkeypatch, answer):
    import fl_studio_mcp.utils.connection as connection

    monkeypatch.setattr(connection, "get_midi_connection", lambda: _Midi(answer))
    return connection.FLConnection()


def _crash_log(age: float = 0.0):
    import os
    import time

    from fl_studio_mcp.utils import errors

    folder = errors.crash_logs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / f"Crash {age}.log"
    log.write_text("x")
    when = time.time() - age
    os.utime(log, (when, when))
    return log


def test_connection_reports_the_crash_window_for_any_command(monkeypatch):
    from fl_studio_mcp.utils import errors

    unsafe = {"success": False, "error": "Operation unsafe at current time"}
    conn = _connection(monkeypatch, unsafe)
    assert conn.send_command("mixer.getTrackInfo") == unsafe  # no crash log: a modal
    log = _crash_log()
    with pytest.raises(errors.FLCrashedError, match="FL Studio crashed") as e:
        conn.send_command("mixer.getTrackInfo")
    assert e.value.log == log


def test_connection_reports_the_crash_window_from_a_connection_error(monkeypatch):
    from fl_studio_mcp.utils import errors

    conn = _connection(monkeypatch, RuntimeError("Operation unsafe at current time"))
    _crash_log()
    with pytest.raises(errors.FLCrashedError):
        conn.send_command("channels.getAll")


def test_reads_answered_after_the_crash_do_not_hide_its_log(monkeypatch):
    import time

    from fl_studio_mcp.utils import errors

    # FL crashed 60 s ago, after it started; reads kept answering since then.
    monkeypatch.setattr(errors, "fl_started_at", lambda: time.time() - 3600)
    _crash_log(age=60)
    errors.note_answer("mixer.getTrackInfo", {"success": True})
    conn = _connection(monkeypatch, {"success": False,
                                     "error": "Operation unsafe at current time"})
    with pytest.raises(errors.FLCrashedError):
        conn.send_command("patterns.getAll")


def test_a_crash_log_older_than_the_running_fl_is_not_its_crash(monkeypatch):
    import time

    from fl_studio_mcp.utils import errors

    monkeypatch.setattr(errors, "fl_started_at", lambda: time.time() - 30)
    _crash_log(age=600)  # the FL that ran before this one
    unsafe = {"success": False, "error": "Operation unsafe at current time"}
    conn = _connection(monkeypatch, unsafe)
    assert conn.send_command("patterns.getAll") == unsafe
    with pytest.raises(errors.FLBusyError) as e:
        errors.unwrap(unsafe)
    assert not isinstance(e.value, errors.FLCrashedError)


def test_crash_logs_written_during_a_render_are_ignored(monkeypatch):
    from fl_studio_mcp.utils import errors

    conn = _connection(monkeypatch, {"success": False,
                                     "error": "Operation unsafe at current time"})
    with errors.render_window():
        _crash_log()
    assert conn.send_command("patterns.getAll")["success"] is False


def test_successful_answers_are_not_checked(monkeypatch):
    conn = _connection(monkeypatch, {"success": True, "value": 1})
    _crash_log()
    assert conn.send_command("mixer.getTrackInfo")["value"] == 1


def test_fl_started_at_parses_ps(monkeypatch):
    import subprocess
    import time

    from fl_studio_mcp.utils import errors

    monkeypatch.undo()  # the conftest stub
    monkeypatch.setattr(errors.platform, "system", lambda: "Darwin")

    def fake_run(cmd, **kwargs):
        out = "12\n34\n" if cmd[0] == "pgrep" else (
            "Sat Oct  3 16:05:49 2026\nSun Sep 20 16:04:33 2026\n")
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(errors.subprocess, "run", fake_run)
    expected = time.mktime(time.strptime("Sun Sep 20 16:04:33 2026", "%a %b %d %H:%M:%S %Y"))
    assert errors.fl_started_at() == expected


def test_read_actions():
    from fl_studio_mcp.utils.errors import is_read_action

    assert all(map(is_read_action, ["mixer.getTrackInfo", "plugins.isValid", "system.info",
                                    "automation.resolve", "project.snapshot"]))
    assert not any(map(is_read_action, ["mixer.setTrackVolume", "transport.save",
                                        "patterns.newEmpty", "general.undo"]))


def test_a_change_fl_carried_out_after_the_log_makes_it_old(monkeypatch):
    import time

    from fl_studio_mcp.utils import errors

    monkeypatch.setattr(errors, "fl_started_at", lambda: time.time() - 3600)
    _crash_log(age=60)
    assert errors.fresh_crash_log() is not None
    errors.note_answer("mixer.setTrackVolume", {"success": True})
    assert errors.fresh_crash_log() is None


def test_reads_report_the_crash_window_even_when_fl_answers(monkeypatch):
    import time

    from fl_studio_mcp.utils import errors

    monkeypatch.setattr(errors, "fl_started_at", lambda: time.time() - 3600)
    conn = _connection(monkeypatch, {"success": True, "tracks": []})
    assert conn.send_command("mixer.getAllTracks")["tracks"] == []
    log = _crash_log()
    monkeypatch.setattr(errors, "_crash_check", None)  # past the 5 s interval
    with pytest.raises(errors.FLCrashedError, match=r"FL Studio crashed \(crash log") as e:
        conn.send_command("mixer.getAllTracks")
    assert e.value.log == log
    # A change FL carries out means its crash window is not open.
    assert conn.send_command("mixer.setTrackVolume")["success"]
    assert conn.send_command("mixer.getAllTracks")["tracks"] == []


def test_the_crash_window_check_runs_at_most_every_few_seconds(monkeypatch):
    import time

    from fl_studio_mcp.utils import errors

    monkeypatch.setattr(errors, "fl_started_at", lambda: time.time() - 3600)
    looks = []
    real = errors.fresh_crash_log
    monkeypatch.setattr(errors, "fresh_crash_log", lambda: looks.append(1) or real())
    conn = _connection(monkeypatch, {"success": True})
    for _ in range(5):
        conn.send_command("mixer.getAllTracks")
    assert len(looks) == 1


def test_no_crash_window_check_without_fl_start_time(monkeypatch):
    conn = _connection(monkeypatch, {"success": True})
    _crash_log()
    assert conn.send_command("mixer.getAllTracks")["success"]


def test_fl_started_at_is_cached(monkeypatch):
    from fl_studio_mcp.utils import errors

    calls = []
    monkeypatch.setattr(errors, "fl_started_at", lambda: calls.append(1) or 1000.0)
    assert errors.fl_started_cached() == errors.fl_started_cached() == 1000.0
    assert len(calls) == 1


def test_no_fl_process_is_noted_once_on_stderr(monkeypatch, capsys):
    import subprocess

    from fl_studio_mcp.utils import errors

    monkeypatch.undo()
    monkeypatch.setattr(errors, "_warned_no_process", False)
    monkeypatch.setattr(errors.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(errors.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", ""))
    assert errors.fl_started_at() is None and errors.fl_started_at() is None
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count("pgrep finds no") == 1


def test_connection_status_reports_the_crash(monkeypatch):
    import time

    from fl_studio_mcp import server
    from fl_studio_mcp.utils import errors

    monkeypatch.setattr(errors, "fl_started_at", lambda: time.time() - 3600)
    log = _crash_log()

    class Conn:
        def ensure_connected(self):
            pass

        def get_status(self):
            return {"connected": True}

        def send_command(self, action, params=None, timeout=2.0):
            return {"success": True, "controller_version": 6}

    monkeypatch.setattr(server, "get_connection", lambda: Conn())
    status = server.fl_connection_status()
    assert str(log) in status["fl_crashed"] and "relaunch" in status["fl_crashed"]
