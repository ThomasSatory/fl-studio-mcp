"""fl_get_project_snapshot, and saving: controller version gate and busy retries."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.project import SNAPSHOT_TIMEOUT, register_project_tools
from fl_studio_mcp.utils import project_files
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


# --- saving (project_files.save_project) -------------------------------------

BUSY = {"success": False, "error": "FL Studio is busy and cannot save right now; retry in a "
        "moment", "busy": True}


class SaveConnection:
    def __init__(self, version=4, save_answers=None):
        self.version = version
        self.save_answers = list(save_answers or [{"success": True, "changed": 0}])
        self.sent = []

    def send_command(self, action, params=None, timeout=2.0):
        self.sent.append(action)
        if action == "system.info":
            return {"success": True, "controller_version": self.version}
        return self.save_answers.pop(0)


def test_save_needs_controller_version_4(monkeypatch):
    conn = SaveConnection(version=3)
    with pytest.raises(ToolError, match="crash"):
        project_files.save_project(conn)
    assert "transport.save" not in conn.sent


def test_other_features_still_accept_controller_version_3():
    assert project_files.controller_info(SaveConnection(version=3))["controller_version"] == 3


def test_save_retries_while_fl_is_busy(monkeypatch):
    monkeypatch.setattr(project_files.time, "sleep", lambda s: None)
    conn = SaveConnection(save_answers=[BUSY, BUSY, {"success": True, "changed": 0}])
    project_files.save_project(conn)
    assert conn.sent.count("transport.save") == 3


def test_save_gives_up_after_three_busy_answers(monkeypatch):
    sleeps = []
    monkeypatch.setattr(project_files.time, "sleep", sleeps.append)
    conn = SaveConnection(save_answers=[BUSY, BUSY, BUSY, BUSY])
    with pytest.raises(ToolError, match="busy"):
        project_files.save_project(conn)
    assert conn.sent.count("transport.save") == 3
    assert sleeps == [0.5, 0.5]


def test_save_does_not_retry_other_errors(monkeypatch):
    monkeypatch.setattr(project_files.time, "sleep", lambda s: None)
    conn = SaveConnection(save_answers=[{"success": False, "error": "Timeout"}])
    with pytest.raises(ToolError, match="Timeout"):
        project_files.save_project(conn)
    assert conn.sent.count("transport.save") == 1
