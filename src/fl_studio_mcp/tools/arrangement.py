"""Playlist arrangement: read and place pattern clips.

FL Studio's scripting API cannot read or place playlist clips. These tools go
through the project file: save (Ctrl+S), read the .flp, and for edits write
the new clip list into it and let FL Studio reopen the project. A copy of the
file is kept in the project's Backup folder before every edit.

Positions are in bars, 1-based like FL's ruler (bar 1 = song start, 9.5 =
half-way through bar 9); tracks are 1-based like the playlist.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.annotations import DESTRUCTIVE, IDEMPOTENT
from fl_studio_mcp.utils import project_files
from fl_studio_mcp.utils.errors import run
from fl_studio_mcp.utils.flp import (
    MAX_TRACKS,
    AutomationPoint,
    Clip,
    FLPError,
    Project,
    add_automation_clip,
    add_sample_channel,
    automation_clips,
    automation_item_template,
    set_automation_points,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

OPS = ("place", "delete", "copy", "move", "clear", "automation", "set_automation")


def _num(op: dict, key: str, default: Any = None, *, minimum: float | None = None) -> Any:
    value = op.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ToolError(f"{op.get('op')}: {key} must be a number, got {value!r}")
    if minimum is not None and value < minimum:
        raise ToolError(f"{op.get('op')}: {key} must be >= {minimum:g}, got {value!r}")
    return value


def _track(op: dict, key: str = "track", required: bool = False) -> int | None:
    value = op.get(key)
    if value is None:
        if required:
            raise ToolError(f"{op.get('op')}: '{key}' (1-{MAX_TRACKS}) is required")
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_TRACKS:
        raise ToolError(f"{op.get('op')}: {key} must be an integer 1-{MAX_TRACKS}, got {value!r}")
    return value - 1


class Timeline:
    """Bar <-> tick conversion for one project."""

    def __init__(self, ppq: int, bar_ticks: int) -> None:
        self.ppq = ppq
        self.bar_ticks = bar_ticks

    def ticks(self, bar: float) -> int:
        return int(round((bar - 1) * self.bar_ticks))

    def length(self, bars: float) -> int:
        return int(round(bars * self.bar_ticks))

    def bar(self, ticks: int) -> float:
        return round(ticks / self.bar_ticks + 1, 4)

    def bars(self, ticks: int) -> float:
        return round(ticks / self.bar_ticks, 4)


def resolve_pattern(ref: Any, patterns: dict[int, dict]) -> int:
    """Pattern number from a number or a (case-insensitive) name."""
    if isinstance(ref, bool):
        raise ToolError(f"pattern must be a number or a name, got {ref!r}")
    if isinstance(ref, int):
        if ref < 1:
            raise ToolError(f"pattern numbers start at 1, got {ref}")
        return ref
    if isinstance(ref, str):
        wanted = ref.strip().lower()
        matches = [n for n, p in patterns.items() if str(p.get("name", "")).lower() == wanted]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ToolError(f"several patterns are named {ref!r}: {matches}; use the number")
        known = ", ".join(f"{n}={p.get('name')!r}" for n, p in sorted(patterns.items()))
        raise ToolError(f"no pattern named {ref!r}. Patterns: {known or 'none'}")
    raise ToolError(f"pattern must be a number or a name, got {ref!r}")


def _in_range(clip: Clip, start: int | None, end: int | None, track: int | None,
              pattern: int | None) -> bool:
    return (
        (track is None or clip.track == track)
        and (pattern is None or clip.pattern == pattern)
        and (start is None or clip.position >= start)
        and (end is None or clip.position < end)
    )


def apply_operations(
    clips: list[Clip], operations: list[dict], timeline: Timeline, patterns: dict[int, dict]
) -> tuple[list[Clip], list[str]]:
    """Apply edit operations to a copy of `clips`; returns (clips, log)."""
    if not isinstance(operations, list) or not operations:
        raise ToolError("operations must be a non-empty list")
    result = list(clips)
    log: list[str] = []
    for n, op in enumerate(operations):
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise ToolError(f"operation {n}: 'op' must be one of {', '.join(OPS)}; got {op!r}")
        kind = op["op"]
        try:
            if kind == "place":
                result, text = _place(result, op, timeline, patterns)
            elif kind == "automation":
                result, text = _place_automation(result, op, timeline)
            elif kind == "set_automation":
                text = op.get("_log", "automation points replaced")
            elif kind == "clear":
                text = f"cleared {len(result)} clip(s)"
                result = []
            elif kind == "delete":
                result, text = _delete(result, op, timeline, patterns)
            else:
                result, text = _copy(result, op, timeline, move=kind == "move")
        except ToolError as e:
            raise ToolError(f"operation {n} ({kind}): {e}") from e
        log.append(text)
    return result, log


def _place(clips, op, timeline, patterns):
    pattern = resolve_pattern(op.get("pattern"), patterns)
    track = _track(op, required=True)
    bar = _num(op, "bar", minimum=1)
    if bar is None:
        raise ToolError("'bar' (1-based start bar) is required")
    default_bars = None
    info = patterns.get(pattern)
    if info and info.get("length_beats"):
        default_bars = info["length_beats"] * timeline.ppq / timeline.bar_ticks
    length_bars = _num(op, "length_bars", default_bars, minimum=0)
    if not length_bars:
        raise ToolError(f"length_bars is required (pattern {pattern}'s length is unknown)")
    repeat = op.get("repeat", 1)
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 512:
        raise ToolError(f"repeat must be an integer 1-512, got {repeat!r}")
    every = _num(op, "every_bars", length_bars, minimum=0) or length_bars
    replace = op.get("replace", True)
    length = timeline.length(length_bars)
    if length < 1:
        raise ToolError("length_bars is shorter than one tick")
    added = []
    for i in range(repeat):
        start = timeline.ticks(bar + i * every)
        if replace:
            clips = [c for c in clips
                     if not (c.track == track and c.position < start + length and c.end > start)]
        clip = Clip(position=start, length=length, track=track, pattern=pattern)
        if default_bars is None or length != timeline.length(default_bars):
            # FL resizes an untrimmed (-1, -1) clip to its pattern on load.
            clip.start_offset, clip.end_offset = 0, length
        added.append(clip)
    name = (info or {}).get("name") or f"Pattern {pattern}"
    times = f" x{repeat} every {every:g} bar(s)" if repeat > 1 else ""
    return clips + added, (
        f"placed {name!r} (pattern {pattern}) on track {track + 1} at bar {bar:g}, "
        f"{length_bars:g} bar(s){times}"
    )


def _place_automation(clips, op, timeline):
    """Place the automation clip created for this op (see prepare_automation)."""
    track = _track(op, required=True)
    start, length = op["_start"], op["_length"]
    clips = [c for c in clips
             if not (c.track == track and c.position < start + length and c.end > start)]
    clip = Clip(position=start, length=length, track=track, channel=op["_channel"],
                raw=op.get("_raw", b""))
    # The raw item is copied from an existing clip: its trim (beats) must not cut this one.
    clip.start_offset, clip.end_offset = 0.0, length / timeline.ppq
    return clips + [clip], (
        f"automation {op['_name']!r} on track {track + 1} from bar {timeline.bar(start):g} "
        f"to {timeline.bar(start + length):g} ({len(op['points'])} points)"
    )


def parse_points(op: dict, timeline: Timeline, absolute: bool) -> tuple[float, list]:
    """Validated points -> (start bar, [AutomationPoint] with beats from the start)."""
    points = op.get("points")
    if not isinstance(points, list) or len(points) < 2:
        raise ToolError("'points' needs at least 2 points")
    key = "bar" if absolute else "beat"
    parsed = []
    for i, point in enumerate(points):
        if not isinstance(point, dict) or key not in point or "value" not in point:
            raise ToolError(f"point {i}: needs '{key}' and 'value' (0-1)")
        position = _num(point, key, minimum=1 if absolute else 0)
        value = _num(point, "value", minimum=0)
        tension = _num(point, "tension", 0.0) or 0.0
        if value > 1:
            raise ToolError(f"point {i}: value must be 0-1, got {value!r}")
        if not -1 <= tension <= 1:
            raise ToolError(f"point {i}: tension must be -1..1, got {tension!r}")
        parsed.append((position, value, tension))
    if any(b[0] < a[0] for a, b in zip(parsed, parsed[1:])):
        raise ToolError(f"points must be in {key} order")
    first = parsed[0][0]
    beats_per_bar = timeline.bar_ticks / timeline.ppq
    out = []
    for position, value, tension in parsed:
        offset = (position - first) * beats_per_bar if absolute else position - first
        out.append(AutomationPoint(beat=offset, value=value, tension=tension))
    if out[-1].beat <= 0:
        raise ToolError("the points must span some time")
    return first, out


def _delete(clips, op, timeline, patterns):
    track = _track(op)
    start_bar = _num(op, "from_bar", minimum=1)
    end_bar = _num(op, "to_bar", minimum=1)
    pattern = resolve_pattern(op["pattern"], patterns) if op.get("pattern") is not None else None
    start = timeline.ticks(start_bar) if start_bar is not None else None
    end = timeline.ticks(end_bar) if end_bar is not None else None
    kept = [c for c in clips if not _in_range(c, start, end, track, pattern)]
    where = f"track {track + 1}" if track is not None else "all tracks"
    span = f"bars {start_bar or 1:g}-{end_bar:g}" if end_bar else f"from bar {start_bar or 1:g}"
    return kept, f"deleted {len(clips) - len(kept)} clip(s) on {where}, {span}"


def _copy(clips, op, timeline, move: bool):
    start_bar = _num(op, "from_bar", minimum=1)
    end_bar = _num(op, "to_bar", minimum=1)
    dest_bar = _num(op, "to", minimum=1)
    if start_bar is None or end_bar is None or dest_bar is None:
        raise ToolError("'from_bar', 'to_bar' (exclusive) and 'to' (destination bar) are required")
    if end_bar <= start_bar:
        raise ToolError("to_bar must be after from_bar")
    track = _track(op)
    dest_track = _track(op, "to_track")
    start, end = timeline.ticks(start_bar), timeline.ticks(end_bar)
    shift = timeline.ticks(dest_bar) - start
    source = [c for c in clips if _in_range(c, start, end, track, None)]
    track_shift = 0 if dest_track is None or track is None else dest_track - track
    copies = []
    for c in source:
        new_track = c.track + track_shift if track is not None else (
            dest_track if dest_track is not None else c.track)
        if not 0 <= new_track < MAX_TRACKS:
            raise ToolError(f"destination track {new_track + 1} is out of range")
        copies.append(Clip(position=c.position + shift, length=c.length, track=new_track,
                           pattern=c.pattern, channel=c.channel, raw=c.raw))
    moved = {id(c) for c in source} if move else set()
    base = [c for c in clips if id(c) not in moved]
    # Clips already at the destination on the same track and position are replaced.
    taken = {(c.track, c.position) for c in copies}
    base = [c for c in base if (c.track, c.position) not in taken]
    verb = "moved" if move else "copied"
    return base + copies, (
        f"{verb} {len(copies)} clip(s) from bars {start_bar:g}-{end_bar:g} to bar {dest_bar:g}"
    )


def describe(project: Project, arrangement, patterns: dict[int, dict] | None = None,
             track: int | None = None, path: Path | None = None) -> dict:
    """The arrangement as bars, grouped by track, for a tool answer."""
    timeline = Timeline(project.ppq, project.bar_ticks)
    names = {n: p.get("name") for n, p in (patterns or {}).items()}
    for number, name in project.pattern_names().items():
        names.setdefault(number, name)
    channel_names = project.channel_names()
    by_track: dict[int, list[dict]] = {}
    end = 0
    for clip in sorted(arrangement.clips, key=lambda c: (c.track, c.position)):
        end = max(end, clip.end)
        if track is not None and clip.track != track - 1:
            continue
        entry = {"bar": timeline.bar(clip.position), "length_bars": timeline.bars(clip.length),
                 "end_bar": timeline.bar(clip.end)}
        if clip.pattern is not None:
            entry["pattern"] = clip.pattern
            entry["pattern_name"] = names.get(clip.pattern) or f"Pattern {clip.pattern}"
        else:
            entry["channel"] = clip.channel
            entry["clip_name"] = channel_names.get(clip.channel, f"Channel {clip.channel}")
        by_track.setdefault(clip.track, []).append(entry)
    num, beat = project.time_signature
    out = {
        "project": str(path) if path else None,
        "fl_version": project.version,
        "time_signature": f"{num}/{beat}",
        "ppq": project.ppq,
        "arrangement": {"index": arrangement.index, "name": arrangement.name},
        "song_length_bars": timeline.bars(end),
        "clip_count": len(arrangement.clips),
        "markers": [{"bar": timeline.bar(m.position), "name": m.name}
                    for m in sorted(arrangement.markers, key=lambda m: m.position)],
        "tracks": [
            {"track": t + 1, "name": arrangement.track_names.get(t + 1), "clips": entries}
            for t, entries in sorted(by_track.items())
        ],
        "patterns": {str(n): name for n, name in sorted(names.items()) if name},
    }
    autos = automation_clips(project)
    if autos:
        out["automation_clips"] = [
            {"channel": a.channel, "name": a.name, "target_id": a.target,
             "length_beats": round(a.length_beats, 4),
             "points": [{"beat": round(pt.beat, 4), "value": round(pt.value, 4),
                         "tension": round(pt.tension, 3)} for pt in a.points]}
            for a in autos
        ]
    return out


def prepare_automation(project: Project, operations: list[dict], timeline: Timeline,
                       resolve) -> None:
    """Create the automation clips asked for, before the clip operations run.

    `resolve(targets)` returns [{"rec_id", "label"}] for the target dicts (the
    controller computes FL's REC event ids). "automation" ops get the new
    channel and their placement; "set_automation" ops rewrite existing points.
    """
    new_ops = [op for op in operations if isinstance(op, dict) and op.get("op") == "automation"]
    for n, op in enumerate(new_ops):
        if not isinstance(op.get("target"), dict):
            raise ToolError(f"automation op {n}: 'target' must be an object, e.g. "
                            '{"type": "mixer_volume", "track": 3}')
        _track(op, required=True)
    resolved = resolve([op["target"] for op in new_ops]) if new_ops else []
    item_raw = automation_item_template(project) if new_ops else None
    for op, target in zip(new_ops, resolved):
        start_bar, points = parse_points(op, timeline, absolute=True)
        name = op.get("name") or target["label"]
        pitch_range = None
        if op["target"].get("type") == "channel_pitch":
            pitch_range = float(target.get("pitch_range") or 2)
        channel = add_automation_clip(project, str(name), int(target["rec_id"]), points,
                                      pitch_range=pitch_range)
        op["_channel"] = channel
        op["_name"] = name
        op["_start"] = timeline.ticks(start_bar)
        op["_length"] = int(round(points[-1].beat * timeline.ppq))
        op["_raw"] = item_raw or b""

    existing = None
    for op in operations:
        if not isinstance(op, dict) or op.get("op") != "set_automation":
            continue
        existing = existing or automation_clips(project)
        ref = op.get("clip")
        matches = [a for a in existing
                   if (isinstance(ref, int) and not isinstance(ref, bool) and a.channel == ref)
                   or (isinstance(ref, str) and a.name.lower() == ref.strip().lower())]
        if len(matches) != 1:
            known = ", ".join(f"{a.channel}={a.name!r}" for a in existing) or "none"
            raise ToolError(f"set_automation: no single automation clip {ref!r}. Clips: {known}")
        _, points = parse_points(op, timeline, absolute=False)
        set_automation_points(project, matches[0], points)
        op["_log"] = f"replaced the points of {matches[0].name!r} ({len(points)} points)"


from fl_studio_mcp.tools.samples import AUDIO_EXTENSIONS  # noqa: E402


def _commit(conn, path: Path, data: bytes, info: dict) -> dict:
    """Back up, write the new project file and make FL Studio reopen it."""
    after_save = project_files.controller_info(conn)
    if after_save.get("changed"):
        raise ToolError(
            "FL Studio still reports unsaved changes after saving; nothing was written. "
            "Save the project by hand and retry."
        )
    Project.parse(data)  # never write something this parser cannot read back
    backup = project_files.backup_project(path)
    project_files.write_atomically(path, data)
    sentinel = project_files.place_sentinel(conn)
    project_files.open_in_fl(path)
    loaded = project_files.wait_for_reload(conn, info, sentinel)
    if loaded.get("last_load_status") == "error":
        raise ToolError(
            f"FL Studio reported an error while reopening {path}. The previous version "
            f"is in {backup}; reopen it with fl_open_project."
        )
    return {"project": str(path), "backup": str(backup), "reloaded": True}


def _live_patterns(conn) -> dict[int, dict]:
    result = run(conn, "patterns.getAll", {"include_default": False})
    return {int(p["index"]): p for p in result.get("patterns", [])}


def _load_checked(path: Path) -> tuple[Project, bytes]:
    data = path.read_bytes()
    try:
        project = Project.parse(data)
    except FLPError as e:
        raise ToolError(f"Cannot read {path}: {e}") from e
    if project.to_bytes() != data:
        raise ToolError(
            f"{path} uses a layout this server cannot rewrite byte for byte; refusing to edit it."
        )
    return project, data


def register_arrangement_tools(mcp: FastMCP) -> None:
    """Register the file-based arrangement tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_save_project(new_version: bool = False, project_path: str | None = None) -> dict:
        """Save the open project (Ctrl+S) and return the path of the saved .flp.

        When FL has no unsaved change and the file is the one it last saved,
        nothing is saved ("saved": false): the file is already current.

        Args:
            new_version: save as a new numbered version ("Save new version")
            project_path: the .flp you expect FL to save to; found automatically if omitted
        """
        conn = get_connection()
        path, saved = project_files.save_and_locate(conn, project_path, new_version)
        out = {"saved": saved, "project": str(path)}
        if not saved:
            out["note"] = "No unsaved changes: the file on disk is already current."
        return out

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_get_arrangement(
        track: int | None = None,
        save: bool = True,
        project_path: str | None = None,
        arrangement: int | None = None,
    ) -> dict:
        """Read the playlist: every clip (pattern, track, start bar, length) and the markers.

        FL's API cannot read clips, so the project is saved first (Ctrl+S) and
        its file is read. Bars are 1-based like FL's ruler, tracks 1-based.

        Args:
            track: only list this playlist track (1-based)
            save: save first so the file matches FL (default True)
            project_path: the open project's .flp, when it cannot be found automatically
            arrangement: arrangement index (default: the current one)
        """
        conn = get_connection()
        path = project_files.current_project_file(conn, save, project_path)
        project, _ = _load_checked(path)
        try:
            arr = project.arrangement(arrangement)
        except FLPError as e:
            raise ToolError(str(e)) from e
        try:
            patterns = _live_patterns(conn)
        except ToolError:
            patterns = {}
        return describe(project, arr, patterns, track, path)

    @mcp.tool(annotations=DESTRUCTIVE)
    def fl_edit_arrangement(
        operations: list[dict],
        dry_run: bool = False,
        project_path: str | None = None,
        arrangement: int | None = None,
    ) -> dict:
        """Place, delete, copy or move clips and create automation clips, in one batch.

        Saves the project, backs the file up (Backup/<name> (before MCP edit
        ...).flp), writes the new clips and makes FL Studio reopen the project
        (a few seconds; the undo history is reset). Bars are 1-based, tracks
        1-based. Operations run in order:

        - {"op": "place", "pattern": 2 or "Hook", "track": 1, "bar": 9,
           "length_bars": 4 (default: the pattern's length),
           "repeat": 4, "every_bars": 4 (default: length_bars),
           "replace": true (default: remove clips it overlaps on that track)}
        - {"op": "delete", "track": 1 (default all), "from_bar": 9,
           "to_bar": 17 (exclusive, default end), "pattern": optional filter}
           Removes clips that START in the range.
        - {"op": "copy" | "move", "from_bar": 9, "to_bar": 25, "to": 41,
           "track": optional source track, "to_track": optional destination}
        - {"op": "clear"}: remove every clip
        - {"op": "automation", "target": {...}, "track": 12, "name": optional,
           "points": [{"bar": 9, "value": 0.2}, {"bar": 17, "value": 0.8,
                      "tension": 0.4}]}
           Creates an automation clip from the first to the last point (song
           bars) and places it on `track`. Values are FL's normalized 0-1:
           volumes use the fader scale of fl_set_track_volume (0.8 = 0 dB),
           pan and pitch 0 = left/-max, 0.5 = centre, 1 = right/+max,
           plugin parameters the value of fl_set_plugin_param. tension -1..1
           curves the segment that ends at that point. FL keeps an
           automation clip's last value after the clip ends: a fade of Master
           to 0 leaves Master at 0, so the song is silent from its start on
           the next playback or render; end a fade with a point back at the
           normal level, or reset the fader afterwards. Targets:
             {"type": "tempo"}
             {"type": "mixer_volume" | "mixer_pan" | "mixer_stereo", "track": 3}
             {"type": "mixer_eq", "track": 3, "band": 0-2, "eq": "gain"|"freq"|"q"}
             {"type": "effect_mix", "track": 3, "slot": 0}
             {"type": "effect_param", "track": 3, "slot": 0, "param": "Cutoff" or 12}
             {"type": "channel_volume" | "channel_pan" | "channel_pitch", "channel": 2}
             {"type": "channel_param", "channel": 2, "param": "Cutoff" or 12}
        - {"op": "set_automation", "clip": channel index or name,
           "points": [{"beat": 0, "value": 0.1}, {"beat": 16, "value": 1}]}
           Replaces the points of an existing automation clip (beats from its start).

        Args:
            operations: the edits, applied in order
            dry_run: compute and return the result without touching FL or the file
            project_path: the open project's .flp, when it cannot be found automatically
            arrangement: arrangement index (default: the current one)

        Example: [{"op": "place", "pattern": "Intro", "track": 1, "bar": 1, "repeat": 4},
                  {"op": "copy", "from_bar": 9, "to_bar": 25, "to": 41}]
        """
        conn = get_connection()
        info = project_files.controller_info(conn)
        patterns = _live_patterns(conn)
        path = project_files.current_project_file(conn, True, project_path)
        project, _ = _load_checked(path)
        try:
            arr = project.arrangement(arrangement)
        except FLPError as e:
            raise ToolError(str(e)) from e
        timeline = Timeline(project.ppq, project.bar_ticks)
        before = len(arr.clips)
        if any(isinstance(op, dict) and op.get("op") in ("automation", "set_automation")
               for op in operations):
            def resolve(targets):
                resolved = run(conn, "automation.resolve", {"targets": targets}).get("targets", [])
                for target, out in zip(targets, resolved):
                    if target.get("type") == "channel_pitch":
                        info = run(conn, "channels.getInfo",
                                   {"index": target.get("channel"), "use_global": True})
                        out["pitch_range"] = info.get("pitch_range")
                return resolved

            try:
                prepare_automation(project, operations, timeline, resolve)
                arr = project.arrangement(arrangement)  # events moved: read it again
            except FLPError as e:
                raise ToolError(str(e)) from e
        new_clips, log = apply_operations(arr.clips, operations, timeline, patterns)

        if dry_run:
            arr.clips = new_clips
            preview = describe(project, arr, patterns, None, path)
            return {"dry_run": True, "log": log, "clips_before": before,
                    "clips_after": len(new_clips), "arrangement": preview}

        try:
            project.set_clips(arr, new_clips)
        except FLPError as e:
            raise ToolError(str(e)) from e
        data = project.to_bytes()
        check = Project.parse(data).arrangement(arr.index)
        expected = sorted((c.position, c.track, c.pattern, c.channel, c.length) for c in new_clips)
        written = sorted((c.position, c.track, c.pattern, c.channel, c.length)
                         for c in check.clips)
        if expected != written:
            raise ToolError("internal check failed: the rewritten file does not hold the clips")
        result = {"log": log, "clips_before": before, "clips_after": len(new_clips)}
        result.update(_commit(conn, path, data, info))
        try:
            length = run(conn, "transport.getLength")
            expected_end = max((c.end for c in new_clips), default=0)
            result["song_length_bars"] = timeline.bars(length.get("ticks") or 0)
            result["expected_length_bars"] = timeline.bars(expected_end)
        except ToolError:
            pass
        return result

    @mcp.tool(annotations=DESTRUCTIVE)
    def fl_add_sample_channels(samples: list[dict], project_path: str | None = None) -> dict:
        """Add Sampler channels that play audio files (kick, 808, vocal chop...).

        FL's API cannot load samples, so this edits the project file: it saves,
        clones a sampler channel of the project (its settings and routing come
        along), points the copies at the files and makes FL reopen the project
        (a few seconds; the undo history is reset; a backup is kept). Pass all
        samples in one call: one reload for all. Find files with fl_find_samples.

        Args:
            samples: [{"path": "/abs/path/Kick.wav", "name": "Kick" (default: file
                       name), "mixer_track": 2 (optional, routed after loading)}]
            project_path: the open project's .flp, when it cannot be found automatically
        """
        if not isinstance(samples, list) or not samples:
            raise ToolError("samples must be a non-empty list")
        checked = []
        for i, sample in enumerate(samples):
            if not isinstance(sample, dict) or not sample.get("path"):
                raise ToolError(f"sample {i}: needs 'path'")
            file = Path(str(sample["path"])).expanduser()
            if not file.is_file():
                raise ToolError(f"sample {i}: {file} does not exist")
            if file.suffix.lower() not in AUDIO_EXTENSIONS:
                raise ToolError(f"sample {i}: {file.suffix} is not an audio format FL loads")
            checked.append((file, sample.get("name") or file.stem, sample.get("mixer_track")))
        conn = get_connection()
        info = project_files.controller_info(conn)
        path = project_files.current_project_file(conn, True, project_path)
        project, _ = _load_checked(path)
        added = []
        try:
            for file, name, mixer_track in checked:
                index = add_sample_channel(project, str(file), str(name))
                added.append({"channel": index, "name": name, "path": str(file),
                              "mixer_track": mixer_track})
        except FLPError as e:
            raise ToolError(str(e)) from e
        result = {"added": added}
        result.update(_commit(conn, path, project.to_bytes(), info))
        for entry in added:
            if entry["mixer_track"] is not None:
                run(conn, "channels.routeToMixer", {"channel_index": entry["channel"],
                                                    "mixer_track": entry["mixer_track"]})
        try:
            live = run(conn, "channels.getAll").get("channels", [])
            names = {c["index"]: c["name"] for c in live}
            result["verified"] = all(names.get(e["channel"]) == e["name"] for e in added)
        except ToolError:
            pass
        return result

    @mcp.tool(annotations=DESTRUCTIVE)
    def fl_open_project(path: str) -> dict:
        """Open a .flp in the running FL Studio (e.g. a backup made by fl_edit_arrangement).

        Unsaved changes in the current project are at stake: FL Studio will ask
        to save them. Waits until the project is loaded.
        """
        target = Path(path).expanduser()
        if not target.is_file() or target.suffix.lower() != ".flp":
            raise ToolError(f"{target} is not an .flp file")
        conn = get_connection()
        info = project_files.controller_info(conn)
        sentinel = project_files.place_sentinel(conn)
        project_files.open_in_fl(target)
        loaded = project_files.wait_for_reload(conn, info, sentinel)
        return {"opened": str(target), "status": loaded.get("last_load_status")}
