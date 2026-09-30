"""Failures surface as ToolError, never as a return value that looks like a result."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.channels import register_channel_tools
from fl_studio_mcp.tools.transport import register_transport_tools
from fl_studio_mcp.utils.errors import run, unwrap
from tests.fakes import RecordingMCP


def test_unwrap_returns_successful_results_untouched():
    assert unwrap({"success": True, "count": 3}) == {"success": True, "count": 3}


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


def test_int_returning_tool_raises_instead_of_minus_one(tools):
    with pytest.raises(ToolError, match="boom on channels.getCount"):
        tools["fl_get_channel_count"]()


def test_dict_returning_tool_raises(tools):
    with pytest.raises(ToolError, match="boom on transport.getStatus"):
        tools["fl_get_transport_status"]()
