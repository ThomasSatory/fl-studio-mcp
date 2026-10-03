"""Mix analysis on synthetic signals written as WAV files."""

from __future__ import annotations

import json
import math
import struct
import time

import numpy as np
import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.utils import mix_analysis as ma

RATE = 48000


def sine(freq, seconds, amp=1.0, rate=RATE, phase=0.0):
    t = np.arange(int(seconds * rate)) / rate
    return amp * np.sin(2 * np.pi * freq * t + phase)


def stereo(mono):
    return np.stack([mono, mono], axis=1)


def write_wav(path, x, rate=RATE, kind="float", junk=False, extensible=False):
    """kind: pcm16, pcm24, float (32-bit), float64."""
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    nch = x.shape[1]
    if kind == "pcm16":
        tag, bits = 1, 16
        body = np.clip(np.round(x * 32767), -32768, 32767).astype("<i2").tobytes()
    elif kind == "pcm24":
        tag, bits = 1, 24
        ints = np.clip(np.round(x * 8388607), -8388608, 8388607).astype("<i4")
        body = ints.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    elif kind == "float64":
        tag, bits = 3, 64
        body = x.astype("<f8").tobytes()
    else:
        tag, bits = 3, 32
        body = x.astype("<f4").tobytes()
    align = nch * bits // 8
    fmt = struct.pack("<HHIIHH", 0xFFFE if extensible else tag, nch, rate, rate * align,
                      align, bits)
    if extensible:
        guid_tail = b"\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
        fmt += struct.pack("<HHIH", 22, bits, 0, tag) + guid_tail
    chunks = b""
    if junk:
        chunks += b"JUNK" + struct.pack("<I", 27) + b"\x00" * 27 + b"\x00"  # odd size, padded
    chunks += b"fmt " + struct.pack("<I", len(fmt)) + fmt
    chunks += b"data" + struct.pack("<I", len(body)) + body
    path.write_bytes(b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks)
    return path


# ------------------------------------------------------------------ loading


@pytest.mark.parametrize("kind,junk,extensible,tol", [
    ("pcm16", False, False, 1e-4),
    ("pcm24", True, False, 1e-6),
    ("float", True, False, 1e-7),
    ("float", False, True, 1e-7),
    ("pcm16", True, True, 1e-4),
    ("float64", False, False, 1e-7),
])
def test_load_wav_formats(tmp_path, kind, junk, extensible, tol):
    x = np.stack([sine(440, 0.1, 0.5), sine(660, 0.1, -0.25)], axis=1)
    path = write_wav(tmp_path / "a.wav", x, kind=kind, junk=junk, extensible=extensible)
    rate, y = ma.load_wav(path)
    assert rate == RATE
    assert y.dtype == np.float32 and y.shape == x.shape
    assert np.max(np.abs(y - x)) < tol


def test_load_wav_keeps_float_overs_and_truncates_partial_frame(tmp_path):
    path = write_wav(tmp_path / "a.wav", stereo(np.array([1.5, -0.5, 0.25])))
    data = bytearray(path.read_bytes())
    # Append 3 stray bytes to the data chunk: not a whole frame.
    pos = data.index(b"data")
    size = struct.unpack_from("<I", data, pos + 4)[0]
    struct.pack_into("<I", data, pos + 4, size + 3)
    data += b"\x01\x02\x03"
    path.write_bytes(bytes(data))
    _, y = ma.load_wav(path)
    assert y.shape == (3, 2)
    assert y[0, 0] == pytest.approx(1.5)


def test_load_wav_rejects_unsupported(tmp_path):
    path = write_wav(tmp_path / "a.wav", stereo(sine(440, 0.01)))
    data = bytearray(path.read_bytes())
    struct.pack_into("<H", data, data.index(b"fmt ") + 8, 2)  # ADPCM
    path.write_bytes(bytes(data))
    with pytest.raises(ToolError):
        ma.load_wav(path)
    (tmp_path / "b.wav").write_bytes(b"not a wav at all")
    with pytest.raises(ToolError):
        ma.load_wav(tmp_path / "b.wav")


# ----------------------------------------------------------------- loudness


def test_k_weighting_matches_bs1770_reference_at_48k():
    (sb, sa), (hb, ha) = ma._k_weighting_coeffs(48000)
    assert sb == pytest.approx((1.53512485958697, -2.69169618940638, 1.19839281085285))
    assert sa == pytest.approx((1.0, -1.69065929318241, 0.73248077421585))
    assert hb == (1.0, -2.0, 1.0)
    assert ha == pytest.approx((1.0, -1.99004745483398, 0.99007225036621))


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_lufs_of_stereo_sine_follows_ebu_3341(rate):
    # EBU Tech 3341 case 1: 1 kHz at -23 dBFS in both channels reads -23.0 LUFS.
    x = stereo(sine(1000, 3, 10 ** (-23 / 20), rate=rate))
    assert ma.integrated_lufs(x, rate) == pytest.approx(-23.0, abs=0.1)


def test_lufs_single_channel_full_scale_sine_is_minus_3():
    left = sine(1000, 5)
    x = np.stack([left, np.zeros_like(left)], axis=1)
    assert ma.integrated_lufs(x, RATE) == pytest.approx(-3.01, abs=0.1)


def test_lufs_relative_gate_ignores_quiet_parts():
    # EBU Tech 3341 case 2: 20 s at -36, 60 s at -23, 20 s at -36 dBFS reads -23 LUFS
    # (8 kHz keeps it fast; 1 kHz is still well inside the band).
    quiet, loud = 10 ** (-36 / 20), 10 ** (-23 / 20)
    x = stereo(np.concatenate([sine(1000, 20, quiet, rate=8000), sine(1000, 60, loud, rate=8000),
                               sine(1000, 20, quiet, rate=8000)]))
    assert ma.integrated_lufs(x, 8000) == pytest.approx(-23.0, abs=0.1)


def test_lufs_none_for_silence_or_too_short():
    assert ma.integrated_lufs(np.zeros((RATE, 2)), RATE) is None
    assert ma.integrated_lufs(stereo(sine(1000, 0.3)), RATE) is None
    assert ma.short_term_lufs_max(stereo(sine(1000, 2)), RATE) is None


def test_short_term_max_finds_loud_part():
    x = stereo(np.concatenate([sine(1000, 4, 0.01), sine(1000, 4, 10 ** (-10 / 20))]))
    assert ma.short_term_lufs_max(x, RATE) == pytest.approx(-10.0, abs=0.2)


# ------------------------------------------------------------- peaks, bands


def test_true_peak_catches_inter_sample_peak():
    n = np.arange(RATE)
    x = np.sin(np.pi / 2 * n + np.pi / 4)  # fs/4, sample peaks at 0.707
    assert 20 * np.log10(np.max(np.abs(x))) == pytest.approx(-3.0, abs=0.1)
    assert ma.true_peak_dbfs(x) == pytest.approx(0.0, abs=0.5)
    assert ma.true_peak_dbfs(np.zeros(100)) is None


@pytest.mark.parametrize("freq,band", [(50, "sub"), (120, "low"), (1000, "low_mid"),
                                       (4000, "high_mid"), (8000, "air")])
def test_band_balance_dominant_band(freq, band):
    bands = ma.band_balance(sine(freq, 2, 0.5), RATE)
    assert set(bands) == {name for name, _, _ in ma.BANDS}
    assert max(bands, key=bands.get) == band
    assert bands[band] > 90
    assert sum(bands.values()) == pytest.approx(100, abs=0.5)


def test_band_balance_of_out_of_phase_stereo_is_not_cancelled():
    s = sine(50, 1, 0.5)
    bands = ma.band_balance(np.stack([s, -s], axis=1), RATE)
    assert bands["sub"] > 90


def test_band_balance_of_short_segment_is_not_zeroed():
    bands = ma.band_balance(sine(1000, 0.04, 0.5), RATE)  # 1920 samples < nfft
    assert bands["low_mid"] > 90
    assert sum(bands.values()) == pytest.approx(100, abs=0.5)


def test_stereo_image():
    s = sine(500, 1, 0.5)
    assert ma.stereo_image(np.stack([s, -s], axis=1))["correlation"] == pytest.approx(-1)
    mono_like = ma.stereo_image(stereo(s))
    assert mono_like == {"width_pct": 0.0, "correlation": 1.0}
    assert ma.stereo_image(s) is None


def test_analyze_segment_overs_and_silence():
    x = stereo(sine(200, 1, 1.5))
    seg = ma.analyze_segment(x, RATE)
    assert seg["over_samples"] > 0
    assert seg["peak_dbfs"] == pytest.approx(3.5, abs=0.1)
    silent = ma.analyze_segment(np.zeros((RATE, 2)), RATE)
    for key in ("peak_dbfs", "true_peak_dbfs", "rms_dbfs", "lufs", "crest_db", "bands",
                "stereo"):
        assert silent[key] is None
    assert silent["over_samples"] == 0


# ---------------------------------------------------------------------- mix


def test_analyze_mix_overs_warning(tmp_path):
    path = write_wav(tmp_path / "mix.wav", stereo(sine(200, 2, 1.5)), junk=True)
    result = ma.analyze_mix(path)
    assert result["sample_rate"] == RATE and result["channels"] == 2
    assert result["duration_seconds"] == pytest.approx(2.0)
    assert result["overall"]["over_samples"] > 0
    assert any("above 0 dBFS" in w for w in result["warnings"])
    assert "sections" not in result


def test_analyze_mix_silent_file(tmp_path):
    result = ma.analyze_mix(write_wav(tmp_path / "s.wav", np.zeros((RATE, 2))))
    assert result["overall"]["lufs"] is None
    assert result["warnings"] == ["the render is silent"]


def test_analyze_mix_leading_silence(tmp_path):
    x = stereo(np.concatenate([np.zeros(3 * RATE), sine(1000, 4, 0.3)]))
    result = ma.analyze_mix(write_wav(tmp_path / "m.wav", x))
    assert result["silence"]["leading_seconds"] == pytest.approx(3.0, abs=0.01)
    assert result["silence"]["trailing_seconds"] < 0.01
    assert any("silence before the music" in w for w in result["warnings"])


def test_analyze_mix_sections_and_loud_section_warning(tmp_path):
    quiet = 10 ** (-30 / 20)
    x = stereo(np.concatenate([sine(1000, 3, quiet), sine(1000, 3, quiet * 4),
                               sine(1000, 3, quiet)]))  # middle +12 dB
    sections = [
        {"name": "intro", "start_seconds": 0, "end_seconds": 3, "start_bar": 1, "end_bar": 3},
        {"name": "drop", "start_seconds": 3, "end_seconds": 6},
        {"name": "outro", "start_seconds": 6, "end_seconds": 60},  # clamped
        {"name": "empty", "start_seconds": 20, "end_seconds": 30},  # beyond the end
    ]
    result = ma.analyze_mix(write_wav(tmp_path / "m.wav", x), sections)
    names = [s["name"] for s in result["sections"]]
    assert names == ["intro", "drop", "outro"]
    intro, drop, outro = result["sections"]
    assert intro["start_bar"] == 1 and intro["end_bar"] == 3
    assert "start_bar" not in drop
    assert outro["end_seconds"] == pytest.approx(9.0)
    assert drop["lufs"] - intro["lufs"] == pytest.approx(12.0, abs=0.3)
    assert set(drop) >= {"lufs", "peak_dbfs", "rms_dbfs", "bands"}
    assert any("section drop is" in w and "louder" in w for w in result["warnings"])
    assert not any("section intro" in w for w in result["warnings"])


def test_analyze_mix_tonal_warnings(tmp_path):
    left = sine(50, 3, 0.5)
    x = np.stack([left, -left], axis=1)  # bass-only, out of phase
    warnings = ma.analyze_mix(write_wav(tmp_path / "m.wav", x))["warnings"]
    text = " | ".join(warnings)
    assert "bass-heavy" in text
    assert "dull" in text
    assert "correlation" in text


def test_analyze_mix_two_sections_jump_warns(tmp_path):
    quiet = 10 ** (-30 / 20)
    x = stereo(np.concatenate([sine(1000, 3, quiet), sine(1000, 3, quiet * 4)]))
    sections = [{"name": "verse", "start_seconds": 0, "end_seconds": 3},
                {"name": "chorus", "start_seconds": 3, "end_seconds": 6}]
    warnings = ma.analyze_mix(write_wav(tmp_path / "m.wav", x), sections)["warnings"]
    assert any("section chorus is 12.0 LU louder" in w for w in warnings)
    assert any("section verse is 12.0 LU quieter" in w for w in warnings)


@pytest.mark.parametrize("bad", [
    {"name": "a", "start_seconds": float("nan"), "end_seconds": 1},
    {"name": "a", "start_seconds": 0, "end_seconds": float("inf")},
    {"name": "a", "start_seconds": "soon", "end_seconds": 1},
    "intro",
])
def test_analyze_mix_rejects_bad_sections(tmp_path, bad):
    path = write_wav(tmp_path / "m.wav", stereo(sine(1000, 1, 0.3)))
    with pytest.raises(ToolError):
        ma.analyze_mix(path, [bad])


def _assert_plain(value, where="result"):
    if isinstance(value, dict):
        for k, v in value.items():
            assert type(k) is str, f"{where}: key {k!r}"
            _assert_plain(v, f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _assert_plain(v, f"{where}[{i}]")
    else:
        assert value is None or type(value) in (str, int, float, bool), \
            f"{where}: {type(value).__name__}"
        if type(value) is float:
            assert math.isfinite(value), where


@pytest.mark.parametrize("signal", ["music", "silent"])
def test_outputs_are_plain_json(tmp_path, signal):
    if signal == "silent":
        x = np.zeros((2 * RATE, 2))
    else:
        x = stereo(np.concatenate([np.zeros(RATE), sine(100, 2, 0.5) + sine(5000, 2, 0.2),
                                   np.zeros(RATE // 2)]))
        x[RATE + 10, 0] = 1.5
    sections = [{"name": "a", "start_seconds": 0, "end_seconds": 2, "start_bar": 1},
                {"name": "b", "start_seconds": 2, "end_seconds": 3.5, "end_bar": 9}]
    result = ma.analyze_mix(write_wav(tmp_path / "m.wav", x), sections)
    _assert_plain(result)
    json.dumps(result, allow_nan=False)
    segment = ma.analyze_segment(x, RATE)
    _assert_plain(segment)
    json.dumps(segment, allow_nan=False)
    stems = [{"track": 1, "name": "kick", "lufs": -10.0, "bands": {"sub": 60}, "silent": False},
             {"track": 2, "name": "808", "lufs": -11.0, "bands": {"sub": 70}, "silent": False}]
    _assert_plain(ma.compare_stems(stems))


# ------------------------------------------------------------------- stems


def test_compare_stems_conflict_silent_and_dominant():
    stems = [
        {"track": 1, "name": "kick", "lufs": -12.0, "bands": {"sub": 55, "low": 30},
         "silent": False},
        {"track": 2, "name": "808", "lufs": -10.0, "bands": {"sub": 70, "low": 20},
         "silent": False},
        {"track": 3, "name": "hats", "lufs": -20.0, "bands": {"sub": 0, "low": 1},
         "silent": False},
        {"track": 4, "name": "pad", "lufs": None, "bands": None, "silent": True},
    ]
    warnings = ma.compare_stems(stems)
    assert ("kick and 808 both carry most of their energy in sub: they mask each other; "
            "sidechain or EQ one of them") in warnings
    assert "track 4 (pad) is silent in the render" in warnings
    assert not any("dominates" in w for w in warnings)  # 808 leads by only 2 LU


def test_compare_stems_no_conflict_when_far_apart_and_dominant_track():
    stems = [
        {"track": 1, "name": "kick", "lufs": -8.0, "bands": {"sub": 60}, "silent": False,
         "share_pct": 75},
        {"track": 2, "name": "808", "lufs": -20.0, "bands": {"sub": 60}, "silent": False,
         "share_pct": 10},
    ]
    warnings = ma.compare_stems(stems)
    assert not any("mask" in w for w in warnings)
    assert any(w.startswith("track 1 (kick) dominates") for w in warnings)
    stems[0]["share_pct"] = 50
    assert not any("dominates" in w for w in ma.compare_stems(stems))


def test_compare_stems_skips_a_bus_and_its_sources():
    stems = [
        {"track": 1, "name": "Kick", "lufs": -12.0, "bands": {"sub": 60}, "silent": False,
         "inserts": [1, 3]},
        {"track": 2, "name": "Bass", "lufs": -20.0, "bands": {"sub": 60}, "silent": False,
         "inserts": [2, 3]},
        {"track": 3, "name": "Bus 1", "lufs": -8.0, "bands": {"sub": 60}, "silent": False,
         "share_pct": 90, "inserts": [1, 2, 3]},
        {"track": 4, "name": "Sub", "lufs": -13.0, "bands": {"sub": 70}, "silent": False,
         "share_pct": 20, "inserts": [4]},
    ]
    warnings = ma.compare_stems(stems)
    for source in ("Kick", "Bass"):
        assert not any(f"{source} and Bus 1" in w or f"Bus 1 and {source}" in w
                       for w in warnings)
    assert any(w.startswith("Kick and Sub both carry") for w in warnings)
    assert any(w.startswith("Bus 1 and Sub both carry") for w in warnings)
    assert not any("Kick and Bass" in w for w in warnings)  # 8 LU apart
    # the bus is compared with Sub only: 5 LU louder
    assert any(w.startswith("track 3 (Bus 1) dominates: 5.0 LU") for w in warnings)
    # without "inserts", the old behaviour: the bus is compared with everyone
    plain = [{k: v for k, v in s.items() if k != "inserts"} for s in stems]
    warnings = ma.compare_stems(plain)
    assert any("Kick and Bus 1" in w for w in warnings)
    assert any(w.startswith("track 3 (Bus 1) dominates: 4.0 LU") for w in warnings)


def test_compare_stems_dominance_skips_stems_related_to_all():
    stems = [
        {"track": 3, "name": "Bus", "lufs": -6.0, "bands": {}, "silent": False,
         "inserts": [1, 2, 3]},
        {"track": 1, "name": "Kick", "lufs": -8.0, "bands": {}, "silent": False,
         "inserts": [1, 3]},
        {"track": 2, "name": "Hat", "lufs": -20.0, "bands": {}, "silent": False,
         "inserts": [2, 3]},
    ]
    warnings = ma.compare_stems(stems)
    assert [w for w in warnings if "dominates" in w] == [
        "track 1 (Kick) dominates: 12.0 LU louder than any other track; "
        "the rest may be buried"]


# -------------------------------------------------------------------- perf


def test_long_render_is_analysed_quickly():
    # 60 s here keeps the suite fast; 4 min measured at ~1.3 s (see report).
    rng = np.random.default_rng(0)
    x = (rng.standard_normal((60 * RATE, 2)) * 0.1).astype(np.float32)
    start = time.perf_counter()
    ma.analyze_segment(x, RATE)
    assert time.perf_counter() - start < 2.5
