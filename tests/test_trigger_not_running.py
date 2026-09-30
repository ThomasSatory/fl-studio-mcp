"""Never activate (and thereby launch) FL Studio when it is not running."""

from __future__ import annotations

import pytest

from fl_studio_mcp.tools import piano_roll
from fl_studio_mcp.utils import fl_trigger


@pytest.fixture
def mac_trigger(monkeypatch):
    monkeypatch.setattr(fl_trigger.platform, "system", lambda: "Darwin")
    return fl_trigger.FLStudioTrigger()


def forbid_subprocess(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError(f"subprocess.run must not be called: {args[0]}")

    monkeypatch.setattr(fl_trigger.subprocess, "run", boom)


class Probe:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_is_fl_running_asks_system_events_by_bundle_id(mac_trigger, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return Probe("true\n")

    monkeypatch.setattr(fl_trigger.subprocess, "run", fake_run)

    assert mac_trigger.is_fl_running() is True
    assert calls[0][0] == "osascript"
    assert fl_trigger.FL_BUNDLE_ID in calls[0][-1]


def test_is_fl_running_false_when_system_events_says_so(mac_trigger, monkeypatch):
    monkeypatch.setattr(fl_trigger.subprocess, "run", lambda *a, **k: Probe("false\n"))

    assert mac_trigger.is_fl_running() is False


def test_is_fl_running_raises_when_the_probe_itself_fails(mac_trigger, monkeypatch):
    denied = Probe(
        returncode=1, stderr="Not authorized to send Apple events to System Events. (-1743)\n"
    )
    monkeypatch.setattr(fl_trigger.subprocess, "run", lambda *a, **k: denied)

    with pytest.raises(fl_trigger.FLProbeError, match="Not authorized"):
        mac_trigger.is_fl_running()


def test_run_reports_probe_failure_not_as_fl_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))

    class UnknownFL:
        is_supported = True
        platform = "Darwin"
        keystroke = "Cmd+Opt+Y"

        def is_fl_running(self):
            raise fl_trigger.FLProbeError("Not authorized to send Apple events to System Events")

        def activate_window(self):
            raise AssertionError("must not activate")

        def trigger(self, delay=0.0):
            raise AssertionError("must not send a keystroke")

    monkeypatch.setattr(piano_roll, "get_trigger", lambda: UnknownFL())

    outcome = piano_roll._run_pr_script(timeout=0.2)

    assert outcome["triggered"] is False
    assert outcome["ran"] is False
    assert not outcome["error"].startswith(piano_roll.PR_FL_NOT_RUNNING)
    assert "Not authorized" in outcome["error"]
    assert "Automation" in outcome["error"]


def test_activate_does_nothing_when_fl_not_running(mac_trigger, monkeypatch):
    monkeypatch.setattr(mac_trigger, "is_fl_running", lambda: False)
    forbid_subprocess(monkeypatch)

    assert mac_trigger.activate_window() is False
    assert mac_trigger.trigger(delay=0) is False


def test_activate_uses_bundle_id_when_running(mac_trigger, monkeypatch):
    monkeypatch.setattr(mac_trigger, "is_fl_running", lambda: True)
    calls = []
    monkeypatch.setattr(fl_trigger.subprocess, "run", lambda args, **k: calls.append(args))

    assert mac_trigger.activate_window() is True
    assert f'application id "{fl_trigger.FL_BUNDLE_ID}"' in calls[0][-1]


def test_run_reports_fl_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))
    piano_roll._write_request({"action": "add_notes", "notes": [{"midi": 60, "duration": 1}]})

    class ClosedFL:
        is_supported = True
        platform = "Darwin"
        keystroke = "Cmd+Opt+Y"

        def is_fl_running(self):
            return False

        def activate_window(self):
            raise AssertionError("must not activate")

        def trigger(self, delay=0.0):
            raise AssertionError("must not send a keystroke")

    monkeypatch.setattr(piano_roll, "get_trigger", lambda: ClosedFL())

    outcome = piano_roll._run_pr_script(timeout=0.2)

    assert outcome["triggered"] is False
    assert outcome["ran"] is False
    assert outcome["error"].startswith(piano_roll.PR_FL_NOT_RUNNING)
    assert "still queued" in outcome["error"]
