"""Each command carries an id; only the response with that id counts."""

from __future__ import annotations

import json
import os
import threading
import time

from fl_studio_mcp.utils import midi_connection
from tests.fakes import make_connection


def test_command_file_carries_id_and_is_written_atomically(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(midi_connection.os, "replace", spy)

    def fl_side():
        command = json.loads(conn._command_file.read_text())
        assert command["action"] == "transport.stop"
        assert len(command["id"]) == 32
        conn._response_file.write_text(json.dumps({"id": command["id"], "success": True}))

    conn._port.on_send = fl_side

    result = conn.send_command("transport.stop", timeout=1.0)

    assert result["success"] is True
    assert replaced == [(str(conn._command_file) + ".tmp", str(conn._command_file))]
    assert not (tmp_path / "Hardware" / "FLStudioMCP" / "mcp_command.json.tmp").exists()


def test_write_command_retries_replace_on_permission_error(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)
    attempts = []
    real_replace = os.replace

    def flaky(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("locked by reader")
        real_replace(src, dst)

    monkeypatch.setattr(midi_connection.os, "replace", flaky)

    conn._write_command({"id": "abc", "action": "x", "params": {}})

    assert len(attempts) == 3
    assert json.loads(conn._command_file.read_text())["id"] == "abc"


def test_response_for_another_request_is_ignored(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)

    def fl_side():
        command = json.loads(conn._command_file.read_text())
        conn._response_file.write_text(json.dumps({"id": "stale", "success": True, "stale": True}))

        def late():
            time.sleep(0.15)
            conn._response_file.write_text(
                json.dumps({"id": command["id"], "success": True, "fresh": True})
            )

        threading.Thread(target=late, daemon=True).start()

    conn._port.on_send = fl_side

    result = conn.send_command("transport.getStatus", timeout=2.0)

    assert result.get("fresh") is True
    assert "stale" not in result
    assert not conn._response_file.exists()


def test_legacy_response_without_id_reports_outdated_controller(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)
    conn._port.on_send = lambda: conn._response_file.write_text(json.dumps({"success": True}))

    result = conn.send_command("transport.getStatus", timeout=0.3)

    assert result["success"] is False
    assert "outdated" in result["error"]
    assert "device_FLStudioMCP.py" in result["error"]


def test_no_response_at_all_is_a_plain_timeout(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)

    result = conn.send_command("transport.getStatus", timeout=0.2)

    assert result["success"] is False
    assert "Timeout" in result["error"]
