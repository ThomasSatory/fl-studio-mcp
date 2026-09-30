"""Note names and chord symbols to MIDI numbers.

Scientific pitch notation: C4 = 60. FL Studio's own UI labels MIDI 60 as
"C5"; every tool docstring states which convention it uses.
"""

from __future__ import annotations

import re

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

_LETTERS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_ACCIDENTALS = {"": 0, "#": 1, "##": 2, "b": -1, "bb": -2}
# Letter in any case, then accidentals in lower case: "bb3" is B-flat 3, "b3" is B3.
_PITCH = r"([A-Ga-g])(##|bb|#|b)?"
_NOTE_RE = re.compile(rf"^{_PITCH}(-?\d+)$")
_ROOT_RE = re.compile(rf"^{_PITCH}")
_BASS_RE = re.compile(rf"^{_PITCH}$")

CHORD_QUALITIES: dict[str, tuple[int, ...]] = {
    "": (0, 4, 7),
    "maj": (0, 4, 7),
    "m": (0, 3, 7),
    "min": (0, 3, 7),
    "dim": (0, 3, 6),
    "aug": (0, 4, 8),
    "+": (0, 4, 8),
    "sus2": (0, 2, 7),
    "sus4": (0, 5, 7),
    "sus": (0, 5, 7),
    "5": (0, 7),
    "6": (0, 4, 7, 9),
    "m6": (0, 3, 7, 9),
    "7": (0, 4, 7, 10),
    "maj7": (0, 4, 7, 11),
    "m7": (0, 3, 7, 10),
    "mM7": (0, 3, 7, 11),
    "m7b5": (0, 3, 6, 10),
    "dim7": (0, 3, 6, 9),
    "9": (0, 4, 7, 10, 14),
    "maj9": (0, 4, 7, 11, 14),
    "m9": (0, 3, 7, 10, 14),
    "add9": (0, 4, 7, 14),
    "11": (0, 4, 7, 10, 14, 17),
    "13": (0, 4, 7, 10, 14, 21),
}


def _pitch_class(letter: str, accidental: str | None) -> int:
    """Semitones from C; may be -2..13 (Cb is -1, B# is 12)."""
    return _LETTERS[letter.upper()] + _ACCIDENTALS[accidental or ""]


def _check_range(midi: int, source: str) -> int:
    if not 0 <= midi <= 127:
        raise ValueError(f"{source!r} gives MIDI {midi}, outside 0-127")
    return midi


def note_to_midi(name: str) -> int:
    """'F#3' -> 54. Letter in any case, '#'/'##'/'b'/'bb', octave -1..9."""
    if not isinstance(name, str):
        raise ValueError(f"note name must be a string like 'C4', got {name!r}")
    match = _NOTE_RE.match(name.strip())
    if not match:
        raise ValueError(
            f"invalid note name {name!r}: expected a letter A-G, optional # or b, "
            "and an octave, e.g. 'C4', 'F#3', 'Bb2' (C4 = 60)"
        )
    letter, accidental, octave = match.groups()
    midi = 12 * (int(octave) + 1) + _pitch_class(letter, accidental)
    return _check_range(midi, name)


def midi_to_note_name(midi: int) -> str:
    """60 -> 'C4' (sharps, C4 = 60)."""
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def chord_to_midi(symbol: str, octave: int = 4, inversion: int = 0) -> list[int]:
    """'Am7', octave 3 -> [57, 60, 64, 67]; the root sits in `octave`.

    `inversion` raises the n lowest notes by an octave; a '/X' bass is then
    added below the lowest note. The result is sorted ascending.
    """
    if not isinstance(symbol, str):
        raise ValueError(f"chord symbol must be a string like 'Am7', got {symbol!r}")
    if isinstance(octave, bool) or not isinstance(octave, int):
        raise ValueError(f"octave must be an integer, got {octave!r}")
    body, slash, bass = symbol.strip().partition("/")
    root = _ROOT_RE.match(body)
    if not root:
        raise ValueError(f"invalid chord symbol {symbol!r}: must start with a note letter A-G")
    quality = body[root.end():]
    if quality not in CHORD_QUALITIES:
        supported = ", ".join(repr(q) for q in CHORD_QUALITIES)
        raise ValueError(
            f"unknown chord quality {quality!r} in {symbol!r}; supported: {supported}"
        )
    base = 12 * (octave + 1) + _pitch_class(*root.groups())
    notes = sorted(base + interval for interval in CHORD_QUALITIES[quality])

    if isinstance(inversion, bool) or not isinstance(inversion, int) or not (
        0 <= inversion < len(notes)
    ):
        raise ValueError(
            f"inversion must be an integer from 0 to {len(notes) - 1} for {symbol!r}, "
            f"got {inversion!r}"
        )
    for i in range(inversion):
        notes[i] += 12
    notes.sort()

    if slash:
        bass_match = _BASS_RE.match(bass)
        if not bass_match:
            raise ValueError(
                f"invalid bass note {bass!r} in {symbol!r}: "
                "expected a note without octave, e.g. 'E'"
            )
        bass_pc = _pitch_class(*bass_match.groups()) % 12
        distance = (notes[0] - bass_pc) % 12 or 12
        notes.insert(0, notes[0] - distance)

    for midi in notes:
        _check_range(midi, symbol)
    return notes
