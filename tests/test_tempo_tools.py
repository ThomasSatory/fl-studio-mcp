"""fl_get_tempo / fl_set_tempo validate locally and trust only the read-back value."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.transport import register_transport_tools
from tests.fakes import RecordingMCP


class FakeConnection:
    def __init__(self, applied=True, tempo=128.0):
        self.sent = []
        self.applied = applied
        self.tempo = tempo

    def send_command(self, action, params=None, timeout=2.0):
        self.sent.append((action, params))
        if action == "transport.getTempo":
            return {"success": True, "tempo": self.tempo}
        if action == "transport.setTempo":
            return {
                "success": True,
                "requested": params["bpm"],
                "tempo": self.tempo,
                "applied": self.applied,
            }
        raise AssertionError(action)


def make_tools(monkeypatch, conn):
    import fl_studio_mcp.utils.connection as connection

    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    mcp = RecordingMCP()
    register_transport_tools(mcp)
    return mcp.tools


def test_get_tempo(monkeypatch):
    tools = make_tools(monkeypatch, FakeConnection(tempo=95.0))

    assert tools["fl_get_tempo"]() == {"tempo": 95.0}


def test_set_tempo_sends_float_bpm(monkeypatch):
    conn = FakeConnection()
    tools = make_tools(monkeypatch, conn)

    assert tools["fl_set_tempo"](128) == {"tempo": 128.0}
    assert conn.sent == [("transport.setTempo", {"bpm": 128.0})]


@pytest.mark.parametrize("bpm", [5, 600, -1])
def test_set_tempo_rejects_out_of_range_before_talking_to_fl(monkeypatch, bpm):
    conn = FakeConnection()
    tools = make_tools(monkeypatch, conn)

    with pytest.raises(ToolError, match="between 10 and 522"):
        tools["fl_set_tempo"](bpm)
    assert conn.sent == []


def test_set_tempo_raises_when_not_applied(monkeypatch):
    tools = make_tools(monkeypatch, FakeConnection(applied=False, tempo=120.0))

    with pytest.raises(ToolError) as excinfo:
        tools["fl_set_tempo"](140)
    message = str(excinfo.value)
    assert "140" in message and "120" in message
