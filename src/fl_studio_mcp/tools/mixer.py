"""Mixer control tools for FL Studio."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from fl_studio_mcp.utils.errors import run

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_mixer_tools(mcp: FastMCP) -> None:
    """Register mixer control tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool()
    def fl_get_mixer_track_count() -> int:
        """Get the total number of mixer tracks available.

        FL Studio typically has 125 mixer tracks (0-124), where track 0 is the Master.
        """
        result = run(get_connection(), "mixer.getTrackCount")

        return result.get("count", 0)

    @mcp.tool()
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

    @mcp.tool()
    def fl_get_all_mixer_tracks(include_empty: bool = False) -> list[dict]:
        """Get information about all mixer tracks.

        Args:
            include_empty: If False, only returns tracks with non-default names.
                          If True, returns all 125 tracks.
        """
        result = run(get_connection(), "mixer.getAllTracks", {"include_empty": include_empty})

        return result.get("tracks", [])

    @mcp.tool()
    def fl_set_track_volume(track: int, volume: float) -> str:
        """Set the volume of a mixer track.

        Args:
            track: Mixer track index (0 = Master)
            volume: Volume level from 0.0 (silence) to 1.0 (0dB).
                   Default FL Studio volume is 0.8 (~-5.2dB).
                   Values above 1.0 may cause clipping.
        """
        if not 0.0 <= volume <= 1.25:
            raise ToolError("Volume should be between 0.0 and 1.25")

        result = run(get_connection(), "mixer.setTrackVolume", {
            "track": track,
            "volume": volume,
        })

        new_vol = result.get("volume", 0)
        new_vol_db = result.get("volume_db", 0)
        return f"Track {track} volume set to {new_vol:.3f} ({new_vol_db:.1f} dB)"

    @mcp.tool()
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
        if new_pan < -0.01:
            direction = f"{abs(new_pan) * 100:.0f}% left"
        elif new_pan > 0.01:
            direction = f"{new_pan * 100:.0f}% right"
        else:
            direction = "center"

        return f"Track {track} pan set to {direction}"

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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

    @mcp.tool()
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
