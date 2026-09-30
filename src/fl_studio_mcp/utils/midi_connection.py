"""MIDI-based connection to FL Studio.

This module provides communication with FL Studio via MIDI messages and JSON files.
The approach is:
1. Write command data to a JSON file
2. Send a MIDI trigger note to FL Studio
3. FL Studio's MIDI controller script executes the command
4. Read the response from a JSON file

This is similar to how the piano_roll module works, but uses MIDI for triggering
instead of keystrokes.
"""

from __future__ import annotations

import json
import os
import platform
import threading
import time
import uuid
from pathlib import Path
from typing import Any

MIDI_PORT_ENV = "FL_STUDIO_MCP_MIDI_PORT"

OUTDATED_CONTROLLER_HINT = (
    "FL Studio answered without a request id: the FLStudioMCP controller script "
    "installed in FL Studio is outdated. Reinstall fl_controller/device_FLStudioMCP.py "
    "(run install.sh or scripts/setup.sh) and restart FL Studio."
)

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


def get_fl_settings_base() -> Path:
    """Get the FL Studio Settings directory (shared base for Hardware/Piano roll)."""
    system = platform.system()

    env_dir = os.environ.get("FL_STUDIO_MCP_SETTINGS_DIR")
    if env_dir:
        return Path(env_dir)

    if system in ("Darwin", "Windows"):
        return Path.home() / "Documents" / "Image-Line" / "FL Studio" / "Settings"

    # Linux: resolve the settings dir used by FL Studio running under Wine.
    # ~/.wine/drive_c/users/<user>/Documents is typically a symlink to the XDG
    # documents folder (here: ~/Dokumente); FL writes its user data below it.
    candidates = []
    wine_users = Path.home() / ".wine" / "drive_c" / "users"
    if wine_users.is_dir():
        for user_dir in sorted(wine_users.iterdir()):
            docs = user_dir / "Documents"
            if docs.exists():
                candidates.append(docs / "Image-Line" / "FL Studio" / "Settings")
    candidates.append(Path.home() / "Documents" / "Image-Line" / "FL Studio" / "Settings")
    candidates.append(Path.home() / "Dokumente" / "Image-Line" / "FL Studio" / "Settings")

    for cand in candidates:
        if cand.is_dir():
            return cand

    # Nothing found yet (FL never ran): use the first candidate so files land
    # in a stable, documented location.
    return candidates[0]


def _get_fl_hardware_dir() -> Path:
    """Get the FL Studio Hardware scripts directory."""
    base = get_fl_settings_base()
    hardware_dir = base / "Hardware" / "FLStudioMCP"
    hardware_dir.mkdir(parents=True, exist_ok=True)
    return hardware_dir


class MIDIConnection:
    """MIDI-based connection to FL Studio.

    Communicates with FL Studio via:
    - JSON files for command/response data
    - MIDI trigger note to execute commands
    """

    # MIDI trigger note (same as in FL Studio controller script)
    TRIGGER_NOTE = 127

    def __init__(self) -> None:
        self._port = None
        self._port_name: str | None = None
        self._connected = False
        self._error: str | None = None
        # One command at a time: FastMCP runs synchronous tools in a thread pool.
        self._lock = threading.Lock()

        # File paths for JSON communication
        self._hardware_dir = _get_fl_hardware_dir()
        self._command_file = self._hardware_dir / "mcp_command.json"
        self._response_file = self._hardware_dir / "mcp_response.json"

    @property
    def is_connected(self) -> bool:
        """Check if connected to FL Studio."""
        return self._connected and self._port is not None

    @property
    def connection_error(self) -> str | None:
        """Get the last connection error, if any."""
        return self._error

    def connect(self) -> bool:
        """Attempt to connect to FL Studio via MIDI.

        Returns True if connection successful, False otherwise.
        """
        if self._connected and self._port is not None:
            return True

        try:
            import mido
        except ImportError:
            self._error = (
                "mido library not installed. Install with: pip install mido python-rtmidi"
            )
            return False

        # Find available MIDI output ports
        try:
            output_ports = mido.get_output_names()
        except Exception as e:
            self._error = f"Failed to get MIDI ports: {e}"
            return False

        try:
            target_port = select_output_port(output_ports, os.environ.get(MIDI_PORT_ENV))
        except PortSelectionError as e:
            self._error = str(e)
            return False

        try:
            self._port = mido.open_output(target_port)
            self._port_name = target_port
            self._connected = True
            self._error = None
            return True
        except Exception as e:
            self._error = f"Failed to open MIDI port '{target_port}': {e}"
            return False

    def disconnect(self) -> None:
        """Close the MIDI connection."""
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None
        self._connected = False
        self._port_name = None

    def ensure_connected(self) -> None:
        """Ensure connection to FL Studio is active. Raises RuntimeError if not."""
        if not self.is_connected:
            if not self.connect():
                raise RuntimeError(
                    self._error or "Failed to connect to FL Studio via MIDI"
                )

    def send_command(
        self,
        action: str,
        params: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> dict[str, Any]:
        """Send a command to FL Studio and wait for its response.

        One command at a time: the whole write / trigger / wait cycle runs
        under a lock because FastMCP runs synchronous tools in a thread pool.

        Args:
            action: The command action (e.g., "transport.start", "mixer.setTrackVolume")
            params: Optional parameters for the command
            timeout: Maximum time to wait for response in seconds

        Returns:
            Response dictionary from FL Studio

        Raises:
            RuntimeError: If no MIDI connection can be established
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
        poll_interval = 0.02  # 20ms between checks
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

    def get_status(self) -> dict[str, Any]:
        """Get connection status information."""
        try:
            import mido
            output_ports = mido.get_output_names()
        except Exception:
            output_ports = []

        return {
            "connected": self.is_connected,
            "port_name": self._port_name,
            "available_ports": output_ports,
            "command_file": str(self._command_file),
            "response_file": str(self._response_file),
            "error": self._error,
        }


# Global connection instance
_connection: MIDIConnection | None = None


def get_connection() -> MIDIConnection:
    """Get the global MIDI connection instance."""
    global _connection
    if _connection is None:
        _connection = MIDIConnection()
    return _connection


def reset_connection() -> None:
    """Reset the connection state to allow reconnection attempts."""
    global _connection
    if _connection is not None:
        _connection.disconnect()
        _connection = None
