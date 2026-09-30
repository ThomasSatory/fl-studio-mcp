"""Note names and chord symbols to MIDI numbers, C4 = 60."""

from __future__ import annotations

import pytest

from fl_studio_mcp.utils.music import (
    CHORD_QUALITIES,
    chord_to_midi,
    midi_to_note_name,
    note_to_midi,
)


@pytest.mark.parametrize(
    ("name", "midi"),
    [
        ("C4", 60),
        ("A4", 69),
        ("C-1", 0),
        ("G9", 127),
        ("F#3", 54),
        ("Bb2", 46),
        ("Cb4", 59),
        ("E#4", 65),
        ("B#3", 60),
        ("C##4", 62),
        ("Dbb4", 60),
        ("c4", 60),
        ("b3", 59),
        ("bb3", 58),
        (" D5 ", 74),
    ],
)
def test_note_to_midi(name, midi):
    assert note_to_midi(name) == midi


@pytest.mark.parametrize("name", ["H4", "C", "4", "C#", "Cx4", "C4.5", "", "C 4", "G#9", "Cb-1"])
def test_note_to_midi_rejects(name):
    with pytest.raises(ValueError, match=repr(name).strip("'\"") or "note"):
        note_to_midi(name)


def test_note_to_midi_rejects_non_string():
    with pytest.raises(ValueError, match="note name must be a string"):
        note_to_midi(60)


@pytest.mark.parametrize(
    ("midi", "name"), [(60, "C4"), (61, "C#4"), (0, "C-1"), (127, "G9"), (57, "A3")]
)
def test_midi_to_note_name(midi, name):
    assert midi_to_note_name(midi) == name


@pytest.mark.parametrize(
    ("symbol", "octave", "inversion", "expected"),
    [
        ("Am7", 3, 0, [57, 60, 64, 67]),
        ("Cmaj9/E", 4, 0, [52, 60, 64, 67, 71, 74]),
        ("C", 4, 1, [64, 67, 72]),
        ("C", 4, 2, [67, 72, 76]),
        ("am7", 3, 0, [57, 60, 64, 67]),
        ("bbm", 3, 0, [58, 61, 65]),
        ("F#m7b5", 3, 0, [54, 57, 60, 64]),
        ("G7/B", 3, 0, [47, 55, 59, 62, 65]),
        ("Dsus4", 4, 0, [62, 67, 69]),
        ("E5", 2, 0, [40, 47]),
        ("C/C", 4, 0, [48, 60, 64, 67]),
    ],
)
def test_chord_to_midi(symbol, octave, inversion, expected):
    assert chord_to_midi(symbol, octave=octave, inversion=inversion) == expected


@pytest.mark.parametrize(
    ("quality", "intervals"),
    [
        ("", (0, 4, 7)), ("maj", (0, 4, 7)), ("m", (0, 3, 7)), ("min", (0, 3, 7)),
        ("dim", (0, 3, 6)), ("aug", (0, 4, 8)), ("+", (0, 4, 8)), ("sus2", (0, 2, 7)),
        ("sus4", (0, 5, 7)), ("sus", (0, 5, 7)), ("5", (0, 7)), ("6", (0, 4, 7, 9)),
        ("m6", (0, 3, 7, 9)), ("7", (0, 4, 7, 10)), ("maj7", (0, 4, 7, 11)),
        ("m7", (0, 3, 7, 10)), ("mM7", (0, 3, 7, 11)), ("m7b5", (0, 3, 6, 10)),
        ("dim7", (0, 3, 6, 9)), ("9", (0, 4, 7, 10, 14)), ("maj9", (0, 4, 7, 11, 14)),
        ("m9", (0, 3, 7, 10, 14)), ("add9", (0, 4, 7, 14)),
        ("11", (0, 4, 7, 10, 14, 17)), ("13", (0, 4, 7, 10, 14, 21)),
    ],
)
def test_every_quality_from_the_spec(quality, intervals):
    assert CHORD_QUALITIES[quality] == intervals
    assert chord_to_midi("C" + quality, octave=4) == [60 + i for i in intervals]


def test_unknown_quality_lists_supported_ones():
    with pytest.raises(ValueError, match="unknown chord quality 'maj13'.*'m7b5'"):
        chord_to_midi("Cmaj13")


@pytest.mark.parametrize("symbol", ["", "H", "7", "C/", "C/H", "C/E4"])
def test_invalid_symbols(symbol):
    with pytest.raises(ValueError):
        chord_to_midi(symbol)


@pytest.mark.parametrize("inversion", [-1, 3])
def test_inversion_out_of_range(inversion):
    with pytest.raises(ValueError, match="inversion"):
        chord_to_midi("C", inversion=inversion)


def test_chord_out_of_midi_range_is_an_error_not_a_wraparound():
    with pytest.raises(ValueError, match="0-127"):
        chord_to_midi("C13", octave=9)


def test_octave_must_be_an_integer():
    with pytest.raises(ValueError, match="octave"):
        chord_to_midi("C", octave=True)
