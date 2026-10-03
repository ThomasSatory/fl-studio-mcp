"""Read and patch FL Studio project files (.flp).

FL Studio's scripting API cannot read or place playlist clips, so the
arrangement is edited in the saved project file instead: parse the event
stream, rewrite the playlist items of one arrangement, write it back and let
FL Studio reopen it.

File format: an "FLhd" chunk (format, channel count, PPQ) then an "FLdt" chunk
holding a flat list of events. The event id selects the payload size: < 64 a
byte, < 128 a word, < 192 a dword, else a varint length followed by data.

Only what the MCP tools need is interpreted; every other event is kept byte
for byte, so a parse followed by to_bytes() reproduces the file exactly.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Event ids (names follow PyFLP's).
EV_TIMESIG_NUM = 17
EV_TIMESIG_BEAT = 18
EV_CHANNEL_TYPE = 21
EV_MARKER_NUMERATOR = 33
EV_MARKER_DENOMINATOR = 34
EV_CHANNEL_NEW = 64
EV_PATTERN_NEW = 65
EV_ARRANGEMENT_NEW = 99
EV_ARRANGEMENT_CURRENT = 100
EV_MARKER_POSITION = 148
EV_TEMPO = 156
EV_PATTERN_NAME = 193
EV_CHANNEL_SAMPLE_PATH = 196
EV_FL_VERSION = 199
EV_CHANNEL_NAME = 203
EV_MARKER_NAME = 205
EV_PLAYLIST_ITEMS = 233
EV_INSERT_ROUTING = 235
EV_INSERT_FLAGS = 236
EV_TRACK_DATA = 238
EV_TRACK_NAME = 239
EV_ARRANGEMENT_NAME = 241

PATTERN_BASE = 20480
MAX_TRACKS = 500

# Size of one playlist item by FL version: 32 bytes up to FL 20, 60 bytes in
# FL 21, 80 bytes in FL 2024/2025.
ITEM_SIZES = (80, 60, 32)

# A pattern clip exactly as FL Studio 2025 writes it (position, pattern,
# length, track and id are overwritten); used when the playlist is empty.
_TEMPLATE_80 = bytes.fromhex(
    "000000000050015000030000f30100007800400040648080ffffffffffffffff"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "000000000000f03f0000000000000000"
)
_TEMPLATE_32 = _TEMPLATE_80[:32]


class FLPError(ValueError):
    """The file is not a project this module can read or patch safely."""


@dataclass
class Event:
    id: int
    value: int | bytes


@dataclass
class Clip:
    """One playlist item. Times are in ticks, track is 0-based."""

    position: int
    length: int
    track: int
    pattern: int | None = None  # 1-based pattern number for a pattern clip
    channel: int | None = None  # channel index for an audio/automation clip
    raw: bytes = b""
    start_offset: float | int | None = None
    end_offset: float | int | None = None
    item_id: int | None = None

    @property
    def end(self) -> int:
        return self.position + self.length


@dataclass
class Marker:
    position: int
    name: str
    kind: int = 0  # high byte of the position event (0 = plain marker)


@dataclass
class Arrangement:
    index: int
    name: str
    start: int  # index of the ArrangementNew event
    items_event: int | None  # index of the PlaylistItems event, if any
    item_size: int | None
    clips: list[Clip] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    track_names: dict[int, str] = field(default_factory=dict)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= len(data):
            raise FLPError("truncated event length")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return value, pos


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def decode_text(value: bytes) -> str:
    """FL 11.5+ stores text as NUL-terminated UTF-16LE (the version string is ASCII)."""
    if len(value) >= 2 and len(value) % 2 == 0 and value[1:2] == b"\x00":
        text = value.decode("utf-16-le", errors="replace")
    else:
        text = value.decode("utf-8", errors="replace")
    return text.rstrip("\x00")


class Project:
    """A parsed .flp: header fields plus the raw event list."""

    def __init__(self, header: bytes, events: list[Event]) -> None:
        self.header = header
        self.events = events

    # ------------------------------------------------------------------ io

    @classmethod
    def parse(cls, data: bytes) -> Project:
        if data[:4] != b"FLhd":
            raise FLPError("not an FL Studio project (missing FLhd header)")
        header_len = struct.unpack_from("<I", data, 4)[0]
        header = data[8 : 8 + header_len]
        pos = 8 + header_len
        if data[pos : pos + 4] != b"FLdt":
            raise FLPError("not an FL Studio project (missing FLdt chunk)")
        end = pos + 8 + struct.unpack_from("<I", data, pos + 4)[0]
        pos += 8
        if end > len(data):
            raise FLPError("truncated project file")
        events: list[Event] = []
        while pos < end:
            eid = data[pos]
            pos += 1
            if eid < 64:
                value: int | bytes = data[pos]
                pos += 1
            elif eid < 128:
                value = struct.unpack_from("<H", data, pos)[0]
                pos += 2
            elif eid < 192:
                value = struct.unpack_from("<I", data, pos)[0]
                pos += 4
            else:
                length, pos = _read_varint(data, pos)
                value = data[pos : pos + length]
                if len(value) != length:
                    raise FLPError("truncated event data")
                pos += length
            events.append(Event(eid, value))
        return cls(header, events)

    @classmethod
    def load(cls, path) -> Project:
        with open(path, "rb") as f:
            return cls.parse(f.read())

    def to_bytes(self) -> bytes:
        body = bytearray()
        for ev in self.events:
            body.append(ev.id)
            if ev.id < 64:
                body.append(int(ev.value) & 0xFF)
            elif ev.id < 128:
                body += struct.pack("<H", int(ev.value) & 0xFFFF)
            elif ev.id < 192:
                body += struct.pack("<I", int(ev.value) & 0xFFFFFFFF)
            else:
                data = bytes(ev.value)
                body += _varint(len(data)) + data
        return (
            b"FLhd" + struct.pack("<I", len(self.header)) + self.header
            + b"FLdt" + struct.pack("<I", len(body)) + bytes(body)
        )

    # -------------------------------------------------------------- header

    @property
    def ppq(self) -> int:
        return struct.unpack_from("<H", self.header, 4)[0]

    @property
    def channel_count(self) -> int:
        return struct.unpack_from("<H", self.header, 2)[0]

    def _first(self, eid: int):
        for ev in self.events:
            if ev.id == eid:
                return ev.value
        return None

    @property
    def version(self) -> str:
        value = self._first(EV_FL_VERSION)
        return decode_text(value) if isinstance(value, bytes) else ""

    @property
    def version_major(self) -> int:
        try:
            return int(self.version.split(".")[0])
        except ValueError:
            return 0

    @property
    def tempo(self) -> float | None:
        value = self._first(EV_TEMPO)
        return value / 1000 if isinstance(value, int) else None

    @property
    def time_signature(self) -> tuple[int, int]:
        num = self._first(EV_TIMESIG_NUM) or 4
        beat = self._first(EV_TIMESIG_BEAT) or 4
        return int(num), int(beat)

    @property
    def bar_ticks(self) -> int:
        num, beat = self.time_signature
        return int(self.ppq * 4 * num / beat)

    # ------------------------------------------------------------ content

    def pattern_names(self) -> dict[int, str]:
        """Pattern number -> name, for patterns that have a name."""
        names: dict[int, str] = {}
        current = None
        for ev in self.events:
            if ev.id == EV_PATTERN_NEW:
                current = int(ev.value)
            elif ev.id == EV_PATTERN_NAME and current is not None:
                names[current] = decode_text(bytes(ev.value))
        return names

    def channel_names(self) -> dict[int, str]:
        names: dict[int, str] = {}
        current = None
        for ev in self.events:
            if ev.id == EV_CHANNEL_NEW:
                current = int(ev.value)
            elif ev.id == EV_CHANNEL_NAME and current is not None and current not in names:
                names[current] = decode_text(bytes(ev.value))
            elif ev.id == EV_ARRANGEMENT_NEW:
                break
        return names

    def current_arrangement(self) -> int:
        value = self._first(EV_ARRANGEMENT_CURRENT)
        return int(value) if isinstance(value, int) else 0

    def arrangements(self) -> list[Arrangement]:
        starts = [i for i, ev in enumerate(self.events) if ev.id == EV_ARRANGEMENT_NEW]
        if not starts:
            # Pre-FL 20 project: a single playlist without arrangement events.
            starts = []
        result = []
        for n, start in enumerate(starts):
            stop = starts[n + 1] if n + 1 < len(starts) else len(self.events)
            result.append(self._read_arrangement(start, stop))
        return result

    def arrangement(self, index: int | None = None) -> Arrangement:
        arrangements = self.arrangements()
        if not arrangements:
            raise FLPError("this project has no arrangement (FL Studio 20 or later required)")
        if index is None:
            index = self.current_arrangement()
        for arr in arrangements:
            if arr.index == index:
                return arr
        raise FLPError(
            f"arrangement {index} not found; available: {[a.index for a in arrangements]}"
        )

    def _read_arrangement(self, start: int, stop: int) -> Arrangement:
        arr = Arrangement(index=int(self.events[start].value), name="", start=start,
                          items_event=None, item_size=None)
        marker: Marker | None = None
        track = 0
        for i in range(start + 1, stop):
            ev = self.events[i]
            if ev.id == EV_ARRANGEMENT_NAME:
                arr.name = decode_text(bytes(ev.value))
            elif ev.id == EV_PLAYLIST_ITEMS and arr.items_event is None:
                arr.items_event = i
                arr.item_size = self._item_size(bytes(ev.value))
                arr.clips = decode_items(bytes(ev.value), arr.item_size)
            elif ev.id == EV_MARKER_POSITION:
                value = int(ev.value)
                marker = Marker(position=value & 0x00FFFFFF, name="", kind=value >> 24)
                arr.markers.append(marker)
            elif ev.id == EV_MARKER_NAME and marker is not None:
                marker.name = decode_text(bytes(ev.value))
            elif ev.id == EV_TRACK_DATA:
                data = bytes(ev.value)
                track = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else track + 1
                marker = None
            elif ev.id == EV_TRACK_NAME:
                arr.track_names[track] = decode_text(bytes(ev.value))
        return arr

    def _item_size(self, data: bytes) -> int:
        if not data:
            major = self.version_major
            return 80 if major >= 24 else 60 if major >= 21 else 32
        for size in ITEM_SIZES:
            if len(data) % size == 0 and all(
                struct.unpack_from("<H", data, i + 4)[0] == PATTERN_BASE
                and struct.unpack_from("<H", data, i + 12)[0] < MAX_TRACKS
                for i in range(0, len(data), size)
            ):
                return size
        raise FLPError(
            f"unrecognised playlist item layout ({len(data)} bytes, FL {self.version})"
        )

    # ------------------------------------------------------------ writing

    def set_clips(self, arr: Arrangement, clips: list[Clip]) -> None:
        """Replace the playlist items of `arr` with `clips` (sorted by position)."""
        size = arr.item_size or self._item_size(b"")
        template = _template(arr.clips, size)
        next_id = self._max_item_id() + 1
        chunks = []
        for clip in sorted(clips, key=lambda c: (c.position, c.track)):
            raw = bytearray(clip.raw if clip.raw and len(clip.raw) == size else template)
            if clip.item_id is None and size >= 36:
                # A new clip (placed or copied): FL expects unique item ids.
                struct.pack_into("<I", raw, 32, next_id)
                next_id += 1
            if clip.pattern is not None:
                index = PATTERN_BASE + clip.pattern
            elif clip.channel is not None:
                index = clip.channel
            else:
                raise FLPError("a clip needs a pattern or a channel")
            if not 0 <= clip.track < MAX_TRACKS:
                raise FLPError(f"track {clip.track + 1} out of range (1..{MAX_TRACKS})")
            struct.pack_into("<I", raw, 0, clip.position)
            struct.pack_into("<HH", raw, 4, PATTERN_BASE, index)
            struct.pack_into("<I", raw, 8, clip.length)
            struct.pack_into("<H", raw, 12, MAX_TRACKS - 1 - clip.track)
            if clip.end_offset is not None:
                if clip.pattern is not None:
                    struct.pack_into("<ii", raw, 24, int(clip.start_offset or 0),
                                     int(clip.end_offset))
                else:
                    struct.pack_into("<ff", raw, 24, float(clip.start_offset or 0),
                                     float(clip.end_offset))
            chunks.append(bytes(raw))
        data = b"".join(chunks)
        if arr.items_event is not None:
            self.events[arr.items_event] = Event(EV_PLAYLIST_ITEMS, data)
        else:
            # Right after the arrangement's name (or its start event).
            at = arr.start + 1
            if at < len(self.events) and self.events[at].id == EV_ARRANGEMENT_NAME:
                at += 1
            self.events.insert(at, Event(EV_PLAYLIST_ITEMS, data))

    def _max_item_id(self) -> int:
        best = 0
        for arr in self.arrangements():
            for clip in arr.clips:
                if clip.item_id is not None:
                    best = max(best, clip.item_id)
        return best


def _template(clips: list[Clip], size: int) -> bytes:
    """Bytes of an existing pattern clip, else FL's own layout for this size."""
    for clip in reversed(clips):
        if clip.pattern is not None and len(clip.raw) == size:
            raw = bytearray(clip.raw)
            # Clip-local edits (trim offsets) must not leak into new clips.
            struct.pack_into("<ii", raw, 24, -1, -1)
            return bytes(raw)
    if size == 80:
        return _TEMPLATE_80
    if size == 32:
        return _TEMPLATE_32
    raise FLPError(
        f"no clip to copy the {size}-byte item layout from: place one pattern in the "
        "playlist by hand, save, and retry"
    )


def decode_items(data: bytes, size: int) -> list[Clip]:
    clips = []
    for off in range(0, len(data) - size + 1, size):
        raw = data[off : off + size]
        position, _base, index, length, rv = struct.unpack_from("<IHHIH", raw, 0)
        clip = Clip(position=position, length=length, track=MAX_TRACKS - 1 - rv, raw=raw)
        if index >= PATTERN_BASE:
            clip.pattern = index - PATTERN_BASE
            clip.start_offset, clip.end_offset = struct.unpack_from("<ii", raw, 24)
        else:
            clip.channel = index
            clip.start_offset, clip.end_offset = struct.unpack_from("<ff", raw, 24)
        if size >= 36:
            clip.item_id = struct.unpack_from("<I", raw, 32)[0]
        clips.append(clip)
    return clips


# ---------------------------------------------------------------- automation

EV_CHANNEL_ENABLED = 0
EV_CHANNEL_COLOR = 128
EV_AUTOMATION_DATA = 234
EV_REMOTE_CONTROLLER = 227
EV_CHANNEL_LEVELS = 219
AUTOMATION_RANGE = 12800  # FL's full scale for an automation clip's min/max
CHANNEL_TYPE_AUTOMATION = 5

AUTOMATION_HEADER_SIZE = 21  # 17 bytes of flags, then the point count (u32)
AUTOMATION_POINT_SIZE = 24  # delta position (f64 beats), value (f64 0-1), tension (f32), 4 flags


@dataclass
class AutomationPoint:
    """A point of an automation clip; `beat` is absolute (quarter notes from the clip start)."""

    beat: float
    value: float
    tension: float = 0.0


@dataclass
class AutomationClip:
    channel: int
    name: str
    target: int | None  # REC event id of the automated parameter
    points: list[AutomationPoint]
    data_event: int  # index of the automation data event

    @property
    def length_beats(self) -> float:
        return self.points[-1].beat if self.points else 0.0


def decode_automation(data: bytes) -> list[AutomationPoint]:
    if len(data) < AUTOMATION_HEADER_SIZE:
        raise FLPError("automation data too short")
    count = struct.unpack_from("<I", data, 17)[0]
    if AUTOMATION_HEADER_SIZE + count * AUTOMATION_POINT_SIZE > len(data):
        raise FLPError("automation point count does not fit the data")
    points, beat = [], 0.0
    for k in range(count):
        off = AUTOMATION_HEADER_SIZE + k * AUTOMATION_POINT_SIZE
        delta, value = struct.unpack_from("<dd", data, off)
        tension = struct.unpack_from("<f", data, off + 16)[0]
        beat += delta
        points.append(AutomationPoint(beat=beat, value=value, tension=tension))
    return points


def encode_automation(template: bytes, points: list[AutomationPoint]) -> bytes:
    """New automation data: the template's header and trailer around `points`."""
    count = struct.unpack_from("<I", template, 17)[0]
    tail = template[AUTOMATION_HEADER_SIZE + count * AUTOMATION_POINT_SIZE:]
    out = bytearray(template[:17]) + struct.pack("<I", len(points))
    previous = 0.0
    for k, point in enumerate(points):
        # Last flag byte as FL writes it: 0 on the first point, else the sign of
        # the tension (1 / 0xFF) or 2 for a straight segment.
        if k == 0:
            flag = 0
        elif point.tension > 0:
            flag = 1
        elif point.tension < 0:
            flag = 0xFF
        else:
            flag = 2
        out += struct.pack("<ddf", point.beat - previous, point.value, point.tension)
        out += bytes((0, 0, 0, flag))
        previous = point.beat
    return bytes(out + tail)


def remote_controller_event(channel: int, target: int, template: bytes | None = None) -> bytes:
    """The event that links automation clip `channel` to REC event `target`."""
    raw = bytearray(template or bytes.fromhex("0000000000000000000000000800000" "0d5010000"))
    struct.pack_into("<H", raw, 2, channel)
    struct.pack_into("<I", raw, 8, target)
    return bytes(raw)


def _channel_blocks(project: Project, strict: bool = True) -> list[tuple[int, int, int]]:
    """(channel index, first event, end event) for every channel block.

    The last block ends at the arrangement section; without one (FL 11 and
    older) a strict caller gets FLPError, others the end of the file.
    """
    starts = [i for i, ev in enumerate(project.events) if ev.id == EV_CHANNEL_NEW]
    blocks = []
    for n, start in enumerate(starts):
        if n + 1 < len(starts):
            end = starts[n + 1]
        else:
            end = next((i for i in range(start, len(project.events))
                        if project.events[i].id == EV_ARRANGEMENT_NEW), None)
            if end is None:
                if strict:
                    raise FLPError("cannot find where the last channel ends")
                end = len(project.events)
        blocks.append((int(project.events[start].value), start, end))
    return blocks


def automation_clips(project: Project) -> list[AutomationClip]:
    targets = {}
    for ev in project.events:
        if ev.id == EV_REMOTE_CONTROLLER and len(bytes(ev.value)) >= 12:
            data = bytes(ev.value)
            targets[struct.unpack_from("<H", data, 2)[0]] = struct.unpack_from("<I", data, 8)[0]
    clips = []
    for channel, start, end in _channel_blocks(project, strict=False):
        block = project.events[start:end]
        if not any(ev.id == EV_CHANNEL_TYPE and ev.value == CHANNEL_TYPE_AUTOMATION
                   for ev in block):
            continue
        name = next((decode_text(bytes(ev.value)) for ev in block if ev.id == EV_CHANNEL_NAME), "")
        data_at = next((start + k for k, ev in enumerate(block) if ev.id == EV_AUTOMATION_DATA),
                       None)
        if data_at is None:
            continue
        points = decode_automation(bytes(project.events[data_at].value))
        clips.append(AutomationClip(channel=channel, name=name, target=targets.get(channel),
                                    points=points, data_event=data_at))
    return clips


def set_automation_points(project: Project, clip: AutomationClip,
                          points: list[AutomationPoint]) -> None:
    template = bytes(project.events[clip.data_event].value)
    project.events[clip.data_event] = Event(EV_AUTOMATION_DATA, encode_automation(template, points))


def encode_text(text: str) -> bytes:
    return (text + "\x00").encode("utf-16-le")


def add_automation_clip(project: Project, name: str, target: int,
                        points: list[AutomationPoint],
                        pitch_range: float | None = None) -> int:
    """Append an automation clip channel linked to REC event `target`; returns its index.

    pitch_range: for a channel pitch clip, the knob's range in semitones, so
    that values 0-1 span -range..+range like FL's own clips (otherwise 0-1
    spans the full +-48 semitones).

    The channel is cloned from an automation clip already in the project, or
    from the FL Studio 2025 template.
    """
    from fl_studio_mcp.utils.flp_templates import AUTOMATION_CHANNEL_25

    blocks = _channel_blocks(project)
    if not blocks:
        raise FLPError("the project has no channel")
    existing = automation_clips(project)
    if existing:
        source = next(b for b in blocks if b[0] == existing[0].channel)
        template = [Event(ev.id, ev.value) for ev in project.events[source[1]:source[2]]]
    elif project.version_major == 25:
        template = [Event(eid, value) for eid, value in AUTOMATION_CHANNEL_25]
    else:
        raise FLPError(
            f"no automation clip to copy in this FL {project.version} project: create one "
            "by hand once (right-click a knob > Create automation clip), save, and retry"
        )
    new_index = max(b[0] for b in blocks) + 1
    for k, ev in enumerate(template):
        if ev.id == EV_CHANNEL_NEW:
            template[k] = Event(EV_CHANNEL_NEW, new_index)
        elif ev.id == EV_CHANNEL_NAME:
            template[k] = Event(EV_CHANNEL_NAME, encode_text(name))
        elif ev.id == EV_AUTOMATION_DATA:
            data = bytearray(encode_automation(bytes(ev.value), points))
            data[8] = 1 if pitch_range else 0
            template[k] = Event(EV_AUTOMATION_DATA, bytes(data))
        elif ev.id == EV_CHANNEL_LEVELS and len(ev.value) >= 8:
            # An automation clip keeps its output range (min, max out of 0-12800) here.
            half = round(AUTOMATION_RANGE / 2 * min(pitch_range, 48) / 48) if pitch_range else None
            low, high = (AUTOMATION_RANGE // 2 - half, AUTOMATION_RANGE // 2 + half) if half \
                else (0, AUTOMATION_RANGE)
            data = bytearray(ev.value)
            struct.pack_into("<ii", data, 0, low, high)
            template[k] = Event(EV_CHANNEL_LEVELS, bytes(data))
    if not any(ev.id == EV_CHANNEL_NAME for ev in template):
        raise FLPError("automation template has no name event")
    insert_at = blocks[-1][2]
    project.events[insert_at:insert_at] = template

    # Link it: after the last remote-controller event, else before the first channel.
    links = [i for i, ev in enumerate(project.events) if ev.id == EV_REMOTE_CONTROLLER]
    link_template = bytes(project.events[links[-1]].value) if links else None
    at = links[-1] + 1 if links else _channel_blocks(project)[0][1]
    project.events.insert(at, Event(EV_REMOTE_CONTROLLER,
                                    remote_controller_event(new_index, target, link_template)))
    header = bytearray(project.header)
    struct.pack_into("<H", header, 2, project.channel_count + 1)
    project.header = bytes(header)
    return new_index


def automation_item_template(project: Project) -> bytes | None:
    """Raw playlist item of an existing automation clip, else FL 2025's."""
    from fl_studio_mcp.utils.flp_templates import AUTOMATION_ITEM_25

    autos = {c.channel for c in automation_clips(project)}
    for arr in project.arrangements():
        for clip in arr.clips:
            if clip.channel in autos:
                return clip.raw
    return AUTOMATION_ITEM_25 if project.version_major == 25 else None


# ------------------------------------------------------------ sample channels

CHANNEL_TYPE_SAMPLER = 0


def add_sample_channel(project: Project, sample_path: str, name: str) -> int:
    """Append a Sampler channel playing `sample_path`; returns its index.

    Cloned from a sampler channel of the project that already has a sample
    (its settings, colour and mixer routing come along).
    """
    blocks = _channel_blocks(project)
    source = None
    for channel, start, end in blocks:
        block = project.events[start:end]
        is_sampler = any(ev.id == EV_CHANNEL_TYPE and ev.value == CHANNEL_TYPE_SAMPLER
                         for ev in block)
        if is_sampler and any(ev.id == EV_CHANNEL_SAMPLE_PATH for ev in block):
            source = (start, end)
            break
    if source is None:
        raise FLPError(
            "the project has no sampler channel with a sample to copy: drag one sample "
            "into the channel rack by hand once, save, and retry"
        )
    new_index = max(b[0] for b in blocks) + 1
    template = [Event(ev.id, ev.value) for ev in project.events[source[0]:source[1]]]
    for k, ev in enumerate(template):
        if ev.id == EV_CHANNEL_NEW:
            template[k] = Event(EV_CHANNEL_NEW, new_index)
        elif ev.id == EV_CHANNEL_NAME:
            template[k] = Event(EV_CHANNEL_NAME, encode_text(name))
        elif ev.id == EV_CHANNEL_SAMPLE_PATH:
            template[k] = Event(EV_CHANNEL_SAMPLE_PATH, encode_text(sample_path))
    if not any(ev.id == EV_CHANNEL_NAME for ev in template):
        at = next(k for k, ev in enumerate(template) if ev.id == EV_CHANNEL_TYPE) + 1
        template.insert(at, Event(EV_CHANNEL_NAME, encode_text(name)))
    insert_at = blocks[-1][2]
    project.events[insert_at:insert_at] = template
    header = bytearray(project.header)
    struct.pack_into("<H", header, 2, project.channel_count + 1)
    project.header = bytes(header)
    return new_index


def sample_paths(project: Project) -> dict[int, str]:
    paths = {}
    for channel, start, end in _channel_blocks(project, strict=False):
        for ev in project.events[start:end]:
            if ev.id == EV_CHANNEL_SAMPLE_PATH:
                paths[channel] = decode_text(bytes(ev.value))
    return paths


INSERT_ENABLED = 0x08


def insert_count(project: Project) -> int:
    """Number of mixer inserts in the file (Master included), i.e. the 236 events."""
    return sum(1 for e in project.events if e.id == EV_INSERT_FLAGS)


def mixer_routes(project: Project) -> dict[int, set[int]]:
    """insert index -> destination inserts, from the 235 events (in file order)."""
    routes: dict[int, set[int]] = {}
    for index, event in enumerate(e for e in project.events if e.id == EV_INSERT_ROUTING):
        routes[index] = {dest for dest, flag in enumerate(event.value) if flag}
    return routes


def disable_inserts(project: Project, indices) -> None:
    """Clear the Enabled bit of these inserts. FLPError for 0 (Master), for the last
    insert (FL 2025's 'Current' track: disabling it makes the render write nothing),
    for an index out of range, or when the file has no 236 events."""
    flags = [e for e in project.events if e.id == EV_INSERT_FLAGS]
    if not flags:
        raise FLPError("the project has no mixer insert events")
    wanted = set(indices)
    for index in sorted(wanted):
        if index == 0:
            raise FLPError("cannot disable the Master insert")
        if index == len(flags) - 1:
            raise FLPError(f"cannot disable insert {index}: it is the 'Current' track")
        if not 0 <= index < len(flags):
            raise FLPError(f"insert {index} out of range (0..{len(flags) - 1})")
    for index in wanted:
        event = flags[index]
        raw = bytearray(event.value)
        value = struct.unpack_from("<I", raw, 4)[0] & ~INSERT_ENABLED
        struct.pack_into("<I", raw, 4, value)
        event.value = bytes(raw)


def stem_keep_set(routes: dict[int, set[int]], target: int) -> set[int]:
    """{0, target} | inserts whose signal reaches target | inserts reached from target."""
    keep = {0, target}
    # upstream: walk the routes backwards from the target
    stack = [target]
    while stack:
        node = stack.pop()
        for source, dests in routes.items():
            if node in dests and source not in keep:
                keep.add(source)
                stack.append(source)
    # downstream: follow the routes forwards from the target
    stack = [target]
    seen = {target}
    while stack:
        node = stack.pop()
        for dest in routes.get(node, ()):
            if dest not in seen:
                seen.add(dest)
                keep.add(dest)
                stack.append(dest)
    return keep


def stem_project_bytes(data: bytes, target: int) -> bytes:
    """A copy of the project where every insert outside stem_keep_set(target) is disabled.
    Never touches 0 or the last insert. FLPError if target is 0, the last insert or out of range."""
    project = Project.parse(data)
    count = insert_count(project)
    if count == 0:
        raise FLPError("the project has no mixer insert events")
    if not 0 < target < count - 1:
        raise FLPError(f"insert {target} cannot be soloed (valid: 1..{count - 2})")
    keep = stem_keep_set(mixer_routes(project), target)
    disable_inserts(project, [i for i in range(1, count - 1) if i not in keep])
    return project.to_bytes()
