"""Stand-ins for what the tests cannot have: FL Studio's embedded modules and a MIDI port.

`load_controller` imports fl_controller/device_FLStudioMCP.py with MagicMock
modules in place of FL's API, and points its files under a temp directory.
`make_connection` builds a MIDIConnection whose port is a FakePort: the
`on_send` callback plays FL Studio (reads mcp_command.json, writes
mcp_response.json) when the trigger note is sent.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable
from unittest.mock import MagicMock

from fl_studio_mcp.utils.midi_connection import MIDIConnection

CONTROLLER_PATH = Path(__file__).resolve().parents[1] / "fl_controller" / "device_FLStudioMCP.py"

FL_MODULES = ("channels", "midi", "mixer", "plugins", "transport", "patterns", "general", "ui")

# Integer constants the controller reads from the `midi` module.
MIDI_CONSTANTS = {
    "REC_Tempo": 1073741829,
    "REC_Control": 973,
    "REC_UpdateValue": 1,
    "REC_UpdateControl": 16,
    "PIM_None": 0,
    "widPianoRoll": 3,
}


class RecordingMCP:
    """Minimal stand-in for FastMCP: collects the functions passed to @mcp.tool()."""

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Callable:
        def decorator(fn: Callable) -> Callable:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


class FakePort:
    """Records sent MIDI messages; `on_send` lets a test act as FL Studio."""

    def __init__(self, on_send: Callable[[], None] | None = None) -> None:
        self.sent: list[Any] = []
        self.on_send = on_send

    def send(self, msg: Any) -> None:
        self.sent.append(msg)
        if self.on_send is not None:
            self.on_send()

    def close(self) -> None:
        pass


def make_fl_modules() -> dict[str, MagicMock]:
    """MagicMock modules with typed return values for the calls the controller makes."""
    mods = {name: MagicMock(name=name) for name in FL_MODULES}
    for key, value in MIDI_CONSTANTS.items():
        setattr(mods["midi"], key, value)

    transport = mods["transport"]
    transport.isPlaying.return_value = 0
    transport.isRecording.return_value = 0
    transport.getSongPosHint.return_value = "1:01:00"
    transport.getLoopMode.return_value = 0

    mixer = mods["mixer"]
    mixer.getCurrentTempo.return_value = 120.0
    mixer.trackCount.return_value = 3
    mixer.getTrackName.side_effect = lambda index, *_: ["Master", "Drums", ""][index]
    mixer.getTrackVolume.return_value = 0.8
    mixer.getTrackPan.return_value = 0.0
    mixer.isTrackMuted.return_value = 0
    mixer.isTrackSolo.return_value = 0

    channels = mods["channels"]
    channels.channelCount.return_value = 2
    channels.getChannelName.side_effect = lambda index, *_: ["Kick", "Bass"][index]
    channels.isChannelMuted.return_value = 0
    channels.isChannelSelected.return_value = 0
    channels.getTargetFxTrack.return_value = 1

    patterns = mods["patterns"]
    patterns.patternNumber.return_value = 1
    patterns.patternCount.return_value = 1
    patterns.patternMax.return_value = 4
    patterns.isPatternDefault.side_effect = lambda index: index != 1
    patterns.getPatternName.side_effect = lambda index: f"Pattern {index}"
    patterns.getPatternColor.return_value = 0
    patterns.getPatternLength.return_value = 16
    patterns.isPatternSelected.return_value = 0
    return mods


def load_controller(
    monkeypatch, tmp_path: Path, modules: dict[str, ModuleType] | None = None
) -> ModuleType:
    """Import the controller script with fake FL modules; its files land under tmp_path."""
    modules = modules or make_fl_modules()
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    spec = importlib.util.spec_from_file_location("fl_controller_under_test", CONTROLLER_PATH)
    assert spec is not None and spec.loader is not None
    controller = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(controller)
    controller.SCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    return controller


def make_connection(tmp_path: Path, monkeypatch) -> MIDIConnection:
    """A MIDIConnection that believes it is connected, with a FakePort and temp files."""
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))
    conn = MIDIConnection()
    conn._port = FakePort()
    conn._connected = True
    conn._port_name = "FakePort"
    return conn


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
