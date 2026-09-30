"""fl_get_project_snapshot: one round-trip, longer timeout, sections passed through."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.project import SNAPSHOT_TIMEOUT, register_project_tools
from tests.fakes import RecordingMCP

SNAPSHOT = {
    "transport": {"is_playing": False},
    "tempo": 120.0,
    "patterns": {"current": 1, "count": 1, "items": []},
    "channels": [],
    "mixer_tracks": [],
}


class FakeConnection:
    def __init__(self, response):
        self.sent = []
        self.response = response

    def send_command(self, action, params=None, timeout=2.0):
        self.sent.append((action, params, timeout))
        return self.response


def make_tool(monkeypatch, conn):
    import fl_studio_mcp.utils.connection as connection

    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    mcp = RecordingMCP()
    register_project_tools(mcp)
    return mcp.tools["fl_get_project_snapshot"]


def test_snapshot_is_one_command_with_longer_timeout(monkeypatch):
    conn = FakeConnection({"success": True, **SNAPSHOT})
    tool = make_tool(monkeypatch, conn)

    assert tool() == SNAPSHOT
    assert conn.sent == [("project.snapshot", None, SNAPSHOT_TIMEOUT)]
    assert SNAPSHOT_TIMEOUT > 2.0


def test_snapshot_failure_raises(monkeypatch):
    tool = make_tool(monkeypatch, FakeConnection({"success": False, "error": "Timeout"}))

    with pytest.raises(ToolError, match="Timeout"):
        tool()
