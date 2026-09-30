# Ergonomie pour le modèle — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal :** un serveur MCP qui décrit correctement ses outils, en expose moins, résout noms de notes et accords, et ne cache plus aucun échec du piano roll.

**Architecture :** théorie musicale dans un module pur `utils/music.py` ; validation et vérification du piano roll dans `tools/piano_roll.py` ; le script FL `ComposeWithLLM.pyscript` passe en version 2 (propriétés de note, erreurs par action, `note_count`) ; le contrôleur enrichit trois réponses pour absorber 12 outils supprimés ; annotations MCP centralisées dans `tools/annotations.py`.

**Tech Stack :** Python 3.10+, FastMCP 4.0.10 (`mcp.types.ToolAnnotations`), pytest, ruff, uv.

**Spec :** `docs/superpowers/specs/2026-09-30-ergonomie-modele-design.md`

## Global Constraints

- Branche `ergonomie-modele` ; aucun push vers `upstream`, `git push` ne vise que `origin`, et seulement si l'utilisateur le demande.
- Convention d'octave : **C4 = 60** partout ; l'interface de FL Studio affiche 60 comme « C5 ».
- Temps et durées en quarts de temps.
- Commandes de vérification : `uv run pytest -q` et `uv run ruff check src fl_controller tests`, verts à chaque commit.
- Les commandes du contrôleur restent toutes ; seuls des outils MCP disparaissent.
- Messages d'erreur des outils en anglais (comme l'existant) ; docs et commits en français.
- Chaque commit se termine par `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Écart assumé avec la spec §4 : les constantes d'annotation vont dans `src/fl_studio_mcp/tools/annotations.py` et non dans `tools/__init__.py`, qui importe les modules d'outils (import circulaire sinon).

## Review Focus

- Mode `replace` avec une note invalide : rien ne doit partir en file, surtout pas le `clear` → test dans la tâche 2 (`test_replace_with_invalid_note_queues_nothing`).
- Noms en minuscules et ambiguïtés du « b » : `"b3"` = B3, `"bb3"` = Bb3, `"am7"` = A mineur 7 → tests dans la tâche 1.
- Accord qui sort de 0–127 à octave extrême (`"C13"` octave 9) : erreur, jamais de repliement → test dans la tâche 1.
- `fl_trigger_script` avec une file vide : le script v1 comme v2 ne répond rien ; pas de faux avertissement « script périmé » → test dans la tâche 4.
- Valeurs numériques envoyées en chaîne par le modèle (`"midi": "60"`, `"duration": "1"`) : erreur claire citant la note, pas d'exception Python → test dans la tâche 2.

---

### Task 1 : module de théorie musicale

**Files :**
- Create : `src/fl_studio_mcp/utils/music.py`
- Test : `tests/test_music.py`

**Interfaces :**
- Produces : `note_to_midi(name: str) -> int`, `chord_to_midi(symbol: str, octave: int = 4, inversion: int = 0) -> list[int]`, `midi_to_note_name(midi: int) -> str`, `CHORD_QUALITIES: dict[str, tuple[int, ...]]`. Toutes lèvent `ValueError` avec un message citant l'entrée.

- [ ] **Step 1 : écrire les tests**

`tests/test_music.py` :

```python
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
```

Note : `test_note_to_midi_rejects` vérifie seulement qu'une `ValueError` est levée avec un texte ; le `match` cite l'entrée quand elle est non vide.

- [ ] **Step 2 : lancer les tests, ils échouent**

Run : `uv run pytest tests/test_music.py -q`
Expected : erreur de collecte `ModuleNotFoundError: No module named 'fl_studio_mcp.utils.music'`.

- [ ] **Step 3 : implémenter**

`src/fl_studio_mcp/utils/music.py` :

```python
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
                f"invalid bass note {bass!r} in {symbol!r}: expected a note without octave, e.g. 'E'"
            )
        bass_pc = _pitch_class(*bass_match.groups()) % 12
        distance = (notes[0] - bass_pc) % 12 or 12
        notes.insert(0, notes[0] - distance)

    for midi in notes:
        _check_range(midi, symbol)
    return notes
```

- [ ] **Step 4 : lancer les tests, ils passent**

Run : `uv run pytest tests/test_music.py -q && uv run ruff check src tests`
Expected : tous PASS, ruff sans erreur. Si un cas de `test_note_to_midi_rejects` échoue sur le `match` (caractères spéciaux regex), remplacer le `match` de ce test par `match="note"` plutôt que d'affaiblir l'implémentation.

- [ ] **Step 5 : commit**

```bash
git add src/fl_studio_mcp/utils/music.py tests/test_music.py
git commit -m "feat: noms de notes et symboles d'accords vers MIDI (C4 = 60)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2 : validation des notes et outils du piano roll

**Files :**
- Modify : `src/fl_studio_mcp/tools/piano_roll.py` (supprimer `_midi_to_note_name` l. 368-373 ; réécrire `fl_send_notes`, `fl_send_chord`, `fl_delete_notes` l. 394-513 ; `fl_get_piano_roll_state` utilise `midi_to_note_name`)
- Test : `tests/test_note_validation.py`

**Interfaces :**
- Consumes : `note_to_midi`, `chord_to_midi`, `midi_to_note_name` (tâche 1).
- Produces (dans `piano_roll.py`) : `NOTE_DEFAULTS: dict`, `EXTENDED_PROPS: tuple[str, ...] = ("pan", "color", "slide", "porta")`, `_validate_notes(notes, *, require_duration: bool = True) -> list[dict]` (lève `ToolError`), `_summarize_notes(notes: list[dict]) -> str`. Une note normalisée d'ajout a exactement les clés `midi, time, duration, velocity, pan, color, slide, porta` ; une note de suppression `midi, time`. `fl_send_chord(chord, octave=4, inversion=0, time=0, duration=1.0, velocity=0.8, pan=0.5, color=0, auto_trigger=True)`.

- [ ] **Step 1 : écrire les tests**

`tests/test_note_validation.py` :

```python
"""Note validation happens before anything is queued (no FL Studio needed)."""

from __future__ import annotations

import copy
import json

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools import piano_roll
from fl_studio_mcp.tools.piano_roll import _validate_notes, register_piano_roll_tools
from tests.fakes import RecordingMCP


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))
    mcp = RecordingMCP()
    register_piano_roll_tools(mcp)
    return mcp.tools


def queued(tmp_path):
    request_file = tmp_path / "Piano roll scripts" / "mcp_request.json"
    return json.loads(request_file.read_text()) if request_file.exists() else []


def test_defaults_are_filled_and_names_resolved():
    assert _validate_notes([{"note": "A3", "duration": 2}]) == [
        {"midi": 57, "time": 0.0, "duration": 2.0, "velocity": 0.8,
         "pan": 0.5, "color": 0, "slide": False, "porta": False}
    ]


def test_input_list_is_not_modified():
    notes = [{"midi": 60, "duration": 1}]
    before = copy.deepcopy(notes)
    _validate_notes(notes)
    assert notes == before


@pytest.mark.parametrize(
    ("note", "message"),
    [
        ({"duration": 1}, "exactly one of 'midi'"),
        ({"midi": 60, "note": "C4", "duration": 1}, "exactly one of 'midi'"),
        ({"midi": 128, "duration": 1}, "midi must be an integer 0-127"),
        ({"midi": True, "duration": 1}, "midi must be an integer 0-127"),
        ({"midi": "60", "duration": 1}, "midi must be an integer 0-127"),
        ({"note": "H4", "duration": 1}, "invalid note name"),
        ({"midi": 60}, "missing 'duration'"),
        ({"midi": 60, "duration": 0}, "duration must be a number > 0"),
        ({"midi": 60, "duration": "1"}, "duration must be a number > 0"),
        ({"midi": 60, "duration": 1, "time": -1}, "time must be a number >= 0"),
        ({"midi": 60, "duration": 1, "velocity": 1.4}, "velocity must be a number 0-1"),
        ({"midi": 60, "duration": 1, "pan": -0.1}, "pan must be a number 0-1"),
        ({"midi": 60, "duration": 1, "color": 16}, "color must be an integer 0-15"),
        ({"midi": 60, "duration": 1, "color": 1.5}, "color must be an integer 0-15"),
        ({"midi": 60, "duration": 1, "slide": 1}, "slide must be true or false"),
        ({"midi": 60, "duration": 1, "porta": "yes"}, "porta must be true or false"),
        ({"midi": 60, "duration": 1, "veloctiy": 0.5}, "unknown field\\(s\\) veloctiy"),
        ("C4", "expected an object"),
    ],
)
def test_each_rule_names_the_note_index(note, message):
    notes = [{"midi": 60, "duration": 1}, note]
    with pytest.raises(ToolError, match=f"note 1: .*{message}"):
        _validate_notes(notes)


def test_empty_list_is_an_error():
    with pytest.raises(ToolError, match="No notes provided"):
        _validate_notes([])


def test_delete_mode_needs_no_duration_and_refuses_add_fields():
    assert _validate_notes([{"note": "C4", "time": 2}], require_duration=False) == [
        {"midi": 60, "time": 2.0}
    ]
    with pytest.raises(ToolError, match="unknown field\\(s\\) duration"):
        _validate_notes([{"midi": 60, "duration": 1}], require_duration=False)


def test_invalid_note_queues_nothing(tools, tmp_path):
    with pytest.raises(ToolError):
        tools["fl_send_notes"]([{"midi": 60, "duration": 1}, {"midi": 60}], auto_trigger=False)
    assert queued(tmp_path) == []


def test_replace_with_invalid_note_queues_nothing(tools, tmp_path):
    with pytest.raises(ToolError):
        tools["fl_send_notes"]([{"midi": 999, "duration": 1}], mode="replace", auto_trigger=False)
    assert queued(tmp_path) == []


def test_unknown_mode_is_rejected(tools, tmp_path):
    with pytest.raises(ToolError, match="mode must be 'add' or 'replace'"):
        tools["fl_send_notes"]([{"midi": 60, "duration": 1}], mode="overwrite", auto_trigger=False)
    assert queued(tmp_path) == []


def test_send_notes_queues_normalized_midi_only(tools, tmp_path):
    text = tools["fl_send_notes"]([{"note": "C4", "duration": 1}], mode="replace", auto_trigger=False)
    requests = queued(tmp_path)
    assert requests[0] == {"action": "clear"}
    assert requests[1]["action"] == "add_notes"
    assert requests[1]["notes"][0]["midi"] == 60
    assert "note" not in requests[1]["notes"][0]
    assert "C4@0" in text and "Not verified" in text


def test_send_chord_symbol(tools, tmp_path):
    text = tools["fl_send_chord"]("Am7", octave=3, time=4, duration=2, auto_trigger=False)
    (request,) = queued(tmp_path)
    assert request["action"] == "add_notes"
    assert [n["midi"] for n in request["notes"]] == [57, 60, 64, 67]
    assert all(n["time"] == 4.0 and n["duration"] == 2.0 for n in request["notes"])
    assert "Am7 → A3 C4 E4 G4" in text


def test_send_chord_mixed_list(tools, tmp_path):
    tools["fl_send_chord"]([57, "C4", "E4"], pan=0.2, color=3, auto_trigger=False)
    (request,) = queued(tmp_path)
    assert [n["midi"] for n in request["notes"]] == [57, 60, 64]
    assert all(n["pan"] == 0.2 and n["color"] == 3 for n in request["notes"])


@pytest.mark.parametrize(
    ("chord", "message"),
    [("Cmaj13", "unknown chord quality"), ([], "No notes provided"), ([60, True], "note 1")],
)
def test_send_chord_errors(tools, tmp_path, chord, message):
    with pytest.raises(ToolError, match=message):
        tools["fl_send_chord"](chord, auto_trigger=False)
    assert queued(tmp_path) == []


def test_delete_notes_accepts_names(tools, tmp_path):
    tools["fl_delete_notes"]([{"note": "C4", "time": 0}, {"midi": 64}], auto_trigger=False)
    (request,) = queued(tmp_path)
    assert request == {
        "action": "delete_notes",
        "notes": [{"midi": 60, "time": 0.0}, {"midi": 64, "time": 0.0}],
    }


def test_state_notes_get_names(tools, tmp_path, monkeypatch):
    monkeypatch.setattr(
        piano_roll, "_read_state", lambda: {"ppq": 96, "notes": [{"midi": 61}]}
    )
    assert tools["fl_get_piano_roll_state"]()["notes"][0]["note_name"] == "C#4"
```

- [ ] **Step 2 : lancer les tests, ils échouent**

Run : `uv run pytest tests/test_note_validation.py -q`
Expected : échec d'import `cannot import name '_validate_notes'`.

- [ ] **Step 3 : implémenter**

Dans `src/fl_studio_mcp/tools/piano_roll.py` :

1. Ajouter aux imports : `from fl_studio_mcp.utils.music import chord_to_midi, midi_to_note_name, note_to_midi`.
2. Supprimer `_midi_to_note_name` (l. 368-373) et remplacer ses deux usages restants par `midi_to_note_name`.
3. Ajouter après `_enrich_pr_context` :

```python
# Defaults of the optional note fields; the script receives every field.
NOTE_DEFAULTS = {
    "time": 0.0, "velocity": 0.8, "pan": 0.5, "color": 0, "slide": False, "porta": False,
}
# Fields a v1 ComposeWithLLM.pyscript silently ignores.
EXTENDED_PROPS = ("pan", "color", "slide", "porta")
_ADD_FIELDS = frozenset({"midi", "note", "duration", *NOTE_DEFAULTS})
_DELETE_FIELDS = frozenset({"midi", "note", "time"})

NOT_VERIFIED_HINT = (
    "Not verified: nothing reaches FL Studio until fl_trigger_script runs the queue."
)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_note(i: int, note: dict, require_duration: bool) -> dict:
    """One validated note with every field filled in; raises ToolError naming note i."""

    def fail(message: str) -> ToolError:
        return ToolError(f"note {i}: {message}")

    if ("midi" in note) == ("note" in note):
        raise fail("give exactly one of 'midi' (0-127) or 'note' (e.g. \"C4\", C4 = 60)")
    if "midi" in note:
        midi = note["midi"]
        if not _is_int(midi) or not 0 <= midi <= 127:
            raise fail(f"midi must be an integer 0-127, got {midi!r}")
    else:
        try:
            midi = note_to_midi(note["note"])
        except ValueError as e:
            raise fail(str(e)) from e

    time = note.get("time", NOTE_DEFAULTS["time"])
    if not _is_number(time) or time < 0:
        raise fail(f"time must be a number >= 0 (quarter notes), got {time!r}")
    if not require_duration:
        return {"midi": midi, "time": float(time)}

    if "duration" not in note:
        raise fail("missing 'duration' (quarter notes, > 0)")
    duration = note["duration"]
    if not _is_number(duration) or duration <= 0:
        raise fail(f"duration must be a number > 0 (quarter notes), got {duration!r}")
    out = {"midi": midi, "time": float(time), "duration": float(duration)}
    for key in ("velocity", "pan"):
        value = note.get(key, NOTE_DEFAULTS[key])
        if not _is_number(value) or not 0 <= value <= 1:
            raise fail(f"{key} must be a number 0-1, got {value!r}")
        out[key] = float(value)
    color = note.get("color", NOTE_DEFAULTS["color"])
    if not _is_int(color) or not 0 <= color <= 15:
        raise fail(f"color must be an integer 0-15, got {color!r}")
    out["color"] = color
    for key in ("slide", "porta"):
        value = note.get(key, NOTE_DEFAULTS[key])
        if not isinstance(value, bool):
            raise fail(f"{key} must be true or false, got {value!r}")
        out[key] = value
    return out


def _validate_notes(notes: object, *, require_duration: bool = True) -> list[dict]:
    """Validate every note before anything is queued; returns new, normalized dicts."""
    if not isinstance(notes, list) or not notes:
        raise ToolError("No notes provided")
    allowed = _ADD_FIELDS if require_duration else _DELETE_FIELDS
    normalized = []
    for i, note in enumerate(notes):
        if not isinstance(note, dict):
            raise ToolError(f"note {i}: expected an object, got {note!r}")
        unknown = sorted(set(note) - allowed)
        if unknown:
            raise ToolError(
                f"note {i}: unknown field(s) {', '.join(unknown)}; "
                f"allowed: {', '.join(sorted(allowed))}"
            )
        normalized.append(_normalize_note(i, note, require_duration))
    return normalized


def _summarize_notes(notes: list[dict], limit: int = 8) -> str:
    """'C4@0, E4@0, ... (3 more)' for a tool answer."""
    text = ", ".join(f"{midi_to_note_name(n['midi'])}@{n['time']:g}" for n in notes[:limit])
    if len(notes) > limit:
        text += f", ... ({len(notes) - limit} more)"
    return text
```

4. Remplacer `fl_send_notes`, `fl_send_chord` et `fl_delete_notes` par (le trigger reste `_get_trigger_info`, remplacé à la tâche 4) :

```python
    @mcp.tool()
    def fl_send_notes(notes: list[dict], mode: str = "add", auto_trigger: bool = True) -> str:
        """Add or replace notes in the open FL Studio piano roll.

        Times and durations are in quarter notes. Pitch convention: C4 = 60
        (FL Studio's own UI labels MIDI 60 as "C5").

        Args:
            notes: note objects. Each needs exactly one of:
                   - midi (int 0-127), or
                   - note (str, e.g. "C4", "F#3", "Bb2")
                   plus:
                   - duration (float > 0, quarter notes) - required
                   - time (float >= 0, quarter notes, default 0)
                   - velocity (0-1, default 0.8)
                   - pan (0-1, 0.5 = centre, default 0.5)
                   - color (int 0-15, FL note colour group, default 0)
                   - slide (bool, default false), porta (bool, default false)
                   Unknown fields are rejected.
            mode: "add" keeps existing notes, "replace" clears the piano roll first
            auto_trigger: run the piano roll script now and verify the result
                          (default True); False only queues the request

        Example:
            [{"note": "C4", "time": 0, "duration": 1},
             {"midi": 64, "time": 1, "duration": 0.5, "velocity": 0.6}]
        """
        if mode not in ("add", "replace"):
            raise ToolError(f"mode must be 'add' or 'replace', got {mode!r}")
        normalized = _validate_notes(notes)
        requests = [{"action": "clear"}] if mode == "replace" else []
        requests.append({"action": "add_notes", "notes": normalized})
        summary = _summarize_notes(normalized)

        with _PR_LOCK:
            _write_request(requests)
            if not auto_trigger:
                return f"Queued {len(normalized)} note(s): {summary}. {NOT_VERIFIED_HINT}"
            trigger_info = _get_trigger_info(True)
        return f"Queued {len(normalized)} note(s): {summary}.{trigger_info}"

    @mcp.tool()
    def fl_send_chord(
        chord: str | list[int | str],
        octave: int = 4,
        inversion: int = 0,
        time: float = 0,
        duration: float = 1.0,
        velocity: float = 0.8,
        pan: float = 0.5,
        color: int = 0,
        auto_trigger: bool = True,
    ) -> str:
        """Add a chord to the open FL Studio piano roll.

        Args:
            chord: a chord symbol such as "Am7", "Cmaj9/E", "F#m7b5", "Dsus4",
                   or a list of MIDI numbers and/or note names ([57, "C4", "E4"]).
                   Qualities: maj, m, dim, aug, sus2, sus4, 5, 6, m6, 7, maj7, m7,
                   mM7, m7b5, dim7, 9, maj9, m9, add9, 11, 13. "/X" adds X as bass.
            octave: octave of the chord root for a symbol (C4 = 60; FL's UI shows
                    MIDI 60 as "C5"). Ignored for a list.
            inversion: raise the n lowest notes an octave (symbol only)
            time: start in quarter notes (default 0)
            duration: length in quarter notes (default 1.0)
            velocity, pan: 0-1 (defaults 0.8 and 0.5)
            color: FL note colour group 0-15 (default 0)
            auto_trigger: run the piano roll script now and verify the result

        Example: fl_send_chord("Am7", octave=3, time=0, duration=4)
        """
        if isinstance(chord, str):
            try:
                pitches: list[int | str] = list(chord_to_midi(chord, octave, inversion))
            except ValueError as e:
                raise ToolError(str(e)) from e
        else:
            pitches = list(chord)
        shared = {"time": time, "duration": duration, "velocity": velocity,
                  "pan": pan, "color": color}
        normalized = _validate_notes([
            {"note": p, **shared} if isinstance(p, str) else {"midi": p, **shared}
            for p in pitches
        ])
        names = " ".join(midi_to_note_name(n["midi"]) for n in normalized)
        label = f"{chord} → {names}" if isinstance(chord, str) else names
        summary = f"{label} at {time:g}, duration {duration:g}"

        with _PR_LOCK:
            _write_request({"action": "add_notes", "notes": normalized})
            if not auto_trigger:
                return f"Queued chord {summary}. {NOT_VERIFIED_HINT}"
            trigger_info = _get_trigger_info(True)
        return f"Queued chord {summary}.{trigger_info}"

    @mcp.tool()
    def fl_delete_notes(notes: list[dict], auto_trigger: bool = True) -> str:
        """Delete notes from the open piano roll, matched by pitch and start time.

        Args:
            notes: objects with exactly one of midi (0-127) or note ("C4", C4 = 60),
                   and time (quarter notes, default 0).
            auto_trigger: run the piano roll script now and verify the result

        Example: [{"note": "C4", "time": 0}, {"midi": 64, "time": 0}]
        """
        normalized = _validate_notes(notes, require_duration=False)
        with _PR_LOCK:
            _write_request({"action": "delete_notes", "notes": normalized})
            if not auto_trigger:
                return f"Queued deletion of {len(normalized)} note(s). {NOT_VERIFIED_HINT}"
            trigger_info = _get_trigger_info(True)
        return f"Queued deletion of {len(normalized)} note(s).{trigger_info}"
```

- [ ] **Step 4 : lancer toute la suite**

Run : `uv run pytest -q && uv run ruff check src fl_controller tests`
Expected : tout PASS (dont `test_piano_roll_lock.py`, inchangé à cette étape).

- [ ] **Step 5 : commit**

```bash
git add src/fl_studio_mcp/tools/piano_roll.py tests/test_note_validation.py
git commit -m "feat(piano roll): noms de notes, accords par symbole, validation stricte avant mise en file

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3 : faux `flpianoroll` et script ComposeWithLLM v2

**Files :**
- Modify : `tests/fakes.py` (ajouter le faux module et le chargeur)
- Modify : `scripts/ComposeWithLLM.pyscript` (l. 120-305)
- Test : `tests/test_pianoroll_script.py`

**Interfaces :**
- Produces (tests) : `FakeNote`, `FakeScore`, `load_piano_roll_script(monkeypatch, tmp_path) -> tuple[ModuleType, ModuleType]` (script, faux `flpianoroll`).
- Produces (script) : `SCRIPT_VERSION = 2` ; le résumé de `process_mcp_request()` contient toujours `status`, `requests_processed`, `notes_added`, `notes_deleted`, `script_version`, `note_count` ; en échec en plus `message` et `failures: [{"index", "action", "message"}]`.

- [ ] **Step 1 : ajouter le faux module à `tests/fakes.py`**

Ajouter `import importlib.machinery` et `import os` aux imports, puis à la fin du fichier :

```python
PIANO_ROLL_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "ComposeWithLLM.pyscript"
)


class FakeNote:
    """flpianoroll.Note with the attributes the script reads and writes."""

    def __init__(self) -> None:
        self.number = 0
        self.time = 0
        self.length = 0
        self.velocity = 0.8
        self.pan = 0.5
        self.color = 0
        self.fcut = 0.5
        self.fres = 0.5
        self.slide = False
        self.porta = False
        self.pitchofs = 0
        self.selected = False
        self.muted = False


class FakeScore:
    """flpianoroll.score: a plain list of notes."""

    PPQ = 96
    tsnum = 4
    tsden = 4
    markerCount = 0
    snap_root_note = 0
    snap_scale_helper = 0

    def __init__(self) -> None:
        self.notes: list[FakeNote] = []

    @property
    def noteCount(self) -> int:  # noqa: N802 - FL's API name
        return len(self.notes)

    def getNote(self, index: int) -> FakeNote:  # noqa: N802
        return self.notes[index]

    def addNote(self, note: FakeNote) -> None:  # noqa: N802
        self.notes.append(note)

    def deleteNote(self, index: int) -> None:  # noqa: N802
        del self.notes[index]

    def getTimelineSelection(self) -> tuple[int, int]:  # noqa: N802
        return (-1, -1)


def load_piano_roll_script(monkeypatch, tmp_path: Path) -> tuple[ModuleType, ModuleType]:
    """Import ComposeWithLLM.pyscript with a fake flpianoroll; its files land under tmp_path."""
    flp = ModuleType("flpianoroll")
    flp.score = FakeScore()
    flp.Note = FakeNote
    monkeypatch.setitem(sys.modules, "flpianoroll", flp)
    monkeypatch.setenv("HOME", str(tmp_path))
    loader = importlib.machinery.SourceFileLoader(
        "compose_with_llm_under_test", str(PIANO_ROLL_SCRIPT_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    script = importlib.util.module_from_spec(spec)
    loader.exec_module(script)
    os.makedirs(script.get_script_dir(), exist_ok=True)
    return script, flp
```

- [ ] **Step 2 : écrire les tests**

`tests/test_pianoroll_script.py` :

```python
"""ComposeWithLLM.pyscript executed under pytest with a fake flpianoroll."""

from __future__ import annotations

import json

import pytest

from tests.fakes import load_piano_roll_script


@pytest.fixture
def pr(tmp_path, monkeypatch):
    return load_piano_roll_script(monkeypatch, tmp_path)


def queue(script, requests):
    with open(script.REQUEST_FILE, "w") as f:
        json.dump(requests, f)


def note(midi, **extra):
    return {"midi": midi, "time": 0.0, "duration": 1.0, "velocity": 0.8, **extra}


def test_extended_properties_are_applied(pr):
    script, flp = pr
    queue(script, [{"action": "add_notes", "notes": [
        note(60, pan=0.2, color=5, slide=True, porta=True, time=1.5)
    ]}])

    result = script.process_mcp_request()

    (added,) = flp.score.notes
    assert (added.number, added.time, added.length) == (60, 144, 96)
    assert (added.pan, added.color, added.slide, added.porta) == (0.2, 5, True, True)
    assert result["status"] == "success"
    assert result["notes_added"] == 1
    assert result["script_version"] == 2
    assert result["note_count"] == 1


def test_partial_failure_reports_counts_and_failures(pr):
    script, flp = pr
    queue(script, [
        {"action": "add_notes", "notes": [note(60), note(62)]},
        {"action": "add_notes", "notes": [note(64), {"time": 0, "duration": 1}]},
        {"action": "delete_notes", "notes": [{"midi": 60, "time": 0}]},
    ])

    result = script.process_mcp_request()

    assert result["status"] == "error"
    assert result["notes_added"] == 3  # 60, 62, then 64 before the broken note
    assert result["notes_deleted"] == 1  # later actions still run
    assert result["failures"] == [
        {"index": 1, "action": "add_notes", "message": result["failures"][0]["message"]}
    ]
    assert "action 1 (add_notes)" in result["message"]
    assert result["note_count"] == len(flp.score.notes) == 2


def test_unknown_action_is_a_failure(pr):
    script, _ = pr
    queue(script, [{"action": "transpose", "semitones": 2}])

    result = script.process_mcp_request()

    assert result["status"] == "error"
    assert result["failures"][0]["action"] == "transpose"
    assert "unknown action" in result["failures"][0]["message"]


def test_queue_is_emptied_even_after_a_failure(pr):
    script, _ = pr
    queue(script, [{"action": "transpose"}])

    script.process_mcp_request()

    with open(script.REQUEST_FILE) as f:
        assert json.load(f) == []


def test_clear_then_add_counts_both(pr):
    script, flp = pr
    queue(script, [{"action": "add_notes", "notes": [note(60), note(64)]}])
    script.process_mcp_request()
    queue(script, [{"action": "clear"}, {"action": "add_notes", "notes": [note(67)]}])

    result = script.process_mcp_request()

    assert (result["notes_deleted"], result["notes_added"], result["note_count"]) == (2, 1, 1)
    assert [n.number for n in flp.score.notes] == [67]


def test_empty_queue_answers_nothing(pr):
    script, _ = pr
    queue(script, [])
    assert script.process_mcp_request() is None


def test_apply_writes_response_with_version(pr):
    script, _ = pr
    queue(script, [{"action": "add_notes", "notes": [note(60)]}])

    script.apply()

    with open(script.RESPONSE_FILE) as f:
        response = json.load(f)
    with open(script.STATE_FILE) as f:
        state = json.load(f)
    assert response["script_version"] == 2
    assert state["noteCount"] == 1
```

- [ ] **Step 3 : lancer les tests, ils échouent**

Run : `uv run pytest tests/test_pianoroll_script.py -q`
Expected : `test_extended_properties_are_applied` échoue (`pan` resté 0.5, `script_version` absent), `test_partial_failure...` et `test_unknown_action...` échouent (`status` = `"success"`) ; `test_empty_queue_answers_nothing` passe déjà.

- [ ] **Step 4 : implémenter le script v2**

Dans `scripts/ComposeWithLLM.pyscript` :

1. Après `STATE_FILE = ...` ajouter :

```python
# Bumped when the request/response format changes; the MCP server checks it.
SCRIPT_VERSION = 2
# Optional note properties sent by the MCP server (server-validated).
NOTE_PROPERTIES = ("pan", "color", "slide", "porta")


def _apply_note_properties(midi_note, note_data):
    for prop in NOTE_PROPERTIES:
        if prop in note_data:
            setattr(midi_note, prop, note_data[prop])
```

2. Remplacer `process_mcp_request` par :

```python
def process_mcp_request():
    """Process every queued request; one failing request does not stop the others.

    FL has no transaction: what succeeded before a failure stays applied, and
    the counters report it.
    """
    if not os.path.exists(REQUEST_FILE):
        return None

    try:
        with open(REQUEST_FILE, 'r') as f:
            content = json.load(f)
    except Exception as e:
        return {"status": "error", "message": "could not read request file: %s" % e,
                "script_version": SCRIPT_VERSION, "note_count": flp.score.noteCount}

    # Handle both single request (old format) and list of requests (new format)
    if isinstance(content, list):
        requests = content
    elif isinstance(content, dict) and content.get("action"):
        requests = [content]
    else:
        return None
    if not requests:
        return None

    total_notes = 0
    notes_deleted = 0
    context_result = None
    failures = []
    handlers = {
        "add_notes": add_notes_to_piano_roll,
        "add_chord": add_chord_to_piano_roll,
        "delete_notes": delete_notes_from_piano_roll,
    }

    for position, request in enumerate(requests):
        action = request.get("action") if isinstance(request, dict) else None
        try:
            if action == "get_context":
                context_result = get_piano_roll_context()
            elif action == "clear":
                count = flp.score.noteCount
                for i in range(count - 1, -1, -1):
                    flp.score.deleteNote(i)
                notes_deleted += count
            elif action in handlers:
                result = handlers[action](request) or {}
                total_notes += result.get("notes_added", 0)
                notes_deleted += result.get("notes_deleted", 0)
                if result.get("status") != "success":
                    failures.append({"index": position, "action": action,
                                     "message": result.get("message", "unknown error")})
            else:
                failures.append({"index": position, "action": action,
                                 "message": "unknown action %r" % (action,)})
        except Exception as e:
            failures.append({"index": position, "action": action, "message": str(e)})

    # Clear the request queue after processing, even after a failure
    with open(REQUEST_FILE, 'w') as f:
        f.write("[]")

    summary = {
        "status": "error" if failures else "success",
        "requests_processed": len(requests),
        "notes_added": total_notes,
        "notes_deleted": notes_deleted,
        "script_version": SCRIPT_VERSION,
        "note_count": flp.score.noteCount,
    }
    if failures:
        first = failures[0]
        summary["message"] = "action %d (%s) failed: %s" % (
            first["index"], first["action"], first["message"])
        summary["failures"] = failures
    if context_result is not None:
        summary["context"] = context_result.get("context", context_result)
    return summary
```

3. Remplacer les trois fonctions d'édition pour compter ce qui a réellement été appliqué :

```python
def add_chord_to_piano_roll(request):
    """Add a chord's notes (legacy action; the MCP server now sends add_notes).

    Request format:
    {"action": "add_chord", "time": 0, "duration": 1.0,
     "notes": [{"midi": 60}, {"midi": 64, "offset": 0.5}, {"midi": 67, "duration": 2.0}]}
    """
    added = 0
    try:
        ppq = flp.score.PPQ
        current_time = int(ppq * request.get("time", 0))
        default_duration = request.get("duration", 1.0)
        for note_data in request.get("notes", []):
            midi_note = flp.Note()
            midi_note.number = note_data["midi"]
            midi_note.time = current_time + int(ppq * note_data.get("offset", 0))
            midi_note.length = int(ppq * note_data.get("duration", default_duration))
            midi_note.velocity = note_data.get("velocity", 0.8)
            _apply_note_properties(midi_note, note_data)
            flp.score.addNote(midi_note)
            added += 1
        return {"status": "success", "notes_added": added}
    except Exception as e:
        return {"status": "error", "message": str(e), "notes_added": added}


def delete_notes_from_piano_roll(request):
    """Delete notes matching (midi, time in quarter notes).

    Request format:
    {"action": "delete_notes", "notes": [{"midi": 60, "time": 0}]}
    """
    deleted = 0
    try:
        ppq = flp.score.PPQ
        criteria = [(n["midi"], int(ppq * n.get("time", 0))) for n in request.get("notes", [])]
        for i in range(flp.score.noteCount - 1, -1, -1):
            note = flp.score.getNote(i)
            if (note.number, note.time) in criteria:
                flp.score.deleteNote(i)
                deleted += 1
        return {"status": "success", "notes_deleted": deleted}
    except Exception as e:
        return {"status": "error", "message": str(e), "notes_deleted": deleted}


def add_notes_to_piano_roll(request):
    """Add notes; times and durations in quarter notes.

    Request format:
    {"action": "add_notes", "notes": [
        {"midi": 60, "time": 0, "duration": 1.0, "velocity": 0.8,
         "pan": 0.5, "color": 0, "slide": false, "porta": false}]}
    """
    added = 0
    try:
        ppq = flp.score.PPQ
        for note_data in request.get("notes", []):
            midi_note = flp.Note()
            midi_note.number = note_data["midi"]
            midi_note.time = int(ppq * note_data.get("time", note_data.get("offset", 0)))
            midi_note.length = int(ppq * note_data["duration"])
            midi_note.velocity = note_data.get("velocity", 0.8)
            _apply_note_properties(midi_note, note_data)
            flp.score.addNote(midi_note)
            added += 1
        return {"status": "success", "notes_added": added}
    except Exception as e:
        return {"status": "error", "message": str(e), "notes_added": added}
```

Dans `delete_notes_from_piano_roll`, le passage de `note_data["time"]` à `n.get("time", 0)` est voulu : le serveur envoie toujours `time`, et un ancien client sans `time` ne plante plus.

- [ ] **Step 5 : lancer toute la suite et ruff**

Run : `uv run pytest -q && uv run ruff check src fl_controller tests`
Expected : tout PASS. Ruff ne vérifie pas le `.pyscript` (extension non Python) : relire le diff du script à la main pour la syntaxe, puis `uv run python -c "import ast; ast.parse(open('scripts/ComposeWithLLM.pyscript').read())"` → aucune sortie.

- [ ] **Step 6 : commit**

```bash
git add tests/fakes.py tests/test_pianoroll_script.py scripts/ComposeWithLLM.pyscript
git commit -m "feat(ComposeWithLLM): v2 — pan/couleur/slide/porta, échecs par action, note_count et version

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4 : vérification côté serveur

**Files :**
- Modify : `src/fl_studio_mcp/tools/piano_roll.py` (supprimer `_get_trigger_info` l. 376-388 ; ajouter `_checked_response` ; adapter `fl_send_notes`, `fl_send_chord`, `fl_delete_notes`, `fl_clear_piano_roll`, `fl_trigger_script`)
- Modify : `tests/test_piano_roll_lock.py:36-38,71` (réponse v2, texte attendu)
- Test : `tests/test_pr_verification.py`

**Interfaces :**
- Consumes : `_run_pr_script() -> dict` (existant : `triggered`, `ran`, `response`, `error`), `NOTE_DEFAULTS`, `EXTENDED_PROPS`, `_summarize_notes` (tâche 2), format de réponse v2 (tâche 3).
- Produces : `PR_SCRIPT_OUTDATED: str`, `_checked_response(outcome: dict, notes_sent: list[dict] | None = None) -> tuple[dict, str]` — renvoie `(response, suffix)` où `suffix` est `""` ou `" Warning: ..."` ; lève `ToolError` sur tout échec.

- [ ] **Step 1 : écrire les tests**

`tests/test_pr_verification.py` :

```python
"""The note tools trust only what the piano roll script reports back."""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools import piano_roll
from fl_studio_mcp.tools.piano_roll import register_piano_roll_tools
from tests.fakes import RecordingMCP


class ScriptedFL:
    """Plays ComposeWithLLM: empties the queue, exports state, writes `response`."""

    is_supported = True
    platform = "TestOS"
    keystroke = "Ctrl+Alt+Y"

    def __init__(self, scripts_dir, response):
        self.scripts_dir = scripts_dir
        self.response = response

    def is_fl_running(self):
        return True

    def activate_window(self):
        return True

    def trigger(self, delay=0.0):
        (self.scripts_dir / "mcp_request.json").write_text("[]")
        (self.scripts_dir / "piano_roll_state.json").write_text(
            json.dumps({"ppq": 96, "noteCount": 0, "notes": []})
        )
        if self.response is not None:
            (self.scripts_dir / "mcp_response.json").write_text(json.dumps(self.response))
        return True


@pytest.fixture
def make_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))
    scripts_dir = tmp_path / "Piano roll scripts"
    monkeypatch.setattr(piano_roll, "_focus_piano_roll", lambda: None)

    def build(response):
        scripts_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(piano_roll, "get_trigger", lambda: ScriptedFL(scripts_dir, response))
        mcp = RecordingMCP()
        register_piano_roll_tools(mcp)
        return mcp.tools

    return build


def v2(**fields):
    return {"status": "success", "requests_processed": 1, "notes_added": 0,
            "notes_deleted": 0, "script_version": 2, "note_count": 0, **fields}


def test_success_names_notes_and_count(make_tools):
    tools = make_tools(v2(notes_added=4, note_count=36))

    text = tools["fl_send_chord"]("Am7", octave=3, duration=4)

    assert "4 note(s) written" in text
    assert "A3 C4 E4 G4" in text
    assert "now has 36 notes" in text
    assert "Warning" not in text


def test_script_error_raises_with_partial_counts(make_tools):
    tools = make_tools(v2(status="error", notes_added=2, notes_deleted=1,
                          message="action 1 (add_notes) failed: 'midi'"))

    with pytest.raises(ToolError, match="applied 2 added and 1 deleted.*action 1 \\(add_notes\\)"):
        tools["fl_send_notes"]([{"midi": 60, "duration": 1}])


def test_added_count_mismatch_raises(make_tools):
    tools = make_tools(v2(notes_added=1))

    with pytest.raises(ToolError, match="Sent 3 note\\(s\\).*reports 1 added"):
        tools["fl_send_chord"]("C")


def test_v1_script_with_extended_props_raises(make_tools):
    tools = make_tools({"status": "success", "notes_added": 1, "notes_deleted": 0})

    with pytest.raises(ToolError, match="outdated.*without pan/color/slide/porta"):
        tools["fl_send_notes"]([{"midi": 60, "duration": 1, "slide": True}])


def test_v1_script_without_extended_props_warns(make_tools):
    tools = make_tools({"status": "success", "notes_added": 1, "notes_deleted": 0})

    text = tools["fl_send_notes"]([{"midi": 60, "duration": 1}])

    assert "1 note(s) written" in text
    assert "Warning: ComposeWithLLM.pyscript in FL Studio is outdated" in text


def test_no_response_for_sent_notes_raises(make_tools, monkeypatch):
    tools = make_tools(None)
    # With a queued request the round-trip waits for a response until the
    # deadline; shorten it so the test does not sleep 5 s.
    original = piano_roll._run_pr_script
    monkeypatch.setattr(piano_roll, "_run_pr_script", lambda: original(timeout=0.3))

    with pytest.raises(ToolError, match="processed nothing"):
        tools["fl_send_notes"]([{"midi": 60, "duration": 1}])


def test_trigger_script_with_empty_queue_has_no_false_warning(make_tools):
    tools = make_tools(None)

    text = tools["fl_trigger_script"]()

    assert text == "FL Studio ran the piano roll script (nothing was queued)."


def test_trigger_script_reports_script_errors(make_tools):
    tools = make_tools(v2(status="error", message="action 0 (transpose) failed: unknown action"))
    piano_roll._write_request({"action": "transpose"})

    with pytest.raises(ToolError, match="unknown action"):
        tools["fl_trigger_script"]()


def test_delete_and_clear_report_deleted_count(make_tools):
    tools = make_tools(v2(notes_deleted=2, note_count=5))

    assert tools["fl_delete_notes"]([{"midi": 60}]) == (
        "2 note(s) deleted. The piano roll now has 5 notes."
    )
    assert tools["fl_clear_piano_roll"]() == (
        "Piano roll cleared (2 note(s) removed). The piano roll now has 5 notes."
    )
```

- [ ] **Step 2 : lancer les tests, ils échouent**

Run : `uv run pytest tests/test_pr_verification.py -q`
Expected : échecs sur les textes (« Queued … » au lieu de « written ») et absence de `ToolError` pour le décalage et le script v1.

- [ ] **Step 3 : implémenter**

Dans `src/fl_studio_mcp/tools/piano_roll.py` :

1. Après `PR_FL_PROBE_FAILED` ajouter :

```python
PR_SCRIPT_OUTDATED = (
    "ComposeWithLLM.pyscript in FL Studio is outdated (its answer has no "
    "script_version). Reinstall it with scripts/setup.sh, then run it once from "
    "the piano roll's Tools > Scripting menu."
)
```

2. Remplacer `_get_trigger_info` (supprimé) par :

```python
def _uses_extended_props(notes: list[dict]) -> bool:
    return any(n.get(p, NOTE_DEFAULTS[p]) != NOTE_DEFAULTS[p] for n in notes for p in EXTENDED_PROPS)


def _note_count_text(response: dict) -> str:
    count = response.get("note_count")
    return f" The piano roll now has {count} notes." if count is not None else ""


def _checked_response(outcome: dict, notes_sent: list[dict] | None = None) -> tuple[dict, str]:
    """Raise ToolError unless the script provably did what was asked.

    Returns the script's response and a warning suffix ("" when none). With
    `notes_sent`, the number of notes FL added must match it.
    """
    if not outcome["ran"]:
        raise ToolError(outcome["error"])
    response = outcome.get("response") or {}
    added = response.get("notes_added") or 0
    deleted = response.get("notes_deleted") or 0
    if not response:
        if notes_sent is not None:
            raise ToolError(
                "FL Studio ran the piano roll script but it processed nothing: the "
                "request was not in its queue. Check fl_get_piano_roll_info and retry."
            )
        return response, ""
    if response.get("status") == "error":
        raise ToolError(
            f"FL Studio applied {added} added and {deleted} deleted note(s), then "
            f"failed: {response.get('message')}"
        )
    suffix = ""
    if "script_version" not in response:
        if notes_sent is not None and _uses_extended_props(notes_sent):
            raise ToolError(
                f"{PR_SCRIPT_OUTDATED} The notes were written without pan/color/slide/porta."
            )
        suffix = f" Warning: {PR_SCRIPT_OUTDATED}"
    if notes_sent is not None and added != len(notes_sent):
        raise ToolError(
            f"Sent {len(notes_sent)} note(s) but FL Studio reports {added} added."
        )
    return response, suffix
```

3. Dans `fl_send_notes`, remplacer les deux dernières lignes du bloc `with` et le `return` final par :

```python
            outcome = _run_pr_script()
        response, warning = _checked_response(outcome, normalized)
        return (
            f"{len(normalized)} note(s) written: {summary}."
            f"{_note_count_text(response)}{warning}"
        )
```

4. Dans `fl_send_chord`, même remplacement avec :

```python
            outcome = _run_pr_script()
        response, warning = _checked_response(outcome, normalized)
        return (
            f"{len(normalized)} note(s) written: {summary}."
            f"{_note_count_text(response)}{warning}"
        )
```

5. Dans `fl_delete_notes` :

```python
            outcome = _run_pr_script()
        response, warning = _checked_response(outcome)
        return (
            f"{response.get('notes_deleted') or 0} note(s) deleted."
            f"{_note_count_text(response)}{warning}"
        )
```

6. `fl_clear_piano_roll` devient :

```python
    @mcp.tool()
    def fl_clear_piano_roll(auto_trigger: bool = True) -> str:
        """Delete every note in the open piano roll.

        Args:
            auto_trigger: run the piano roll script now and verify the result
        """
        with _PR_LOCK:
            _write_request({"action": "clear"})
            if not auto_trigger:
                return f"Queued clear of all notes. {NOT_VERIFIED_HINT}"
            outcome = _run_pr_script()
        response, warning = _checked_response(outcome)
        return (
            f"Piano roll cleared ({response.get('notes_deleted') or 0} note(s) removed)."
            f"{_note_count_text(response)}{warning}"
        )
```

7. `fl_trigger_script` devient :

```python
    @mcp.tool()
    def fl_trigger_script() -> str:
        """Run the ComposeWithLLM piano roll script on the queued requests.

        Sends the keystroke (Cmd+Opt+Y on macOS, Ctrl+Alt+Y on Windows/Linux)
        and reports what the script did; raises if it failed or did not run.
        """
        with _PR_LOCK:
            outcome = _run_pr_script()
        response, warning = _checked_response(outcome)
        if not response:
            return "FL Studio ran the piano roll script (nothing was queued)."
        return f"{_describe_run(outcome)}{_note_count_text(response)}{warning}"
```

`_describe_run` reste inchangé (utilisé ici et par `test_piano_roll_script_run.py`).

8. Adapter `tests/test_piano_roll_lock.py` : dans `trigger`, la réponse devient

```python
                json.dumps({"status": "success", "notes_added": after, "notes_deleted": 0,
                            "script_version": 2, "note_count": after})
```

et l'assertion finale `assert all("1 note(s) written" in r for r in results)`.

- [ ] **Step 4 : lancer toute la suite**

Run : `uv run pytest -q && uv run ruff check src fl_controller tests`
Expected : tout PASS.

- [ ] **Step 5 : commit**

```bash
git add src/fl_studio_mcp/tools/piano_roll.py tests/test_pr_verification.py tests/test_piano_roll_lock.py
git commit -m "feat(piano roll): vérifier la réponse du script — échecs, décalage de notes, script périmé

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5 : contrôleur enrichi et suppression des 12 outils

**Files :**
- Modify : `fl_controller/device_FLStudioMCP.py` (`handle_mixer_get_all_tracks` l. 817-840, `handle_channels_get_all` l. 963-977, `handle_plugins_get_params` l. 1211-1245)
- Modify : `tests/fakes.py` (`make_fl_modules` : valeurs typées canaux)
- Modify : `src/fl_studio_mcp/tools/channels.py`, `mixer.py`, `plugins.py`, `patterns.py`, `src/fl_studio_mcp/server.py`
- Modify : `tests/test_errors.py:80-82`
- Test : `tests/test_controller.py` (ajouts), `tests/test_tool_consolidation.py`

**Interfaces :**
- Produces (contrôleur) : `channels.getAll` → chaque canal a en plus `volume`, `pan`, `is_solo` ; `mixer.getAllTracks` → `{"track_count": int, "tracks": [...]}` ; `plugins.getParams` → `{"valid": bool, "name", "param_count", "preset_count", "color", "params"}` (invalide : `{"valid": False, "params": []}`) ; helper `_plugin_target(params) -> tuple[int, int, bool]`.
- Produces (outils) : `fl_select_channel(index, select=True, exclusive=False)`, `fl_get_all_mixer_tracks(include_empty=False) -> dict`, `fl_get_plugin_params(...) -> dict`, `fl_connection_status(reconnect=False) -> dict`.

- [ ] **Step 1 : tests contrôleur**

Dans `tests/fakes.py`, `make_fl_modules`, bloc `channels`, ajouter :

```python
    channels.getChannelVolume.return_value = 0.78
    channels.getChannelPan.return_value = 0.0
    channels.isChannelSolo.return_value = 0
```

Ajouter à `tests/test_controller.py` :

```python
def test_channels_get_all_includes_mix_fields(fl):
    controller, _ = fl

    (kick, _bass) = controller.dispatch_command("channels.getAll", {})["channels"]

    assert kick == {"index": 0, "name": "Kick", "volume": 0.78, "pan": 0.0,
                    "is_muted": False, "is_solo": False, "is_selected": False,
                    "target_fx_track": 1}


def test_mixer_get_all_tracks_reports_total_count(fl):
    controller, _ = fl

    result = controller.dispatch_command("mixer.getAllTracks", {"include_empty": False})

    assert result["track_count"] == 3
    assert [t["name"] for t in result["tracks"]] == ["Master", "Drums"]


def test_plugins_get_params_bundles_plugin_info(fl):
    controller, modules = fl
    plugins = modules["plugins"]
    plugins.isValid.return_value = True
    plugins.getPluginName.return_value = "Sytrus"
    plugins.getParamCount.return_value = 2
    plugins.getPresetCount.return_value = 12
    plugins.getColor.return_value = 0xFF8800
    plugins.getParamName.side_effect = lambda i, *_: ["Cutoff", "Res"][i]
    plugins.getParamValue.return_value = 0.5
    plugins.getParamValueString.return_value = "50%"

    result = controller.dispatch_command("plugins.getParams", {"index": 3, "slot_index": 2})

    assert {k: v for k, v in result.items() if k != "params"} == {
        "valid": True, "name": "Sytrus", "param_count": 2, "preset_count": 12,
        "color": "0xff8800",
    }
    assert [p["name"] for p in result["params"]] == ["Cutoff", "Res"]
    plugins.getParamCount.assert_called_with(3, 2, True)


def test_plugins_get_params_invalid_plugin_is_not_an_error(fl):
    controller, modules = fl
    modules["plugins"].isValid.return_value = False

    result = controller.dispatch_command("plugins.getParams", {"index": 9})

    assert result == {"valid": False, "params": []}
    modules["plugins"].getParamCount.assert_not_called()
```

- [ ] **Step 2 : tests des outils**

`tests/test_tool_consolidation.py` :

```python
"""Tools that absorbed the removed duplicates (no FL Studio needed)."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.channels import register_channel_tools
from fl_studio_mcp.tools.mixer import register_mixer_tools
from fl_studio_mcp.tools.plugins import register_plugin_tools
from tests.fakes import RecordingMCP


class FakeConnection:
    def __init__(self, response=None):
        self.sent = []
        self.response = response or {"success": True}

    def send_command(self, action, params=None, timeout=2.0):
        self.sent.append((action, params))
        return self.response


def make_tools(monkeypatch, conn):
    import fl_studio_mcp.utils.connection as connection

    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    mcp = RecordingMCP()
    for register in (register_channel_tools, register_mixer_tools, register_plugin_tools):
        register(mcp)
    return mcp.tools


def test_select_channel_exclusive_uses_select_one(monkeypatch):
    conn = FakeConnection({"success": True, "channel_name": "Kick"})
    tools = make_tools(monkeypatch, conn)

    assert tools["fl_select_channel"](0, exclusive=True) == "Channel 'Kick' selected exclusively"
    assert conn.sent == [("channels.selectOne", {"index": 0})]


def test_select_channel_default_is_unchanged(monkeypatch):
    conn = FakeConnection({"success": True, "channel_name": "Kick"})
    tools = make_tools(monkeypatch, conn)

    tools["fl_select_channel"](0)

    assert conn.sent == [("channels.select", {"index": 0, "select": True})]


def test_exclusive_deselect_is_rejected(monkeypatch):
    conn = FakeConnection()
    tools = make_tools(monkeypatch, conn)

    with pytest.raises(ToolError, match="exclusive=True requires select=True"):
        tools["fl_select_channel"](0, select=False, exclusive=True)
    assert conn.sent == []


def test_all_mixer_tracks_returns_count_and_tracks(monkeypatch):
    tracks = [{"index": 0, "name": "Master"}]
    tools = make_tools(monkeypatch, FakeConnection(
        {"success": True, "track_count": 127, "tracks": tracks}
    ))

    assert tools["fl_get_all_mixer_tracks"]() == {"track_count": 127, "tracks": tracks}


def test_plugin_params_passes_plugin_info_through(monkeypatch):
    response = {"success": True, "valid": True, "name": "Sytrus", "param_count": 2,
                "preset_count": 12, "color": "0xff8800", "params": []}
    tools = make_tools(monkeypatch, FakeConnection(response))

    assert tools["fl_get_plugin_params"](3) == {
        "valid": True, "name": "Sytrus", "param_count": 2, "preset_count": 12,
        "color": "0xff8800", "params": [],
    }


def test_plugin_params_invalid(monkeypatch):
    tools = make_tools(monkeypatch, FakeConnection({"success": True, "valid": False, "params": []}))

    assert tools["fl_get_plugin_params"](9) == {"valid": False, "params": []}


def test_connection_status_reconnect_resets_first(monkeypatch):
    import fl_studio_mcp.server as server

    calls = []

    class Conn:
        def ensure_connected(self):
            calls.append("ensure")

        def get_status(self):
            return {"connected": True, "port_name": "IAC", "available_ports": ["IAC"]}

    monkeypatch.setattr(server, "reset_connection", lambda: calls.append("reset"))
    monkeypatch.setattr(server, "get_connection", lambda: Conn())

    status = server.fl_connection_status(reconnect=True)

    assert calls == ["reset", "ensure"]
    assert status["connected"] is True
```

Note : sous FastMCP 4.0.10, `@mcp.tool()` renvoie la fonction elle-même (vérifié : `type(server.fl_connection_status)` est `function`), d'où l'appel direct.

Dans `tests/test_errors.py`, remplacer `test_int_returning_tool_raises_instead_of_minus_one` par :

```python
def test_list_returning_tool_raises_instead_of_empty_list(tools):
    with pytest.raises(ToolError, match="boom on channels.getAll"):
        tools["fl_get_all_channels"]()
```

- [ ] **Step 3 : lancer les tests, ils échouent**

Run : `uv run pytest tests/test_controller.py tests/test_tool_consolidation.py tests/test_errors.py -q`
Expected : échecs sur les champs absents (`volume`, `track_count`, `valid`), `exclusive` inconnu, `reconnect` inconnu.

- [ ] **Step 4 : contrôleur**

Dans `handle_channels_get_all`, le dict de chaque canal devient :

```python
        channels_list.append({
            "index": i,
            "name": channels.getChannelName(i, True),
            "volume": channels.getChannelVolume(i, True),
            "pan": channels.getChannelPan(i, True),
            "is_muted": channels.isChannelMuted(i, True) == 1,
            "is_solo": channels.isChannelSolo(i, True) == 1,
            "is_selected": channels.isChannelSelected(i, True) == 1,
            "target_fx_track": channels.getTargetFxTrack(i, True),
        })
```

Dans `handle_mixer_get_all_tracks`, `return {"tracks": tracks}` devient `return {"track_count": track_count, "tracks": tracks}` (le snapshot lit `["tracks"]`, inchangé).

Remplacer `handle_plugins_get_params` par :

```python
def _plugin_target(params: dict) -> tuple:
    """(index, slot_index, use_global) as the plugins module expects them.

    A mixer effect slot always uses global indexing; a channel rack plugin
    (slot_index -1) honours use_global.
    """
    index = params.get("index", 0)
    slot_index = params.get("slot_index", -1)
    if slot_index >= 0:
        return index, slot_index, True
    return index, -1, params.get("use_global", True)


def handle_plugins_get_params(params: dict) -> dict:
    """Plugin identity plus its parameters; an empty location is not an error."""
    index, slot_index, use_global = _plugin_target(params)
    max_params = params.get("max_params", 50)

    if not plugins.isValid(index, slot_index, use_global):
        return {"valid": False, "params": []}

    param_count = plugins.getParamCount(index, slot_index, use_global)
    param_list = []
    for i in range(min(param_count, max_params)):
        try:
            param_list.append({
                "index": i,
                "name": plugins.getParamName(i, index, slot_index, use_global),
                "value": plugins.getParamValue(i, index, slot_index, use_global),
                "value_string": plugins.getParamValueString(i, index, slot_index, use_global),
            })
        except Exception as e:
            print(f"Warning: could not read param {i}: {e}")

    return {
        "valid": True,
        "name": plugins.getPluginName(index, slot_index, use_global),
        "param_count": param_count,
        "preset_count": plugins.getPresetCount(index, slot_index, use_global),
        "color": hex(plugins.getColor(index, slot_index, use_global)),
        "params": param_list,
    }
```

Les autres handlers `plugins.*` ne changent pas (leur commande reste utilisable).

- [ ] **Step 5 : outils**

`src/fl_studio_mcp/tools/channels.py` : supprimer `fl_get_channel_count`, `fl_get_selected_channel`, `fl_select_one_channel`, `fl_get_grid_bit`. Remplacer `fl_get_all_channels` et `fl_select_channel` par :

```python
    @mcp.tool()
    def fl_get_all_channels() -> list[dict]:
        """List every channel in the channel rack.

        Each channel: index (global), name, volume, pan, is_muted, is_solo,
        is_selected, target_fx_track. The channel count is the list length;
        the selected channel(s) have is_selected true.
        """
        result = run(get_connection(), "channels.getAll")

        return result.get("channels", [])

    @mcp.tool()
    def fl_select_channel(index: int, select: bool = True, exclusive: bool = False) -> str:
        """Select or deselect a channel.

        Args:
            index: Channel index (global)
            select: True to select, False to deselect
            exclusive: True to select only this channel, deselecting all others
        """
        if exclusive:
            if not select:
                raise ToolError("exclusive=True requires select=True")
            result = run(get_connection(), "channels.selectOne", {"index": index})
            channel_name = result.get("channel_name", f"Channel {index}")
            return f"Channel '{channel_name}' selected exclusively"

        result = run(get_connection(), "channels.select", {
            "index": index,
            "select": select,
        })

        channel_name = result.get("channel_name", f"Channel {index}")
        return f"Channel '{channel_name}' {'selected' if select else 'deselected'}"
```

`src/fl_studio_mcp/tools/mixer.py` : supprimer `fl_get_mixer_track_count` ; `fl_get_all_mixer_tracks` devient :

```python
    @mcp.tool()
    def fl_get_all_mixer_tracks(include_empty: bool = False) -> dict:
        """List mixer tracks.

        Returns {"track_count": total tracks in the mixer (Master is 0),
        "tracks": [{index, name, volume, pan, is_muted, is_solo}]}.

        Args:
            include_empty: False (default) lists Master and named tracks only;
                           True lists every track.
        """
        result = run(get_connection(), "mixer.getAllTracks", {"include_empty": include_empty})

        return {"track_count": result.get("track_count"), "tracks": result.get("tracks", [])}
```

`src/fl_studio_mcp/tools/plugins.py` : supprimer `fl_is_plugin_valid`, `fl_get_plugin_name`, `fl_get_plugin_param_count`, `fl_get_preset_count`, `fl_get_plugin_color`. `fl_get_plugin_params` devient :

```python
    @mcp.tool()
    def fl_get_plugin_params(
        index: int,
        slot_index: int = -1,
        use_global_index: bool = True,
        max_params: int = 50
    ) -> dict:
        """Describe a plugin and list its parameters with current values.

        Returns {"valid", "name", "param_count", "preset_count", "color",
        "params": [{index, name, value, value_string}]}. When no plugin sits
        at that location: {"valid": false, "params": []} (not an error).

        Args:
            index: Channel index (global) or mixer track index
            slot_index: Effect slot index for mixer plugins (-1 for channel rack)
            use_global_index: Whether to use global channel indexing
            max_params: Maximum number of parameters to return (default 50)
        """
        result = run(get_connection(), "plugins.getParams", {
            "index": index,
            "slot_index": slot_index,
            "use_global": use_global_index,
            "max_params": max_params,
        })

        keys = ("valid", "name", "param_count", "preset_count", "color", "params")
        return {key: result[key] for key in keys if key in result}
```

`src/fl_studio_mcp/tools/patterns.py` : supprimer `fl_get_current_pattern` ; dans la docstring de `fl_list_patterns`, ajouter : « `current` is the index of the active pattern. »

`src/fl_studio_mcp/server.py` : supprimer `fl_connect` ; `fl_connection_status` devient :

```python
@mcp.tool()
def fl_connection_status(reconnect: bool = False) -> dict:
    """Get the FL Studio MIDI connection status.

    Args:
        reconnect: drop the current connection and connect again first - use
                   after starting FL Studio or when commands stopped arriving.
    """
    if reconnect:
        reset_connection()
    conn = get_connection()
    # Eagerly attempt a connection so the reported status reflects actual
    # reachability rather than the lazily-initialized flag.
    try:
        conn.ensure_connected()
    except RuntimeError:
        pass
    status = conn.get_status()
    return {
        "connected": status.get("connected", False),
        "port_name": status.get("port_name"),
        "available_ports": status.get("available_ports", []),
        "error": status.get("error"),
    }
```

Retirer l'import `ToolError` de `server.py` s'il n'est plus utilisé (ruff le signalera).

- [ ] **Step 6 : lancer toute la suite**

Run : `uv run pytest -q && uv run ruff check src fl_controller tests`
Expected : tout PASS. `grep -rnE "fl_connect\b|fl_get_channel_count|fl_get_grid_bit" src tests` → aucune occurrence.

- [ ] **Step 7 : commit**

```bash
git add fl_controller/device_FLStudioMCP.py src tests
git commit -m "refactor: supprimer 12 outils redondants, enrichir canaux, pistes mixer et plugins

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6 : annotations et test du registre

**Files :**
- Create : `src/fl_studio_mcp/tools/annotations.py`
- Modify : les dix modules qui déclarent des outils (`server.py`, `tools/*.py`) : chaque `@mcp.tool()` devient `@mcp.tool(annotations=...)`
- Test : `tests/test_tool_registry.py`

**Interfaces :**
- Produces : `READ_ONLY`, `IDEMPOTENT`, `DESTRUCTIVE`, `ACTION` (`mcp.types.ToolAnnotations`).

- [ ] **Step 1 : écrire le test**

`tests/test_tool_registry.py` :

```python
"""The real FastMCP server: exact tool set and one annotation class per tool."""

from __future__ import annotations

import asyncio

from fl_studio_mcp.server import mcp

READ_ONLY = {
    "fl_connection_status", "fl_get_transport_status", "fl_get_song_length", "fl_get_tempo",
    "fl_get_mixer_track_info", "fl_get_all_mixer_tracks", "fl_get_channel_info",
    "fl_get_all_channels", "fl_get_step_sequence", "fl_get_plugin_params",
    "fl_get_plugin_param_value", "fl_get_piano_roll_state", "fl_get_pr_context",
    "fl_get_piano_roll_info", "fl_list_patterns", "fl_get_undo_status",
    "fl_get_focused_window", "fl_get_project_snapshot",
}
IDEMPOTENT = {
    "fl_set_song_position", "fl_set_loop_mode", "fl_set_playback_speed", "fl_set_tempo",
    "fl_set_track_volume", "fl_set_track_pan", "fl_set_track_name", "fl_set_track_color",
    "fl_set_stereo_separation", "fl_set_channel_volume", "fl_set_channel_pan",
    "fl_set_channel_name", "fl_set_channel_color", "fl_route_channel_to_mixer",
    "fl_set_grid_bit", "fl_select_channel", "fl_set_plugin_param_value",
    "fl_select_pattern", "fl_rename_pattern", "fl_show_window", "fl_hide_window",
    "fl_focus_window", "fl_open_piano_roll",
}
DESTRUCTIVE = {
    "fl_clear_piano_roll", "fl_delete_notes", "fl_send_notes", "fl_set_step_sequence",
    "fl_undo", "fl_redo", "fl_clear_request_queue",
}
ACTION = {
    "fl_play", "fl_stop", "fl_record", "fl_trigger_note", "fl_send_chord",
    "fl_trigger_script", "fl_next_preset", "fl_prev_preset", "fl_new_pattern",
    "fl_clone_pattern", "fl_save_undo_point", "fl_mute_track", "fl_solo_track",
    "fl_arm_track", "fl_mute_channel", "fl_solo_channel",
}
REMOVED = {
    "fl_get_channel_count", "fl_get_selected_channel", "fl_select_one_channel",
    "fl_get_grid_bit", "fl_get_mixer_track_count", "fl_is_plugin_valid",
    "fl_get_plugin_name", "fl_get_plugin_param_count", "fl_get_preset_count",
    "fl_get_plugin_color", "fl_get_current_pattern", "fl_connect",
}


def registered():
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def hints(tool):
    a = tool.annotations
    return (a.read_only_hint, a.destructive_hint, a.idempotent_hint)


def test_exact_tool_set():
    names = set(registered())
    assert len(names) == 64
    assert names == READ_ONLY | IDEMPOTENT | DESTRUCTIVE | ACTION
    assert not names & REMOVED


def test_every_tool_is_annotated():
    missing = [name for name, tool in registered().items() if tool.annotations is None]
    assert missing == []


def test_annotation_classes():
    tools = registered()
    for name in READ_ONLY:
        assert hints(tools[name])[0] is True, name
    for name in IDEMPOTENT:
        assert hints(tools[name]) == (False, False, True), name
    for name in DESTRUCTIVE:
        assert hints(tools[name])[:2] == (False, True), name
    for name in ACTION:
        assert hints(tools[name]) == (False, False, False), name
```

- [ ] **Step 2 : lancer le test, il échoue**

Run : `uv run pytest tests/test_tool_registry.py -q`
Expected : `test_exact_tool_set` PASS (64 outils depuis la tâche 5), les deux autres FAIL (`annotations` à `None`).

- [ ] **Step 3 : implémenter**

`src/fl_studio_mcp/tools/annotations.py` :

```python
"""MCP behaviour hints shared by every tool.

Clients use them to auto-approve reads and to warn before destructive calls.
A setter taking an absolute value is idempotent; a toggle (mute/solo/arm
with no explicit value, play/stop) is an ACTION.
"""

from __future__ import annotations

from mcp.types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True)
IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
```

Dans chaque module, importer les constantes utilisées (`from fl_studio_mcp.tools.annotations import READ_ONLY, IDEMPOTENT, ...`) et remplacer chaque `@mcp.tool()` par `@mcp.tool(annotations=<CLASSE>)` selon les ensembles du test (source de vérité). `RecordingMCP.tool` accepte déjà `**kwargs`, les tests existants ne changent pas.

- [ ] **Step 4 : lancer toute la suite**

Run : `uv run pytest -q && uv run ruff check src fl_controller tests`
Expected : tout PASS, et `uv run python -W error::DeprecationWarning -c "import fl_studio_mcp.server"` sans avertissement (la construction en camelCase ne doit pas en émettre ; sinon passer aux noms `read_only_hint=` etc.).

- [ ] **Step 5 : commit**

```bash
git add src tests/test_tool_registry.py
git commit -m "feat: annotations MCP (lecture, idempotent, destructif, action) sur les 64 outils

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7 : instructions du serveur, README, vérifications manuelles

**Files :**
- Modify : `src/fl_studio_mcp/server.py` (docstring du module l. 1-17, `instructions=` l. 40-64)
- Modify : `README.md` (sections Features, Important Limitations, Available Tools)
- Modify : `docs/superpowers/specs/manual-checks.md`
- Test : `tests/test_tool_registry.py` (ajout)

- [ ] **Step 1 : test des instructions**

Ajouter à `tests/test_tool_registry.py` :

```python
def test_instructions_match_the_tool_set():
    text = mcp.instructions
    assert "Cannot create new patterns" not in text
    assert "C4 = 60" in text and "C5" in text
    for name in ("fl_get_project_snapshot", "fl_open_piano_roll", "fl_send_chord",
                 "fl_save_undo_point", "fl_connection_status"):
        assert name in text, name
    for name in REMOVED:
        assert name not in text, name
```

Run : `uv run pytest tests/test_tool_registry.py::test_instructions_match_the_tool_set -q` → FAIL.

- [ ] **Step 2 : réécrire les instructions et la docstring**

Docstring du module `server.py` :

```python
"""FL Studio MCP Server - Control FL Studio via Model Context Protocol.

Two channels to FL Studio:
1. MIDI + JSON files - transport, mixer, channels, plugins, patterns, undo,
   windows (controller script device_FLStudioMCP.py)
2. Piano roll script - persistent notes via JSON files + a trigger keystroke
   (ComposeWithLLM.pyscript)

Requirements: FL Studio 20.7+ running with the FLStudioMCP controller enabled
on a virtual MIDI port (IAC Driver on macOS, loopMIDI on Windows); both
scripts installed by scripts/setup.sh.
"""
```

`instructions=` :

```python
    instructions="""
FL Studio MCP Server - control a running FL Studio from an AI assistant.

Start with fl_get_project_snapshot: transport, tempo, patterns, channels and
mixer tracks in one call. If a tool reports no connection, call
fl_connection_status(reconnect=True).

Domains:
- Transport and tempo: play/stop/record, position, loop mode, speed, tempo
- Mixer: track volume, pan, mute, solo, arm, name, colour, stereo separation
- Channels: channel rack properties, mixer routing, step sequencer
  (fl_set_step_sequence / fl_set_grid_bit for persistent drum steps)
- Plugins: fl_get_plugin_params describes a plugin and its parameters;
  parameter values and preset navigation. Loading new plugins is impossible.
- Piano roll: fl_send_notes, fl_send_chord ("Am7", "Cmaj9/E"),
  fl_delete_notes, fl_clear_piano_roll, fl_get_piano_roll_state,
  fl_get_pr_context
- Patterns: list, select, create empty, clone, rename (FL Studio 2024+)
- Undo: fl_save_undo_point before a batch of edits, fl_undo, fl_redo
- Windows: show, hide, focus; fl_open_piano_roll(channel) targets a channel

Piano roll rules:
- A piano roll window must be open, and ComposeWithLLM must have been run
  once by hand in this FL Studio session (piano roll > Tools > Scripting).
- Call fl_open_piano_roll(channel) first to choose which channel the notes
  go to; the piano roll does not follow channel selection made by scripting.
- Times and durations are in quarter notes (1.0 = one beat in 4/4).
- Pitch names use C4 = 60. FL Studio's own UI labels MIDI 60 as "C5", so a
  note written as "A3" appears as "A4" in FL.
- The note tools verify what FL Studio applied and raise on any failure.

Limits:
- fl_trigger_note plays a note in real time only; it is not recorded unless
  FL Studio is recording.
- FL's undo does not track every scripting change (pattern renames).
""",
```

- [ ] **Step 3 : README**

Dans `README.md` :

1. « Important Limitations » : supprimer le paragraphe « Cannot Create Patterns » ; garder « Cannot Load Plugins ».
2. « Piano Roll Control » (Features) : remplacer par

```markdown
### Piano Roll Control

- **Add notes** by MIDI number or name (`"C4"`, `"F#3"`, `"Bb2"` — C4 = 60; FL Studio's UI shows MIDI 60 as "C5")
- **Add chords** by symbol (`"Am7"`, `"Cmaj9/E"`, `"F#m7b5"`) with octave and inversion, or as a list of notes
- Per-note velocity, pan, colour group, slide and portamento
- **Delete specific notes**, **clear** the piano roll, **read** its state
- Every write is verified: the tool reports how many notes FL Studio applied and fails loudly otherwise
- Auto-triggering via keystroke (Cmd+Opt+Y on macOS, Ctrl+Alt+Y on Windows/Linux)
```

3. « Available Tools » : dans chaque tableau, retirer les 12 outils supprimés ; puis
   - Connection : `| fl_connection_status | Connection status; reconnect=True to reconnect |`
   - Channels : `| fl_get_all_channels | List channels (volume, pan, mute, solo, selection, mixer target) |` et `| fl_select_channel | Select/deselect a channel; exclusive=True deselects the others |`
   - Mixer : `| fl_get_all_mixer_tracks | Track count and used tracks |`
   - Plugins : `| fl_get_plugin_params | Plugin name, validity, presets, colour and parameters |`
   - Patterns : `| fl_list_patterns | List patterns and the active one |`
4. Ajouter après les tableaux :

```markdown
### Removed in 0.3.0

| Removed | Use instead |
|---|---|
| `fl_connect` | `fl_connection_status(reconnect=True)` |
| `fl_get_channel_count`, `fl_get_selected_channel` | `fl_get_all_channels` |
| `fl_select_one_channel` | `fl_select_channel(index, exclusive=True)` |
| `fl_get_grid_bit` | `fl_get_step_sequence` |
| `fl_get_mixer_track_count` | `fl_get_all_mixer_tracks` (`track_count`) |
| `fl_is_plugin_valid`, `fl_get_plugin_name`, `fl_get_plugin_param_count`, `fl_get_preset_count`, `fl_get_plugin_color` | `fl_get_plugin_params` |
| `fl_get_current_pattern` | `fl_list_patterns` (`current`) |

`fl_send_chord` now takes `chord` (symbol or list) instead of `midi_notes`.
After updating, rerun `scripts/setup.sh` so FL Studio gets the new controller
and ComposeWithLLM scripts.
```

5. `pyproject.toml` et `src/fl_studio_mcp/__init__.py` : version `0.3.0` (l'interface change).

- [ ] **Step 4 : vérifications manuelles**

Ajouter à `docs/superpowers/specs/manual-checks.md` :

```markdown
7. Après `scripts/setup.sh`, piano roll ouvert : `fl_send_chord("Am7", octave=3)` → FL affiche A4, C5, E5, G5 (son étiquetage ; A3–G4 en convention C4 = 60) ; la réponse dit « 4 note(s) written ».
8. `fl_send_notes([{"note": "C4", "duration": 1, "slide": true}])` → la note apparaît en slide.
9. Recopier l'ancien `ComposeWithLLM.pyscript` (`git show main:scripts/ComposeWithLLM.pyscript`), le relancer une fois, refaire 8 → erreur « outdated … without pan/color/slide/porta ».
10. `fl_get_plugin_params` sur un canal vide → `{"valid": false, "params": []}`, pas d'erreur.
```

- [ ] **Step 5 : lancer toute la suite**

Run : `uv run pytest -q && uv run ruff check src fl_controller tests`
Expected : tout PASS.

- [ ] **Step 6 : commit**

```bash
git add src/fl_studio_mcp/server.py src/fl_studio_mcp/__init__.py pyproject.toml README.md docs/superpowers/specs/manual-checks.md tests/test_tool_registry.py
git commit -m "docs: instructions du serveur et README alignés sur les 64 outils ; version 0.3.0

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```
