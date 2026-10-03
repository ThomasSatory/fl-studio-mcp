"""Listen to the mix: render the saved project to WAV and measure it.

FL Studio's scripting API cannot render. fl_render_mix saves the project,
runs FL's command-line renderer on a copy of the saved .flp (about 20 s per
render, one render at a time), then analyses the WAV as a whole and section
by section (playlist markers, or 8-bar blocks). Per-mixer-track stems are
rendered from copies of the file where every unrelated insert is disabled:
the renderer ignores solo flags.
"""

from __future__ import annotations

import functools
import math
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio.to_thread
from fastmcp import Context
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.annotations import ACTION
from fl_studio_mcp.tools.arrangement import Timeline, song_end
from fl_studio_mcp.utils import flp, mix_analysis, project_files, render
from fl_studio_mcp.utils.errors import run
from fl_studio_mcp.utils.flp import FLPError, Project

if TYPE_CHECKING:
    from fastmcp import FastMCP

MAX_STEMS = 8
BLOCK_BARS = 8


def _seconds(ticks: int, ppq: int, tempo: float) -> float:
    return ticks / ppq * 60.0 / tempo


def _song_end_ticks(project: Project) -> int | None:
    try:
        clips = project.arrangement().clips
    except FLPError:
        return None
    if not clips:
        return None
    return max(c.end for c in clips)


def song_seconds(project: Project, tempo: float | None = None) -> float | None:
    """Length of the current arrangement in seconds, as FL renders it.

    The song ends with its last clip, or at the last playlist marker when
    that is later (FL counts markers in the song length).
    tempo: BPM; default the file's (FL 2025 files may not store one).
    """
    if _song_end_ticks(project) is None:
        return None
    arr = project.arrangement()
    end = song_end(arr.clips, arr.markers)
    tempo = tempo or project.tempo
    if not tempo:
        return None
    return _seconds(end, project.ppq, tempo)


TRAILING_SILENCE = 2.0  # seconds of silence at the end worth a warning


def trailing_silence_warning(project: Project | None, seconds: float | None) -> str | None:
    """A warning for silence at the end of the render, naming a marker that causes it."""
    if seconds is None or seconds <= TRAILING_SILENCE:
        return None
    text = f"{seconds} s of silence at the end of the render"
    try:
        arr = project.arrangement() if project is not None else None
    except FLPError:
        arr = None
    if arr is not None and arr.clips and arr.markers:
        clips_end = max(c.end for c in arr.clips)
        last = max(arr.markers, key=lambda m: m.position)
        if last.position > clips_end:
            timeline = Timeline(project.ppq, project.bar_ticks)
            return (f"{text}: the playlist marker {last.name or 'unnamed'!r} at bar "
                    f"{timeline.bar(last.position):g} lies past the last clip (which ends at "
                    f"bar {timeline.bar(clips_end):g}), and FL counts markers in the song "
                    "length, so the render runs on to it. Move or delete that marker to end "
                    "the song with the music.")
    return text + " (a long effect tail, or clips that end in silence)."


def section_bounds(project: Project, tempo: float | None = None) -> list[dict]:
    """Sections of the song from the current arrangement's markers, in seconds and bars.

    Each marker starts a section that runs to the next one (the last runs to
    the end of the song); an implicit "Intro" covers the bars before a first
    marker placed after bar 1. Without markers, the song is cut into 8-bar
    blocks named "bars 1-8", "bars 9-16"... Empty when the song has no clips
    or no tempo. tempo: BPM; default the file's.
    """
    end = _song_end_ticks(project)
    tempo = tempo or project.tempo
    if end is None or not tempo:
        return []
    timeline = Timeline(project.ppq, project.bar_ticks)
    markers = sorted(project.arrangement().markers, key=lambda m: m.position)
    starts: list[tuple[int, str]] = []
    for n, marker in enumerate(markers):
        if marker.position >= end:
            break
        if starts and starts[-1][0] == marker.position:
            continue
        starts.append((marker.position, marker.name or f"marker {n + 1}"))
    if not starts:
        last_bar = math.ceil(end / timeline.bar_ticks)
        step = BLOCK_BARS * timeline.bar_ticks
        for pos in range(0, end, step):
            first = pos // timeline.bar_ticks + 1
            starts.append((pos, f"bars {first}-{min(first + BLOCK_BARS - 1, last_bar)}"))
    elif starts[0][0] > 0:
        starts.insert(0, (0, "Intro"))
    sections = []
    for n, (pos, name) in enumerate(starts):
        stop = starts[n + 1][0] if n + 1 < len(starts) else end
        sections.append({
            "name": name,
            "start_seconds": round(_seconds(pos, project.ppq, tempo), 3),
            "end_seconds": round(_seconds(stop, project.ppq, tempo), 3),
            "start_bar": timeline.bar(pos),
            "end_bar": timeline.bar(stop),
        })
    return sections


def _used_and_buses(routes: dict[int, set[int]], channel_targets: list[int],
                    last_insert: int) -> tuple[set[int], set[int]]:
    """Inserts fed by a channel, and buses.

    Used inserts are those fed by a channel plus every insert their signal
    reaches. A bus is an insert (not Master, not the last one) that receives
    routes from at least two used inserts, whether or not a channel also
    plays into it; a single send or sidechain destination is not a bus.
    """
    direct = {t for t in channel_targets if 1 <= t < last_insert}
    used = set(direct)
    stack = list(direct)
    while stack:
        source = stack.pop()
        for dest in routes.get(source, ()):
            if 1 <= dest < last_insert and dest not in used:
                used.add(dest)
                stack.append(dest)
    feeders: dict[int, set[int]] = {}
    for source in used:
        for dest in routes.get(source, ()):
            if dest != source and 1 <= dest < last_insert:
                feeders.setdefault(dest, set()).add(source)
    buses = {dest for dest, sources in feeders.items() if len(sources) >= 2}
    return direct, buses


def resolve_stems(spec: Any, routes: dict[int, set[int]], channel_targets: list[int],
                  last_insert: int) -> list[int]:
    """Mixer inserts to render as stems.

    spec: None (no stems), a list of insert numbers (1 .. last_insert - 1),
    "all" (every insert fed by a channel, plus buses) or "buses" (inserts
    receiving routes from at least two used inserts, see _used_and_buses;
    a single send or sidechain target is not a bus). channel_targets should
    leave out automation-clip channels. At most MAX_STEMS per call.
    """
    if spec is None or (isinstance(spec, (list, tuple)) and not spec):
        return []
    if isinstance(spec, str):
        direct, buses = _used_and_buses(routes, channel_targets, last_insert)
        if spec == "all":
            targets = sorted(direct | buses)
        elif spec == "buses":
            targets = sorted(buses)
        else:
            raise ToolError(f"stems must be a list of mixer tracks, 'all' or 'buses'; "
                            f"got {spec!r}")
        if not targets:
            raise ToolError(f"stems={spec!r} matches no mixer track in this project")
    elif isinstance(spec, (list, tuple)):
        targets = []
        for t in spec:
            if isinstance(t, bool) or not isinstance(t, int) or not 1 <= t < last_insert:
                raise ToolError(f"stem track {t!r} is not a usable mixer insert "
                                f"(valid: 1..{last_insert - 1})")
            if t not in targets:
                targets.append(t)
        if not targets:
            return []
    else:
        raise ToolError(f"stems must be a list of mixer tracks, 'all' or 'buses'; got {spec!r}")
    if len(targets) > MAX_STEMS:
        raise ToolError(
            f"{len(targets)} stems requested ({targets}); at most {MAX_STEMS} per call "
            "(each takes about 20 s). Split them over several calls with explicit lists, "
            f"e.g. stems={targets[:MAX_STEMS]}."
        )
    return targets


def _energy(rms_dbfs: float | None) -> float:
    return 10 ** (rms_dbfs / 10) if rms_dbfs is not None else 0.0


def live_audio_routes(conn: Any, target: int) -> dict[int, set[int]]:
    """Routes carrying audio from target and every insert it reaches, from the running FL.

    Asks mixer.getRouting for target, then for each insert found downstream
    (breadth first). A route with level 0 is a sidechain-only route and is left
    out; a route without a level (FL API < 36) counts as audio. Raises ToolError
    (or ValueError/TypeError/KeyError on a malformed answer) when FL cannot answer.
    """
    audio: dict[int, set[int]] = {}
    queue = [target]
    while queue:
        node = queue.pop(0)
        if node in audio:
            continue
        answer = run(conn, "mixer.getRouting", {"track": node})
        dests = set()
        for route in answer.get("routes") or []:
            level = route.get("level")
            if level is not None and float(level) <= 0.0:
                continue
            dests.add(int(route["to"]))
        audio[node] = dests
        queue.extend(d for d in sorted(dests) if d not in audio)
    return audio


async def _in_thread(fn, *args, **kwargs):
    return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


async def _progress(ctx: Context | None, done: int, total: int, message: str) -> None:
    if ctx is None:
        return
    try:
        await ctx.report_progress(progress=done, total=total, message=message)
    except Exception:  # noqa: BLE001 - progress is a courtesy, never a failure
        pass


# Master fader position (0-1, 0.8 = 0 dB) at or below which a render is
# (nearly) silent.
MASTER_SILENT = 0.05


def _master_volume(conn: Any) -> float | None:
    """The live Master fader position, or None when FL does not report it."""
    try:
        answer = run(conn, "mixer.getTrackInfo", {"track": 0})
        return float(answer["volume"])
    except (ToolError, KeyError, TypeError, ValueError):
        return None


def _master_automation(conn: Any, project: Project) -> list[str] | None:
    """Names of the automation clips that drive the Master fader (None if unknown)."""
    try:
        answer = run(conn, "automation.resolve",
                     {"targets": [{"type": "mixer_volume", "track": 0}]})
        rec_id = int(answer["targets"][0]["rec_id"])
        return [a.name for a in flp.automation_clips(project) if a.target == rec_id]
    except (ToolError, FLPError, KeyError, IndexError, TypeError, ValueError):
        return None


def master_silent_warning(master: float, clips: list[str] | None) -> str:
    """The silent-Master warning, pointing at what actually fixes it."""
    text = f"Master fader is at {master:.2f}: the render is (nearly) silent. "
    fix_clip = (
        "FL holds an automation clip's last value after the clip ends, so fl_set_track_volume "
        "cannot set Master while the clip exists: fix the clip with fl_edit_arrangement "
        "(set_automation so it ends at the normal level, 0.8 = 0 dB, or delete it); the "
        "edit then passes the playhead over the clip so FL applies it. If the clip is "
        "already fixed, move the playhead into it with fl_set_song_position, then render "
        "again.")
    if clips:
        names = ", ".join(repr(name) for name in clips)
        return text + f"An automation clip drives Master ({names}). " + fix_clip
    if clips == []:
        return text + "No automation clip drives it: set it back with " \
            "fl_set_track_volume(track=0, volume=0.8)."
    return text + ("If an automation clip drives Master: " + fix_clip + " Otherwise set it "
                   "back with fl_set_track_volume(track=0, volume=0.8).")


def _is_open_project(path: Path, save: bool) -> bool:
    """True when `path` is the project open in FL (just saved, or the one located)."""
    if save:
        return True
    located = project_files._last_project
    if located is None:
        return False
    try:
        return path.resolve() == Path(located).resolve()
    except OSError:
        return False


def register_render_tools(mcp: FastMCP) -> None:
    """Register fl_render_mix with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool(annotations=ACTION)
    async def fl_render_mix(
        stems: list[int] | str | None = None,
        sections: bool = True,
        save: bool = True,
        project_path: str | None = None,
        ctx: Context | None = None,
    ) -> dict:
        """Render the song to WAV with FL Studio and measure the mix: listen to your work.

        Use it after changing the mix or the arrangement, to check the result
        instead of guessing. It saves the project (Ctrl+S) and renders the
        SAVED file in the background with FL's command-line renderer: about
        20 s for the whole song, plus about 20 s per stem, one render at a time.
        If your client stops waiting after 60 s, ask for 1 or 2 stems per call.

        Returns overall loudness (integrated LUFS, short-term max), sample and
        true peak, RMS, crest factor, samples over 0 dBFS, the energy in 5
        bands (sub <60 Hz, low, low_mid, high_mid, air >6 kHz), stereo width
        and correlation, leading/trailing silence, per-section figures
        (playlist markers, else 8-bar blocks) and plain-language warnings.
        Look at the warnings first, then: LUFS near -14 for streaming, true
        peak below -1 dBTP, crest above ~6 dB, sections much quieter or
        louder than the rest, a band share that is way off for the genre.

        Stems answer "which track causes it": each requested mixer insert is
        rendered alone (with the buses it feeds and the inserts feeding it;
        sidechain routes are not followed) and compared: level, bands, share
        of the mix energy, masking in the low end, silent tracks. A bus stem
        is not compared with the tracks it contains. The WAVs stay in the
        renders folder; play one with fl_preview_sample. Each stem gives
        track, name, path, lufs, peak_dbfs, share_pct (its energy as a % of
        the mix), silent, inserts (mixer inserts left playing) and, when a
        kept bus also has its own channels, also_contains; stem_warnings
        carries masking, dominant and silent tracks and leaks.

        Args:
            stems: mixer inserts to render alone - a list like [1, 3], "all"
                   (every insert a channel plays into, plus buses) or "buses";
                   at most 8 per call. Default: none (fastest).
            sections: also analyse each section of the song (default True)
            save: save first so the render matches FL (default True)
            project_path: the open project's .flp, when it cannot be found automatically
        """
        if isinstance(stems, (list, tuple)) and not stems:
            stems = None
        conn = get_connection()
        path = await _in_thread(project_files.current_project_file, conn, save, project_path)
        data = path.read_bytes()
        render.take_restore_problems()  # only this call's renders count
        notes = [
            "The render is of the saved .flp file, not of what is playing in FL.",
        ]
        if not save:
            notes.append("save=False: unsaved changes in FL are not in this render.")

        project: Project | None
        try:
            project = Project.parse(data)
        except FLPError as e:
            project = None
            notes.append(f"The project file could not be read ({e}): the mix was rendered "
                         "without sections or stems.")

        # Resolve the stems before spending time on the render.
        targets: list[int] = []
        channels: list[dict] = []
        stems_refused = None
        if stems is not None:
            if project is None:
                stems_refused = "the project file could not be read"
            elif project.to_bytes() != data:
                stems_refused = (f"{path} uses a layout this server cannot rewrite byte "
                                 "for byte")
            elif flp.insert_count(project) < 3:
                stems_refused = "the project file has no mixer inserts to render alone"
            else:
                answer = await _in_thread(run, conn, "channels.getAll")
                channels = answer.get("channels", [])
                automation = {a.channel for a in flp.automation_clips(project)}
                channels = [c for c in channels
                            if c.get("type") != "automation_clip"
                            and c.get("index") not in automation]
                targets = resolve_stems(
                    stems, flp.mixer_routes(project),
                    [int(c.get("target_fx_track") or 0) for c in channels],
                    flp.insert_count(project) - 1,
                )

        tempo = project.tempo if project is not None else None
        if project is not None and not tempo:
            # FL 2025 files may store no tempo event: ask the running FL.
            try:
                answer = await _in_thread(run, conn, "transport.getTempo")
                tempo = float(answer.get("tempo") or 0) or None
            except (ToolError, TypeError, ValueError):
                tempo = None
        length = song_seconds(project, tempo) if project is not None else None
        timeout = render.render_timeout(length)
        total = 1 + len(targets)
        started = time.monotonic()
        wav = await _in_thread(render.render_wav, data, path.stem, timeout)
        elapsed = round(time.monotonic() - started, 1)

        bounds = None
        if sections and project is not None:
            try:
                bounds = section_bounds(project, tempo)
            except FLPError as e:
                notes.append(f"No sections: {e}")
            if bounds == []:
                notes.append("No sections: the arrangement has no clips or the file no tempo.")
                bounds = None
            elif bounds:
                notes.append("Section times come from the project tempo; tempo automation "
                             "shifts them.")
        analysis = await _in_thread(mix_analysis.analyze_mix, wav, bounds)
        # The live fader only says something about the file when it is the open project.
        master = (await _in_thread(_master_volume, conn)
                  if _is_open_project(path, save) else None)
        tail = trailing_silence_warning(
            project, (analysis.get("silence") or {}).get("trailing_seconds"))
        if tail:
            analysis.setdefault("warnings", []).append(tail)
        if master is not None and master <= MASTER_SILENT:
            clips = (await _in_thread(_master_automation, conn, project)
                     if project is not None else None)
            analysis.setdefault("warnings", []).insert(0, master_silent_warning(master, clips))
        await _progress(ctx, 1, total, "Mix rendered and analysed"
                        + (f"; rendering {len(targets)} stem(s)" if targets else ""))

        result: dict[str, Any] = {
            "project": str(path),
            "render": {"path": str(wav), "seconds": elapsed},
            "analysis": analysis,
        }

        if stems is not None:
            stem_warnings: list[str] = []
            out: list[dict] = []
            if stems_refused:
                stem_warnings.append(f"Stems not rendered: {stems_refused}.")
            else:
                try:
                    tracks = await _in_thread(
                        run, conn, "mixer.getAllTracks", {"include_empty": True})
                    names = {int(t["index"]): t.get("name") or f"Insert {t['index']}"
                             for t in tracks.get("tracks", [])}
                except (ToolError, KeyError, TypeError, ValueError):
                    names = {}
                routes = flp.mixer_routes(project)
                last = flp.insert_count(project) - 1
                routing_failed = False

                def render_stem(t: int, name: str) -> dict:
                    nonlocal routing_failed
                    try:
                        audio = live_audio_routes(conn, t)
                    except (ToolError, KeyError, TypeError, ValueError):
                        audio = None
                        routing_failed = True
                    stem_data = flp.stem_project_bytes(data, t, audio)
                    stem_wav = render.render_wav(
                        stem_data, f"{path.stem} - stem {t} {name}", timeout)
                    overall = mix_analysis.analyze_mix(stem_wav)["overall"]
                    peak = overall.get("peak_dbfs")
                    keep = flp.stem_keep_set(routes, t, audio)
                    down = flp.downstream_inserts(routes, t, audio)
                    also = []
                    for d in sorted(down - {0, t, last}):
                        fed = [c for c in channels if int(c.get("target_fx_track") or 0) == d]
                        if fed:
                            also.append({
                                "track": d,
                                "name": names.get(d, f"Insert {d}"),
                                "channels": [str(c.get("name") or f"channel {c.get('index')}")
                                             for c in fed],
                            })
                    entry = {
                        "track": t,
                        "name": name,
                        "path": str(stem_wav),
                        "lufs": overall.get("lufs"),
                        "peak_dbfs": peak,
                        "true_peak_dbfs": overall.get("true_peak_dbfs"),
                        "rms_dbfs": overall.get("rms_dbfs"),
                        "bands": overall.get("bands"),
                        "silent": peak is None or peak < mix_analysis.SILENCE_DBFS,
                        "inserts": sorted(keep - {0, last}),
                    }
                    if also:
                        entry["also_contains"] = also
                    return entry

                for n, t in enumerate(targets, start=1):
                    name = names.get(t, f"Insert {t}")
                    try:
                        entry = await _in_thread(render_stem, t, name)
                    except Exception as e:  # noqa: BLE001 - one stem never sinks the call
                        stem_warnings.append(
                            f"stem {t} ({name}) not rendered: {type(e).__name__}: {e}")
                        await _progress(ctx, 1 + n, total, f"Stem {t} ({name}) failed")
                        continue
                    out.append(entry)
                    for leak in entry.get("also_contains", []):
                        stem_warnings.append(
                            f"stem {t} ({name}) also contains insert {leak['track']} "
                            f"({leak['name']}), which it feeds and which has its own "
                            f"channel(s): {', '.join(leak['channels'])}")
                    await _progress(ctx, 1 + n, total, f"Stem {t} ({name}) rendered")
                if routing_failed:
                    notes.append("FL did not report the mixer routing, so the stems follow "
                                 "the routes saved in the file: a sidechain-only route may "
                                 "pull its destination into a stem.")
                mix_energy = _energy(analysis["overall"].get("rms_dbfs"))
                for s in out:
                    s["share_pct"] = (round(100 * _energy(s["rms_dbfs"]) / mix_energy, 1)
                                      if mix_energy > 0 else None)
                # Master and the last insert ('Current') are never disabled.
                always = [c for c in channels
                          if int(c.get("target_fx_track") or 0) in (0, last)]
                if always:
                    who = ", ".join(str(c.get("name") or f"channel {c.get('index')}")
                                    for c in always)
                    stem_warnings.append(
                        f"{len(always)} channel(s) play straight into Master (or the last "
                        f"insert, {last}) and are in every stem: {who}")
                stem_warnings += mix_analysis.compare_stems(out)
                if len(out) > 1:
                    notes.append("share_pct is a stem's energy as a % of the whole mix; a bus "
                                 "stem includes the tracks that feed it, so shares can add "
                                 "up to more than 100.")
            result["stems"] = out
            result["stem_warnings"] = stem_warnings

        notes.append(f"WAV files are kept in {render.renders_dir()} (the 30 most recent); "
                     "play one with fl_preview_sample.")
        unrestored = render.take_restore_problems()
        if unrestored:
            notes.append(render.restore_problems_note(unrestored))
        result["notes"] = notes
        return result
