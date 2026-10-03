"""Find samples on disk and measure audio files, without FL Studio's browser.

The search covers FL Studio's own browser folders (the "Search paths" of its
settings), its factory packs and FL_STUDIO_MCP_SAMPLE_DIRS. Analysis reads
WAV directly (other formats through macOS's afconvert) and reports levels,
clipping, stereo width, spectral balance and, for tonal one-shots such as
808s, the fundamental as a note name.
"""

from __future__ import annotations

import math
import os
import platform
import re
import struct
import subprocess
import tempfile
import time
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.annotations import ACTION, READ_ONLY
from fl_studio_mcp.utils.midi_connection import get_fl_settings_base
from fl_studio_mcp.utils.music import midi_to_note_name

if TYPE_CHECKING:
    from fastmcp import FastMCP

AUDIO_EXTENSIONS = (".wav", ".aif", ".aiff", ".mp3", ".flac", ".ogg", ".wv")

# Words that identify a kind of sound in file or folder names.
KINDS = {
    "kick": ("kick", "kck", "bd", "bassdrum"),
    "snare": ("snare", "snr", "sd"),
    "clap": ("clap", "clp"),
    "hihat": ("hihat", "hi hat", "hi-hat", "hh", "hat", "closed"),
    "openhat": ("open hat", "openhat", "open-hat", "oh", "open"),
    "808": ("808",),
    "bass": ("bass", "sub"),
    "perc": ("perc", "percussion", "rim", "shaker", "conga", "bongo", "tamb", "cowbell"),
    "cymbal": ("crash", "ride", "cymbal", "china", "splash"),
    "tom": ("tom",),
    "fx": ("fx", "sfx", "riser", "impact", "sweep", "noise", "whoosh", "transition"),
    "vocal": ("vocal", "vox", "voice", "chant", "adlib", "acapella"),
    "loop": ("loop", "bpm"),
    "melody": ("melody", "melodic", "keys", "piano", "guitar", "pluck", "synth", "lead"),
    "bell": ("bell", "chime", "glock"),
    "pad": ("pad", "atmos", "ambient", "texture"),
    "snap": ("snap", "finger"),
}

INDEX_TTL = 600.0
MAX_FILES = 200_000
_index: dict[str, tuple[float, list[str]]] = {}


def fl_browser_folders() -> list[Path]:
    """Folders FL Studio's browser lists (its settings' Search paths) plus factory packs."""
    folders: list[Path] = []
    reg = Path.home() / "Library" / "Preferences" / "Image-Line" / "reg.xml"
    if reg.is_file():
        text = reg.read_text(encoding="utf-8", errors="replace")
        for block in re.findall(r'<Key Name="Search paths">(.*?)</Key>', text, re.S):
            for value in re.findall(r"<Value[^>]*>([^<]*)</Value>", block):
                path = value.strip().strip(",").strip('"')
                if path:
                    folders.append(Path(path))
    for app in Path("/Applications").glob("FL Studio*.app"):
        folders.append(app / "Contents" / "Resources" / "FL" / "Data" / "Patches" / "Packs")
    folders.append(get_fl_settings_base().parent / "Audio")
    extra = os.environ.get("FL_STUDIO_MCP_SAMPLE_DIRS")
    if extra:
        folders = [Path(p).expanduser() for p in extra.split(os.pathsep) if p] + folders
    unique, seen = [], set()
    for folder in folders:
        key = str(folder).rstrip("/")
        if key not in seen and folder.is_dir():
            seen.add(key)
            unique.append(folder)
    return unique


def _files_under(root: Path) -> list[str]:
    key = str(root)
    cached = _index.get(key)
    if cached and time.time() - cached[0] < INDEX_TTL:
        return cached[1]
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.lower().endswith(AUDIO_EXTENSIONS) and not name.startswith("._"):
                files.append(os.path.join(dirpath, name))
        if len(files) >= MAX_FILES:
            break
    _index[key] = (time.time(), files)
    return files


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9#]+", text.lower()) if w]


def _matches_kind(path: str, kind: str) -> bool:
    lowered = path.lower()
    tokens = set(_words(lowered))
    for alias in KINDS[kind]:
        if " " in alias or "-" in alias:
            if alias in lowered:
                return True
        elif alias in tokens or (len(alias) > 3 and alias in lowered):
            return True
    return False


def search_samples(query: str = "", kind: str | None = None, folders: list[Path] | None = None,
                   limit: int = 40) -> list[dict]:
    """Samples whose path contains every query word, filtered by kind, best first."""
    words = _words(query)
    results = []
    for root in folders or fl_browser_folders():
        for path in _files_under(root):
            lowered = path.lower()
            if words and not all(w in lowered for w in words):
                continue
            if kind and not _matches_kind(path, kind):
                continue
            name = os.path.basename(path)
            score = sum(3 for w in words if w in name.lower())
            if kind and _matches_kind(name, kind):
                score += 2
            results.append((-score, len(path), path, root))
    results.sort()
    out = []
    for _, _, path, root in results[:limit]:
        rel = os.path.relpath(path, root)
        out.append({"name": os.path.basename(path), "path": path,
                    "pack": rel.split(os.sep)[0] if os.sep in rel else root.name})
    return out


# ------------------------------------------------------------------ analysis


def _to_wav(path: Path) -> tuple[Path, bool]:
    if path.suffix.lower() == ".wav":
        return path, False
    if platform.system() != "Darwin":
        raise ToolError(f"Only .wav can be analysed on {platform.system()}, got {path.suffix}")
    tmp = Path(tempfile.mkstemp(suffix=".wav")[1])
    result = subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16", str(path), str(tmp)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ToolError(f"Could not decode {path}: {result.stderr.strip()}")
    return tmp, True


def read_wav(path: Path, start: float = 0.0, duration: float | None = None,
             max_seconds: float = 60.0) -> tuple[int, list[list[float]]]:
    """(sample rate, channels as float lists -1..1) for a PCM or float WAV."""
    with open(path, "rb") as f:
        head = f.read(64)
    if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        raise ToolError(f"{path} is not a WAV file")
    fmt = _wav_format(path)
    try:
        with wave.open(str(path), "rb") as w:
            rate, nch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            w.setpos(min(int(start * rate), w.getnframes()))
            frames = w.getnframes() - w.tell()
            if duration is not None:
                frames = min(frames, int(duration * rate))
            frames = min(frames, int(max_seconds * rate))
            raw = w.readframes(frames)
    except wave.Error:
        rate, nch, width, raw = _read_float_wav(path, start, duration, max_seconds)
        fmt = 3
    count = len(raw) // (width * nch)
    if fmt == 3 and width == 4:
        values = struct.unpack(f"<{count * nch}f", raw[: count * nch * 4])
    elif width == 1:
        values = [(b - 128) / 128 for b in raw]
    elif width == 2:
        values = [v / 32768 for v in struct.unpack(f"<{count * nch}h", raw[: count * nch * 2])]
    elif width == 3:
        values = [int.from_bytes(raw[i:i + 3], "little", signed=True) / 8388608
                  for i in range(0, count * nch * 3, 3)]
    elif width == 4:
        values = [v / 2147483648 for v in struct.unpack(f"<{count * nch}i", raw[: count * nch * 4])]
    else:
        raise ToolError(f"Unsupported sample width {width * 8} bits")
    return rate, [list(values[c::nch]) for c in range(nch)]


def _wav_format(path: Path) -> int:
    """The WAVE format tag (1 = PCM, 3 = float, 0xFFFE = extensible)."""
    with open(path, "rb") as f:
        data = f.read(4096)
    pos = 12
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"fmt ":
            tag = struct.unpack_from("<H", data, pos + 8)[0]
            if tag == 0xFFFE and size >= 40:
                tag = struct.unpack_from("<H", data, pos + 32)[0]
            return tag
        pos += 8 + size + (size & 1)
    return 1


def _read_float_wav(path: Path, start, duration, max_seconds):
    """Minimal RIFF reader for IEEE float WAVs (the wave module refuses them)."""
    data = path.read_bytes()
    pos, rate, nch, width, body = 12, 0, 0, 0, b""
    while pos + 8 <= len(data):
        cid, size = data[pos:pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"fmt ":
            nch, rate = struct.unpack_from("<HI", data, pos + 10)
            width = struct.unpack_from("<H", data, pos + 22)[0] // 8
        elif cid == b"data":
            body = data[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    if not rate or width != 4:
        raise ToolError(f"Unsupported WAV encoding in {path}")
    frame = nch * width
    first = int(start * rate) * frame
    length = len(body) - first
    if duration is not None:
        length = min(length, int(duration * rate) * frame)
    length = min(length, int(max_seconds * rate) * frame)
    return rate, nch, width, body[first:first + max(0, length)]


def _db(value: float) -> float | None:
    return round(20 * math.log10(value), 1) if value > 0 else None


def estimate_pitch(signal: list[float], rate: int) -> float | None:
    """Fundamental frequency by autocorrelation on a stable window (None if unclear)."""
    if len(signal) < rate // 10:
        return None
    step = max(1, rate // 11025)
    start = min(len(signal) // 4, int(0.05 * rate))  # skip the attack
    window = signal[start:start + int(0.25 * rate)][::step]
    sr = rate / step
    n = len(window)
    if n < 256:
        return None
    mean = sum(window) / n
    window = [v - mean for v in window]
    energy = sum(v * v for v in window)
    if energy <= 1e-9:
        return None
    min_lag, max_lag = max(2, int(sr / 1000)), min(int(sr / 25), n // 2)
    corr = [sum(window[i] * window[i + lag] for i in range(0, n - lag, 2)) / (energy / 2)
            for lag in range(max_lag)]
    # Skip the main lobe around lag 0, then take the strongest peak: taking
    # the global maximum instead would lock onto a tiny lag for bass sounds.
    lag = min_lag
    while lag < max_lag and corr[lag] > 0:
        lag += 1
    if lag >= max_lag:
        return None
    best_lag = max(range(lag, max_lag), key=lambda k: corr[k])
    if corr[best_lag] < 0.3:
        return None
    # Parabolic interpolation for sub-sample precision.
    if 0 < best_lag < max_lag - 1:
        a, b, c = corr[best_lag - 1], corr[best_lag], corr[best_lag + 1]
        denom = a - 2 * b + c
        if denom:
            return sr / (best_lag + 0.5 * (a - c) / denom)
    return sr / best_lag


def _band_levels(signal: list[float], rate: int) -> dict:
    """Energy share below 150 Hz, 150-2500 Hz and above, via one-pole filters."""
    def lowpass(values, cutoff):
        a = 1 - math.exp(-2 * math.pi * cutoff / rate)
        out, y = [], 0.0
        for v in values:
            y += a * (v - y)
            out.append(y)
        return out

    low = lowpass(signal, 150)
    mid_low = lowpass(signal, 2500)
    e_low = sum(v * v for v in low)
    e_mid = sum((b - a) ** 2 for a, b in zip(low, mid_low))
    e_high = sum((s - b) ** 2 for s, b in zip(signal, mid_low))
    total = e_low + e_mid + e_high or 1.0
    return {"low_pct": round(100 * e_low / total, 1), "mid_pct": round(100 * e_mid / total, 1),
            "high_pct": round(100 * e_high / total, 1)}


def analyze(path: Path, start: float = 0.0, duration: float | None = None) -> dict:
    wav, temporary = _to_wav(path)
    try:
        rate, chans = read_wav(wav, start, duration)
    finally:
        if temporary:
            wav.unlink(missing_ok=True)
    frames = len(chans[0]) if chans else 0
    if not frames:
        raise ToolError(f"{path} holds no audio in the requested range")
    mono = [sum(c[i] for c in chans) / len(chans) for i in range(frames)]
    peak = max(max(abs(v) for v in c) for c in chans)
    rms = math.sqrt(sum(v * v for v in mono) / frames)
    clipped = sum(1 for c in chans for v in c if abs(v) >= 0.999)
    out = {
        "path": str(path),
        "sample_rate": rate,
        "channels": len(chans),
        "duration_seconds": round(frames / rate, 3),
        "peak_dbfs": _db(peak),
        "rms_dbfs": _db(rms),
        "crest_db": round(_db(peak) - _db(rms), 1) if peak > 0 and rms > 0 else None,
        "clipped_samples": clipped,
        "dc_offset": round(sum(mono) / frames, 5),
    }
    if len(chans) == 2:
        left, right = chans
        side = sum((a - b) ** 2 for a, b in zip(left, right))
        mid = sum((a + b) ** 2 for a, b in zip(left, right)) or 1.0
        out["stereo_width_pct"] = round(100 * side / (side + mid), 1)
    # Loudness over time: where the sound starts and how it decays.
    block = max(1, rate // 20)
    env = [max((abs(v) for v in mono[i:i + block]), default=0.0) for i in range(0, frames, block)]
    loud = [i for i, v in enumerate(env) if v > peak * 0.1] if peak else []
    if loud:
        out["sound_ends_seconds"] = round((loud[-1] + 1) * block / rate, 2)
    out["spectrum"] = _band_levels(mono[: rate * 20], rate)
    freq = estimate_pitch(mono, rate)
    if freq:
        midi = 69 + 12 * math.log2(freq / 440)
        nearest = int(round(midi))
        out["pitch"] = {
            "hz": round(freq, 2),
            "note": midi_to_note_name(max(0, min(127, nearest))),
            "cents_off": round((midi - nearest) * 100),
            "midi": nearest,
        }
    warnings = []
    if clipped:
        warnings.append(f"{clipped} clipped samples")
    if peak and _db(peak) > -0.3:
        warnings.append("peaks at 0 dBFS: no headroom")
    if abs(out["dc_offset"]) > 0.01:
        warnings.append("DC offset")
    if warnings:
        out["warnings"] = warnings
    return out


def register_sample_tools(mcp: FastMCP) -> None:
    """Register sample search / analysis tools with the MCP server."""

    @mcp.tool(annotations=READ_ONLY)
    def fl_find_samples(
        query: str = "",
        kind: str | None = None,
        folder: str | None = None,
        limit: int = 40,
    ) -> dict:
        """Search audio samples on disk (FL's browser folders by default).

        Args:
            query: words that must all appear in the path, e.g. "808 dark" or "vinyl snare"
            kind: kick, snare, clap, hihat, openhat, 808, bass, perc, cymbal, tom, fx,
                  vocal, loop, melody, bell, pad, snap
            folder: search only this folder instead of FL's browser folders
            limit: max results (1-200)

        Load results with fl_add_sample_channels; check a file with fl_analyze_audio.
        """
        if kind is not None and kind not in KINDS:
            raise ToolError(f"unknown kind {kind!r}; valid: {', '.join(KINDS)}")
        if not 1 <= limit <= 200:
            raise ToolError("limit must be 1-200")
        folders = None
        if folder:
            root = Path(folder).expanduser()
            if not root.is_dir():
                raise ToolError(f"{root} is not a folder")
            folders = [root]
        searched = folders or fl_browser_folders()
        results = search_samples(query, kind, searched, limit)
        return {"count": len(results), "results": results,
                "searched": [str(f) for f in searched]}

    @mcp.tool(annotations=READ_ONLY)
    def fl_analyze_audio(
        path: str, start_seconds: float = 0.0, duration_seconds: float | None = None
    ) -> dict:
        """Measure an audio file: peak/RMS dBFS, clipping, stereo width, low/mid/high
        balance, where the sound ends and (for tonal sounds like 808s) its pitch.

        Use it on a sample before loading it (to tune an 808 to the song's key)
        or on a mixdown exported from FL (File > Export) to check the mix.
        At most 60 s are analysed from start_seconds.
        """
        target = Path(path).expanduser()
        if not target.is_file():
            raise ToolError(f"{target} does not exist")
        if start_seconds < 0 or (duration_seconds is not None and duration_seconds <= 0):
            raise ToolError("start_seconds must be >= 0 and duration_seconds > 0")
        return analyze(target, start_seconds, duration_seconds)

    @mcp.tool(annotations=ACTION)
    def fl_preview_sample(path: str) -> str:
        """Play an audio file on this computer's speakers (for the user to hear), macOS only."""
        target = Path(path).expanduser()
        if not target.is_file():
            raise ToolError(f"{target} does not exist")
        if platform.system() != "Darwin":
            raise ToolError("Preview needs macOS (afplay)")
        subprocess.Popen(["afplay", "-t", "8", str(target)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Playing {target.name} (up to 8 s)."
