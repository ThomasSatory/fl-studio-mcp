"""Samples, keystroke permission errors, piano roll targeting and step validation."""

from __future__ import annotations

import math
import struct
import wave

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools import channels as channel_tools
from fl_studio_mcp.tools import piano_roll, samples
from fl_studio_mcp.tools.playlist import parse_color
from fl_studio_mcp.utils import fl_trigger


def write_sine(path, freq, seconds=1.0, amplitude=0.5, rate=44100):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / rate)))
            for i in range(int(seconds * rate))))


def test_analyze_reports_level_and_pitch(tmp_path):
    path = tmp_path / "sub.wav"
    write_sine(path, 55.0)
    result = samples.analyze(path)
    assert result["pitch"]["note"] == "A1"
    assert abs(result["peak_dbfs"] + 6.0) < 0.2
    assert result["clipped_samples"] == 0
    assert result["spectrum"]["low_pct"] > 80  # one-pole filters: gentle slopes


def write_808(path, rate=44100, seconds=1.2):
    """808-like tone: pitch glides from 2x down to A1 over 80 ms, then holds and decays."""
    frames, phase = [], 0.0
    for i in range(int(seconds * rate)):
        t = i / rate
        freq = 55.0 * (2 ** (1 - t / 0.08)) if t < 0.08 else 55.0
        phase += 2 * math.pi * freq / rate
        amplitude = 0.9 * math.exp(-t / 0.6) * min(1.0, t / 0.002)
        frames.append(struct.pack("<h", int(32767 * amplitude * math.sin(phase))))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(frames))


def test_analyze_808_skips_the_pitch_glide(tmp_path):
    path = tmp_path / "808.wav"
    write_808(path)
    pitch = samples.analyze(path)["pitch"]
    assert pitch["note"] == "A1"
    assert abs(pitch["cents_off"]) <= 10  # the attack alone read +22 cents


def test_estimate_pitch_prefers_the_fundamental_over_a_subharmonic():
    rate = 11025
    # A strong 2nd harmonic: the autocorrelation peak at twice the period is as high.
    signal = [math.sin(2 * math.pi * 55 * i / rate) + 0.8 * math.sin(2 * math.pi * 110 * i / rate)
              for i in range(rate)]
    freq = samples.estimate_pitch(signal, rate)
    assert abs(12 * math.log2(freq / 55)) < 0.2


def test_analyze_flags_clipping(tmp_path):
    path = tmp_path / "hot.wav"
    write_sine(path, 440.0, amplitude=1.0)
    assert "clipped samples" in " ".join(samples.analyze(path)["warnings"])


def test_search_by_kind_and_words(tmp_path):
    kit = tmp_path / "Kit" / "808s"
    kit.mkdir(parents=True)
    for name in ("Dark 808.wav", "Kick Hard.wav", "notes.txt"):
        (kit / name).write_bytes(b"")
    samples._index.clear()
    assert [r["name"] for r in samples.search_samples("", "808", [tmp_path])] == ["Dark 808.wav"]
    assert [r["name"] for r in samples.search_samples("hard", None, [tmp_path])] == [
        "Kick Hard.wav"]


def test_accessibility_denial_is_reported(monkeypatch):
    trigger = fl_trigger.FLStudioTrigger()
    trigger._system = "Darwin"
    monkeypatch.setattr(trigger, "_fl_running_or_false", lambda: True)

    class Denied:
        returncode = 1
        stderr = "execution error: System Events got an error: osascript is not allowed " \
                 "to send keystrokes. (1002)"

    monkeypatch.setattr(fl_trigger.subprocess, "run", lambda *a, **k: Denied())
    assert trigger._trigger_macos() is False
    assert trigger.last_error == fl_trigger.ACCESSIBILITY_HINT


def test_target_piano_roll_selects_pattern_then_channel(monkeypatch):
    calls = []

    class Conn:
        def send_command(self, action, params=None, timeout=2.0):
            calls.append((action, params))
            if action == "pianoroll.open":
                return {"success": True, "channel": {"index": 3, "name": "808"}}
            return {"success": True, "name": "Verse"}

    monkeypatch.setattr("fl_studio_mcp.utils.connection.get_connection", lambda: Conn())
    text = piano_roll._target_piano_roll(3, 2)
    assert calls == [("patterns.select", {"index": 2}), ("pianoroll.open", {"index": 3})]
    assert text == " to channel 3 (808) in pattern 2 (Verse)"
    assert piano_roll._target_piano_roll(None, None) == ""
    with pytest.raises(ToolError):
        piano_roll._target_piano_roll(-1, None)


def test_step_normalization():
    step = channel_tools._normalize_step(0, {"step": 4, "note": "F1", "velocity": 120,
                                             "pan": 0.0, "shift": 0.5}, 96)
    assert step == {"step": 4, "pitch": 29, "velocity": 120, "pan": 64, "shift": 12}
    with pytest.raises(ToolError, match="unknown field"):
        channel_tools._normalize_step(0, {"step": 0, "length": 2}, 96)
    with pytest.raises(ToolError):
        channel_tools._normalize_step(0, {"step": 600}, 96)


def test_parse_color():
    assert parse_color("#FF8000") == [255, 128, 0]
    assert parse_color([1, 2, 3]) == [1, 2, 3]
    with pytest.raises(ToolError):
        parse_color("red")
