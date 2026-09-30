"""Tools that absorbed the removed duplicates (no FL Studio needed)."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.channels import register_channel_tools
from fl_studio_mcp.tools.mixer import register_mixer_tools
from fl_studio_mcp.tools.plugins import register_plugin_tools
from tests.fakes import RecordingMCP


class FakeConnection:
    def __init__(self, response=None):
        self.sent = []
        self.response = response or {"success": True}

    def send_command(self, action, params=None, timeout=2.0):
        self.sent.append((action, params))
        return self.response


def make_tools(monkeypatch, conn):
    import fl_studio_mcp.utils.connection as connection

    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    mcp = RecordingMCP()
    for register in (register_channel_tools, register_mixer_tools, register_plugin_tools):
        register(mcp)
    return mcp.tools


def test_select_channel_exclusive_uses_select_one(monkeypatch):
    conn = FakeConnection({"success": True, "channel_name": "Kick"})
    tools = make_tools(monkeypatch, conn)

    assert tools["fl_select_channel"](0, exclusive=True) == "Channel 'Kick' selected exclusively"
    assert conn.sent == [("channels.selectOne", {"index": 0})]


def test_select_channel_default_is_unchanged(monkeypatch):
    conn = FakeConnection({"success": True, "channel_name": "Kick"})
    tools = make_tools(monkeypatch, conn)

    tools["fl_select_channel"](0)

    assert conn.sent == [("channels.select", {"index": 0, "select": True})]


def test_exclusive_deselect_is_rejected(monkeypatch):
    conn = FakeConnection()
    tools = make_tools(monkeypatch, conn)

    with pytest.raises(ToolError, match="exclusive=True requires select=True"):
        tools["fl_select_channel"](0, select=False, exclusive=True)
    assert conn.sent == []


def test_all_mixer_tracks_returns_count_and_tracks(monkeypatch):
    tracks = [{"index": 0, "name": "Master"}]
    tools = make_tools(monkeypatch, FakeConnection(
        {"success": True, "track_count": 127, "tracks": tracks}
    ))

    assert tools["fl_get_all_mixer_tracks"]() == {"track_count": 127, "tracks": tracks}


def test_plugin_params_passes_plugin_info_through(monkeypatch):
    response = {"success": True, "valid": True, "name": "Sytrus", "param_count": 2,
                "preset_count": 12, "color": "0xff8800", "params": []}
    tools = make_tools(monkeypatch, FakeConnection(response))

    assert tools["fl_get_plugin_params"](3) == {
        "valid": True, "name": "Sytrus", "param_count": 2, "preset_count": 12,
        "color": "0xff8800", "params": [],
    }


def test_plugin_params_invalid(monkeypatch):
    tools = make_tools(monkeypatch, FakeConnection({"success": True, "valid": False, "params": []}))

    assert tools["fl_get_plugin_params"](9) == {"valid": False, "params": []}


def test_connection_status_reconnect_resets_first(monkeypatch):
    import fl_studio_mcp.server as server

    calls = []

    class Conn:
        def ensure_connected(self):
            calls.append("ensure")

        def get_status(self):
            return {"connected": True, "port_name": "IAC", "available_ports": ["IAC"]}

    monkeypatch.setattr(server, "reset_connection", lambda: calls.append("reset"))
    monkeypatch.setattr(server, "get_connection", lambda: Conn())

    status = server.fl_connection_status(reconnect=True)

    assert calls == ["reset", "ensure"]
    assert status["connected"] is True
