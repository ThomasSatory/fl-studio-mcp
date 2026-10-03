"""fl_add_marker: FL toggles markers, so an occupied position is refused and the result checked."""

from __future__ import annotations

import os
import time

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.playlist import register_playlist_tools
from fl_studio_mcp.utils import project_files
from fl_studio_mcp.utils.flp import Event, encode_text
from tests.fakes import RecordingMCP
from tests.flp_builder import build_project


class MarkerFL:
    """FL stand-in: markers live in the project; a save writes it; adding toggles."""

    def __init__(self, path, adds=True):
        self.path = path
        self.project = build_project()  # markers: bar 1 "Intro", bar 9 "Verse"
        self.adds = adds
        self.calls = []

    def _marker_events(self):
        return [i for i, ev in enumerate(self.project.events) if ev.id == 148]

    def send_command(self, action, params=None, timeout=2.0):
        self.calls.append(action)
        if action == "system.info":
            return {"success": True, "controller_version": 6, "ppq": 96, "changed": 0}
        if action == "transport.save":
            self.path.write_bytes(self.project.to_bytes())
            os.utime(self.path, (time.time(), time.time()))
            return {"success": True, "changed": 0}
        if action == "arrangement.addMarker":
            events = self.project.events
            same = [i for i in self._marker_events() if int(events[i].value) == params["ticks"]]
            if same:  # FL deletes the marker that is there
                del events[same[0]:same[0] + 2]
            elif self.adds:
                at = self._marker_events()[-1] + 2
                events[at:at] = [Event(148, params["ticks"]),
                                 Event(205, encode_text(params["name"]))]
            return {"success": True, "ticks": params["ticks"], "name": params["name"]}
        return {"success": True}


@pytest.fixture
def tool(tmp_path, monkeypatch):
    path = tmp_path / "song.flp"
    path.write_bytes(build_project().to_bytes())
    os.utime(path, (time.time() - 100, time.time() - 100))
    fake = MarkerFL(path)
    monkeypatch.setattr("fl_studio_mcp.utils.connection.get_connection", lambda: fake)
    monkeypatch.setattr(project_files, "SETTLE_AFTER_RELOAD", 0)
    mcp = RecordingMCP()
    register_playlist_tools(mcp)
    return mcp.tools["fl_add_marker"], fake, path


def _markers(path):
    from fl_studio_mcp.utils.flp import Project

    return [(m.position, m.name) for m in Project.parse(path.read_bytes()).arrangement().markers]


def test_a_marker_is_added_and_checked_in_the_saved_file(tool):
    add, fake, path = tool
    result = add(bar=5, name="Hook", project_path=str(path))
    assert result["verified"] is True and result["ticks"] == 4 * 384
    assert (4 * 384, "Hook") in _markers(path)
    assert fake.calls.count("transport.save") == 2


def test_a_bar_that_already_has_a_marker_is_refused_and_kept(tool):
    add, fake, path = tool
    with pytest.raises(ToolError, match=r"Bar 9 already has a marker \('Verse'\)"):
        add(bar=9, name="Chorus", project_path=str(path))
    assert "arrangement.addMarker" not in fake.calls
    assert (3072, "Verse") in _markers(path)


def test_a_marker_fl_did_not_add_fails_loudly(tool):
    add, fake, path = tool
    fake.adds = False
    with pytest.raises(ToolError, match="did not add the marker 'Hook' at bar 5"):
        add(bar=5, name="Hook", project_path=str(path))


def test_without_save_the_marker_is_added_unchecked(tool):
    add, fake, path = tool
    result = add(bar=5, name="Hook", save=False)
    assert result["verified"] is False and "deleted it" in result["note"]
    assert "transport.save" not in fake.calls
