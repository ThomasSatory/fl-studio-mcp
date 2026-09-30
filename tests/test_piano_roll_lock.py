"""Two concurrent note tools must not mix their batches in one request file."""

from __future__ import annotations

import json
import threading
import time

from fl_studio_mcp.tools import piano_roll
from fl_studio_mcp.tools.piano_roll import register_piano_roll_tools
from tests.fakes import RecordingMCP


def test_concurrent_send_notes_are_serialised(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))
    scripts_dir = tmp_path / "Piano roll scripts"
    request_file = scripts_dir / "mcp_request.json"
    first_call_processing = threading.Event()
    batches_seen = []

    class SlowFL:
        is_supported = True
        platform = "TestOS"
        keystroke = "Ctrl+Alt+Y"

        def is_fl_running(self):
            return True

        def activate_window(self):
            return True

        def trigger(self, delay=0.0):
            before = len(json.loads(request_file.read_text()))
            first_call_processing.set()
            time.sleep(0.1)  # FL is busy: nobody may append to the queue meanwhile
            after = len(json.loads(request_file.read_text()))
            batches_seen.append((before, after))
            request_file.write_text("[]")
            (scripts_dir / "piano_roll_state.json").write_text(
                json.dumps({"ppq": 96, "noteCount": 1, "notes": []})
            )
            (scripts_dir / "mcp_response.json").write_text(
                json.dumps({"status": "success", "notes_added": after, "notes_deleted": 0,
                            "script_version": 2, "note_count": after})
            )
            return True

    monkeypatch.setattr(piano_roll, "get_trigger", lambda: SlowFL())
    monkeypatch.setattr(piano_roll, "_focus_piano_roll", lambda: None)
    mcp = RecordingMCP()
    register_piano_roll_tools(mcp)
    send_notes = mcp.tools["fl_send_notes"]
    results = []

    def call(midi, wait_for=None):
        if wait_for is not None:
            assert wait_for.wait(timeout=2), "first call never reached the trigger"
        results.append(send_notes([{"midi": midi, "duration": 1.0}]))

    first = threading.Thread(target=call, args=(60,))
    second = threading.Thread(target=call, args=(64, first_call_processing))
    first.start()
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert batches_seen == [(1, 1), (1, 1)]
    assert len(results) == 2
    assert all("1 note(s) written" in r for r in results)
