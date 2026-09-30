# Fiabilisation de fl-studio-mcp — plan d'implémentation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rendre le pont MIDI + fichiers déterministe (identifiant, verrou, port sûr), rendre le piano roll honnête sur macOS, ajouter tempo et snapshot, uniformiser les erreurs, tester sans FL Studio, corriger l'installateur. Tout reste dans le fork privé ; rien n'est envoyé au dépôt d'origine.

**Architecture:** Le serveur FastMCP (Python 3.12, `src/fl_studio_mcp`) écrit des JSON et envoie une note MIDI ; le contrôleur `fl_controller/device_FLStudioMCP.py` tourne dans le Python embarqué de FL Studio, exécute l'API et répond par fichier. Le piano roll passe par un raccourci clavier et `scripts/ComposeWithLLM.pyscript`. Les tests remplacent les modules FL par des `MagicMock` et le port MIDI par un faux port qui joue le rôle de FL Studio.

**Tech Stack:** Python 3.12, FastMCP 4, mido + python-rtmidi, pytest, ruff, uv, bash (installateur), API FL Studio (stubs `fl-studio-api-stubs` en dev).

**Spec:** `docs/superpowers/specs/2026-09-30-fiabilisation-design.md`

## Global Constraints

- Branche de travail : `fiabilisation` (base `main` du fork = upstream/main + PR #5). Dépôt privé : `git push` uniquement vers `origin`, jamais vers `upstream`.
- Python `>=3.10` (pyproject) ; venv de dev en 3.12 : `uv sync --python 3.12 --group dev`.
- Commandes de vérification : `uv run pytest -q` et `uv run ruff check src fl_controller tests` (ligne max 100).
- Côté FL Studio (contrôleur, pyscript) : jamais `os.replace`, jamais de fichier temporaire ; écriture en place, texte sérialisé avant l'ouverture du fichier.
- Variables d'environnement : `FL_STUDIO_MCP_MIDI_PORT`, `FL_STUDIO_MCP_SETTINGS_DIR` (noms de la PR #5, inchangés).
- Aucun repli sur un port MIDI qui ne contient pas `IAC`, `loopMIDI` ou `Midi Through`, sauf variable d'environnement.
- Les valeurs de retour des outils en cas de succès ne changent pas ; les échecs lèvent `fastmcp.exceptions.ToolError`.
- Messages et code en anglais (langue du dépôt) ; commits en français court, préfixes `feat:`, `fix:`, `test:`, `docs:`.
- Tous les commits se terminent par `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Ne pas modifier `install.ps1`, ni le format des requêtes piano roll.

## Review Focus

- Réponse tardive d'une commande précédente (délai dépassé puis FL répond) : elle doit être jetée, pas prise pour la réponse courante → test `test_response_for_another_request_is_ignored` (Task 3).
- Contrôleur ancien installé dans FL Studio (réponse sans `id`) : délai dépassé avec message « controller script is outdated », pas un délai muet → test `test_legacy_response_without_id_reports_outdated_controller` (Task 3).
- Un seul port MIDI, matériel (`Arturia KeyStep 32`) : refus explicite, jamais choisi → test `test_hardware_only_ports_are_refused` (Task 2).
- `fl_send_notes` avec FL Studio fermé : erreur « FL Studio is not running », aucun `osascript activate`, requête signalée comme restée en file → tests `test_activate_does_nothing_when_fl_not_running` et `test_run_reports_fl_not_running` (Task 5).
- `fl_set_tempo(128)` quand FL Studio n'applique pas la valeur (événement REC ignoré) : erreur avec les deux valeurs, pas un succès → test `test_set_tempo_raises_when_not_applied` (Task 6).

---

### Task 1 : faux modules FL Studio et chargement du contrôleur sous pytest

**Files:**
- Create: `tests/fakes.py`
- Create: `tests/test_controller.py`
- Modify: `pyproject.toml` (section `[tool.pytest.ini_options]`)

**Interfaces:**
- Consumes: `fl_controller/device_FLStudioMCP.py` (importe `channels, midi, mixer, plugins, transport` au chargement ; `patterns, general, ui` paresseusement ; `SCRIPT_DIR` calculé depuis `Path.home()`).
- Produces: `tests/fakes.py` avec `RecordingMCP`, `FakePort(on_send=None)`, `make_fl_modules() -> dict[str, MagicMock]`, `load_controller(monkeypatch, tmp_path, modules=None) -> ModuleType`, `make_connection(tmp_path, monkeypatch) -> MIDIConnection`.

- [ ] **Step 1: Écrire `tests/fakes.py`**

```python
"""Stand-ins for what the tests cannot have: FL Studio's embedded modules and a MIDI port.

`load_controller` imports fl_controller/device_FLStudioMCP.py with MagicMock
modules in place of FL's API, and points its files under a temp directory.
`make_connection` builds a MIDIConnection whose port is a FakePort: the
`on_send` callback plays FL Studio (reads mcp_command.json, writes
mcp_response.json) when the trigger note is sent.
"""

from __future__ import annotations

import importlib.util
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
```

- [ ] **Step 2: Écrire le premier test dans `tests/test_controller.py`**

```python
"""The controller script, executed under pytest with fake FL Studio modules."""

from __future__ import annotations

import json

import pytest

from tests.fakes import load_controller, make_fl_modules


@pytest.fixture
def fl(tmp_path, monkeypatch):
    modules = make_fl_modules()
    controller = load_controller(monkeypatch, tmp_path, modules)
    return controller, modules


def test_controller_dispatches_with_fake_modules(fl):
    controller, modules = fl
    modules["transport"].isPlaying.return_value = 1

    result = controller.dispatch_command("transport.getStatus", {})

    assert result["is_playing"] is True
    assert result["loop_mode"] == "pattern"


def test_execute_pending_command_writes_response_file(fl):
    controller, _ = fl
    controller.COMMAND_FILE.write_text(json.dumps({"action": "transport.getStatus", "params": {}}))

    controller.execute_pending_command()

    response = json.loads(controller.RESPONSE_FILE.read_text())
    assert response["success"] is True
    assert response["is_playing"] is False
```

- [ ] **Step 3: Déclarer `tests` comme package importable pour pytest**

Dans `pyproject.toml`, ajouter :

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

- [ ] **Step 4: Lancer les tests**

Run: `uv run pytest tests/test_controller.py -v`
Expected: 2 PASS (le contrôleur se charge, `SCRIPT_DIR` est sous `tmp_path`).

- [ ] **Step 5: Lint puis commit**

```bash
uv run ruff check tests
git add tests/fakes.py tests/test_controller.py pyproject.toml
git commit -m "test: faux modules FL Studio et chargement du contrôleur sous pytest

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2 : sélection stricte du port MIDI

**Files:**
- Modify: `src/fl_studio_mcp/utils/midi_connection.py` (imports ligne 16-21, `connect()` lignes 97-176)
- Create: `tests/test_port_selection.py`

**Interfaces:**
- Produces: `MIDI_PORT_ENV = "FL_STUDIO_MCP_MIDI_PORT"`, `PORT_HINTS = ("IAC", "loopMIDI", "Midi Through")`, `class PortSelectionError(RuntimeError)`, `select_output_port(available: list[str], requested: str | None) -> str`.

- [ ] **Step 1: Écrire les tests `tests/test_port_selection.py`**

```python
"""select_output_port never picks a hardware port by accident."""

from __future__ import annotations

import pytest

from fl_studio_mcp.utils.midi_connection import (
    MIDI_PORT_ENV,
    PortSelectionError,
    select_output_port,
)

HARDWARE = ["Arturia KeyStep 32", "LUMI Keys BLOCK", "FLkey 37 MIDI"]


def test_requested_exact_name_wins():
    assert select_output_port(["Gestionnaire IAC Bus 1", "loopMIDI Port"], "loopMIDI Port") == (
        "loopMIDI Port"
    )


def test_requested_substring_is_case_insensitive():
    assert select_output_port(["Gestionnaire IAC Bus 1"], "iac bus") == "Gestionnaire IAC Bus 1"


def test_requested_name_not_found_lists_ports():
    with pytest.raises(PortSelectionError) as excinfo:
        select_output_port(HARDWARE, "IAC Driver Bus 1")
    message = str(excinfo.value)
    assert MIDI_PORT_ENV in message
    assert "Arturia KeyStep 32" in message


@pytest.mark.parametrize(
    "ports, expected",
    [
        (["Arturia KeyStep 32", "Gestionnaire IAC Bus 1"], "Gestionnaire IAC Bus 1"),
        (["IAC Driver Bus 1", "IAC Driver Bus 2"], "IAC Driver Bus 1"),
        (["LUMI Keys BLOCK", "loopMIDI Port"], "loopMIDI Port"),
        (["Midi Through Port-0", "USB MIDI"], "Midi Through Port-0"),
    ],
)
def test_virtual_port_hints(ports, expected):
    assert select_output_port(ports, None) == expected


def test_hardware_only_ports_are_refused():
    with pytest.raises(PortSelectionError) as excinfo:
        select_output_port(HARDWARE, None)
    message = str(excinfo.value)
    assert "FLkey 37 MIDI" in message
    assert "IAC" in message and MIDI_PORT_ENV in message


def test_no_ports_at_all_is_refused():
    with pytest.raises(PortSelectionError):
        select_output_port([], None)
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_port_selection.py -q`
Expected: ImportError sur `PortSelectionError`.

- [ ] **Step 3: Implémenter dans `midi_connection.py`**

Après les imports, ajouter :

```python
MIDI_PORT_ENV = "FL_STUDIO_MCP_MIDI_PORT"

# Substrings identifying virtual (software) MIDI ports, in order of preference.
PORT_HINTS = ("IAC", "loopMIDI", "Midi Through")


class PortSelectionError(RuntimeError):
    """No MIDI output port could be chosen safely."""


def select_output_port(available: list[str], requested: str | None) -> str:
    """Pick the port to send trigger notes to, or raise PortSelectionError.

    Sending note 127 at full velocity to a hardware synth is not acceptable,
    so there is no fallback: the port must match `requested` (exact name,
    then case-insensitive substring) or contain one of PORT_HINTS.
    """
    listing = ", ".join(repr(p) for p in available) or "none"
    if requested:
        for port in available:
            if port == requested:
                return port
        lowered = requested.lower()
        for port in available:
            if lowered in port.lower():
                return port
        raise PortSelectionError(
            f"{MIDI_PORT_ENV}={requested!r} matches none of the MIDI output ports: {listing}."
        )
    for hint in PORT_HINTS:
        for port in available:
            if hint.lower() in port.lower():
                return port
    raise PortSelectionError(
        "No virtual MIDI port found; refusing to send trigger notes to another device. "
        f"Ports seen: {listing}. On macOS enable the IAC Driver in Audio MIDI Setup "
        "(Window > Show MIDI Studio, double-click IAC Driver, tick 'Device is online'); "
        "on Windows create a loopMIDI port; or set "
        f"{MIDI_PORT_ENV} to the name of the port FL Studio listens on."
    )
```

Dans `connect()`, remplacer tout le bloc depuis `if not output_ports:` (ligne 120) jusqu'à la fin du `else:` (ligne 166) par :

```python
        try:
            target_port = select_output_port(output_ports, os.environ.get(MIDI_PORT_ENV))
        except PortSelectionError as e:
            self._error = str(e)
            return False
```

Supprimer la variable `system = platform.system()` de `connect()` (plus utilisée là ; `platform` reste importé pour `get_fl_settings_base`).

- [ ] **Step 4: Tests et lint**

Run: `uv run pytest tests/test_port_selection.py -q && uv run ruff check src`
Expected: 9 PASS, lint propre.

- [ ] **Step 5: Commit**

```bash
git add src/fl_studio_mcp/utils/midi_connection.py tests/test_port_selection.py
git commit -m "fix: sélection stricte du port MIDI, plus de repli sur du matériel

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3 : identifiant de requête, verrou, écriture atomique serveur, écho côté contrôleur

**Files:**
- Modify: `src/fl_studio_mcp/utils/midi_connection.py` (`__init__`, `send_command`, `_wait_for_response`)
- Modify: `fl_controller/device_FLStudioMCP.py` (`execute_pending_command` lignes 88-115, `write_response` lignes 472-477)
- Modify: `tests/test_midi_connection_wait.py` (nouvelle signature de `_wait_for_response`)
- Create: `tests/test_request_id.py`, `tests/test_connection_lock.py`
- Modify: `tests/test_controller.py` (écho de l'id)

**Interfaces:**
- Consumes: `make_connection`, `FakePort` (Task 1).
- Produces: `MIDIConnection._lock: threading.Lock`, `send_command(action, params=None, timeout=2.0)` inchangé en surface, `_write_command(command: dict) -> None`, `_discard_response_file() -> None`, `_wait_for_response(request_id: str, timeout: float) -> dict`. Fichier de commande `{"id": str, "action": str, "params": dict}` ; réponse avec `"id"` recopié. Constante `OUTDATED_CONTROLLER_HINT`.

- [ ] **Step 1: Écrire `tests/test_request_id.py`**

```python
"""Each command carries an id; only the response with that id counts."""

from __future__ import annotations

import json
import os
import threading
import time

from fl_studio_mcp.utils import midi_connection
from tests.fakes import make_connection


def test_command_file_carries_id_and_is_written_atomically(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(midi_connection.os, "replace", spy)

    def fl_side():
        command = json.loads(conn._command_file.read_text())
        assert command["action"] == "transport.stop"
        assert len(command["id"]) == 32
        conn._response_file.write_text(json.dumps({"id": command["id"], "success": True}))

    conn._port.on_send = fl_side

    result = conn.send_command("transport.stop", timeout=1.0)

    assert result["success"] is True
    assert replaced == [(str(conn._command_file) + ".tmp", str(conn._command_file))]
    assert not (tmp_path / "Hardware" / "FLStudioMCP" / "mcp_command.json.tmp").exists()


def test_write_command_retries_replace_on_permission_error(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)
    attempts = []
    real_replace = os.replace

    def flaky(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("locked by reader")
        real_replace(src, dst)

    monkeypatch.setattr(midi_connection.os, "replace", flaky)

    conn._write_command({"id": "abc", "action": "x", "params": {}})

    assert len(attempts) == 3
    assert json.loads(conn._command_file.read_text())["id"] == "abc"


def test_response_for_another_request_is_ignored(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)

    def fl_side():
        command = json.loads(conn._command_file.read_text())
        conn._response_file.write_text(json.dumps({"id": "stale", "success": True, "stale": True}))

        def late():
            time.sleep(0.15)
            conn._response_file.write_text(
                json.dumps({"id": command["id"], "success": True, "fresh": True})
            )

        threading.Thread(target=late, daemon=True).start()

    conn._port.on_send = fl_side

    result = conn.send_command("transport.getStatus", timeout=2.0)

    assert result.get("fresh") is True
    assert "stale" not in result
    assert not conn._response_file.exists()


def test_legacy_response_without_id_reports_outdated_controller(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)
    conn._port.on_send = lambda: conn._response_file.write_text(json.dumps({"success": True}))

    result = conn.send_command("transport.getStatus", timeout=0.3)

    assert result["success"] is False
    assert "outdated" in result["error"]
    assert "device_FLStudioMCP.py" in result["error"]


def test_no_response_at_all_is_a_plain_timeout(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)

    result = conn.send_command("transport.getStatus", timeout=0.2)

    assert result["success"] is False
    assert "Timeout" in result["error"]
```

- [ ] **Step 2: Écrire `tests/test_connection_lock.py`**

```python
"""Concurrent tool calls must not overwrite each other's command file."""

from __future__ import annotations

import json
import threading
import time

from tests.fakes import make_connection


def test_concurrent_commands_do_not_interleave(tmp_path, monkeypatch):
    conn = make_connection(tmp_path, monkeypatch)
    violations = []

    def fl_side():
        first = json.loads(conn._command_file.read_text())
        time.sleep(0.1)  # FL Studio is "busy": the file must not change meanwhile
        again = json.loads(conn._command_file.read_text())
        if again["id"] != first["id"]:
            violations.append((first["action"], again["action"]))
        conn._response_file.write_text(
            json.dumps({"id": first["id"], "success": True, "action": first["action"]})
        )

    conn._port.on_send = fl_side
    results = {}

    def call(action):
        results[action] = conn.send_command(action, timeout=2.0)

    threads = [threading.Thread(target=call, args=(f"cmd{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert violations == []
    assert results["cmd0"]["action"] == "cmd0"
    assert results["cmd1"]["action"] == "cmd1"
```

- [ ] **Step 3: Adapter `tests/test_midi_connection_wait.py` à la nouvelle signature**

Remplacer les deux corps de test :

```python
def test_partial_response_is_polled_until_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))
    conn = MIDIConnection()
    full = json.dumps({"id": "req1", "success": True, "window": "piano_roll", "focused": True})
    conn._response_file.write_text(full[: len(full) // 2])  # FL is still writing

    def finish():
        time.sleep(0.15)
        conn._response_file.write_text(full)

    threading.Thread(target=finish, daemon=True).start()

    result = conn._wait_for_response("req1", timeout=2.0)

    assert result == json.loads(full)
    assert not conn._response_file.exists()


def test_never_completed_response_reports_the_parse_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))
    conn = MIDIConnection()
    conn._response_file.write_text('{"success": tr')

    result = conn._wait_for_response("req1", timeout=0.3)

    assert result["success"] is False
    assert "never became readable" in result["error"]
```

- [ ] **Step 4: Ajouter au contrôleur le test d'écho dans `tests/test_controller.py`**

```python
def test_execute_pending_command_echoes_request_id(fl):
    controller, _ = fl
    controller.COMMAND_FILE.write_text(
        json.dumps({"id": "req42", "action": "transport.getStatus", "params": {}})
    )

    controller.execute_pending_command()

    assert json.loads(controller.RESPONSE_FILE.read_text())["id"] == "req42"


def test_invalid_command_json_still_answers_with_null_id(fl):
    controller, _ = fl
    controller.COMMAND_FILE.write_text("{not json")

    controller.execute_pending_command()

    response = json.loads(controller.RESPONSE_FILE.read_text())
    assert response["success"] is False
    assert response["id"] is None
    assert "Invalid JSON" in response["error"]


def test_write_response_serialises_before_opening_the_file(fl):
    controller, _ = fl
    controller.RESPONSE_FILE.write_text('{"id": "keep-me", "success": true}')

    controller.write_response({"id": "x", "bad": object()})  # not JSON serialisable

    assert json.loads(controller.RESPONSE_FILE.read_text())["id"] == "keep-me"
```

- [ ] **Step 5: Vérifier l'échec**

Run: `uv run pytest tests/test_request_id.py tests/test_connection_lock.py tests/test_controller.py tests/test_midi_connection_wait.py -q`
Expected: échecs (pas d'`id`, pas de `.tmp`, `_wait_for_response` n'accepte pas deux arguments, `write_response` écrase le fichier).

- [ ] **Step 6: Implémenter côté serveur (`midi_connection.py`)**

Imports : ajouter `import threading` et `import uuid`.

Constante de module :

```python
OUTDATED_CONTROLLER_HINT = (
    "FL Studio answered without a request id: the FLStudioMCP controller script "
    "installed in FL Studio is outdated. Reinstall fl_controller/device_FLStudioMCP.py "
    "(run install.sh or scripts/setup.sh) and restart FL Studio."
)
```

Dans `__init__`, après `self._error = None` : `self._lock = threading.Lock()`.

Remplacer `send_command` et `_wait_for_response` (lignes 197-297) par :

```python
    def send_command(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        """Send a command to FL Studio and wait for its response.

        One command at a time: the whole write / trigger / wait cycle runs
        under a lock because FastMCP runs synchronous tools in a thread pool.

        Raises:
            RuntimeError: If no MIDI connection can be established.
        """
        with self._lock:
            return self._send_command_locked(action, params, timeout)

    def _send_command_locked(
        self, action: str, params: dict[str, Any] | None, timeout: float
    ) -> dict[str, Any]:
        self.ensure_connected()

        request_id = uuid.uuid4().hex
        command = {"id": request_id, "action": action, "params": params or {}}
        try:
            self._write_command(command)
        except Exception as e:
            return {"success": False, "error": f"Failed to write command file: {e}"}

        self._discard_response_file()

        try:
            import mido

            self._port.send(mido.Message("note_on", note=self.TRIGGER_NOTE, velocity=127))
        except Exception as e:
            return {"success": False, "error": f"Failed to send MIDI trigger: {e}"}

        return self._wait_for_response(request_id, timeout)

    def _write_command(self, command: dict[str, Any]) -> None:
        """Write the command file atomically (temp file + replace, retried on Windows)."""
        text = json.dumps(command, indent=2)
        tmp_file = self._command_file.with_name(self._command_file.name + ".tmp")
        tmp_file.write_text(text)
        for attempt in range(5):
            try:
                os.replace(tmp_file, self._command_file)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02)

    def _discard_response_file(self) -> None:
        try:
            self._response_file.unlink()
        except OSError:
            pass

    def _wait_for_response(self, request_id: str, timeout: float) -> dict[str, Any]:
        """Wait for the response carrying `request_id`.

        Responses with another id (a late answer to a timed-out command) are
        discarded. A response without any id comes from an outdated controller
        script; it is discarded too and reported at the deadline. Unparseable
        JSON means FL Studio is still writing: keep polling.
        """
        start_time = time.time()
        poll_interval = 0.02
        last_error: str | None = None
        saw_legacy_response = False

        while time.time() - start_time < timeout:
            if self._response_file.exists():
                try:
                    response = json.loads(self._response_file.read_text())
                except (json.JSONDecodeError, OSError) as e:
                    last_error = f"{type(e).__name__}: {e}"
                    time.sleep(poll_interval)
                    continue

                if isinstance(response, dict) and response.get("id") == request_id:
                    self._discard_response_file()
                    return response
                if not isinstance(response, dict) or "id" not in response:
                    saw_legacy_response = True
                self._discard_response_file()

            time.sleep(poll_interval)

        if saw_legacy_response:
            return {"success": False, "error": OUTDATED_CONTROLLER_HINT}
        if last_error:
            return {
                "success": False,
                "error": f"Response file never became readable within {timeout}s ({last_error})",
            }
        return {
            "success": False,
            "error": (
                f"Timeout waiting for FL Studio response after {timeout}s. "
                "Make sure FL Studio is running and the MCP controller is enabled "
                "in MIDI Settings."
            ),
        }
```

- [ ] **Step 7: Implémenter côté contrôleur (`device_FLStudioMCP.py`)**

Remplacer `execute_pending_command` par :

```python
def execute_pending_command():
    """Read command from JSON file, execute it, and write response tagged with its id."""
    response = {"id": None, "success": False, "error": None}

    try:
        if not COMMAND_FILE.exists():
            response["error"] = "No command file found"
            write_response(response)
            return

        command = json.loads(COMMAND_FILE.read_text())
        response["id"] = command.get("id")

        action = command.get("action", "")
        params = command.get("params", {})

        result = dispatch_command(action, params)
        # A handler that returns {"error": ...} has failed; do not mark it a success.
        response = {"id": command.get("id"), "success": not result.get("error"), **result}

    except json.JSONDecodeError as e:
        response["error"] = f"Invalid JSON in command file: {e}"
    except Exception as e:
        response["error"] = f"Error executing command: {e}"

    write_response(response)
```

Remplacer `write_response` par :

```python
def write_response(response: dict):
    """Write the response file in place.

    Never via a temp file + os.replace: in FL Studio 2026 os.replace returns
    NULL without setting an exception and leaves the embedded interpreter
    failing every later call. Serialise first so a bad value cannot truncate
    the file; the server only accepts a response carrying its own request id.
    """
    try:
        text = json.dumps(response, indent=2)
    except Exception as e:
        print(f"Error serialising response: {e}")
        return
    try:
        RESPONSE_FILE.write_text(text)
    except Exception as e:
        print(f"Error writing response: {e}")
```

- [ ] **Step 8: Tests complets et lint**

Run: `uv run pytest -q && uv run ruff check src fl_controller tests`
Expected: tous PASS (27 hérités + nouveaux), lint propre.

- [ ] **Step 9: Commit**

```bash
git add src/fl_studio_mcp/utils/midi_connection.py fl_controller/device_FLStudioMCP.py tests/
git commit -m "feat: identifiant de requête, verrou et écriture atomique côté serveur

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4 : erreurs uniformes via ToolError

**Files:**
- Create: `src/fl_studio_mcp/utils/errors.py`
- Create: `tests/test_errors.py`
- Modify: `src/fl_studio_mcp/tools/transport.py`, `mixer.py`, `channels.py`, `plugins.py`, `patterns.py`, `history.py`, `ui.py`, `piano_roll.py`, `src/fl_studio_mcp/server.py`
- Modify: `tests/test_pattern_and_history_validation.py`, `tests/test_ui_tools.py`

**Interfaces:**
- Produces: `errors.unwrap(result: dict) -> dict` (lève `ToolError`), `errors.run(conn, action: str, params: dict | None = None, timeout: float = 2.0) -> dict`.
- `patterns._index_error(index) -> str | None` et `ui._window_error(window) -> str | None` renvoient désormais le message **sans** préfixe `Error: `.

- [ ] **Step 1: Écrire `tests/test_errors.py`**

```python
"""Failures surface as ToolError, never as a return value that looks like a result."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.channels import register_channel_tools
from fl_studio_mcp.tools.transport import register_transport_tools
from fl_studio_mcp.utils.errors import run, unwrap
from tests.fakes import RecordingMCP


def test_unwrap_returns_successful_results_untouched():
    assert unwrap({"success": True, "count": 3}) == {"success": True, "count": 3}


@pytest.mark.parametrize(
    "result",
    [
        {"success": False, "error": "Timeout waiting for FL Studio"},
        {"error": "patterns module not available"},
        {"success": False, "error": None},
    ],
)
def test_unwrap_raises_on_failures(result):
    with pytest.raises(ToolError):
        unwrap(result)


def test_unwrap_message_is_the_controller_error():
    with pytest.raises(ToolError, match="Timeout waiting"):
        unwrap({"success": False, "error": "Timeout waiting for FL Studio"})


def test_run_converts_connection_errors():
    class Broken:
        def send_command(self, action, params=None, timeout=2.0):
            raise RuntimeError("No virtual MIDI port found")

    with pytest.raises(ToolError, match="No virtual MIDI port"):
        run(Broken(), "transport.stop")


def test_run_forwards_action_params_and_timeout():
    sent = []

    class Recorder:
        def send_command(self, action, params=None, timeout=2.0):
            sent.append((action, params, timeout))
            return {"success": True}

    run(Recorder(), "mixer.muteTrack", {"track": 1}, timeout=4.0)

    assert sent == [("mixer.muteTrack", {"track": 1}, 4.0)]


class FailingConnection:
    def send_command(self, action, params=None, timeout=2.0):
        return {"success": False, "error": f"boom on {action}"}


@pytest.fixture
def tools(monkeypatch):
    import fl_studio_mcp.utils.connection as connection

    monkeypatch.setattr(connection, "get_connection", lambda: FailingConnection())
    mcp = RecordingMCP()
    register_transport_tools(mcp)
    register_channel_tools(mcp)
    return mcp.tools


def test_string_returning_tool_raises(tools):
    with pytest.raises(ToolError, match="boom on transport.stop"):
        tools["fl_stop"]()


def test_int_returning_tool_raises_instead_of_minus_one(tools):
    with pytest.raises(ToolError, match="boom on channels.getCount"):
        tools["fl_get_channel_count"]()


def test_dict_returning_tool_raises(tools):
    with pytest.raises(ToolError, match="boom on transport.getStatus"):
        tools["fl_get_transport_status"]()
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_errors.py -q`
Expected: ImportError sur `fl_studio_mcp.utils.errors`.

- [ ] **Step 3: Écrire `src/fl_studio_mcp/utils/errors.py`**

```python
"""Turn controller failures into MCP tool errors.

The controller answers {"success": false, "error": "..."} (or {"error": ...}
from an older handler). Returning that as a tool result makes a failure look
like data; raising ToolError marks the MCP response as an error instead.
"""

from __future__ import annotations

from typing import Any

from fastmcp.exceptions import ToolError


def unwrap(result: dict[str, Any]) -> dict[str, Any]:
    """Return `result` if it is a success, else raise ToolError with its message."""
    if not isinstance(result, dict):
        raise ToolError(f"Unexpected response from FL Studio: {result!r}")
    failed = result.get("success") is False or (
        result.get("error") and not result.get("success")
    )
    if failed:
        raise ToolError(str(result.get("error") or "FL Studio reported a failure"))
    return result


def run(
    conn: Any,
    action: str,
    params: dict[str, Any] | None = None,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Send one command through `conn` and unwrap its response.

    Connection problems (no MIDI port, port not found) arrive as RuntimeError
    from the connection layer and become ToolError too.
    """
    try:
        result = conn.send_command(action, params, timeout)
    except RuntimeError as e:
        raise ToolError(str(e)) from e
    return unwrap(result)
```

- [ ] **Step 4: Migrer les outils, fichier par fichier**

Règle unique, appliquée à chaque outil : la paire

```python
        conn = get_connection()
        result = conn.send_command("<action>", {...})

        if not result.get("success", False) and "error" in result:
            return f"Error: {result['error']}"      # ou return {"error": ...}
                                                    # ou return [{"error": ...}]
                                                    # ou return -1
```

devient

```python
        result = run(get_connection(), "<action>", {...})
```

avec en tête de fichier `from fl_studio_mcp.utils.errors import run` (et `from fastmcp.exceptions import ToolError` là où une validation locale lève). Les validations locales `return "Error: ..."` deviennent `raise ToolError("...")`. Détail par fichier :

- `transport.py` : 8 outils, toutes les branches `if not result.get("success"...)`. `fl_set_loop_mode` : `return "Error: mode must be..."` → `raise ToolError("mode must be 'pattern' or 'song'")`. `fl_set_playback_speed` : idem pour la plage.
- `mixer.py` : toutes les branches ; `fl_get_mixer_track_count` renvoie `-1` en erreur → supprimer, `run` lève.
- `channels.py` : toutes les branches ; `fl_get_channel_count` (`return -1`) et `fl_get_all_channels` (`return [{"error": ...}]`) passent par `run`.
- `plugins.py` : toutes les branches.
- `patterns.py` : `_index_error` renvoie `f"pattern index must be a positive integer (1-based), got {index!r}"` ; les outils font `if error := _index_error(index): raise ToolError(error)` ; `fl_list_patterns` et `fl_get_current_pattern` renvoient `run(get_connection(), "patterns.getAll", {...})` ; les branches `if result.get("error"): return f"Error: ..."` disparaissent.
- `history.py` : `fl_save_undo_point` : flags inconnus → `raise ToolError(f"Unknown undo flag(s): {', '.join(unknown)}. Valid: {', '.join(UNDO_FLAG_NAMES)}")` ; tous les `conn.send_command` → `run`.
- `ui.py` : `_window_error` sans préfixe ; les outils lèvent ; `fl_open_piano_roll` : index négatif → `raise ToolError("channel index must be >= 0")` ; `send_command` → `run`.
- `piano_roll.py` :
  - `fl_send_notes` : `return "Error: No notes provided"` → `raise ToolError("No notes provided")` ; champs manquants → `raise ToolError(f"Note {i} missing 'midi' field")` etc.
  - `fl_send_chord`, `fl_delete_notes` : idem.
  - `fl_get_piano_roll_state` : `return {"error": ...}` → `raise ToolError(...)`.
  - `fl_trigger_script` : `return f"Error: {run['error']}"` → `raise ToolError(run["error"])` (renommer la variable locale `run` en `outcome` pour ne pas masquer `errors.run`).
  - `fl_get_pr_context` : les trois `return {"error": ...}` → `raise ToolError(...)` (le message du script : `f"Piano roll script error: {response.get('message')}"`).
  - `_get_trigger_info` devient :

```python
def _get_trigger_info(auto_trigger: bool) -> str:
    """Run the piano roll script (if requested); raise when FL Studio did not process it."""
    if not auto_trigger:
        return ""
    outcome = _run_pr_script()
    if not outcome["ran"]:
        raise ToolError(outcome["error"])
    response = outcome.get("response") or {}
    if response.get("status") == "error":
        raise ToolError(f"FL Studio ran the script but it reported an error: {response.get('message')}")
    return " " + _describe_run(outcome)
```

- `server.py` : `fl_connect` : `except RuntimeError as e: raise ToolError(f"Connection failed: {e}") from e`. `fl_connection_status` inchangé (diagnostic, ne lève jamais). Les deux ressources `fl://status` et `fl://project` inchangées.

- [ ] **Step 5: Adapter les tests hérités**

`tests/test_pattern_and_history_validation.py` :

```python
from fastmcp.exceptions import ToolError
...
def test_pattern_tools_reject_bad_index_before_talking_to_fl(tools):
    with pytest.raises(ToolError, match="positive integer"):
        tools["fl_select_pattern"](0)
    with pytest.raises(ToolError, match="positive integer"):
        tools["fl_rename_pattern"](-2, "Verse")
    with pytest.raises(ToolError, match="positive integer"):
        tools["fl_clone_pattern"](0)


def test_save_undo_point_rejects_unknown_flags(tools):
    with pytest.raises(ToolError) as excinfo:
        tools["fl_save_undo_point"](flags=["pr", "bogus"])

    message = str(excinfo.value)
    assert "bogus" in message
    for name in UNDO_FLAG_NAMES:
        assert name in message
```

`tests/test_ui_tools.py` :

```python
from fastmcp.exceptions import ToolError
...
def test_open_piano_roll_rejects_negative_channel(env):
    tools, conn = env

    with pytest.raises(ToolError):
        tools["fl_open_piano_roll"](-1)
    assert conn.sent == []


def test_window_tools_reject_unknown_window(env):
    tools, conn = env

    with pytest.raises(ToolError, match="mixr"):
        tools["fl_show_window"]("mixr")
    with pytest.raises(ToolError, match="timeline"):
        tools["fl_focus_window"]("timeline")
    assert conn.sent == []
```

- [ ] **Step 6: Tests complets et lint**

Run: `uv run pytest -q && uv run ruff check src fl_controller tests`
Expected: tous PASS. Vérification qu'aucune branche n'a survécu :

```bash
grep -rn 'return f"Error' src/fl_studio_mcp/ ; grep -rn 'return -1' src/fl_studio_mcp/ ; grep -rn 'return {"error"' src/fl_studio_mcp/tools/
```
Expected: aucune ligne.

- [ ] **Step 7: Commit**

```bash
git add src/fl_studio_mcp tests
git commit -m "feat: les échecs d'outils lèvent ToolError au lieu de renvoyer une valeur

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5 : piano roll — FL Studio doit tourner, verrou, message

**Files:**
- Modify: `src/fl_studio_mcp/utils/fl_trigger.py` (`_trigger_macos`, `_trigger_macos_pynput`, `_focus_fl_studio_windows`, `activate_window`)
- Modify: `src/fl_studio_mcp/tools/piano_roll.py` (`_run_pr_script`, outils de notes, `fl_trigger_script`, `fl_get_pr_context`)
- Modify: `tests/test_piano_roll_script_run.py` (`FakeTrigger.is_fl_running`)
- Create: `tests/test_trigger_not_running.py`, `tests/test_piano_roll_lock.py`

**Interfaces:**
- Produces: `FLStudioTrigger.is_fl_running() -> bool` ; constante `FL_BUNDLE_ID = "com.image-line.flstudio"` ; `piano_roll.PR_FL_NOT_RUNNING` ; `piano_roll._PR_LOCK: threading.Lock` ; `_run_pr_script` renvoie `{"triggered": False, "ran": False, "response": None, "error": PR_FL_NOT_RUNNING (+ PR_SCRIPT_QUEUED_HINT si file non vide)}` quand FL Studio ne tourne pas.

- [ ] **Step 1: Écrire `tests/test_trigger_not_running.py`**

```python
"""Never activate (and thereby launch) FL Studio when it is not running."""

from __future__ import annotations

import pytest

from fl_studio_mcp.tools import piano_roll
from fl_studio_mcp.utils import fl_trigger


@pytest.fixture
def mac_trigger(monkeypatch):
    monkeypatch.setattr(fl_trigger.platform, "system", lambda: "Darwin")
    return fl_trigger.FLStudioTrigger()


def forbid_subprocess(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError(f"subprocess.run must not be called: {args[0]}")

    monkeypatch.setattr(fl_trigger.subprocess, "run", boom)


def test_is_fl_running_asks_system_events_by_bundle_id(mac_trigger, monkeypatch):
    calls = []

    class Probe:
        stdout = "true\n"

    def fake_run(args, **kwargs):
        calls.append(args)
        return Probe()

    monkeypatch.setattr(fl_trigger.subprocess, "run", fake_run)

    assert mac_trigger.is_fl_running() is True
    assert calls[0][0] == "osascript"
    assert fl_trigger.FL_BUNDLE_ID in calls[0][-1]


def test_is_fl_running_false_when_system_events_says_so(mac_trigger, monkeypatch):
    class Probe:
        stdout = "false\n"

    monkeypatch.setattr(fl_trigger.subprocess, "run", lambda *a, **k: Probe())

    assert mac_trigger.is_fl_running() is False


def test_activate_does_nothing_when_fl_not_running(mac_trigger, monkeypatch):
    monkeypatch.setattr(mac_trigger, "is_fl_running", lambda: False)
    forbid_subprocess(monkeypatch)

    assert mac_trigger.activate_window() is False
    assert mac_trigger.trigger(delay=0) is False


def test_activate_uses_bundle_id_when_running(mac_trigger, monkeypatch):
    monkeypatch.setattr(mac_trigger, "is_fl_running", lambda: True)
    calls = []
    monkeypatch.setattr(fl_trigger.subprocess, "run", lambda args, **k: calls.append(args))

    assert mac_trigger.activate_window() is True
    assert f'application id "{fl_trigger.FL_BUNDLE_ID}"' in calls[0][-1]


def test_run_reports_fl_not_running(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))
    piano_roll._write_request({"action": "add_notes", "notes": [{"midi": 60, "duration": 1}]})

    class ClosedFL:
        is_supported = True
        platform = "Darwin"
        keystroke = "Cmd+Opt+Y"

        def is_fl_running(self):
            return False

        def activate_window(self):
            raise AssertionError("must not activate")

        def trigger(self, delay=0.0):
            raise AssertionError("must not send a keystroke")

    monkeypatch.setattr(piano_roll, "get_trigger", lambda: ClosedFL())

    outcome = piano_roll._run_pr_script(timeout=0.2)

    assert outcome["triggered"] is False
    assert outcome["ran"] is False
    assert outcome["error"].startswith(piano_roll.PR_FL_NOT_RUNNING)
    assert "still queued" in outcome["error"]
```

- [ ] **Step 2: Écrire `tests/test_piano_roll_lock.py`**

```python
"""Two concurrent note tools must not mix their batches in one request file."""

from __future__ import annotations

import json
import threading
import time

from fl_studio_mcp.tools.piano_roll import register_piano_roll_tools
from fl_studio_mcp.tools import piano_roll
from tests.fakes import RecordingMCP


def test_concurrent_send_notes_are_serialised(tmp_path, monkeypatch):
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path))
    scripts_dir = tmp_path / "Piano roll scripts"
    batches_seen = []

    class SlowFL:
        is_supported = True
        platform = "TestOS"
        keystroke = "Ctrl+Alt+Y"

        def is_fl_running(self):
            return True

        def activate_window(self):
            return True

        def trigger(self, delay=0.0):
            queue = json.loads((scripts_dir / "mcp_request.json").read_text())
            time.sleep(0.1)  # FL is busy: nobody may append to the queue meanwhile
            batches_seen.append(len(queue))
            (scripts_dir / "mcp_request.json").write_text("[]")
            (scripts_dir / "piano_roll_state.json").write_text(
                json.dumps({"ppq": 96, "noteCount": 1, "notes": []})
            )
            (scripts_dir / "mcp_response.json").write_text(
                json.dumps({"status": "success", "notes_added": len(queue), "notes_deleted": 0})
            )
            return True

    monkeypatch.setattr(piano_roll, "get_trigger", lambda: SlowFL())
    monkeypatch.setattr(piano_roll, "_focus_piano_roll", lambda: None)
    mcp = RecordingMCP()
    register_piano_roll_tools(mcp)
    send_notes = mcp.tools["fl_send_notes"]
    results = []

    def call(midi):
        results.append(send_notes([{"midi": midi, "duration": 1.0}]))

    threads = [threading.Thread(target=call, args=(m,)) for m in (60, 64)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert batches_seen == [1, 1]
    assert len(results) == 2
    assert all("1 note(s) added" in r for r in results)
```

- [ ] **Step 3: Ajouter `is_fl_running` au `FakeTrigger` de `tests/test_piano_roll_script_run.py`**

Dans la classe `FakeTrigger`, ajouter :

```python
    def is_fl_running(self):
        return True
```

- [ ] **Step 4: Vérifier l'échec**

Run: `uv run pytest tests/test_trigger_not_running.py tests/test_piano_roll_lock.py -q`
Expected: AttributeError `is_fl_running`, `FL_BUNDLE_ID` absent, `batches_seen == [2, 0]` ou similaire.

- [ ] **Step 5: Implémenter `fl_trigger.py`**

Constante de module après `TRIGGER_DELAY` :

```python
# Bundle identifier of FL Studio on macOS; the app bundle is named after the
# version ("FL Studio 2025.app") and its process is "OsxFL", so neither name
# is a reliable way to address it.
FL_BUNDLE_ID = "com.image-line.flstudio"
```

Nouvelle méthode dans `FLStudioTrigger` (avant `_trigger_macos`) :

```python
    def is_fl_running(self) -> bool:
        """True when an FL Studio process exists. Never launches it."""
        try:
            if self._system == "Darwin":
                probe = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        'tell application "System Events" to (exists (processes whose '
                        f'bundle identifier is "{FL_BUNDLE_ID}"))',
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                return probe.stdout.strip() == "true"
            if self._system == "Windows":
                return self._find_fl_window_windows() is not None
            if self._system == "Linux":
                return bool(self._find_fl_windows_linux())
        except Exception:
            return False
        return False
```

`_trigger_macos` : première ligne du corps `if not self.is_fl_running(): return False` ; dans le script AppleScript remplacer `tell application "FL Studio"` par `tell application id "com.image-line.flstudio"` (utiliser la f-string avec `FL_BUNDLE_ID`). `_trigger_macos_pynput` : même remplacement dans l'appel `osascript` d'activation.

`activate_window`, branche Darwin :

```python
            if self._system == "Darwin":
                if not self.is_fl_running():
                    return False
                subprocess.run(
                    ["osascript", "-e", f'tell application id "{FL_BUNDLE_ID}" to activate'],
                    capture_output=True,
                    timeout=5,
                )
                return True
```

Windows : extraire de `_focus_fl_studio_windows` la recherche de fenêtre dans une méthode `_find_fl_window_windows(self)` qui renvoie `hwnd` ou `None` (même corps jusqu'à `hwnd = found["hwnd"]`, puis `return hwnd or None`), et faire commencer `_focus_fl_studio_windows` par `hwnd = self._find_fl_window_windows(); if hwnd is None: return False` avant `SW_RESTORE`.

- [ ] **Step 6: Implémenter `piano_roll.py`**

Imports : `import threading` ; `from fastmcp.exceptions import ToolError` (déjà là depuis Task 4).

Constantes après `PR_SCRIPT_QUEUED_HINT` :

```python
PR_FL_NOT_RUNNING = (
    "FL Studio is not running. Start it, open a piano roll and run ComposeWithLLM once "
    "from the piano roll's Tools > Scripting menu, then retry."
)

# Serialises the whole note round-trip (queue write, focus, keystroke, wait).
_PR_LOCK = threading.Lock()
```

Dans `_run_pr_script`, juste après le bloc `if not trigger.is_supported:` :

```python
    if not trigger.is_fl_running():
        error = PR_FL_NOT_RUNNING
        if _load_request_queue():
            error += PR_SCRIPT_QUEUED_HINT
        return {"triggered": False, "ran": False, "response": None, "error": error}
```

Dans chaque outil de notes (`fl_send_notes`, `fl_send_chord`, `fl_delete_notes`, `fl_clear_piano_roll`), envelopper l'écriture et le déclenchement :

```python
        with _PR_LOCK:
            _write_request(requests)
            trigger_info = _get_trigger_info(auto_trigger)
```

`fl_trigger_script` : `with _PR_LOCK: outcome = _run_pr_script()`. `fl_get_pr_context` : `with _PR_LOCK:` autour de `_write_request(...)` + `_run_pr_script()` + `_remove_queued_actions(...)`.

- [ ] **Step 7: Tests complets et lint**

Run: `uv run pytest -q && uv run ruff check src fl_controller tests`
Expected: tous PASS.

- [ ] **Step 8: Commit**

```bash
git add src/fl_studio_mcp/utils/fl_trigger.py src/fl_studio_mcp/tools/piano_roll.py tests/
git commit -m "fix: piano roll — refuser d'activer FL Studio fermé, verrou sur le cycle de notes

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6 : tempo

**Files:**
- Modify: `fl_controller/device_FLStudioMCP.py` (dispatch + deux handlers dans la section Transport)
- Modify: `src/fl_studio_mcp/tools/transport.py`
- Modify: `tests/test_controller.py`
- Create: `tests/test_tempo_tools.py`

**Interfaces:**
- Produces: actions `transport.getTempo` → `{"tempo": float}` ; `transport.setTempo` params `{"bpm": float}` → `{"requested", "tempo", "applied"}`. Outils `fl_get_tempo() -> dict`, `fl_set_tempo(bpm: float) -> dict`. Constantes `TEMPO_MIN = 10.0`, `TEMPO_MAX = 522.0` dans `transport.py` (serveur) et dans le contrôleur.

- [ ] **Step 1: Tests contrôleur (ajout à `tests/test_controller.py`)**

```python
def test_get_tempo_reads_mixer(fl):
    controller, modules = fl
    modules["mixer"].getCurrentTempo.return_value = 133.5

    assert controller.dispatch_command("transport.getTempo", {}) == {"tempo": 133.5}


def test_set_tempo_uses_rec_event_and_reads_back(fl):
    controller, modules = fl
    midi = modules["midi"]
    modules["mixer"].getCurrentTempo.return_value = 128.0

    result = controller.dispatch_command("transport.setTempo", {"bpm": 128})

    modules["general"].processRECEvent.assert_called_once_with(
        midi.REC_Tempo, 128000, midi.REC_Control | midi.REC_UpdateValue | midi.REC_UpdateControl
    )
    assert result == {"requested": 128.0, "tempo": 128.0, "applied": True}


def test_set_tempo_reports_when_fl_did_not_apply(fl):
    controller, modules = fl
    modules["mixer"].getCurrentTempo.return_value = 120.0

    result = controller.dispatch_command("transport.setTempo", {"bpm": 140})

    assert result["applied"] is False
    assert result["tempo"] == 120.0


@pytest.mark.parametrize("bpm", [9.9, 522.1, 0])
def test_set_tempo_rejects_out_of_range(fl, bpm):
    controller, modules = fl

    result = controller.dispatch_command("transport.setTempo", {"bpm": bpm})

    assert "out of range" in result["error"]
    modules["general"].processRECEvent.assert_not_called()
```

- [ ] **Step 2: Tests serveur `tests/test_tempo_tools.py`**

```python
"""fl_get_tempo / fl_set_tempo validate locally and trust only the read-back value."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.transport import register_transport_tools
from tests.fakes import RecordingMCP


class FakeConnection:
    def __init__(self, applied=True, tempo=128.0):
        self.sent = []
        self.applied = applied
        self.tempo = tempo

    def send_command(self, action, params=None, timeout=2.0):
        self.sent.append((action, params))
        if action == "transport.getTempo":
            return {"success": True, "tempo": self.tempo}
        if action == "transport.setTempo":
            return {
                "success": True,
                "requested": params["bpm"],
                "tempo": self.tempo,
                "applied": self.applied,
            }
        raise AssertionError(action)


def make_tools(monkeypatch, conn):
    import fl_studio_mcp.utils.connection as connection

    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    mcp = RecordingMCP()
    register_transport_tools(mcp)
    return mcp.tools


def test_get_tempo(monkeypatch):
    tools = make_tools(monkeypatch, FakeConnection(tempo=95.0))

    assert tools["fl_get_tempo"]() == {"tempo": 95.0}


def test_set_tempo_sends_float_bpm(monkeypatch):
    conn = FakeConnection()
    tools = make_tools(monkeypatch, conn)

    assert tools["fl_set_tempo"](128) == {"tempo": 128.0}
    assert conn.sent == [("transport.setTempo", {"bpm": 128.0})]


@pytest.mark.parametrize("bpm", [5, 600, -1])
def test_set_tempo_rejects_out_of_range_before_talking_to_fl(monkeypatch, bpm):
    conn = FakeConnection()
    tools = make_tools(monkeypatch, conn)

    with pytest.raises(ToolError, match="between 10 and 522"):
        tools["fl_set_tempo"](bpm)
    assert conn.sent == []


def test_set_tempo_raises_when_not_applied(monkeypatch):
    tools = make_tools(monkeypatch, FakeConnection(applied=False, tempo=120.0))

    with pytest.raises(ToolError) as excinfo:
        tools["fl_set_tempo"](140)
    message = str(excinfo.value)
    assert "140" in message and "120" in message
```

- [ ] **Step 3: Vérifier l'échec**

Run: `uv run pytest tests/test_controller.py tests/test_tempo_tools.py -q`
Expected: `Unknown action: transport.getTempo`, KeyError `fl_get_tempo`.

- [ ] **Step 4: Implémenter le contrôleur**

Dans `dispatch_command`, après `elif action == "transport.setPlaybackSpeed":` :

```python
    elif action == "transport.getTempo":
        return handle_transport_get_tempo()
    elif action == "transport.setTempo":
        return handle_transport_set_tempo(params)
```

Dans la section Transport Handlers, après `handle_transport_set_playback_speed` :

```python
TEMPO_MIN = 10.0
TEMPO_MAX = 522.0


def handle_transport_get_tempo() -> dict:
    """Current project tempo in BPM."""
    return {"tempo": mixer.getCurrentTempo()}


def handle_transport_set_tempo(params: dict) -> dict:
    """Set the project tempo through the REC event (the only scripting route).

    processRECEvent is documented as fragile, so the tempo is read back and
    `applied` tells the caller whether FL Studio really took the value.
    """
    bpm = float(params.get("bpm", 0))
    if not TEMPO_MIN <= bpm <= TEMPO_MAX:
        return {"error": "Tempo %g out of range (%g..%g BPM)" % (bpm, TEMPO_MIN, TEMPO_MAX)}
    general = _general_mod()
    general.processRECEvent(
        midi.REC_Tempo,
        int(round(bpm * 1000)),
        midi.REC_Control | midi.REC_UpdateValue | midi.REC_UpdateControl,
    )
    current = mixer.getCurrentTempo()
    return {"requested": bpm, "tempo": current, "applied": abs(current - bpm) < 0.01}
```

- [ ] **Step 5: Implémenter les outils (`transport.py`)**

Constantes de module : `TEMPO_MIN = 10.0`, `TEMPO_MAX = 522.0`. Dans `register_transport_tools`, après `fl_set_playback_speed` :

```python
    @mcp.tool()
    def fl_get_tempo() -> dict:
        """Get the project tempo in BPM."""
        result = run(get_connection(), "transport.getTempo")
        return {"tempo": result.get("tempo")}

    @mcp.tool()
    def fl_set_tempo(bpm: float) -> dict:
        """Set the project tempo in BPM (10 to 522).

        FL Studio only exposes tempo through an automation event, so the value
        is read back afterwards; the call fails if FL Studio did not apply it.
        """
        if isinstance(bpm, bool) or not isinstance(bpm, (int, float)):
            raise ToolError(f"Tempo must be a number between 10 and 522 BPM, got {bpm!r}")
        if not TEMPO_MIN <= bpm <= TEMPO_MAX:
            raise ToolError(f"Tempo must be between 10 and 522 BPM, got {bpm!r}")
        result = run(get_connection(), "transport.setTempo", {"bpm": float(bpm)})
        if not result.get("applied"):
            raise ToolError(
                f"Requested tempo {bpm:g} BPM but FL Studio reports {result.get('tempo')} BPM"
            )
        return {"tempo": result.get("tempo")}
```

Dans `server.py`, la ligne d'instructions `- Transport: Play, stop, record, tempo, position control` reste vraie ; ajouter dans la docstring de module de `server.py` rien de plus.

- [ ] **Step 6: Tests complets et lint**

Run: `uv run pytest -q && uv run ruff check src fl_controller tests`
Expected: tous PASS.

- [ ] **Step 7: Commit**

```bash
git add fl_controller/device_FLStudioMCP.py src/fl_studio_mcp/tools/transport.py tests/
git commit -m "feat: outils de tempo avec relecture après écriture

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7 : snapshot du projet

**Files:**
- Modify: `fl_controller/device_FLStudioMCP.py` (dispatch + `handle_project_snapshot`, `_snapshot_patterns`)
- Create: `src/fl_studio_mcp/tools/project.py`
- Modify: `src/fl_studio_mcp/tools/__init__.py`, `src/fl_studio_mcp/server.py`
- Modify: `tests/test_controller.py`
- Create: `tests/test_project_tools.py`

**Interfaces:**
- Produces: action `project.snapshot` → `{"transport": dict, "tempo": float, "patterns": {"current": int, "count": int, "items": list}, "channels": list, "mixer_tracks": list}` (une section en échec vaut `{"error": str}`). Outil `fl_get_project_snapshot() -> dict` ; `SNAPSHOT_TIMEOUT = 5.0` ; `register_project_tools(mcp)`.

- [ ] **Step 1: Tests contrôleur (ajout à `tests/test_controller.py`)**

```python
def test_snapshot_assembles_all_sections(fl):
    controller, modules = fl
    modules["mixer"].getCurrentTempo.return_value = 124.0

    snapshot = controller.dispatch_command("project.snapshot", {})

    assert snapshot["transport"]["is_playing"] is False
    assert snapshot["tempo"] == 124.0
    assert snapshot["patterns"] == {
        "current": 1,
        "count": 1,
        "items": [
            {
                "index": 1,
                "name": "Pattern 1",
                "color": 0,
                "length_beats": 16,
                "is_default": False,
                "is_selected": False,
            }
        ],
    }
    assert [c["name"] for c in snapshot["channels"]] == ["Kick", "Bass"]
    assert [t["name"] for t in snapshot["mixer_tracks"]] == ["Master", "Drums"]


def test_snapshot_isolates_a_failing_section(fl):
    controller, modules = fl
    modules["patterns"].patternMax.side_effect = RuntimeError("patterns API missing")

    snapshot = controller.dispatch_command("project.snapshot", {})

    assert snapshot["patterns"] == {"error": "patterns API missing"}
    assert snapshot["tempo"] == 120.0
    assert len(snapshot["channels"]) == 2
```

- [ ] **Step 2: Tests serveur `tests/test_project_tools.py`**

```python
"""fl_get_project_snapshot: one round-trip, longer timeout, sections passed through."""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.tools.project import SNAPSHOT_TIMEOUT, register_project_tools
from tests.fakes import RecordingMCP

SNAPSHOT = {
    "transport": {"is_playing": False},
    "tempo": 120.0,
    "patterns": {"current": 1, "count": 1, "items": []},
    "channels": [],
    "mixer_tracks": [],
}


class FakeConnection:
    def __init__(self, response):
        self.sent = []
        self.response = response

    def send_command(self, action, params=None, timeout=2.0):
        self.sent.append((action, params, timeout))
        return self.response


def make_tool(monkeypatch, conn):
    import fl_studio_mcp.utils.connection as connection

    monkeypatch.setattr(connection, "get_connection", lambda: conn)
    mcp = RecordingMCP()
    register_project_tools(mcp)
    return mcp.tools["fl_get_project_snapshot"]


def test_snapshot_is_one_command_with_longer_timeout(monkeypatch):
    conn = FakeConnection({"success": True, **SNAPSHOT})
    tool = make_tool(monkeypatch, conn)

    assert tool() == SNAPSHOT
    assert conn.sent == [("project.snapshot", None, SNAPSHOT_TIMEOUT)]
    assert SNAPSHOT_TIMEOUT > 2.0


def test_snapshot_failure_raises(monkeypatch):
    tool = make_tool(monkeypatch, FakeConnection({"success": False, "error": "Timeout"}))

    with pytest.raises(ToolError, match="Timeout"):
        tool()
```

- [ ] **Step 3: Vérifier l'échec**

Run: `uv run pytest tests/test_controller.py tests/test_project_tools.py -q`
Expected: `Unknown action: project.snapshot`, ModuleNotFoundError `fl_studio_mcp.tools.project`.

- [ ] **Step 4: Implémenter le contrôleur**

Dispatch, avant le `else:` final :

```python
    elif action == "project.snapshot":
        return handle_project_snapshot(params)
```

Nouvelle section avant `write_response` :

```python
# =============================================================================
# Project snapshot (one round-trip overview)
# =============================================================================


def _snapshot_patterns() -> dict:
    listing = handle_patterns_get_all({"include_default": False})
    if listing.get("error"):
        raise RuntimeError(listing["error"])
    counts = handle_patterns_get_count({})
    if counts.get("error"):
        raise RuntimeError(counts["error"])
    return {"current": listing["current"], "count": counts["count"], "items": listing["patterns"]}


def handle_project_snapshot(params: dict) -> dict:
    """Transport, tempo, patterns, channels and mixer tracks in one response.

    Each section is computed on its own; one failing section becomes
    {"error": ...} without hiding the others.
    """

    def section(compute):
        try:
            return compute()
        except Exception as e:
            return {"error": str(e)}

    return {
        "transport": section(handle_transport_get_status),
        "tempo": section(mixer.getCurrentTempo),
        "patterns": section(_snapshot_patterns),
        "channels": section(lambda: handle_channels_get_all()["channels"]),
        "mixer_tracks": section(
            lambda: handle_mixer_get_all_tracks({"include_empty": False})["tracks"]
        ),
    }
```

- [ ] **Step 5: Implémenter `src/fl_studio_mcp/tools/project.py`**

```python
"""Project overview tool: everything an assistant needs before acting, in one call."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fl_studio_mcp.utils.errors import run

if TYPE_CHECKING:
    from fastmcp import FastMCP

# The controller walks every pattern, channel and mixer track: allow more than
# the 2 s used for single commands.
SNAPSHOT_TIMEOUT = 5.0

SECTIONS = ("transport", "tempo", "patterns", "channels", "mixer_tracks")


def register_project_tools(mcp: FastMCP) -> None:
    """Register project-level tools with the MCP server."""
    from fl_studio_mcp.utils.connection import get_connection

    @mcp.tool()
    def fl_get_project_snapshot() -> dict:
        """Get a one-call overview of the open project.

        Returns transport state, tempo, the current pattern and the list of
        used patterns, all channels (name, mute, mixer target) and the mixer
        tracks in use (name, volume, pan, mute, solo). A section FL Studio
        could not read comes back as {"error": ...} while the others stay
        valid. The piano roll is not included: use fl_get_pr_context.
        """
        result = run(get_connection(), "project.snapshot", timeout=SNAPSHOT_TIMEOUT)
        return {key: result[key] for key in SECTIONS if key in result}
```

`tools/__init__.py` : importer et exporter `register_project_tools`. `server.py` : importer et appeler `register_project_tools(mcp)` après `register_ui_tools(mcp)` ; ajouter `- Project: fl_get_project_snapshot for a one-call overview` dans les instructions.

- [ ] **Step 6: Tests complets et lint**

Run: `uv run pytest -q && uv run ruff check src fl_controller tests`
Expected: tous PASS.

- [ ] **Step 7: Commit**

```bash
git add fl_controller/device_FLStudioMCP.py src/fl_studio_mcp tests/
git commit -m "feat: fl_get_project_snapshot, vue d'ensemble du projet en un appel

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8 : installateur, version, README

**Files:**
- Modify: `install.sh`, `scripts/setup.sh`, `scripts/install_mcp_for_claude.sh`
- Modify: `pyproject.toml` (version), `src/fl_studio_mcp/__init__.py`, `src/fl_studio_mcp/server.py`
- Modify: `README.md`
- Create: `tests/test_installer.py`, `docs/superpowers/specs/manual-checks.md`

**Interfaces:**
- `install.sh [--client desktop|code|both|none] [--yes]` ; exporte `FL_MCP_CLIENT`, `FL_MCP_YES=1`, `UV_BIN` vers les scripts.
- `scripts/install_mcp_for_claude.sh` lit `FL_MCP_CLIENT` (sinon menu), `UV_BIN` (sinon `command -v uv`), `PROJECT_DIR` calculé.
- `fl_studio_mcp.__version__ = "0.2.0"`.

- [ ] **Step 1: Écrire `tests/test_installer.py`**

```python
"""The Claude configuration script, run against fake `uv` and `claude` binaries."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "install_mcp_for_claude.sh"


def write_exec(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_exec(bindir / "uv", 'echo "fake uv $@"\n')
    log = tmp_path / "claude.log"
    write_exec(bindir / "claude", f'echo "$@" >> "{log}"\nexit 0\n')
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "FL_MCP_YES": "1",
    }
    return home, bindir, log, env


def desktop_config(home: Path) -> Path:
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def run_script(env, client):
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env={**env, "FL_MCP_CLIENT": client},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_desktop_config_uses_absolute_uv_path(sandbox):
    home, bindir, log, env = sandbox

    proc = run_script(env, "desktop")

    assert proc.returncode == 0, proc.stderr
    config = json.loads(desktop_config(home).read_text())
    assert config["mcpServers"]["fl-studio"]["command"] == str(bindir / "uv")
    assert config["mcpServers"]["fl-studio"]["args"] == [
        "run", "--directory", str(REPO), "fl-studio-mcp",
    ]
    assert not log.exists(), "Claude Code must not be touched for --client desktop"


def test_desktop_config_keeps_existing_keys(sandbox):
    home, bindir, log, env = sandbox
    path = desktop_config(home)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"preferences": {"theme": "dark"}, "mcpServers": {"other": {}}}))

    run_script(env, "desktop")

    config = json.loads(path.read_text())
    assert config["preferences"] == {"theme": "dark"}
    assert set(config["mcpServers"]) == {"other", "fl-studio"}


def test_claude_code_goes_through_claude_mcp_add(sandbox):
    home, bindir, log, env = sandbox

    proc = run_script(env, "code")

    assert proc.returncode == 0, proc.stderr
    assert log.read_text().strip() == (
        f"mcp add --scope user fl-studio -- {bindir / 'uv'} run --directory {REPO} fl-studio-mcp"
    )
    assert not (home / ".claude.json").exists()


def test_refused_claude_mcp_add_prints_manual_config(sandbox):
    home, bindir, log, env = sandbox
    write_exec(bindir / "claude", 'echo "not allowed by enterprise policy" >&2\nexit 1\n')

    proc = run_script(env, "code")

    assert proc.returncode == 0
    assert "enterprise policy" in proc.stdout + proc.stderr
    assert '"fl-studio"' in proc.stdout
    assert str(bindir / "uv") in proc.stdout


def test_missing_claude_binary_prints_manual_config(sandbox):
    home, bindir, log, env = sandbox
    (bindir / "claude").unlink()

    proc = run_script(env, "code")

    assert proc.returncode == 0
    assert "claude" in proc.stdout.lower() and '"fl-studio"' in proc.stdout


def test_client_none_changes_nothing(sandbox):
    home, bindir, log, env = sandbox

    proc = run_script(env, "none")

    assert proc.returncode == 0
    assert not desktop_config(home).exists()
    assert not log.exists()
```

- [ ] **Step 2: Vérifier l'échec**

Run: `uv run pytest tests/test_installer.py -q`
Expected: échecs (le script lit un menu interactif, `command` vaut `uv` nu, `~/.claude.json` écrit).

- [ ] **Step 3: Réécrire `scripts/install_mcp_for_claude.sh`**

```bash
#!/bin/bash
# Configure Claude Desktop and/or Claude Code to use the FL Studio MCP server.
#
# Environment (all optional, set by install.sh):
#   FL_MCP_CLIENT  desktop | code | both | none   (otherwise an interactive menu)
#   FL_MCP_YES     1 to skip every prompt
#   UV_BIN         absolute path to uv (otherwise resolved from PATH)

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo ""
echo -e "${BLUE}FL Studio MCP - Claude Configuration${NC}"
echo "======================================"
echo ""
echo -e "Project directory: ${CYAN}$PROJECT_DIR${NC}"

# GUI apps (Claude Desktop) do not inherit the shell PATH, so the config must
# carry uv's absolute path.
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "$UV_BIN" ]]; then
    echo -e "${YELLOW}Warning: uv is not installed.${NC} The MCP server needs it:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    UV_BIN="uv"
fi
echo -e "uv: ${CYAN}$UV_BIN${NC}"
echo ""

if [[ "$OSTYPE" == "darwin"* ]]; then
    CLAUDE_DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" || "$OSTYPE" == "cygwin" ]]; then
    CLAUDE_DESKTOP_CONFIG="$APPDATA/Claude/claude_desktop_config.json"
else
    CLAUDE_DESKTOP_CONFIG="$HOME/.config/Claude/claude_desktop_config.json"
fi

print_manual_config() {
    echo ""
    echo -e "${CYAN}Manual configuration${NC}"
    echo "Claude Desktop config file: $CLAUDE_DESKTOP_CONFIG"
    echo "Claude Code: claude mcp add --scope user fl-studio -- \"$UV_BIN\" run --directory \"$PROJECT_DIR\" fl-studio-mcp"
    echo ""
    echo "JSON to add under \"mcpServers\":"
    echo '{'
    echo '  "mcpServers": {'
    echo '    "fl-studio": {'
    echo "      \"command\": \"$UV_BIN\","
    echo "      \"args\": [\"run\", \"--directory\", \"$PROJECT_DIR\", \"fl-studio-mcp\"]"
    echo '    }'
    echo '  }'
    echo '}'
    echo ""
}

configure_desktop() {
    echo -e "Configuring ${CYAN}Claude Desktop${NC}..."
    mkdir -p "$(dirname "$CLAUDE_DESKTOP_CONFIG")"
    if [[ ! -f "$CLAUDE_DESKTOP_CONFIG" ]]; then
        echo '{}' > "$CLAUDE_DESKTOP_CONFIG"
    fi
    if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$CLAUDE_DESKTOP_CONFIG" 2>/dev/null; then
        echo -e "  ${YELLOW}Invalid JSON in config: backing it up and starting fresh.${NC}"
        cp "$CLAUDE_DESKTOP_CONFIG" "${CLAUDE_DESKTOP_CONFIG}.backup"
        echo '{}' > "$CLAUDE_DESKTOP_CONFIG"
    fi
    python3 - "$CLAUDE_DESKTOP_CONFIG" "$UV_BIN" "$PROJECT_DIR" <<'EOF'
import json, sys
path, uv_bin, project_dir = sys.argv[1:4]
with open(path) as f:
    config = json.load(f)
config.setdefault("mcpServers", {})["fl-studio"] = {
    "command": uv_bin,
    "args": ["run", "--directory", project_dir, "fl-studio-mcp"],
}
with open(path, "w") as f:
    json.dump(config, f, indent=2)
EOF
    echo -e "  ${GREEN}✓${NC} fl-studio added to $CLAUDE_DESKTOP_CONFIG"
}

configure_code() {
    echo -e "Configuring ${CYAN}Claude Code${NC}..."
    if ! command -v claude >/dev/null 2>&1; then
        echo -e "  ${YELLOW}The 'claude' command is not installed; add the server manually:${NC}"
        print_manual_config
        return 0
    fi
    if claude mcp add --scope user fl-studio -- "$UV_BIN" run --directory "$PROJECT_DIR" fl-studio-mcp; then
        echo -e "  ${GREEN}✓${NC} fl-studio registered in Claude Code (user scope)"
    else
        echo -e "  ${YELLOW}'claude mcp add' refused (an organisation policy can forbid local MCP servers).${NC}"
        print_manual_config
    fi
}

CHOICE="${FL_MCP_CLIENT:-}"
if [[ -z "$CHOICE" ]]; then
    if [[ "${FL_MCP_YES:-}" == "1" ]]; then
        CHOICE="both"
    else
        echo "Select which Claude client(s) to configure:"
        echo "  1) Claude Desktop only"
        echo "  2) Claude Code only"
        echo "  3) Both"
        echo "  4) Show manual configuration"
        echo "  5) Skip"
        read -p "Enter choice [1-5]: " menu
        case "$menu" in
            1) CHOICE="desktop" ;;
            2) CHOICE="code" ;;
            3) CHOICE="both" ;;
            4) print_manual_config; exit 0 ;;
            *) CHOICE="none" ;;
        esac
    fi
fi

case "$CHOICE" in
    desktop) configure_desktop ;;
    code) configure_code ;;
    both) configure_desktop; echo ""; configure_code ;;
    none) echo "Skipping Claude configuration." ;;
    *) echo -e "${RED}Unknown FL_MCP_CLIENT '$CHOICE' (desktop|code|both|none)${NC}"; exit 1 ;;
esac

echo ""
echo -e "${GREEN}Configuration complete.${NC} Restart Claude Desktop/Code to load the server."
```

- [ ] **Step 4: Adapter `install.sh`**

Après `SCRIPT_DIR=...` / `cd "$SCRIPT_DIR"`, ajouter le parsing :

```bash
FL_MCP_CLIENT=""
FL_MCP_YES=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --client) FL_MCP_CLIENT="$2"; shift 2 ;;
        --client=*) FL_MCP_CLIENT="${1#*=}"; shift ;;
        --yes|-y) FL_MCP_YES="1"; shift ;;
        -h|--help)
            echo "Usage: ./install.sh [--client desktop|code|both|none] [--yes]"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done
export FL_MCP_CLIENT FL_MCP_YES
```

Après l'étape 1 (uv installé), ajouter `UV_BIN="$(command -v uv)"; export UV_BIN`.

Étape 3, avant `bash "$SCRIPT_DIR/scripts/setup.sh"` : rien à changer (setup.sh lit `FL_MCP_YES`).

Étape 5 : inchangée (`bash "$SCRIPT_DIR/scripts/install_mcp_for_claude.sh"`).

- [ ] **Step 5: Adapter `scripts/setup.sh`**

Remplacer le bloc macOS de l'étape 1 (de `if system_profiler ...` jusqu'au second `read -p ... configured..."`) par :

```bash
    IAC_PORTS=""
    if command -v uv >/dev/null 2>&1; then
        IAC_PORTS="$(cd "$PROJECT_DIR" && uv run python -c 'import mido; print("\n".join(p for p in mido.get_output_names() if "IAC" in p))' 2>/dev/null || true)"
    fi
    if [[ -n "$IAC_PORTS" ]]; then
        echo -e "${GREEN}✓ IAC bus online:${NC} $IAC_PORTS"
    else
        echo -e "${YELLOW}⚠ No IAC bus is online (the bus name is localised, e.g. 'Gestionnaire IAC Bus 1' in French).${NC}"
        echo ""
        echo "To enable it:"
        echo "  1. Open 'Audio MIDI Setup' (search in Spotlight)"
        echo "  2. Window > Show MIDI Studio"
        echo "  3. Double-click 'IAC Driver' and tick 'Device is online'"
        echo ""
        if [[ "${FL_MCP_YES:-}" != "1" ]]; then
            read -p "Press Enter to open Audio MIDI Setup, or 's' to skip: " choice
            if [[ "$choice" != "s" && "$choice" != "S" ]]; then
                open -a "Audio MIDI Setup"
                read -p "Press Enter when the IAC Driver is online..."
            fi
        fi
    fi
```

Attention à l'ordre : ce contrôle a besoin de `uv sync` ; déplacer l'étape 4 (dépendances Python) **avant** l'étape 1 dans `setup.sh`, en gardant les numéros affichés cohérents (renuméroter 1..4 : dépendances, MIDI virtuel, contrôleur, piano roll). La branche Windows garde son `read -p` sous la même garde `FL_MCP_YES`.

- [ ] **Step 6: Version**

`pyproject.toml` : `version = "0.2.0"`. `src/fl_studio_mcp/__init__.py` :

```python
"""FL Studio MCP server."""

__version__ = "0.2.0"
```

`server.py` : `from fl_studio_mcp import __version__` et `mcp = FastMCP(name="fl-studio-mcp", version=__version__, instructions=...)`. Vérifier : `uv run python -c "from fl_studio_mcp.server import mcp; print(mcp.version)"` affiche `0.2.0`.

- [ ] **Step 7: README**

Ajouter après « Configure Claude » une section :

```markdown
### Configuration (environment variables)

| Variable | Purpose |
|---|---|
| `FL_STUDIO_MCP_MIDI_PORT` | Name (or case-insensitive substring) of the MIDI output port to use. Without it the server only accepts a port containing `IAC`, `loopMIDI` or `Midi Through`, and refuses to send anything to another device. |
| `FL_STUDIO_MCP_SETTINGS_DIR` | FL Studio's `Settings` folder when it is not the default (Options > File settings > User data folder). |

On a French macOS the IAC bus is called `Gestionnaire IAC Bus 1`; it contains `IAC`, so it is picked up automatically.

`./install.sh --client desktop|code|both|none --yes` runs without prompts. Claude Code is configured through `claude mcp add`; if your organisation forbids local MCP servers the script prints the configuration to apply by hand.
```

Dans « Available Tools » : ajouter `fl_get_tempo`, `fl_set_tempo` sous Transport et une sous-section « Project » avec `fl_get_project_snapshot`. Dans « Troubleshooting » : ajouter « *FL Studio is not running* : the piano roll tools refuse to launch FL Studio; start it yourself » et « *controller script is outdated* : re-run `scripts/setup.sh` and restart FL Studio ». Dans « How It Works » : « Every command carries an id; the server ignores any response that does not echo it. » Mentionner que les échecs d'outils arrivent comme erreurs MCP (`isError`), plus comme texte « Error: ». Mentionner que `install.ps1` n'a pas été mis à jour pour ces options.

- [ ] **Step 8: `docs/superpowers/specs/manual-checks.md`**

```markdown
# Vérifications manuelles (FL Studio réel)

Prérequis : FL Studio lancé, contrôleur FLStudioMCP activé sur le bus IAC, serveur enregistré dans Claude Desktop.

1. `fl_get_tempo` → renvoie le tempo affiché dans la barre de transport.
2. `fl_set_tempo(128)` → la barre de transport affiche 128.000, l'outil renvoie `{"tempo": 128.0}`.
3. `fl_set_tempo(5)` → erreur « between 10 and 522 », tempo inchangé.
4. Projet avec trois canaux et deux pistes mixer nommées : `fl_get_project_snapshot` → les trois canaux, les deux pistes plus Master, le pattern courant.
5. Quitter FL Studio, `fl_send_notes([{"midi": 60, "duration": 1}])` → erreur « FL Studio is not running », FL Studio ne se lance pas.
6. Relancer FL Studio, ouvrir un piano roll, lancer ComposeWithLLM une fois, puis `fl_trigger_script` → la note C4 en attente apparaît.
```

- [ ] **Step 9: Tests, syntaxe bash, lint**

Run: `uv run pytest -q && uv run ruff check src fl_controller tests && bash -n install.sh scripts/setup.sh scripts/install_mcp_for_claude.sh`
Expected: tous PASS, aucune erreur de syntaxe.

- [ ] **Step 10: Commit**

```bash
git add install.sh scripts/ pyproject.toml src/fl_studio_mcp/__init__.py src/fl_studio_mcp/server.py README.md tests/test_installer.py docs/superpowers/specs/manual-checks.md
git commit -m "fix(install): uv absolu, claude mcp add, détection IAC par ports MIDI, mode non interactif ; version 0.2.0

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9 : pousser le fork privé

**Files:** aucun fichier ; opérations git uniquement. Le dépôt `ThomasSatory/fl-studio-mcp` est privé et rien n'est poussé vers `upstream`.

- [ ] **Step 1: Vérifier que les remotes ne visent que le fork en écriture**

```bash
git remote -v
```
Expected: `origin` = `ThomasSatory/fl-studio-mcp` ; `upstream` = `karl-andres/fl-studio-mcp` (lecture seule par convention : jamais de `git push upstream`).

- [ ] **Step 2: Pousser les branches du fork**

```bash
git push origin main
git push -u origin fiabilisation
git branch -D pr9-joanj94
```

(`pr5-giang17` est conservée localement pour rebaser si l'auteur retouche sa PR.)

---

## Self-review

- **Couverture du spec** : §4.1 → Tasks 2 et 3 ; §4.2 → Task 3 ; §4.3 → Task 3 (test « outdated ») ; §5 → Task 5 ; §6.1 → Task 6 ; §6.2 → Task 7 ; §7 → Task 4 ; §8 → Tasks 1 à 8 (chaque fichier de test du tableau a une tâche) ; §9 → Task 8 ; §3 et §10 → Task 9 (push du fork uniquement). Le spec §8 mentionnait des faux `flpianoroll` pour charger `ComposeWithLLM.pyscript` : aucun test n'en a besoin (le pyscript n'est pas modifié), ils ne sont pas écrits ; le spec est corrigé en conséquence.
- **Placeholders** : aucun « TBD » ; chaque étape de code montre le code ; la migration de la Task 4 donne la règle et les cas particuliers par fichier.
- **Cohérence des noms** : `select_output_port`, `PortSelectionError`, `MIDI_PORT_ENV`, `PORT_HINTS` (Task 2) ; `_write_command`, `_discard_response_file`, `_wait_for_response(request_id, timeout)`, `OUTDATED_CONTROLLER_HINT` (Task 3) ; `unwrap`, `run` (Tasks 4, 6, 7) ; `is_fl_running`, `FL_BUNDLE_ID`, `_find_fl_window_windows`, `PR_FL_NOT_RUNNING`, `_PR_LOCK` (Task 5) ; `TEMPO_MIN/MAX`, `transport.getTempo/setTempo` (Task 6) ; `project.snapshot`, `SNAPSHOT_TIMEOUT`, `register_project_tools` (Task 7) ; `FL_MCP_CLIENT`, `FL_MCP_YES`, `UV_BIN` (Task 8) ; `RecordingMCP`, `FakePort`, `make_connection`, `load_controller`, `make_fl_modules` (Task 1, utilisés partout).
- **Review Focus** : les cinq lignes ont chacune leur test nommé dans la tâche propriétaire.
