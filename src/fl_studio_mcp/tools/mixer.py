"""Mixer control tools for FL Studio."""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.annotations import ACTION, IDEMPOTENT, READ_ONLY
from fl_studio_mcp.utils import project_files
from fl_studio_mcp.utils.errors import run
from fl_studio_mcp.utils.midi_connection import OUTDATED_CONTROLLER_HINT

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_mixer_tools(mcp: FastMCP) -> None:
    """Register mixer control tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_mixer_track_info(track: int) -> dict:
        """Get detailed information about a specific mixer track.

        Args:
            track: Mixer track index (0 = Master, 1-124 = insert tracks)
        """
        result = run(get_connection(), "mixer.getTrackInfo", {"track": track})

        return {
            "index": result.get("index", track),
            "name": result.get("name", ""),
            "volume": result.get("volume", 0),
            "volume_db": result.get("volume_db", 0),
            "pan": result.get("pan", 0),
            "stereo_separation": result.get("stereo_separation", 0),
            "is_muted": result.get("is_muted", False),
            "is_solo": result.get("is_solo", False),
            "is_armed": result.get("is_armed", False),
            "color": result.get("color", "0x0"),
        }

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_all_mixer_tracks(include_empty: bool = False) -> dict:
        """List mixer tracks.

        Returns {"track_count": total tracks in the mixer (Master is 0),
        "tracks": [{index, name, volume, pan, is_muted, is_solo}]}.

        Args:
            include_empty: False (default) lists Master and named tracks only;
                           True lists every track.
        """
        result = run(get_connection(), "mixer.getAllTracks", {"include_empty": include_empty})
        if "track_count" not in result:
            raise ToolError(OUTDATED_CONTROLLER_HINT)

        return {"track_count": result.get("track_count"), "tracks": result.get("tracks", [])}

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_track_volume(
        track: int, volume: float | None = None, volume_db: float | None = None
    ) -> str:
        """Set the volume of a mixer track, as a fader position or in dB.

        Args:
            track: Mixer track index (0 = Master)
            volume: fader position 0.0-1.25; 0.8 is FL's default (0 dB),
                    1.0 is +5.6 dB
            volume_db: target level in dB (e.g. -6); the fader is moved until
                       FL reports it. Give volume or volume_db, not both.

        An automated fader (automation clip or linked controller) does not
        follow; the tool then fails and names the automation.
        """
        if (volume is None) == (volume_db is None):
            raise ToolError("give exactly one of volume (0-1.25) or volume_db")
        params: dict = {"track": track}
        if volume is not None:
            if not 0.0 <= volume <= 1.25:
                raise ToolError("Volume should be between 0.0 and 1.25")
            params["volume"] = volume
        else:
            if not -100.0 <= volume_db <= 5.6:
                raise ToolError("volume_db must be between -100 and +5.6 dB")
            params["db"] = volume_db
        result = run(get_connection(), "mixer.setTrackVolume", params)
        new_vol = result.get("volume", 0)
        new_vol_db = result.get("volume_db", 0)
        if volume is not None and abs(new_vol - volume) > 0.01:
            raise ToolError(
                f"Asked for {volume:.3f} but FL Studio reports {new_vol:.3f} on track {track}"
                " (is the fader automated or linked to a controller?)"
            )
        if volume_db is not None and abs(new_vol_db - volume_db) > 0.2:
            raise ToolError(f"Asked for {volume_db:g} dB but FL Studio reports {new_vol_db:.1f} dB")
        name = result.get("track_name") or f"Track {track}"
        return f"{name} (track {track}) volume set to {new_vol:.3f} ({new_vol_db:.1f} dB)"

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_track_pan(track: int, pan: float) -> str:
        """Set the pan position of a mixer track.

        Args:
            track: Mixer track index (0 = Master)
            pan: Pan position from -1.0 (full left) to 1.0 (full right).
                 0.0 is center.
        """
        if not -1.0 <= pan <= 1.0:
            raise ToolError("Pan must be between -1.0 (left) and 1.0 (right)")

        result = run(get_connection(), "mixer.setTrackPan", {
            "track": track,
            "pan": pan,
        })

        new_pan = result.get("pan", 0)
        if abs(new_pan - pan) > 0.02:
            raise ToolError(f"Asked for pan {pan:g} but FL Studio reports {new_pan:g}")
        if new_pan < -0.01:
            direction = f"{abs(new_pan) * 100:.0f}% left"
        elif new_pan > 0.01:
            direction = f"{new_pan * 100:.0f}% right"
        else:
            direction = "center"

        return f"Track {track} pan set to {direction}"

    @mcp.tool(annotations=ACTION)
    def fl_mute_track(track: int, muted: bool | None = None) -> str:
        """Mute or unmute a mixer track.

        Args:
            track: Mixer track index
            muted: True to mute, False to unmute, None to toggle
        """
        result = run(get_connection(), "mixer.muteTrack", {
            "track": track,
            "muted": muted,
        })

        is_muted = result.get("is_muted", False)
        track_name = result.get("track_name", f"Track {track}")
        return f"{track_name} {'muted' if is_muted else 'unmuted'}"

    @mcp.tool(annotations=ACTION)
    def fl_solo_track(track: int, solo: bool | None = None, mode: int = 3) -> str:
        """Solo or unsolo a mixer track.

        Args:
            track: Mixer track index
            solo: True to solo, False to unsolo, None to toggle
            mode: Solo mode:
                  1 = Solo with source tracks
                  2 = Solo with send tracks
                  3 = Solo with both source and send tracks (default)
                  4 = Solo track only
        """
        result = run(get_connection(), "mixer.soloTrack", {
            "track": track,
            "solo": solo,
            "mode": mode,
        })

        is_solo = result.get("is_solo", False)
        track_name = result.get("track_name", f"Track {track}")
        return f"{track_name} {'soloed' if is_solo else 'unsoloed'}"

    @mcp.tool(annotations=ACTION)
    def fl_arm_track(track: int) -> str:
        """Toggle the recording arm state of a mixer track.

        Armed tracks will record audio when recording is enabled.

        Args:
            track: Mixer track index
        """
        result = run(get_connection(), "mixer.armTrack", {"track": track})

        is_armed = result.get("is_armed", False)
        track_name = result.get("track_name", f"Track {track}")
        return f"{track_name} recording {'armed' if is_armed else 'disarmed'}"

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_track_name(track: int, name: str) -> str:
        """Set the name of a mixer track.

        Args:
            track: Mixer track index
            name: New name for the track. Empty string resets to default.
        """
        run(get_connection(), "mixer.setTrackName", {
            "track": track,
            "name": name,
        })

        return f"Track {track} renamed to '{name}'"

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_track_color(track: int, red: int, green: int, blue: int) -> str:
        """Set the color of a mixer track.

        Args:
            track: Mixer track index
            red: Red component (0-255)
            green: Green component (0-255)
            blue: Blue component (0-255)
        """
        run(get_connection(), "mixer.setTrackColor", {
            "track": track,
            "r": red,
            "g": green,
            "b": blue,
        })

        return f"Track {track} color set to RGB({red}, {green}, {blue})"

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_stereo_separation(track: int, separation: float) -> str:
        """Set the stereo separation of a mixer track.

        Args:
            track: Mixer track index
            separation: Stereo separation from -1.0 (merged/mono) to 1.0 (full separation).
                       0.0 is default.
        """
        if not -1.0 <= separation <= 1.0:
            raise ToolError("Separation must be between -1.0 and 1.0")

        run(get_connection(), "mixer.setStereoSep", {
            "track": track,
            "separation": separation,
        })

        return f"Track {track} stereo separation set to {separation}"

    @mcp.tool(annotations=READ_ONLY)
    def fl_get_mixer_routing(track: int) -> dict:
        """List where a mixer track sends its signal (destination tracks and send levels)."""
        project_files.controller_info(get_connection())
        return run(get_connection(), "mixer.getRouting", {"track": track})

    @mcp.tool(annotations=IDEMPOTENT)
    def fl_set_send(
        source: int, dest: int, level: float | None = None, enabled: bool = True
    ) -> dict:
        """Route a mixer track to another one (a send), e.g. to a reverb bus.

        A track keeps its route to Master; add sends on top of it. To move a
        track under a bus instead, send it to the bus and remove the Master
        route with enabled=False on dest=0. FL refuses a route that would make
        a feedback loop (dest already feeds source); the error says so.

        Args:
            source: sending mixer track
            dest: receiving mixer track (0 = Master)
            level: send level 0-1 (0.8 = 0 dB; needs FL 2025 / API 36+)
            enabled: False removes the route
        """
        if level is not None and not 0.0 <= level <= 1.0:
            raise ToolError("level must be between 0.0 and 1.0")
        conn = get_connection()
        project_files.controller_info(conn)
        result = run(conn, "mixer.setRoute", {
            "source": source, "dest": dest, "level": level, "enabled": enabled,
        })
        if result.get("active") != enabled:
            raise ToolError(f"FL Studio did not {'create' if enabled else 'remove'} the route")
        return result

    @mcp.tool(annotations=READ_ONLY)
    def fl_list_effects(track: int) -> dict:
        """List the effects loaded in a mixer track's slots (slot, plugin, name, param count).

        Use the slot with fl_get_plugin_params(index=track, slot_index=slot) and
        fl_set_plugin_param. Loading a new effect is impossible from a script:
        add it in FL Studio's mixer by hand.
        """
        project_files.controller_info(get_connection())
        return run(get_connection(), "mixer.getEffects", {"track": track})

    @mcp.tool(annotations=ACTION)
    def fl_get_mixer_levels(
        seconds: float = 4.0, tracks: list[int] | None = None, play: bool = True
    ) -> dict:
        """Measure peak levels per mixer track while the song or pattern plays.

        Samples FL's peak meters for `seconds`, then reports each track's
        highest peak in dBFS (0 dBFS = clipping) and flags tracks that clip or
        stay silent. With play=True playback is started if needed and stopped
        afterwards.

        Args:
            seconds: how long to listen (0.5-30)
            tracks: mixer tracks to watch (default: Master and the tracks
                    channels are routed to)
            play: start playback for the measurement (default True)
        """
        if not 0.5 <= seconds <= 30:
            raise ToolError("seconds must be between 0.5 and 30")
        conn = get_connection()
        project_files.controller_info(conn)
        names: dict[int, str] = {}
        listing = run(conn, "mixer.getAllTracks", {"include_empty": True})
        names = {t["index"]: t["name"] for t in listing.get("tracks", [])}
        if tracks is None:
            # Master plus the tracks channels play into: template projects
            # name dozens of tracks nothing is routed to.
            fed = {c.get("target_fx_track") for c in
                   run(conn, "channels.getAll").get("channels", [])}
            tracks = sorted({0} | {t for t in fed if isinstance(t, int) and t >= 0})
        started_here = False
        if play and not run(conn, "transport.getStatus").get("is_playing"):
            run(conn, "transport.start")
            started_here = True
        peaks = {t: 0.0 for t in tracks}
        samples = 0
        deadline = time.time() + seconds
        try:
            while time.time() < deadline:
                result = run(conn, "mixer.getPeaks", {"tracks": tracks})
                for key, value in (result.get("peaks") or {}).items():
                    t = int(key)
                    peaks[t] = max(peaks.get(t, 0.0), float(value or 0.0))
                samples += 1
                time.sleep(0.05)
        finally:
            if started_here:
                run(conn, "transport.stop")
        out = []
        for t in tracks:
            peak = peaks.get(t, 0.0)
            db = round(20 * math.log10(peak), 1) if peak > 0 else None
            entry = {"track": t, "name": names.get(t), "peak": round(peak, 4), "peak_dbfs": db}
            if peak >= 1.0:
                entry["warning"] = "clipping"
            elif peak == 0:
                entry["warning"] = "silent"
            elif db is not None and db < -30:
                entry["warning"] = "very quiet"
            out.append(entry)
        return {"seconds": seconds, "samples": samples, "tracks": out}
