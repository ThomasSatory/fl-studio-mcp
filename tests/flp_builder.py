"""Build small synthetic .flp files shaped like FL Studio 2025's, for tests."""

from __future__ import annotations

import struct

from fl_studio_mcp.utils.flp import _TEMPLATE_80, Event, Project, encode_text


def pattern_item(position: int, pattern: int, length: int, track: int, item_id: int) -> bytes:
    raw = bytearray(_TEMPLATE_80)
    struct.pack_into("<I", raw, 0, position)
    struct.pack_into("<HH", raw, 4, 20480, 20480 + pattern)
    struct.pack_into("<I", raw, 8, length)
    struct.pack_into("<H", raw, 12, 499 - track)
    struct.pack_into("<I", raw, 32, item_id)
    return bytes(raw)


def build_project(clips: list[tuple[int, int, int, int]] | None = None) -> Project:
    """Two patterns, one sampler channel, one arrangement with `clips`.

    clips: (position ticks, pattern, length ticks, track 0-based)
    """
    clips = clips if clips is not None else [(0, 1, 768, 0), (768, 2, 768, 0)]
    header = struct.pack("<hHH", 0, 1, 96)
    items = b"".join(pattern_item(p, pat, ln, t, 100 + n) for n, (p, pat, ln, t) in
                     enumerate(clips))
    events = [
        Event(199, b"25.2.2.4864\x00"),
        Event(17, 4),
        Event(18, 4),
        Event(65, 1), Event(193, encode_text("Intro")),
        Event(65, 2), Event(193, encode_text("Hook")),
        Event(64, 0),
        Event(21, 0),
        Event(203, encode_text("Kick")),
        Event(196, encode_text("/samples/kick.wav")),
        Event(51, 0),
        Event(99, 0),
        Event(241, encode_text("Arrangement")),
        Event(36, 0),
        Event(233, items),
        Event(148, 0), Event(205, encode_text("Intro")),
        Event(148, 3072), Event(205, encode_text("Verse")),
        Event(238, struct.pack("<I", 1) + bytes(66)),
        Event(239, encode_text("Drums")),
        Event(47, 0),
    ]
    return Project(header, events)
