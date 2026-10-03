"""The .flp reader/writer: exact round trips, clips, markers, automation, samples."""

from __future__ import annotations

import struct

import pytest

from fl_studio_mcp.utils.flp import (
    AutomationPoint,
    Clip,
    FLPError,
    Project,
    add_automation_clip,
    add_sample_channel,
    automation_clips,
    automation_item_template,
    decode_automation,
    encode_automation,
    sample_paths,
)
from tests.flp_builder import build_project


def reparse(project: Project) -> Project:
    return Project.parse(project.to_bytes())


def test_round_trip_is_byte_exact():
    data = build_project().to_bytes()
    assert Project.parse(data).to_bytes() == data


def test_rejects_non_project():
    with pytest.raises(FLPError):
        Project.parse(b"RIFF....WAVE")


def test_header_and_names():
    project = build_project()
    assert project.ppq == 96
    assert project.bar_ticks == 384
    assert project.version_major == 25
    assert project.pattern_names() == {1: "Intro", 2: "Hook"}
    assert project.channel_names() == {0: "Kick"}


def test_reads_clips_markers_and_track_names():
    arr = build_project().arrangement()
    assert arr.item_size == 80
    assert [(c.position, c.pattern, c.length, c.track) for c in arr.clips] == [
        (0, 1, 768, 0), (768, 2, 768, 0)]
    assert [(m.position, m.name) for m in arr.markers] == [(0, "Intro"), (3072, "Verse")]
    assert arr.track_names == {1: "Drums"}


def test_set_clips_keeps_existing_bytes_and_gives_new_clips_fresh_ids():
    project = build_project()
    arr = project.arrangement()
    keep = arr.clips[0]
    project.set_clips(arr, [keep, Clip(position=1536, length=384, track=4, pattern=2)])
    new = reparse(project).arrangement()
    assert [(c.position, c.pattern, c.length, c.track) for c in new.clips] == [
        (0, 1, 768, 0), (1536, 2, 384, 4)]
    assert new.clips[0].raw == keep.raw
    assert new.clips[1].item_id == 102  # max existing id (101) + 1


def test_set_clips_writes_pattern_clip_trim_offsets():
    project = build_project()
    arr = project.arrangement()
    project.set_clips(arr, [Clip(position=0, length=1152, track=0, pattern=1,
                                 start_offset=0, end_offset=1152)])
    clip = reparse(project).arrangement().clips[0]
    assert (clip.length, clip.start_offset, clip.end_offset) == (1152, 0, 1152)


def test_set_clips_rejects_out_of_range_track():
    project = build_project()
    arr = project.arrangement()
    with pytest.raises(FLPError):
        project.set_clips(arr, [Clip(position=0, length=384, track=500, pattern=1)])


def test_empty_playlist_uses_the_fl_2025_layout():
    project = build_project(clips=[])
    arr = project.arrangement()
    project.set_clips(arr, [Clip(position=384, length=384, track=2, pattern=1)])
    clip = reparse(project).arrangement().clips[0]
    assert (clip.position, clip.pattern, clip.track, clip.start_offset) == (384, 1, 2, -1)


def test_automation_points_round_trip():
    template = bytes.fromhex("0100000040000000000400000003000000") + bytes(4) + bytes(112)
    points = [AutomationPoint(0, 0.2), AutomationPoint(16, 0.9, 0.5),
              AutomationPoint(32, 0.1, -0.3)]
    data = encode_automation(template, points)
    decoded = decode_automation(data)
    assert [(p.beat, p.value) for p in decoded] == [(0, 0.2), (16, 0.9), (32, 0.1)]
    assert len(data) == 21 + 3 * 24 + 112


def test_add_automation_clip_links_target_and_counts_channel():
    project = build_project()
    index = add_automation_clip(project, "Master - Volume", 0x70001FC0,
                                [AutomationPoint(0, 0.8), AutomationPoint(16, 0.0)])
    assert index == 1
    again = reparse(project)
    assert again.channel_count == 2
    (clip,) = automation_clips(again)
    assert (clip.channel, clip.name, clip.target) == (1, "Master - Volume", 0x70001FC0)
    assert clip.length_beats == 16
    assert automation_item_template(again) is not None
    # The arrangement is still readable after the channel was inserted.
    assert len(again.arrangement().clips) == 2


def test_channel_pitch_automation_follows_the_knob_range():
    # FL keeps a clip's output range in event 219 (min, max of 0-12800): a pitch
    # clip spans the knob's +-2 semitones, not the full +-48.
    project = build_project()
    add_automation_clip(project, "Pitch", 0x30004, [AutomationPoint(0, 0.5), AutomationPoint(4, 1)],
                        pitch_range=2)
    add_automation_clip(project, "Volume", 0x70001FC0,
                        [AutomationPoint(0, 0.8), AutomationPoint(4, 0)])
    events = reparse(project).events
    assert [bytes(ev.value)[8] for ev in events if ev.id == 234] == [1, 0]
    levels = [struct.unpack_from("<ii", bytes(ev.value)) for ev in events if ev.id == 219]
    assert levels[-2:] == [(6133, 6667), (0, 12800)]


def test_add_sample_channel_clones_a_sampler():
    project = build_project()
    index = add_sample_channel(project, "/samples/808.wav", "808")
    again = reparse(project)
    assert index == 1
    assert again.channel_names()[1] == "808"
    assert sample_paths(again) == {0: "/samples/kick.wav", 1: "/samples/808.wav"}
    assert again.channel_count == 2
