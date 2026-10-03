"""Listen to a render: loudness, peaks, spectral balance, stereo and sections.

Everything here works on WAV files written by FL Studio (or anything else)
with numpy only. Loudness follows ITU-R BS.1770-4 (K-weighting, 400 ms
gated blocks); the true peak uses 4x oversampling as in its Annex 2. The
results are meant to be read by the model, so warnings are short sentences
in producer terms with the numbers in them.
"""

from __future__ import annotations

import math
import struct
from functools import lru_cache
from pathlib import Path

import numpy as np
from fastmcp.exceptions import ToolError

BANDS = (("sub", 0, 60), ("low", 60, 250), ("low_mid", 250, 2000),
         ("high_mid", 2000, 6000), ("air", 6000, None))

SILENCE_DBFS = -60.0
_SILENCE_AMP = 10 ** (SILENCE_DBFS / 20)

# K-weighting stages (BS.1770-4), expressed as analogue prototypes so the
# biquads can be derived for any sample rate.
_SHELF_F0 = 1681.974450955533
_SHELF_GAIN_DB = 3.999843853973347
_SHELF_Q = 0.7071752369554196
_HP_F0 = 38.13547087602444
_HP_Q = 0.5003270373238773


# --------------------------------------------------------------------- WAV


def load_wav(path: Path) -> tuple[int, np.ndarray]:
    """Read a WAV file as (sample_rate, float32 array of shape (frames, channels)).

    PCM 8/16/24/32-bit, IEEE float 32/64-bit and WAVE_FORMAT_EXTENSIBLE are
    supported; any chunk (JUNK, LIST, bext...) before or after fmt is skipped.
    Float files keep values above 1.0 as they are.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ToolError(f"Cannot read {path}: {exc}") from exc
    if len(data) < 12 or data[:4] not in (b"RIFF", b"RF64") or data[8:12] != b"WAVE":
        raise ToolError(f"{path} is not a WAV file")
    pos, fmt, body = 12, None, None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        if cid == b"fmt ":
            fmt = data[pos + 8:pos + 8 + size]
        elif cid == b"data":
            body = data[pos + 8:pos + 8 + size]  # a truncated file just gives less
            if fmt is not None:
                break
        pos += 8 + size + (size & 1)
    if fmt is None or len(fmt) < 16 or body is None:
        raise ToolError(f"{path} has no fmt or data chunk")
    tag, nch, rate = struct.unpack_from("<HHI", fmt, 0)
    block_align, bits = struct.unpack_from("<HH", fmt, 12)
    if tag == 0xFFFE:
        if len(fmt) < 26:
            raise ToolError(f"{path}: truncated WAVE_FORMAT_EXTENSIBLE header")
        tag = struct.unpack_from("<H", fmt, 24)[0]
    if nch < 1 or rate < 1:
        raise ToolError(f"{path}: invalid channel count or sample rate")
    width = bits // 8
    if block_align != width * nch:
        block_align = width * nch
    usable = len(body) - len(body) % block_align if block_align else 0
    raw = np.frombuffer(body, dtype=np.uint8, count=usable)
    if tag == 1 and bits == 16:
        x = raw.view("<i2").astype(np.float32) / 32768.0
    elif tag == 1 and bits == 24:
        b = raw.reshape(-1, 3).astype(np.int32)
        ints = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        ints = np.where(ints >= 1 << 23, ints - (1 << 24), ints)
        x = ints.astype(np.float32) / 8388608.0
    elif tag == 1 and bits == 32:
        x = (raw.view("<i4").astype(np.float64) / 2147483648.0).astype(np.float32)
    elif tag == 1 and bits == 8:
        x = (raw.astype(np.float32) - 128.0) / 128.0
    elif tag == 3 and bits == 32:
        x = raw.view("<f4").astype(np.float32)
    elif tag == 3 and bits == 64:
        x = raw.view("<f8").astype(np.float32)
    else:
        kind = {1: "PCM", 3: "float"}.get(tag, f"format tag {tag:#x}")
        raise ToolError(f"{path}: unsupported WAV encoding ({kind}, {bits} bits)")
    x = np.nan_to_num(x, nan=0.0, posinf=0.0, neginf=0.0)
    return rate, x.reshape(-1, nch)


# ----------------------------------------------------------------- helpers


def _db(value: float) -> float | None:
    return round(20 * math.log10(value), 1) if value > 0 else None


def _as_2d(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x)
    return x[:, None] if x.ndim == 1 else x


def _cascade_impulse(stages, limit: int) -> np.ndarray:
    """Impulse response of cascaded biquads (a[0] == 1), cut once it has died out."""
    state = [[0.0, 0.0, 0.0, 0.0] for _ in stages]  # x1, x2, y1, y2 per stage
    out, peak = [], 0.0
    for i in range(limit):
        v = 1.0 if i == 0 else 0.0
        for (b, a), st in zip(stages, state):
            x1, x2, y1, y2 = st
            y = b[0] * v + b[1] * x1 + b[2] * x2 - a[1] * y1 - a[2] * y2
            st[:] = [v, x1, y, y1]
            v = y
        out.append(v)
        peak = max(peak, abs(v))
        if i > 64 and all(abs(st[2]) + abs(st[3]) < 1e-12 * peak for st in state):
            break
    return np.array(out)


def _k_weighting_coeffs(rate: int):
    """(b, a) of the BS.1770 shelving pre-filter and RLB high-pass at `rate`."""
    k = math.tan(math.pi * _SHELF_F0 / rate)
    vh = 10 ** (_SHELF_GAIN_DB / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / _SHELF_Q + k * k
    shelf_b = ((vh + vb * k / _SHELF_Q + k * k) / a0, 2 * (k * k - vh) / a0,
               (vh - vb * k / _SHELF_Q + k * k) / a0)
    shelf_a = (1.0, 2 * (k * k - 1) / a0, (1 - k / _SHELF_Q + k * k) / a0)
    k = math.tan(math.pi * _HP_F0 / rate)
    a0 = 1 + k / _HP_Q + k * k
    hp_b = (1.0, -2.0, 1.0)
    hp_a = (1.0, 2 * (k * k - 1) / a0, (1 - k / _HP_Q + k * k) / a0)
    return (shelf_b, shelf_a), (hp_b, hp_a)


@lru_cache(maxsize=8)
def _k_weighting_fir(rate: int) -> np.ndarray:
    """The cascaded K-weighting filter as its impulse response.

    Both biquads are stable with poles well inside the unit circle, so the
    response is truncated once it has decayed below ~1e-12 of its peak: the
    FIR equals the IIR cascade to that precision and can then be applied
    with FFT convolution, which numpy does fast without scipy.
    """
    h = _cascade_impulse(_k_weighting_coeffs(rate), limit=max(4096, rate))
    big = np.nonzero(np.abs(h) > 1e-12 * np.abs(h).max())[0]
    return h[: big[-1] + 1]


def _fft_filter(x: np.ndarray, h: np.ndarray, chunk: int = 1 << 16) -> np.ndarray:
    """Causal FIR filtering of every column of x (overlap-add, output length = input)."""
    n, taps = len(x), len(h)
    nfft = 1 << max(chunk, 4 * taps - 1).bit_length()
    step = nfft - taps + 1
    spectrum = np.fft.rfft(h, nfft)[:, None]
    out = np.zeros((n + nfft, x.shape[1]))
    for start in range(0, n, step):
        part = np.fft.rfft(x[start:start + step], nfft, axis=0)
        out[start:start + nfft] += np.fft.irfft(part * spectrum, nfft, axis=0)
    return out[:n]


def _hop_energy(x: np.ndarray, rate: int) -> tuple[np.ndarray, int]:
    """Sum of squared K-weighted samples per 100 ms hop: shape (hops, channels)."""
    x = _as_2d(x).astype(np.float64)
    hop = max(1, round(rate / 10))
    hops = len(x) // hop
    if hops == 0:
        return np.zeros((0, x.shape[1])), hop
    y = _fft_filter(x[: hops * hop], _k_weighting_fir(rate))
    return (y * y).reshape(hops, hop, -1).sum(axis=1), hop


def _block_loudness(energy: np.ndarray, hop: int, size: int, step: int):
    """Loudness (LUFS) of windows of `size` hops every `step` hops, plus their mean squares."""
    if len(energy) < size:
        return None, None
    csum = np.vstack([np.zeros((1, energy.shape[1])), np.cumsum(energy, axis=0)])
    starts = np.arange(0, len(energy) - size + 1, step)
    ms = (csum[starts + size] - csum[starts]) / (size * hop)  # (blocks, channels)
    total = ms.sum(axis=1)
    with np.errstate(divide="ignore"):
        loud = -0.691 + 10 * np.log10(total)
    return loud, total


def _gated_lufs(energy: np.ndarray, hop: int) -> float | None:
    loud, total = _block_loudness(energy, hop, 4, 1)  # 400 ms blocks, 75 % overlap
    if loud is None:
        return None
    keep = loud > -70.0
    if not keep.any():
        return None
    relative = -0.691 + 10 * math.log10(total[keep].mean()) - 10.0
    keep &= loud > relative
    return round(-0.691 + 10 * math.log10(total[keep].mean()), 1)


def _short_term_max(energy: np.ndarray, hop: int) -> float | None:
    loud, _ = _block_loudness(energy, hop, 30, 10)  # 3 s windows every 1 s
    if loud is None or not np.isfinite(loud).any():
        return None
    best = float(np.max(loud))
    return round(best, 1) if best > -70.0 else None


# --------------------------------------------------------------- measures


def integrated_lufs(x: np.ndarray, rate: int) -> float | None:
    """Integrated loudness per BS.1770-4 (None when silent or under 400 ms).

    Every channel is weighted 1.0 (meant for mono/stereo: no surround or LFE weighting).
    """
    return _gated_lufs(*_hop_energy(x, rate))


def short_term_lufs_max(x: np.ndarray, rate: int) -> float | None:
    """Loudest 3 s window (1 s steps, ungated); None when silent or under 3 s."""
    return _short_term_max(*_hop_energy(x, rate))


@lru_cache(maxsize=1)
def _true_peak_kernels() -> np.ndarray:
    """Three 12-tap phases (1/4, 2/4, 3/4) of a 48-tap Kaiser-windowed sinc."""
    j = np.arange(-5, 7)  # neighbours used for a point between sample 0 and 1
    kernels = []
    for k in (1, 2, 3):
        t = j - k / 4
        window = np.i0(8.0 * np.sqrt(np.clip(1 - (t / 6.5) ** 2, 0, None))) / np.i0(8.0)
        g = np.sinc(t) * window
        kernels.append(g / g.sum())
    return np.array(kernels)


def true_peak_dbfs(x: np.ndarray) -> float | None:
    """Inter-sample peak by 4x oversampling (dBTP, 0.1 dB); None when silent.

    A signal that starts or stops abruptly (hard cut to/from digital silence)
    reads a little above its sample peak: that overshoot is real, a DAC or
    lossy encoder reconstructs it too, so it is kept rather than smoothed away.
    """
    x = _as_2d(x)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    if peak <= 0:
        return None
    for c in range(x.shape[1]):
        col = x[:, c].astype(np.float64)
        for g in _true_peak_kernels():
            # y[m] = sum_j col[m + j] * g[j]: the value at m + k/4
            peak = max(peak, float(np.max(np.abs(np.convolve(col, g[::-1], mode="full")))))
    return _db(peak)


def band_balance(mono: np.ndarray, rate: int) -> dict[str, float]:
    """Share of energy per band in percent (sums to ~100; all zero when silent).

    A (frames, channels) array is also accepted: the channels' spectra are
    added rather than summed to mono first, so out-of-phase content still counts.
    """
    x = _as_2d(np.asarray(mono, dtype=np.float64))
    nfft = 1 << max(10, math.ceil(math.log2(rate / 6)))  # ~6 Hz resolution
    size = min(nfft, len(x))  # a short segment is windowed over its own length
    frames = max(1, len(x) // nfft)
    index = np.unique(np.linspace(0, frames - 1, min(frames, 600)).astype(int))
    window = np.hanning(size) if size > 1 else np.ones(max(size, 1))
    power = np.zeros(nfft // 2 + 1)
    for c in range(x.shape[1]):
        segs = x[: frames * size, c].reshape(frames, size)[index]
        power += (np.abs(np.fft.rfft(segs * window, nfft, axis=1)) ** 2).sum(axis=0)
    power[0] = 0.0  # DC is not sound
    freqs = np.fft.rfftfreq(nfft, 1 / rate)
    total = power.sum()
    out = {}
    for name, lo, hi in BANDS:
        mask = (freqs >= lo) & (freqs < hi if hi is not None else True)
        out[name] = round(100 * float(power[mask].sum() / total), 1) if total > 0 else 0.0
    return out


def stereo_image(x: np.ndarray) -> dict | None:
    """{"width_pct", "correlation"} of the first two channels; None if mono or silent.

    width_pct is side energy over mid + side (0 = mono, 50 = uncorrelated,
    100 = fully out of phase); correlation runs from -1 (L = -R) to +1 (L = R).
    """
    x = _as_2d(x)
    if x.shape[1] < 2:
        return None
    left, right = x[:, 0].astype(np.float64), x[:, 1].astype(np.float64)
    ll, rr, lr = float(left @ left), float(right @ right), float(left @ right)
    if ll <= 0 and rr <= 0:
        return None
    side, mid = ll + rr - 2 * lr, ll + rr + 2 * lr
    corr = lr / math.sqrt(ll * rr) if ll > 0 and rr > 0 else 0.0
    return {"width_pct": round(100 * side / (side + mid), 1),
            "correlation": round(corr, 2) + 0.0}


def _levels(x: np.ndarray) -> dict:
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms = math.sqrt(float(np.mean(np.square(x, dtype=np.float64)))) if x.size else 0.0
    peak_db, rms_db = _db(peak), _db(rms)
    return {
        "peak_dbfs": peak_db,
        "rms_dbfs": rms_db,
        "crest_db": round(peak_db - rms_db, 1) if peak_db is not None and rms_db is not None
        else None,
        "silent": peak < _SILENCE_AMP,
    }


def _segment(x: np.ndarray, rate: int, energy: np.ndarray | None = None,
             hop: int | None = None) -> dict:
    x = _as_2d(x)
    lv = _levels(x)
    if energy is None:
        energy, hop = _hop_energy(x, rate)
    out = {
        "peak_dbfs": lv["peak_dbfs"],
        "true_peak_dbfs": true_peak_dbfs(x) if lv["peak_dbfs"] is not None else None,
        "rms_dbfs": lv["rms_dbfs"],
        "lufs": _gated_lufs(energy, hop),
        "crest_db": lv["crest_db"],
        "over_samples": int(np.count_nonzero(np.abs(x) > 1.0)),
        "bands": band_balance(x, rate) if lv["rms_dbfs"] is not None else None,
    }
    if x.shape[1] >= 2:
        out["stereo"] = stereo_image(x)
    return out


def analyze_segment(x: np.ndarray, rate: int) -> dict:
    """Levels, loudness, crest, overs, band balance and stereo of one stretch of audio."""
    return _segment(x, rate)


# ------------------------------------------------------------------- mix


def _silence(x: np.ndarray, rate: int) -> dict:
    loud = np.nonzero(np.max(np.abs(_as_2d(x)), axis=1) > _SILENCE_AMP)[0]
    if len(loud) == 0:
        whole = round(len(x) / rate, 2)
        return {"leading_seconds": whole, "trailing_seconds": whole}
    first, last = int(loud[0]), int(loud[-1])
    return {"leading_seconds": round(first / rate, 2),
            "trailing_seconds": round((len(x) - 1 - last) / rate, 2)}


def _mix_warnings(overall: dict, silence: dict, sections: list[dict]) -> list[str]:
    if overall["peak_dbfs"] is None or overall["peak_dbfs"] < SILENCE_DBFS:
        return ["the render is silent"]
    w = []
    if overall["over_samples"]:
        w.append(f"{overall['over_samples']} samples above 0 dBFS: "
                 "they clip when exported to 16/24-bit")
    tp = overall["true_peak_dbfs"]
    if tp is not None and tp > -1.0:
        w.append(f"true peak {tp} dBTP is above -1 dBTP: "
                 "it may clip after MP3/AAC encoding on streaming platforms")
    lufs = overall["lufs"]
    if lufs is not None and lufs > -7:
        w.append(f"{lufs} LUFS integrated: very loud, likely over-limited")
    if lufs is not None and lufs < -18:
        w.append(f"{lufs} LUFS integrated: quiet: streaming platforms normalise "
                 "to about -14 LUFS")
    crest = overall["crest_db"]
    if crest is not None and crest < 6:
        w.append(f"crest factor {crest} dB: transients are squashed, little punch left")
    stereo = overall.get("stereo")
    if stereo and stereo["correlation"] < 0:
        w.append(f"stereo correlation {stereo['correlation']}: out-of-phase content, "
                 "parts will cancel in mono (phone, club PA)")
    bands = overall["bands"]
    if bands:
        bass = round(bands["sub"] + bands["low"], 1)
        top = round(bands["high_mid"] + bands["air"], 1)
        if bass > 65:
            w.append(f"bass-heavy: {bass}% of the energy is below 250 Hz")
        if top < 4:
            w.append(f"dull, little top end: only {top}% of the energy is above 2 kHz")
    if silence["leading_seconds"] > 2:
        w.append(f"{silence['leading_seconds']} s of silence before the music starts")
    live = [s for s in sections if s.get("lufs") is not None]
    if len(live) >= 2:
        for i, s in enumerate(live):
            # Compare with the others only: with two sections, including the
            # section itself would halve any jump between them.
            others = [o["lufs"] for j, o in enumerate(live) if j != i]
            gap = s["lufs"] - float(np.median(others))
            if abs(gap) > 8:
                side = "louder" if gap > 0 else "quieter"
                w.append(f"section {s['name']} is {round(abs(gap), 1)} LU {side} "
                         "than the rest")
    return w


def analyze_mix(path: Path, sections: list[dict] | None = None) -> dict:
    """Analyse a whole render, optionally section by section, with warnings.

    sections: [{"name", "start_seconds", "end_seconds", ...}]; other keys
    (start_bar, end_bar...) are carried over to the result. Bounds are
    clamped to the file and empty sections are skipped.
    """
    rate, x = load_wav(Path(path))
    if len(x) == 0:
        raise ToolError(f"{path} holds no audio")
    energy, hop = _hop_energy(x, rate)
    overall = _segment(x, rate, energy, hop)
    overall["short_term_lufs_max"] = _short_term_max(energy, hop)
    duration = len(x) / rate
    out_sections = []
    for i, sec in enumerate(sections or []):
        if not isinstance(sec, dict):
            raise ToolError(f"section {i} must be an object with name, start_seconds, "
                            "end_seconds")
        try:
            start = float(sec.get("start_seconds", 0.0))
            end = float(sec.get("end_seconds", duration))
        except (TypeError, ValueError) as exc:
            raise ToolError(f"section {i}: start_seconds/end_seconds must be numbers") from exc
        if not (math.isfinite(start) and math.isfinite(end)):
            raise ToolError(f"section {i}: start_seconds/end_seconds must be finite")
        start, end = max(0.0, start), min(duration, end)
        a, b = int(round(start * rate)), int(round(end * rate))
        if b <= a:
            continue
        part = x[a:b]
        lv = _levels(part)
        entry = dict(sec)
        entry.update({
            "name": sec.get("name", f"section {i + 1}"),
            "start_seconds": round(start, 3),
            "end_seconds": round(end, 3),
            "lufs": integrated_lufs(part, rate),
            "peak_dbfs": lv["peak_dbfs"],
            "rms_dbfs": lv["rms_dbfs"],
            "bands": band_balance(part, rate) if lv["rms_dbfs"] is not None
            else None,
        })
        out_sections.append(entry)
    silence = _silence(x, rate)
    result = {
        "path": str(path),
        "sample_rate": rate,
        "channels": x.shape[1],
        "duration_seconds": round(duration, 3),
        "overall": overall,
        "silence": silence,
    }
    if sections is not None:
        result["sections"] = out_sections
    result["warnings"] = _mix_warnings(overall, silence, out_sections)
    return result


def compare_stems(stems: list[dict]) -> list[str]:
    """Warnings across per-track stems: silent tracks, low-end masking, one track dominating.

    Each stem: {"track", "name", "lufs", "bands", "silent"} and optionally
    "share_pct" (its share of the mix energy) and "inserts" (the mixer inserts
    left playing in its render). Two stems where one's inserts include the
    other's track (a bus and a track feeding it) are not compared: the bus
    contains that track.
    """
    def label(s: dict) -> str:
        name = s.get("name")
        return f"track {s.get('track')} ({name})" if name else f"track {s.get('track')}"

    def short(s: dict) -> str:
        return s.get("name") or f"track {s.get('track')}"

    def related(a: dict, b: dict) -> bool:
        return (a.get("track") in (b.get("inserts") or ())
                or b.get("track") in (a.get("inserts") or ()))

    w = []
    for s in stems:
        if s.get("silent"):
            w.append(f"{label(s)} is silent in the render")
    live = [s for s in stems if not s.get("silent") and s.get("lufs") is not None]
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            if related(a, b) or abs(a["lufs"] - b["lufs"]) >= 6:
                continue
            ba, bb = a.get("bands") or {}, b.get("bands") or {}
            shared = [band for band in ("sub", "low")
                      if ba.get(band, 0) >= 35 and bb.get(band, 0) >= 35]
            if shared:
                band = max(shared, key=lambda n: ba[n] + bb[n])
                w.append(f"{short(a)} and {short(b)} both carry most of their energy in "
                         f"{band}: they mask each other; sidechain or EQ one of them")
    # The loudest stem that has something to be compared with: every louder stem
    # is related to all the others (a bus containing them).
    ranked = sorted(live, key=lambda s: s["lufs"], reverse=True)
    for top in ranked:
        others = [s for s in ranked if s is not top and not related(top, s)]
        if not others:
            continue
        lead = top["lufs"] - max(s["lufs"] for s in others)
        share = top.get("share_pct")
        if lead >= 3 and (share is None or share > 60):
            extra = f", {round(share)}% of the mix energy" if share is not None else ""
            w.append(f"{label(top)} dominates: {round(lead, 1)} LU louder than any other "
                     f"track{extra}; the rest may be buried")
        break
    return w
