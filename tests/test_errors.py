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
