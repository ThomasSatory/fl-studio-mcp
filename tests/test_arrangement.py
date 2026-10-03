"""Arrangement editing: operations in bars, and the save / patch / reload cycle."""

from __future__ import annotations

import os
import time

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools import arrangement as arr_tools
from fl_studio_mcp.utils import project_files
from fl_studio_mcp.utils.flp import Clip, Project, automation_clips
from tests.fakes import RecordingMCP
from tests.flp_builder import build_project

TIMELINE = arr_tools.Timeline(96, 384)
# As controller v5 reports them: FL's step count, and beats (2 and 4 bars).
PATTERNS = {1: {"index": 1, "name": "Intro", "length_steps": 32, "length_beats": 8},
            2: {"index": 2, "name": "Hook", "length_steps": 64, "length_beats": 16}}


def apply(clips, *ops):
    result, log = arr_tools.apply_operations(list(clips), list(ops), TIMELINE, PATTERNS)
    return sorted((c.track + 1, TIMELINE.bar(c.position), TIMELINE.bars(c.length), c.pattern)
                  for c in result), log


def test_place_uses_pattern_length_and_repeats():
    clips, log = apply([], {"op": "place", "pattern": "hook", "track": 2, "bar": 5, "repeat": 3})
    assert clips == [(2, 5.0, 4.0, 2), (2, 9.0, 4.0, 2), (2, 13.0, 4.0, 2)]
    assert "Hook" in log[0]


def test_place_replaces_overlapping_clips_on_the_same_track_only():
    existing = [Clip(position=0, length=768, track=0, pattern=1),
                Clip(position=0, length=768, track=1, pattern=1)]
    clips, _ = apply(existing, {"op": "place", "pattern": 2, "track": 1, "bar": 2,
                                "length_bars": 1})
    assert clips == [(1, 2.0, 1.0, 2), (2, 1.0, 2.0, 1)]


def test_place_trims_clips_shorter_or_longer_than_the_pattern():
    # FL ignores a clip's length on load unless its trim offsets say so.
    result, _ = arr_tools.apply_operations(
        [], [{"op": "place", "pattern": 2, "track": 1, "bar": 1, "length_bars": 3},
             {"op": "place", "pattern": 2, "track": 2, "bar": 1}], TIMELINE, PATTERNS)
    trimmed, full = sorted(result, key=lambda c: c.track)
    assert (trimmed.start_offset, trimmed.end_offset) == (0, 1152)
    assert (full.start_offset, full.end_offset) == (None, None)


def test_delete_by_track_and_range():
    existing = [Clip(position=TIMELINE.ticks(b), length=384, track=t, pattern=1)
                for b in (1, 2, 3) for t in (0, 1)]
    clips, log = apply(existing, {"op": "delete", "track": 1, "from_bar": 2, "to_bar": 3})
    assert (1, 2.0, 1.0, 1) not in clips and (2, 2.0, 1.0, 1) in clips
    assert "deleted 1" in log[0]


def test_copy_and_move_sections():
    existing = [Clip(position=0, length=384, track=0, pattern=1),
                Clip(position=384, length=384, track=1, pattern=2)]
    copied, _ = apply(existing, {"op": "copy", "from_bar": 1, "to_bar": 3, "to": 9})
    assert (1, 9.0, 1.0, 1) in copied and (2, 10.0, 1.0, 2) in copied and len(copied) == 4
    moved, _ = apply(existing, {"op": "move", "from_bar": 2, "to_bar": 3, "to": 5})
    assert moved == [(1, 1.0, 1.0, 1), (2, 5.0, 1.0, 2)]


def test_bad_operations_name_the_problem():
    with pytest.raises(ToolError, match="operation 0"):
        apply([], {"op": "place", "pattern": "Nope", "track": 1, "bar": 1})
    with pytest.raises(ToolError, match="track"):
        apply([], {"op": "place", "pattern": 1, "track": 0, "bar": 1})
    with pytest.raises(ToolError, match="'op'"):
        apply([], {"op": "explode"})


class FakeFL:
    """Controller stand-in: saves by touching the file, reloads on open_in_fl."""

    def __init__(self, path):
        self.path = path
        self.load_count = 0
        self.calls = []

    def send_command(self, action, params=None, timeout=2.0):
        self.calls.append((action, params))
        if action == "system.info":
            return {"success": True, "controller_version": 4, "instance_id": "a",
                    "load_count": self.load_count, "changed": 0, "ppq": 96}
        if action == "transport.save":
            os.utime(self.path, (time.time(), time.time()))
            return {"success": True, "changed": 0}
        if action == "patterns.getAll":
            return {"success": True, "patterns": list(PATTERNS.values())}
        if action == "transport.getLength":
            return {"success": True, "ticks": 3072}
        if action == "automation.resolve":
            return {"success": True, "targets": [
                {"rec_id": 0x40000005, "label": "Tempo"} for _ in params["targets"]]}
        return {"success": True}


@pytest.fixture
def fl(tmp_path, monkeypatch):
    path = tmp_path / "song.flp"
    path.write_bytes(build_project().to_bytes())
    os.utime(path, (time.time() - 100, time.time() - 100))
    fake = FakeFL(path)
    monkeypatch.setattr(project_files, "_last_project", None)
    monkeypatch.setattr("fl_studio_mcp.utils.connection.get_connection", lambda: fake)

    def reopen(target):
        fake.load_count += 1
        fake.calls.append(("open", str(target)))

    monkeypatch.setattr(project_files, "open_in_fl", reopen)
    mcp = RecordingMCP()
    arr_tools.register_arrangement_tools(mcp)
    return mcp.tools, fake, path


def test_get_arrangement_reads_bars(fl):
    tools, _, path = fl
    result = tools["fl_get_arrangement"](project_path=str(path))
    assert result["tracks"][0]["clips"][1] == {
        "bar": 3.0, "length_bars": 2.0, "end_bar": 5.0, "pattern": 2, "pattern_name": "Hook"}
    assert result["markers"] == [{"bar": 1.0, "name": "Intro"}, {"bar": 9.0, "name": "Verse"}]


def test_edit_arrangement_writes_backup_and_reloads(fl):
    tools, fake, path = fl
    result = tools["fl_edit_arrangement"](
        [{"op": "clear"}, {"op": "place", "pattern": "Intro", "track": 3, "bar": 1, "repeat": 2},
         {"op": "automation", "target": {"type": "tempo"}, "track": 5,
          "points": [{"bar": 1, "value": 0.2}, {"bar": 5, "value": 0.6}]}],
        project_path=str(path),
    )
    assert result["clips_after"] == 3
    assert os.path.exists(result["backup"])
    assert ("open", str(path)) in fake.calls
    project = Project.load(path)
    clips = project.arrangement().clips
    assert sorted((c.track, c.position, c.pattern, c.channel) for c in clips) == [
        (2, 0, 1, None), (2, 768, 1, None), (4, 0, None, 1)]
    (auto,) = automation_clips(project)
    assert auto.target == 0x40000005 and auto.length_beats == 16


def test_new_automation_clips_are_not_trimmed_to_an_existing_clip(fl):
    # New clips copy an existing automation item, trim offsets (in beats) included.
    tools, _, path = fl
    for bars in ((1, 3), (1, 9)):
        tools["fl_edit_arrangement"](
            [{"op": "automation", "target": {"type": "tempo"}, "track": 5,
              "points": [{"bar": bars[0], "value": 0.2}, {"bar": bars[1], "value": 0.6}]}],
            project_path=str(path))
    (clip,) = [c for c in Project.load(path).arrangement().clips if c.track == 4]
    assert clip.length == 8 * 384
    assert (clip.start_offset, clip.end_offset) == (0, 32)


def test_dry_run_touches_nothing(fl):
    tools, fake, path = fl
    before = path.read_bytes()
    result = tools["fl_edit_arrangement"]([{"op": "clear"}], dry_run=True,
                                          project_path=str(path))
    assert result["clips_after"] == 0
    assert path.read_bytes() == before
    assert not any(call[0] == "open" for call in fake.calls)


def test_untitled_project_gives_a_clear_error(fl, monkeypatch):
    tools, fake, path = fl
    monkeypatch.setattr(project_files, "SAVE_TIMEOUT", 0.2)
    monkeypatch.setattr(project_files, "search_roots", lambda: [path.parent / "nothing"])
    fake.path = path.parent / "other.flp"
    fake.path.write_bytes(b"")
    os.utime(fake.path, (time.time() - 100, time.time() - 100))
    monkeypatch.setattr(fake, "send_command", lambda a, p=None, t=2.0: (
        {"success": True, "controller_version": 4, "load_count": 0, "changed": 0}
        if a == "system.info" else {"success": True}))
    with pytest.raises(ToolError, match="untitled"):
        tools["fl_get_arrangement"]()


def test_add_sample_channels(fl, tmp_path):
    tools, fake, path = fl
    sample = tmp_path / "808 Dark.wav"
    sample.write_bytes(b"RIFF")
    result = tools["fl_add_sample_channels"]([{"path": str(sample), "mixer_track": 4}],
                                             project_path=str(path))
    assert result["added"][0]["name"] == "808 Dark"
    assert ("channels.routeToMixer", {"channel_index": 1, "mixer_track": 4}) in fake.calls
    assert Project.load(path).channel_names()[1] == "808 Dark"


def test_reload_is_detected_by_the_playhead_leaving_the_sentinel(monkeypatch):
    positions = iter(["1:01:07", "1:01:07", "1:01:00"])

    class Conn:
        def send_command(self, action, params=None, timeout=2.0):
            if action == "system.info":
                return {"success": True, "instance_id": "a", "load_count": 0}
            return {"success": True, "position": next(positions)}

    monkeypatch.setattr(project_files.time, "sleep", lambda s: None)
    info = project_files.wait_for_reload(Conn(), {"instance_id": "a", "load_count": 0},
                                         sentinel="1:01:07", timeout=5)
    assert info["instance_id"] == "a"


def _saves(fake):
    return sum(1 for call in fake.calls if call[0] == "transport.save")


def test_pattern_lengths_from_an_old_controller_are_steps(fl, monkeypatch):
    # Controllers before v5 pass FL's step count as "length_beats".
    tools, fake, path = fl
    old = [{"index": 1, "name": "Intro", "length_beats": 32}]
    original = fake.send_command
    monkeypatch.setattr(fake, "send_command", lambda a, p=None, t=2.0: (
        {"success": True, "patterns": [dict(x) for x in old]} if a == "patterns.getAll"
        else original(a, p, t)))
    result = tools["fl_edit_arrangement"](
        [{"op": "place", "pattern": "Intro", "track": 7, "bar": 81, "repeat": 2}],
        dry_run=True, project_path=str(path))
    assert "2 bar(s) x2 every 2 bar(s)" in result["log"][0]
    clips = [c for t in result["arrangement"]["tracks"] if t["track"] == 7 for c in t["clips"]]
    assert [(c["bar"], c["length_bars"]) for c in clips] == [(81.0, 2.0), (83.0, 2.0)]


def test_place_refuses_patterns_that_do_not_exist_before_saving(fl):
    tools, fake, path = fl
    for ref in (99, 3):
        with pytest.raises(ToolError, match=f"no pattern {ref} .*Patterns: 1='Intro'"):
            tools["fl_edit_arrangement"](
                [{"op": "place", "pattern": ref, "track": 3, "bar": 20, "length_bars": 2}],
                project_path=str(path))
    assert _saves(fake) == 0


def test_an_invalid_batch_is_refused_before_the_project_is_saved(fl):
    tools, fake, path = fl
    before = path.read_bytes()
    bad_batches = [
        [{"op": "clear"}, {"op": "copy", "from_bar": 9, "to_bar": 5, "to": 1}],
        [{"op": "automation", "target": {"type": "tempo"}, "track": 5,
          "points": [{"bar": 1, "value": 2}]}],
        [{"op": "set_automation", "clip": None, "points": []}],
        [{"op": "place", "pattern": "Hook", "track": 0, "bar": 1}],
    ]
    for batch in bad_batches:
        with pytest.raises(ToolError, match="operation"):
            tools["fl_edit_arrangement"](batch, project_path=str(path))
    assert _saves(fake) == 0 and path.read_bytes() == before


def test_an_edit_that_changes_nothing_writes_nothing(fl):
    tools, fake, path = fl
    before = path.read_bytes()
    result = tools["fl_edit_arrangement"](
        [{"op": "delete", "track": 3, "from_bar": 13, "to_bar": 13}], project_path=str(path))
    assert result["changed"] is False and "deleted 0" in result["log"][0]
    assert path.read_bytes() == before
    assert not any(call[0] == "open" for call in fake.calls)
    assert not (path.parent / "Backup").exists()


def test_the_callers_operations_are_left_untouched(fl):
    tools, _, path = fl
    ops = [{"op": "automation", "target": {"type": "tempo"}, "track": 5,
            "points": [{"bar": 1, "value": 0.2}, {"bar": 5, "value": 0.6}]}]
    snapshot = repr(ops)
    tools["fl_edit_arrangement"](ops, project_path=str(path))
    assert repr(ops) == snapshot


def test_automation_edits_move_the_playhead_through_the_clip(fl):
    tools, fake, path = fl
    tools["fl_edit_arrangement"](
        [{"op": "automation", "target": {"type": "tempo"}, "track": 5, "name": "Fade",
          "points": [{"bar": 3, "value": 0.2}, {"bar": 5, "value": 0.6}]}],
        project_path=str(path))
    fake.calls.clear()
    result = tools["fl_edit_arrangement"](
        [{"op": "set_automation", "clip": "Fade",
          "points": [{"beat": 0, "value": 0.8}, {"beat": 8, "value": 0.8}]}],
        project_path=str(path))
    opened = fake.calls.index(("open", str(path)))
    moves = [p["ticks"] for a, p in fake.calls[opened:] if a == "transport.setPosition"]
    assert moves == [2 * 384, 0]
    assert "bar 3" in result["automation_note"]
