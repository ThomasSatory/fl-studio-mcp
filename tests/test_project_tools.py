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


# --- save safety: wait for FL's own write, skip needless saves, report crashes ---

class LateSaver:
    """FL stand-in whose save lands later: temp file first, then the rename."""

    def __init__(self, path, delay=0.4, changed=1, title=None, version=4, use_temp=True):
        self.path, self.delay, self.changed, self.title = path, delay, changed, title
        self.use_temp = use_temp
        self.version = version
        self.saves = 0
        self.answer_info = True
        self.timers = []

    def send_command(self, action, params=None, timeout=2.0):
        import threading

        if action == "system.info":
            if not self.answer_info:
                return {"success": False, "error": "Timeout waiting for FL Studio response"}
            return {"success": True, "controller_version": self.version,
                    "changed": self.changed, "title": self.title}
        if action == "transport.save":
            self.saves += 1
            temp = project_files.temp_file(self.path)

            def start():
                if self.use_temp:
                    temp.write_bytes(b"half")

            def finish():
                if not self.use_temp:
                    self.path.write_bytes(b"new content %d" % self.saves)
                    return
                temp.write_bytes(b"new content %d" % self.saves)
                temp.replace(self.path)

            for delay, fn in ((self.delay / 4, start), (self.delay, finish)):
                timer = threading.Timer(delay, fn)
                timer.start()
                self.timers.append(timer)
            return {"success": True, "changed": 0}
        return {"success": True}


@pytest.fixture
def song(tmp_path):
    path = tmp_path / "Song.flp"
    path.write_bytes(b"old content")  # written "just now", like a save a moment ago
    return path


def test_a_file_written_just_before_the_request_is_not_taken_for_the_save(song):
    conn = LateSaver(song)
    path = project_files.save_and_locate(conn, str(song))
    assert path == song
    assert song.read_bytes() == b"new content 1"  # waited for FL's write and rename
    assert not project_files.temp_file(song).exists()


def test_a_stale_temp_file_from_a_crash_does_not_block_the_save(song):
    stale = project_files.temp_file(song)
    stale.write_bytes(b"")
    conn = LateSaver(song, delay=0.2, use_temp=False)
    path = project_files.save_and_locate(conn, str(song))
    assert path.read_bytes() == b"new content 1"
    assert stale.exists()


def test_every_save_really_saves_even_when_nothing_changed(song):
    conn = LateSaver(song, delay=0.1, changed=0, title="FL Studio 2025")
    project_files.save_and_locate(conn, str(song))
    project_files.save_and_locate(conn, str(song))
    assert project_files.current_project_file(conn, True, str(song)) == song
    assert conn.saves == 3
    assert song.read_bytes() == b"new content 3"


def test_save_waits_for_fl_to_settle_after_a_reload(song, monkeypatch):
    sleeps = []
    monkeypatch.setattr(project_files.time, "sleep", sleeps.append)
    project_files.reload_finished()
    conn = SaveConnection()
    project_files.save_project(conn)
    assert sleeps and 2.5 < sleeps[0] <= project_files.SETTLE_AFTER_RELOAD


def test_fl_dying_during_the_save_is_reported_as_a_crash(song, monkeypatch):
    conn = LateSaver(song, delay=60)
    running = iter([True, False])
    monkeypatch.setattr(project_files, "fl_process_running", lambda: next(running, False))
    with pytest.raises(ToolError, match=r"stopped \(it probably crashed\) while saving") as e:
        project_files.save_and_locate(conn, str(song))
    assert str(song.parent / "Backup") in str(e.value) and "dialog" not in str(e.value)
    for timer in conn.timers:
        timer.cancel()


def test_fl_not_answering_after_the_save_timeout_is_reported_as_a_crash(song, monkeypatch):
    monkeypatch.setattr(project_files, "SAVE_TIMEOUT", 0.3)
    monkeypatch.setattr(project_files, "fl_process_running", lambda: None)  # not macOS
    conn = LateSaver(song, delay=60)
    request = project_files.save_project(conn, project_path=str(song))
    conn.answer_info = False
    with pytest.raises(ToolError, match="probably crashed"):
        project_files.locate_saved_project(request, str(song), conn)
    for timer in conn.timers:
        timer.cancel()


def test_save_request_failing_with_fl_gone_is_reported_as_a_crash(monkeypatch):
    running = iter([True, False])  # running when asked, gone after the failure
    monkeypatch.setattr(project_files, "fl_process_running", lambda: next(running, False))
    conn = SaveConnection(save_answers=[{"success": False, "error": "Timeout waiting"}])
    with pytest.raises(ToolError, match="probably crashed"):
        project_files.save_project(conn)


def test_written_after_allows_no_slack_on_fine_timestamps():
    t = 1_700_000_000.25
    assert not project_files.written_after(int((t - 0.5) * 1e9), t)
    assert project_files.written_after(int((t + 0.01) * 1e9), t)
    # A whole-second mtime (coarse file system) in the request's second is accepted.
    assert project_files.written_after(1_700_000_000 * 1_000_000_000, t)


def test_pgrep_not_seeing_fl_is_no_crash_when_it_never_saw_fl(song, monkeypatch):
    # FL under another process name (or pgrep blind): "not running" proves nothing.
    monkeypatch.setattr(project_files, "SAVE_TIMEOUT", 0.3)
    monkeypatch.setattr(project_files, "fl_process_running", lambda: False)
    conn = LateSaver(song, delay=60)
    with pytest.raises(ToolError, match="did not save to") as e:
        project_files.save_and_locate(conn, str(song))
    assert "crashed" not in str(e.value)
    for timer in conn.timers:
        timer.cancel()
    conn = SaveConnection(save_answers=[{"success": False, "error": "Timeout waiting"}])
    with pytest.raises(ToolError, match="Timeout waiting"):
        project_files.save_project(conn)


def _crash_log(errors_module, name="Crash 2026-10-03_142507.log"):
    folder = errors_module.crash_logs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / name
    log.write_text("EAccessViolation - Access violation - main thread")
    return log


def test_fl_crash_window_during_the_save_is_reported_from_its_crash_log(song, monkeypatch):
    from fl_studio_mcp.utils import errors

    conn = LateSaver(song, delay=60)
    request = project_files.save_project(conn, project_path=str(song))
    log = _crash_log(errors)  # FL crashed: its process lives on behind the crash window
    with pytest.raises(errors.FLCrashedError) as e:
        project_files.locate_saved_project(request, str(song), conn)
    text = str(e.value)
    assert "crash window is open" in text and str(log) in text
    assert f"reopen {song}" in text and str(song.parent / "Backup") in text
    assert "dialog may be waiting" not in text
    for timer in conn.timers:
        timer.cancel()


def test_an_old_crash_log_is_not_this_save(song, monkeypatch):
    from fl_studio_mcp.utils import errors

    log = _crash_log(errors, "Crash old.log")
    import os
    os.utime(log, (1_000_000, 1_000_000))
    conn = LateSaver(song, delay=0.1)
    assert project_files.save_and_locate(conn, str(song)) == song


UNSAFE = {"success": False, "error": "Error executing command: Operation unsafe at current time"}


def test_save_refused_as_unsafe_after_a_crash(monkeypatch):
    from fl_studio_mcp.utils import errors

    _crash_log(errors)
    conn = SaveConnection(save_answers=[UNSAFE])
    with pytest.raises(errors.FLCrashedError, match=r"crashed \(its crash window is open"):
        project_files.save_project(conn)
    assert conn.sent.count("transport.save") == 1


def test_save_refused_as_unsafe_without_a_crash_log(monkeypatch):
    from fl_studio_mcp.utils import errors

    conn = SaveConnection(save_answers=[UNSAFE])
    with pytest.raises(errors.FLBusyError) as e:
        project_files.save_project(conn)
    assert not isinstance(e.value, errors.FLCrashedError)
    assert "modal window" in str(e.value) and "crash window" in str(e.value)


def test_save_timeout_with_fl_refusing_commands_is_not_blamed_on_a_dialog(song, monkeypatch):
    monkeypatch.setattr(project_files, "SAVE_TIMEOUT", 0.3)
    conn = LateSaver(song, delay=60)
    request = project_files.save_project(conn, project_path=str(song))
    real = conn.send_command

    def refusing(action, params=None, timeout=2.0):
        if action == "transport.getStatus":
            return dict(UNSAFE)
        return real(action, params, timeout)

    conn.send_command = refusing
    with pytest.raises(ToolError, match="crash window") as e:
        project_files.locate_saved_project(request, str(song), conn)
    assert "Is that the open project" not in str(e.value)
    for timer in conn.timers:
        timer.cancel()


def test_a_renderer_crash_log_of_another_server_does_not_block_saving(monkeypatch):
    # The review's reproduction: a log 10 min old, FL started 1 h ago, the render
    # that wrote it ran in another server process (persisted window only).
    import json
    import os
    import time

    from fl_studio_mcp.utils import errors

    now = time.time()
    monkeypatch.setattr(errors, "fl_started_at", lambda: now - 3600)
    errors._windows_file().write_text(json.dumps(
        {"windows": [{"start": now - 660, "end": now - 590, "pid": 999_999}]}))
    log = _crash_log(errors)
    os.utime(log, (now - 600, now - 600))
    conn = SaveConnection()
    project_files.save_project(conn)  # no FLCrashedError
    assert conn.sent.count("transport.save") == 1


def test_save_refused_by_a_modal_after_a_relaunch_is_not_an_old_crash(monkeypatch):
    # The server outlived FL's crash (log at T1); FL was relaunched and a dialog is
    # open: no command has succeeded since. The T1 log is the previous FL's.
    import os
    import time

    from fl_studio_mcp.utils import errors

    now = time.time()
    monkeypatch.setattr(errors, "_loaded_at", now - 7200)  # the server started first
    log = _crash_log(errors)
    os.utime(log, (now - 600, now - 600))
    monkeypatch.setattr(errors, "fl_started_at", lambda: now - 60)  # relaunched since
    conn = SaveConnection(save_answers=[UNSAFE])
    with pytest.raises(errors.FLBusyError) as e:
        project_files.save_project(conn)
    assert not isinstance(e.value, errors.FLCrashedError)
    assert "modal window" in str(e.value)
    # The same log, with FL started before it: that FL's crash.
    monkeypatch.setattr(errors, "fl_started_at", lambda: now - 3600)
    monkeypatch.setattr(errors, "_started_cache", None)
    with pytest.raises(errors.FLCrashedError):
        project_files.save_project(SaveConnection(save_answers=[UNSAFE]))
