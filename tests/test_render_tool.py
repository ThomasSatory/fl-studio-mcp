"""fl_render_mix: song length, sections, stem selection and the whole tool with fakes."""

from __future__ import annotations

import functools
import struct
import wave
from pathlib import Path

import anyio
import numpy as np
import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools import render as render_tool
from fl_studio_mcp.utils import flp, mix_analysis, project_files
from fl_studio_mcp.utils import render as render_util
from fl_studio_mcp.utils.flp import INSERT_ENABLED, Event, Project, encode_text
from tests.fakes import RecordingMCP
from tests.flp_builder import add_mixer, build_project

BAR = 384  # ticks per 4/4 bar at ppq 96; 2 s at 120 BPM


def song(bars: int = 16, markers: list[tuple[int, str]] | None = None,
         tempo: int | None = 120000, beats: int = 4) -> Project:
    """A project whose arrangement ends at `bars` bars of `beats`/4, with these markers."""
    bar = BAR * beats // 4
    project = build_project(clips=[(0, 1, bars * bar, 0)])
    events = [Event(17, beats) if e.id == 17 else e
              for e in project.events if e.id not in (148, 205)]
    at = next(i for i, e in enumerate(events) if e.id == 238)
    for pos, name in reversed(markers or []):
        events[at:at] = [Event(148, pos), Event(205, encode_text(name))]
    if tempo is not None:
        events.insert(1, Event(156, tempo))
    project.events = events
    return Project.parse(project.to_bytes())


# ------------------------------------------------------------ song / sections


def test_song_seconds():
    assert render_tool.song_seconds(song(16)) == pytest.approx(32.0)
    assert render_tool.song_seconds(song(16, tempo=None)) is None
    assert render_tool.song_seconds(Project.parse(build_project(clips=[]).to_bytes())) is None


def test_sections_from_markers():
    sections = render_tool.section_bounds(
        song(16, [(0, "Intro"), (4 * BAR, "Verse"), (12 * BAR, "Hook"), (20 * BAR, "Late")]))
    assert sections == [
        {"name": "Intro", "start_seconds": 0.0, "end_seconds": 8.0,
         "start_bar": 1.0, "end_bar": 5.0},
        {"name": "Verse", "start_seconds": 8.0, "end_seconds": 24.0,
         "start_bar": 5.0, "end_bar": 13.0},
        {"name": "Hook", "start_seconds": 24.0, "end_seconds": 32.0,
         "start_bar": 13.0, "end_bar": 17.0},
    ]


def test_implicit_intro_when_the_first_marker_is_after_bar_one():
    sections = render_tool.section_bounds(song(16, [(8 * BAR, "Drop")]))
    assert [(s["name"], s["start_bar"], s["end_bar"]) for s in sections] == [
        ("Intro", 1.0, 9.0), ("Drop", 9.0, 17.0)]


def test_eight_bar_blocks_without_markers():
    sections = render_tool.section_bounds(song(20))
    assert [s["name"] for s in sections] == ["bars 1-8", "bars 9-16", "bars 17-20"]
    assert sections[-1]["end_seconds"] == 40.0
    assert render_tool.section_bounds(song(20, tempo=None)) == []


def test_explicit_tempo_when_the_file_has_none():
    project = song(16, tempo=None)
    assert render_tool.song_seconds(project, 60.0) == pytest.approx(64.0)
    sections = render_tool.section_bounds(project, 120.0)
    assert [s["end_seconds"] for s in sections] == [16.0, 32.0]


def test_sections_in_three_four():
    sections = render_tool.section_bounds(song(12, beats=3))
    assert [(s["name"], s["start_seconds"], s["end_seconds"], s["end_bar"])
            for s in sections] == [("bars 1-8", 0.0, 12.0, 9.0),
                                   ("bars 9-12", 12.0, 18.0, 13.0)]


# ------------------------------------------------------------------ stems

# 0 Master, 1 Kick -> 3, 2 Bass -> 3, 3 Bass bus -> 0, 4 'Current'
ROUTES = {0: set(), 1: {3}, 2: {3}, 3: {0}, 4: {0}}
LAST = 4


def test_resolve_explicit_list():
    assert render_tool.resolve_stems(None, ROUTES, [1, 2], LAST) == []
    assert render_tool.resolve_stems([], ROUTES, [1, 2], LAST) == []
    assert render_tool.resolve_stems([3, 1, 3], ROUTES, [1, 2], LAST) == [3, 1]
    for bad in ([0], [4], [9], [True], ["1"]):
        with pytest.raises(ToolError, match="not a usable mixer insert"):
            render_tool.resolve_stems(bad, ROUTES, [1, 2], LAST)


def test_resolve_all_and_buses():
    assert render_tool.resolve_stems("all", ROUTES, [1, 2, 0, 2], LAST) == [1, 2, 3]
    assert render_tool.resolve_stems("buses", ROUTES, [1, 2, 0], LAST) == [3]
    with pytest.raises(ToolError, match="matches no mixer track"):
        render_tool.resolve_stems("buses", ROUTES, [1], LAST)
    with pytest.raises(ToolError, match="'all' or 'buses'"):
        render_tool.resolve_stems("drums", ROUTES, [1, 2], LAST)


def test_a_bus_needs_two_used_inputs_even_if_a_channel_feeds_it():
    # 1 is a bus fed by 3, 4 and a channel; 3 sends once to 5 (sidechain/send)
    routes = {0: set(), 1: {0}, 3: {1, 5}, 4: {1}, 5: {0}, 6: {0}}
    assert render_tool.resolve_stems("buses", routes, [1, 3, 4], 6) == [1]
    assert render_tool.resolve_stems("all", routes, [1, 3, 4], 6) == [1, 3, 4]
    # a bus of buses: 1 and 2 (each fed twice) both feed 3
    routes = {0: set(), 1: {3}, 2: {3}, 3: {0}, 4: {1}, 5: {1}, 6: {2}, 7: {2}, 8: {0}}
    assert render_tool.resolve_stems("buses", routes, [4, 5, 6, 7], 8) == [1, 2, 3]
    # a single send destination fed by no channel is not a bus either
    routes = {0: set(), 1: {0, 2}, 2: {0}, 3: {0}}
    with pytest.raises(ToolError, match="matches no mixer track"):
        render_tool.resolve_stems("buses", routes, [1], 3)


def test_resolve_too_many_stems():
    routes = {i: {0} for i in range(12)}
    with pytest.raises(ToolError, match="at most 8 per call"):
        render_tool.resolve_stems("all", routes, list(range(1, 11)), 11)
    with pytest.raises(ToolError, match="Split them"):
        render_tool.resolve_stems(list(range(1, 10)), routes, [], 11)


# -------------------------------------------------------------------- tool


def write_wav(path: Path, freq: float, amp: float, seconds: float = 6.0,
              rate: int = 44100) -> Path:
    t = np.arange(int(seconds * rate)) / rate
    mono = amp * np.sin(2 * np.pi * freq * t)
    pcm = (np.stack([mono, mono], axis=1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return path


class FakeFL:
    def __init__(self):
        self.calls = []
        self.tempo = 120.0
        # live routing: track -> {dest: level}; None makes mixer.getRouting fail
        self.routing: dict[int, dict[int, float]] | None = {
            t: {d: 0.8 for d in dests} for t, dests in ROUTES.items()}
        self.extra_channels: list[dict] = []
        self.master_volume: float | None = 0.8

    def send_command(self, action, params=None, timeout=2.0):
        self.calls.append(action)
        if action == "mixer.getRouting":
            if self.routing is None:
                return {"success": False, "error": "mixer.getRouting unavailable"}
            track = params["track"]
            return {"success": True, "track": track, "routes": [
                {"to": d, "name": f"Insert {d}", "level": level}
                for d, level in self.routing.get(track, {}).items()]}
        if action == "system.info":
            return {"success": True, "controller_version": 4}
        if action == "mixer.getTrackInfo":
            if self.master_volume is None:
                return {"success": False, "error": "Timeout"}
            return {"success": True, "index": params["track"], "volume": self.master_volume}
        if action == "transport.getTempo":
            return {"success": True, "tempo": self.tempo}
        if action == "channels.getAll":
            return {"success": True, "channels": [
                {"index": 0, "name": "Kick", "type": "sampler", "target_fx_track": 1},
                {"index": 1, "name": "Bass", "type": "plugin", "target_fx_track": 2},
                {"index": 2, "name": "Pad", "type": "plugin", "target_fx_track": 0},
                {"index": 3, "name": "Fade out", "type": "automation_clip",
                 "target_fx_track": 0},
                {"index": 4, "name": "Vox", "type": "audio_clip", "target_fx_track": 4},
            ] + self.extra_channels}
        if action == "mixer.getAllTracks":
            return {"success": True, "tracks": [
                {"index": 0, "name": "Master"}, {"index": 1, "name": "Kick"},
                {"index": 2, "name": "Bass"}, {"index": 3, "name": "Bass bus"},
                {"index": 4, "name": "Current"}]}
        return {"success": True}


def enabled(data: bytes) -> list[int]:
    project = Project.parse(data)
    flags = [struct.unpack_from("<I", e.value, 4)[0] for e in project.events if e.id == 236]
    return [i for i, f in enumerate(flags) if f & INSERT_ENABLED]


@pytest.fixture(params=[120000])
def tool(tmp_path, monkeypatch, request):
    project = song(8, [(0, "Intro"), (2 * BAR, "Drop")], tempo=request.param)
    add_mixer(project, [sorted(ROUTES[i]) for i in range(LAST + 1)])
    flp_path = tmp_path / "My Song.flp"
    flp_path.write_bytes(project.to_bytes())
    fake = FakeFL()
    renders = []
    fail_on = set()

    def fake_render(data, name, timeout):
        renders.append({"name": name, "data": data, "timeout": timeout})
        if any(f"stem {t} " in name for t in fail_on):
            raise ToolError("FL Studio wrote no audio file (exit code 0).")
        if "stem 1" in name:
            freq, amp = 50.0, 0.5
        elif "stem 2" in name or "stem 3" in name:
            freq, amp = 55.0, 0.4
        else:
            freq, amp = 440.0, 0.5
        return write_wav(tmp_path / f"{name} {len(renders)}.wav", freq, amp)

    def fake_current(conn, save, project_path):
        fake.calls.append(f"current(save={save})")
        return flp_path

    monkeypatch.setenv("FL_STUDIO_MCP_RENDER_DIR", str(tmp_path / "renders"))
    monkeypatch.setattr("fl_studio_mcp.utils.connection.get_connection", lambda: fake)
    monkeypatch.setattr(project_files, "current_project_file", fake_current)
    monkeypatch.setattr(render_util, "render_wav", fake_render)
    mcp = RecordingMCP()
    render_tool.register_render_tools(mcp)
    fake.fail_on = fail_on
    fake.progress = []

    class FakeContext:
        async def report_progress(self, progress, total=None, message=None):
            fake.progress.append((progress, total, message))

    def call(**kwargs):
        return anyio.run(functools.partial(mcp.tools["fl_render_mix"], ctx=FakeContext(),
                                           **kwargs))

    return call, fake, renders, flp_path


def test_render_mix_without_stems(tool):
    fl_render_mix, fake, renders, flp_path = tool
    result = fl_render_mix()
    assert fake.calls == ["current(save=True)", "mixer.getTrackInfo"]
    assert not any("Master fader" in w for w in result["analysis"]["warnings"])
    assert len(renders) == 1 and renders[0]["name"] == "My Song"
    assert renders[0]["data"] == flp_path.read_bytes()
    assert renders[0]["timeout"] == render_util.render_timeout(16.0)
    assert result["project"] == str(flp_path)
    assert Path(result["render"]["path"]).exists()
    analysis = result["analysis"]
    assert analysis["overall"]["lufs"] is not None
    assert [s["name"] for s in analysis["sections"]] == ["Intro", "Drop"]
    assert analysis["sections"][1]["start_bar"] == 3.0
    assert "stems" not in result
    notes = " ".join(result["notes"])
    assert "saved .flp" in notes and "tempo automation" in notes
    assert "fl_preview_sample" in notes


def test_render_mix_without_sections(tool):
    fl_render_mix, _, _, _ = tool
    result = fl_render_mix(sections=False)
    assert "sections" not in result["analysis"]


def test_render_mix_with_stems(tool):
    fl_render_mix, fake, renders, _ = tool
    result = fl_render_mix(stems="all")
    assert [r["name"] for r in renders] == [
        "My Song", "My Song - stem 1 Kick", "My Song - stem 2 Bass",
        "My Song - stem 3 Bass bus"]
    # Each source stem keeps the bus it feeds; the bus stem keeps both sources.
    assert enabled(renders[1]["data"]) == [0, 1, 3, 4]
    assert enabled(renders[2]["data"]) == [0, 2, 3, 4]
    assert enabled(renders[3]["data"]) == [0, 1, 2, 3, 4]
    stems = result["stems"]
    assert [(s["track"], s["name"]) for s in stems] == [
        (1, "Kick"), (2, "Bass"), (3, "Bass bus")]
    # shares of the MIX energy (rms 0.5 vs 0.4 amplitude; dB values are rounded)
    assert [s["share_pct"] for s in stems] == pytest.approx([100.0, 64.0, 64.0], abs=1.0)
    assert all(not s["silent"] and s["bands"]["sub"] > 35 for s in stems)
    warnings = " ".join(result["stem_warnings"])
    assert ("2 channel(s) play straight into Master (or the last insert, 4) and are in "
            "every stem: Pad, Vox") in warnings
    assert "Fade out" not in warnings
    assert "Kick and Bass both carry" in warnings
    # the bus contains its sources: never compared with them
    assert "Bass bus both" not in warnings and "and Bass bus" not in warnings
    assert "Bass bus) dominates" not in warnings
    assert [s["inserts"] for s in stems] == [[1, 3], [2, 3], [1, 2, 3]]
    assert all("also_contains" not in s for s in stems)
    assert [p[:2] for p in fake.progress] == [(1, 4), (2, 4), (3, 4), (4, 4)]
    assert "channels.getAll" in fake.calls and "mixer.getAllTracks" in fake.calls


def test_bad_stems_fail_before_rendering(tool):
    fl_render_mix, _, renders, _ = tool
    with pytest.raises(ToolError, match="valid: 1..3"):
        fl_render_mix(stems=[4])
    assert renders == []


def test_unreadable_project_still_renders_the_mix(tool):
    fl_render_mix, _, renders, flp_path = tool
    flp_path.write_bytes(b"not a project")
    result = fl_render_mix(stems=[1])
    assert len(renders) == 1
    assert renders[0]["timeout"] == render_util.render_timeout(None)
    assert "sections" not in result["analysis"]
    assert result["stems"] == []
    assert "Stems not rendered" in result["stem_warnings"][0]
    assert any("could not be read" in n for n in result["notes"])


@pytest.mark.parametrize("tool", [None], indirect=True)
def test_live_tempo_when_the_file_has_none(tool):
    fl_render_mix, fake, renders, _ = tool
    result = fl_render_mix()
    assert "transport.getTempo" in fake.calls
    assert [s["name"] for s in result["analysis"]["sections"]] == ["Intro", "Drop"]
    assert renders[0]["timeout"] == render_util.render_timeout(16.0)


def test_a_failed_stem_keeps_the_mix_and_the_other_stems(tool):
    fl_render_mix, fake, renders, _ = tool
    fake.fail_on.add(2)
    result = fl_render_mix(stems=[1, 2, 3])
    assert len(renders) == 4
    assert [s["track"] for s in result["stems"]] == [1, 3]
    assert "stem 2 (Bass) not rendered: ToolError: FL Studio wrote no audio file" in " ".join(
        result["stem_warnings"])
    assert result["analysis"]["overall"]["lufs"] is not None


def test_project_that_does_not_round_trip_refuses_stems_only(tool, monkeypatch):
    fl_render_mix, _, renders, _ = tool
    monkeypatch.setattr(Project, "to_bytes", lambda self: b"different")
    result = fl_render_mix(stems="all")
    assert len(renders) == 1
    assert result["stems"] == []
    assert "cannot rewrite byte for byte" in result["stem_warnings"][0]
    assert [s["name"] for s in result["analysis"]["sections"]] == ["Intro", "Drop"]


def rewrite_mixer(flp_path: Path, routes: dict[int, list[int]], last: int = LAST) -> None:
    project = song(8, [(0, "Intro"), (2 * BAR, "Drop")])
    add_mixer(project, [routes.get(i, []) for i in range(last + 1)])
    flp_path.write_bytes(project.to_bytes())


def test_empty_stem_list_is_no_stems(tool):
    fl_render_mix, fake, renders, _ = tool
    result = fl_render_mix(stems=[])
    assert len(renders) == 1
    assert "stems" not in result and "stem_warnings" not in result
    assert "channels.getAll" not in fake.calls
    assert fake.progress == [(1, 1, "Mix rendered and analysed")]


def test_sidechain_destination_is_not_in_the_source_stem(tool):
    # 1 Kick -> Master, sidechain (level 0) into 2 Bass; 2 Bass -> Master
    fl_render_mix, fake, renders, flp_path = tool
    rewrite_mixer(flp_path, {1: [0, 2], 2: [0], 3: [0], 4: [0]})
    fake.routing = {1: {0: 0.8, 2: 0.0}, 2: {0: 0.8}, 3: {0: 0.8}, 4: {0: 0.8}}
    result = fl_render_mix(stems=[1, 2])
    assert enabled(renders[1]["data"]) == [0, 1, 4]
    # the bass stem keeps the kick (it feeds the bass) but cuts its route to Master
    assert enabled(renders[2]["data"]) == [0, 1, 2, 4]
    routes = flp.mixer_routes(Project.parse(renders[2]["data"]))
    assert routes[1] == {2} and routes[2] == {0}
    assert [s["inserts"] for s in result["stems"]] == [[1], [1, 2]]
    assert "mixer.getRouting" in fake.calls
    assert not any("routing" in n for n in result["notes"])


def test_file_routes_when_fl_cannot_report_routing(tool):
    fl_render_mix, fake, renders, flp_path = tool
    rewrite_mixer(flp_path, {1: [0, 2], 2: [0], 3: [0], 4: [0]})
    fake.routing = None
    result = fl_render_mix(stems=[1])
    assert enabled(renders[1]["data"]) == [0, 1, 2, 4]
    assert len(result["stems"]) == 1
    assert any("follow the routes saved in the file" in n for n in result["notes"])


def test_a_kept_bus_with_its_own_channel_is_reported(tool):
    fl_render_mix, fake, _, _ = tool
    fake.extra_channels = [{"index": 5, "name": "Bus synth", "type": "plugin",
                            "target_fx_track": 3}]
    result = fl_render_mix(stems=[1])
    assert result["stems"][0]["also_contains"] == [
        {"track": 3, "name": "Bass bus", "channels": ["Bus synth"]}]
    assert ("stem 1 (Kick) also contains insert 3 (Bass bus), which it feeds and which "
            "has its own channel(s): Bus synth") in result["stem_warnings"]


def test_any_stem_exception_is_isolated(tool, monkeypatch):
    fl_render_mix, fake, renders, _ = tool
    real = mix_analysis.analyze_mix

    def flaky(path, bounds=None):
        if "stem 1" in str(path):
            raise RuntimeError("bad wav")
        return real(path, bounds)

    monkeypatch.setattr(mix_analysis, "analyze_mix", flaky)
    result = fl_render_mix(stems=[1, 2])
    assert [s["track"] for s in result["stems"]] == [2]
    assert "stem 1 (Kick) not rendered: RuntimeError: bad wav" in result["stem_warnings"]
    assert [p[:2] for p in fake.progress] == [(1, 3), (2, 3), (3, 3)]


@pytest.mark.parametrize("volume", [0.0, 0.05])
def test_render_warns_when_master_fader_is_down(tool, volume):
    fl_render_mix, fake, _, _ = tool
    fake.master_volume = volume
    warnings = fl_render_mix(sections=False)["analysis"]["warnings"]
    assert warnings[0].startswith(f"Master fader is at {volume:.2f}: the render is (nearly) "
                                  "silent")
    # Unknown whether a clip drives Master: both fixes, the clip one first.
    assert "fl_edit_arrangement" in warnings[0] and "fl_set_track_volume" in warnings[0]


def test_master_warning_names_the_fix_that_works():
    driven = render_tool.master_silent_warning(0.0, ["Fade out"])
    assert "'Fade out'" in driven and "fl_edit_arrangement" in driven
    assert "cannot set Master" in driven and "fl_set_song_position" in driven
    free = render_tool.master_silent_warning(0.0, [])
    assert "fl_set_track_volume(track=0, volume=0.8)" in free
    assert "fl_edit_arrangement" not in free


def test_master_warning_checks_the_automation_clips_of_the_file(tool, monkeypatch):
    fl_render_mix, fake, _, _ = tool
    fake.master_volume = 0.0
    original = fake.send_command
    monkeypatch.setattr(fake, "send_command", lambda a, p=None, t=2.0: (
        {"success": True, "targets": [{"rec_id": 0x7ABCDEF, "label": "Master volume"}]}
        if a == "automation.resolve" else original(a, p, t)))
    warning = fl_render_mix(sections=False)["analysis"]["warnings"][0]
    assert "No automation clip drives it" in warning


def test_render_without_master_volume_does_not_fail(tool):
    fl_render_mix, fake, _, _ = tool
    fake.master_volume = None
    warnings = fl_render_mix(sections=False)["analysis"]["warnings"]
    assert not any("Master fader" in w for w in warnings)


def test_master_warning_skipped_when_the_file_is_not_the_open_project(tool, monkeypatch, tmp_path):
    fl_render_mix, fake, _, flp_path = tool
    fake.master_volume = 0.0
    monkeypatch.setattr(project_files, "_last_project", tmp_path / "Other.flp")
    result = fl_render_mix(sections=False, save=False, project_path=str(flp_path))
    assert not any("Master fader" in w for w in result["analysis"]["warnings"])
    assert "mixer.getTrackInfo" not in fake.calls


def test_master_warning_kept_for_the_located_open_project_without_saving(tool, monkeypatch):
    fl_render_mix, fake, _, flp_path = tool
    fake.master_volume = 0.0
    monkeypatch.setattr(project_files, "_last_project", flp_path)
    result = fl_render_mix(sections=False, save=False, project_path=str(flp_path))
    assert result["analysis"]["warnings"][0].startswith("Master fader is at 0.00")


def test_song_seconds_run_to_a_marker_past_the_last_clip():
    # FL counts markers in the song length: the render runs on to the last one.
    assert render_tool.song_seconds(song(16, markers=[(20 * BAR, "End")])) == pytest.approx(40.0)
    assert render_tool.song_seconds(song(16, markers=[(4 * BAR, "B")])) == pytest.approx(32.0)


def test_trailing_silence_warning_names_the_marker_that_causes_it():
    project = song(16, markers=[(0, "Intro"), (20 * BAR, "Outro end")])
    text = render_tool.trailing_silence_warning(project, 8.0)
    assert "'Outro end' at bar 21" in text and "ends at bar 17" in text
    assert "counts markers in the song length" in text
    plain = render_tool.trailing_silence_warning(song(16, markers=[(0, "Intro")]), 3.0)
    assert plain.startswith("3.0 s of silence") and "marker" not in plain
    assert render_tool.trailing_silence_warning(project, 1.0) is None
    assert render_tool.trailing_silence_warning(None, None) is None


def test_render_mix_notes_shared_files_that_could_not_be_put_back(tool, monkeypatch):
    fl_render_mix, _, _, _ = tool
    real = render_util.render_wav

    def failing(*args, **kwargs):
        render_util._restore_problems.append(Path("/fl/reg.xml"))
        return real(*args, **kwargs)

    render_util._restore_problems.append(Path("/stale/from/an/earlier/call"))
    monkeypatch.setattr(render_util, "render_wav", failing)
    notes = " ".join(fl_render_mix()["notes"])
    assert "could not be put back: /fl/reg.xml." in notes and "/stale" not in notes
