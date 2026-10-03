"""Save, locate and reopen the project file of the running FL Studio.

FL Studio's scripting API neither reports the project's path nor loads a
project. The path is found by saving (Ctrl+S through the controller) and
looking for the .flp that was just written; reopening goes through the OS
("open" on macOS, the file association on Windows), which makes the running
FL Studio load the file. The controller counts finished loads so the server
can wait for the reload it asked for.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from pathlib import Path

from fastmcp.exceptions import ToolError

from fl_studio_mcp.utils.errors import run, unwrap
from fl_studio_mcp.utils.fl_trigger import FL_BUNDLE_ID
from fl_studio_mcp.utils.midi_connection import get_fl_settings_base

# Controller version that has system.info, transport.save and the new actions.
MIN_CONTROLLER_VERSION = 3
# Saving needs version 4: older scripts call FL's save without the MIDI event's
# PME flags, and FL then often writes nothing and crashes seconds later. Reads
# and live edits work with version 3, so only saving is gated on 4.
SAVE_CONTROLLER_VERSION = 4

OUTDATED_FOR_SAVING = (
    "The FLStudioMCP controller script installed in FL Studio (version {version}) is too "
    "old to save the project: older scripts can make FL Studio crash when saving. Copy "
    "fl_controller/device_FLStudioMCP.py to FL Studio's Settings/Hardware/FLStudioMCP "
    "folder (install.sh or scripts/setup.sh does it), then restart FL Studio or reload "
    "the script (Options > MIDI settings). Until then, save by hand in FL Studio and pass "
    "save=False where the tool allows it."
)
SAVE_BUSY_MARKER = "busy and cannot save"
SAVE_RETRIES = 3
SAVE_RETRY_DELAY = 0.5

OUTDATED_FOR_FEATURE = (
    "The FLStudioMCP controller script installed in FL Studio is too old for this tool "
    "(it has no {action}). Copy fl_controller/device_FLStudioMCP.py to FL Studio's "
    "Settings/Hardware/FLStudioMCP folder (install.sh or scripts/setup.sh does it), "
    "then restart FL Studio or reload the script (Options > MIDI settings)."
)

SKIP_DIRS = {"Backup", "Library", "node_modules", "Applications", "Cache", "Caches"}
SEARCH_DEPTH = 6
SEARCH_BUDGET = 4.0  # seconds spent walking folders for the saved project
SAVE_TIMEOUT = 10.0
RELOAD_TIMEOUT = 120.0
MIN_RELOAD_WAIT = 4.0

# The last project path found, reused while it keeps being the file FL saves to.
_last_project: Path | None = None


def controller_info(conn, timeout: float = 2.0,
                    min_version: int = MIN_CONTROLLER_VERSION) -> dict:
    """system.info from the controller; ToolError when the script predates it."""
    try:
        result = conn.send_command("system.info", None, timeout)
    except RuntimeError as e:
        raise ToolError(str(e)) from e
    if not result.get("success") and "Unknown action" in str(result.get("error", "")):
        raise ToolError(OUTDATED_FOR_FEATURE.format(action="system.info"))
    info = unwrap(result)
    version = int(info.get("controller_version") or 0)
    if version < MIN_CONTROLLER_VERSION:
        raise ToolError(OUTDATED_FOR_FEATURE.format(action="this action"))
    if version < min_version:
        raise ToolError(OUTDATED_FOR_SAVING.format(version=version))
    return info


def search_roots() -> list[Path]:
    home = Path.home()
    roots = [get_fl_settings_base().parent / "Projects"]
    for name in ("Documents", "Desktop", "Music", "Downloads"):
        roots.append(home / name)
    extra = os.environ.get("FL_STUDIO_MCP_PROJECT_DIRS")
    if extra:
        roots = [Path(p).expanduser() for p in extra.split(os.pathsep) if p] + roots
    seen, unique = set(), []
    for root in roots:
        key = str(root)
        if key not in seen and root.is_dir():
            seen.add(key)
            unique.append(root)
    return unique


def _is_backup(path: Path) -> bool:
    name = path.name
    return (
        "Backup" in path.parts
        or "(overwritten at" in name
        or "(autosaved at" in name
        or "(before MCP edit" in name
    )


def find_recent_projects(since: float, roots: list[Path] | None = None) -> list[Path]:
    """.flp files modified at or after `since` (epoch seconds), newest first."""
    found: list[tuple[float, Path]] = []
    deadline = time.time() + SEARCH_BUDGET
    for root in roots or search_roots():
        root_depth = len(root.parts)
        for dirpath, dirnames, filenames in os.walk(root):
            if time.time() > deadline:
                break
            depth = len(Path(dirpath).parts) - root_depth
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in SKIP_DIRS and depth < SEARCH_DEPTH
            ]
            for name in filenames:
                if not name.lower().endswith(".flp"):
                    continue
                path = Path(dirpath) / name
                if _is_backup(path):
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime >= since:
                    found.append((mtime, path))
    found.sort(reverse=True)
    unique, seen = [], set()
    for _, path in found:
        if str(path) not in seen:
            seen.add(str(path))
            unique.append(path)
    return unique


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def save_project(conn, new_version: bool = False) -> float:
    """Ask FL Studio to save; returns the time just before the request.

    Needs controller version 4 (see SAVE_CONTROLLER_VERSION). The controller
    refuses to save while FL says it is not safe (a modal window, FL busy);
    that is retried a few times before giving up.
    """
    controller_info(conn, min_version=SAVE_CONTROLLER_VERSION)
    started = time.time() - 1.0  # file system timestamps can be coarse
    for attempt in range(SAVE_RETRIES):
        try:
            run(conn, "transport.save", {"new_version": new_version}, timeout=SAVE_TIMEOUT)
            return started
        except ToolError as e:
            if SAVE_BUSY_MARKER not in str(e) or attempt == SAVE_RETRIES - 1:
                raise
            time.sleep(SAVE_RETRY_DELAY)
    return started


def locate_saved_project(started: float, project_path: str | None = None) -> Path:
    """The .flp FL Studio wrote since `started`, waiting a few seconds for it."""
    global _last_project
    explicit = Path(project_path).expanduser() if project_path else None
    deadline = time.time() + SAVE_TIMEOUT
    while True:
        for candidate in (explicit, _last_project):
            if candidate is not None and _mtime(candidate) >= started:
                _last_project = candidate
                return candidate
        if explicit is None:
            recent = find_recent_projects(started)
            if recent:
                _last_project = recent[0]
                return recent[0]
        if time.time() > deadline:
            break
        time.sleep(0.3)
    if explicit is not None:
        raise ToolError(
            f"FL Studio did not save to {explicit}. Is that the open project? A save "
            "dialog may be waiting in FL Studio."
        )
    raise ToolError(
        "FL Studio saved no .flp file: the project is probably still untitled and FL is "
        "showing a Save As dialog. Save it once by hand (File > Save as), then retry; or "
        "pass project_path. Searched: " + ", ".join(str(r) for r in search_roots())
    )


def current_project_file(conn, save: bool, project_path: str | None) -> Path:
    """The open project's .flp: saved first (Ctrl+S) unless save=False.

    With save=False, `project_path` or the last located project is used as is.
    """
    controller_info(conn)
    if not save:
        if project_path:
            return Path(project_path).expanduser()
        if _last_project is not None:
            return _last_project
        raise ToolError("save=False needs project_path (the project was never located)")
    started = save_project(conn)
    return locate_saved_project(started, project_path)


def backup_project(path: Path) -> Path:
    """Copy the project to Backup/<name> (before MCP edit HHhMMmSS).flp."""
    folder = path.parent / "Backup"
    folder.mkdir(exist_ok=True)
    stamp = time.strftime("%Hh%Mm%S")
    target = folder / f"{path.stem} (before MCP edit {stamp}){path.suffix}"
    shutil.copy2(path, target)
    return target


def write_atomically(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".mcp-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def open_in_fl(path: Path) -> None:
    """Make the running FL Studio open `path` (it loads it as the current project)."""
    system = platform.system()
    if system == "Darwin":
        result = subprocess.run(
            ["open", "-b", FL_BUNDLE_ID, str(path)], capture_output=True, text=True, timeout=15
        )
        if result.returncode != 0:
            raise ToolError(f"Could not open {path} in FL Studio: {result.stderr.strip()}")
    elif system == "Windows":
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    else:
        raise ToolError(
            f"Reopening a project automatically is not supported on {system}. Open {path} "
            "in FL Studio by hand (File > Open)."
        )


SENTINEL_TICKS = 7  # a playhead position no project is saved or loaded at


def place_sentinel(conn) -> str | None:
    """Park the playhead on SENTINEL_TICKS; loading a project moves it back to 1:01:00.

    FL Studio 2025 does not call the controller's OnProjectLoad, so the moved
    playhead is how a finished reload is recognised. Returns the parked
    position's hint text, or None when FL clamped it (empty song).
    """
    try:
        result = conn.send_command("transport.setPosition", {"ticks": SENTINEL_TICKS}, 2.0)
    except RuntimeError:
        return None
    if not result.get("success") or not result.get("ticks"):
        return None
    return result.get("position")


def wait_for_reload(conn, before: dict, sentinel: str | None = None,
                    timeout: float = RELOAD_TIMEOUT) -> dict:
    """Poll FL until the project reload started by open_in_fl() has finished.

    Finished means: the controller answers again and either its load counter
    moved, its instance changed, or the playhead left the sentinel position.
    Without a sentinel, a controller that answers again after at least
    MIN_RELOAD_WAIT seconds is taken as done.
    """
    started = time.time()
    deadline = started + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            info = conn.send_command("system.info", None, 2.0)
        except RuntimeError:
            info = {}
        if not info.get("success"):
            continue
        if (info.get("instance_id") != before.get("instance_id")
                or int(info.get("load_count") or 0) > int(before.get("load_count") or 0)):
            return info
        if sentinel is not None:
            try:
                status = conn.send_command("transport.getStatus", None, 2.0)
            except RuntimeError:
                continue
            if status.get("success") and status.get("position") != sentinel:
                time.sleep(0.5)  # let FL finish what follows the load
                return info
        elif time.time() - started >= MIN_RELOAD_WAIT:
            return info
    raise ToolError(
        f"FL Studio did not finish reopening the project within {timeout:.0f} s. Check FL "
        "Studio for a dialog (e.g. 'Save changes?') and answer it."
    )
