"""Concurrent tool calls must not overwrite each other's command file."""

from __future__ import annotations

import json
import threading
import time

from tests.fakes import make_connection


def test_concurrent_commands_do_not_interleave(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)
    violations = []

    def fl_side():
        first = json.loads(conn._command_file.read_text())
        time.sleep(0.1)  # FL Studio is "busy": the file must not change meanwhile
        again = json.loads(conn._command_file.read_text())
        if again["id"] != first["id"]:
            violations.append((first["action"], again["action"]))
        conn._response_file.write_text(
            json.dumps({"id": first["id"], "success": True, "action": first["action"]})
        )

    conn._port.on_send = fl_side
    results = {}

    def call(action):
        results[action] = conn.send_command(action, timeout=2.0)

    threads = [threading.Thread(target=call, args=(f"cmd{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert violations == []
    assert results["cmd0"]["action"] == "cmd0"
    assert results["cmd1"]["action"] == "cmd1"
