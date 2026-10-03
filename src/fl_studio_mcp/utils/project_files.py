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
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp.exceptions import ToolError

from fl_studio_mcp.utils import errors
from fl_studio_mcp.utils.errors import FLBusyError, run, unwrap
from fl_studio_mcp.utils.fl_trigger import FL_BUNDLE_ID
from fl_studio_mcp.utils.midi_connection import get_fl_settings_base

# Controller version that has system.info, transport.save and the new actions.
MIN_CONTROLLER_VERSION = 3
# Saving needs version 4: older scripts call FL's save without the MIDI event's
# PME flags, and FL then often writes nothing and crashes seconds later. Reads
# and live edits work with version 3, so only saving is gated on 4.
SAVE_CONTROLLER_VERSION = 4
# The version of fl_controller/device_FLStudioMCP.py in this repository. Only
# step shift needs 5 (see tools/channels.py); everything else works with 4.
# Version 6 checks a written shift against the exact form written and redoes
# a step FL left without note data; nothing requires it.
LATEST_CONTROLLER_VERSION = 6

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
# A save is finished when the file stopped changing for this long (and FL's
# temporary "<name>.flp~" is gone): FL writes the temp file, then renames it.
SAVE_STABLE_DELAY = 0.2
# FL crashed (SIGILL) during a save that came ~18 s after a quick save ->
# MCP write -> reload sequence; saving right after a reload is avoided.
SETTLE_AFTER_RELOAD = 3.0
FL_PROCESS_NAME = "OsxFL"  # FL Studio's main process on macOS

# The last project path found, reused while it keeps being the file FL saves to.
_last_project: Path | None = None
# time.monotonic() when the last MCP-triggered reload finished.
_reload_finished_at: float | None = None


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def reload_finished() -> None:
    """Note that an MCP-triggered reload just finished (see SETTLE_AFTER_RELOAD)."""
    global _reload_finished_at
    _reload_finished_at = time.monotonic()


def _settle_after_reload() -> None:
    if _reload_finished_at is None:
        return
    remaining = SETTLE_AFTER_RELOAD - (time.monotonic() - _reload_finished_at)
    if remaining > 0:
        time.sleep(remaining)


def fl_process_running() -> bool | None:
    """Whether FL Studio's process runs (macOS, via pgrep); None when unknown."""
    if platform.system() != "Darwin":
        return None
    try:
        result = subprocess.run(["pgrep", "-x", FL_PROCESS_NAME], capture_output=True,
                                timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode == 0


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


def written_after(mtime_ns: int, since: float) -> bool:
    """True when a file with this mtime was written after `since` (epoch seconds).

    APFS (macOS) and NTFS store sub-second mtimes, so a file saved after the
    request is strictly newer than it; no slack is allowed, or a file written a
    moment before the request (FL's previous save, the MCP's last write) would
    pass for the new save. A whole-second mtime comes from a coarse file system
    (HFS+, FAT, some network shares): it is accepted from the start of the
    request's second, the best that can be done there.
    """
    since_ns = int(since * 1_000_000_000)
    if mtime_ns > since_ns:
        return True
    second = 1_000_000_000
    return mtime_ns % second == 0 and mtime_ns >= since_ns // second * second


def find_recent_projects(since: float, roots: list[Path] | None = None) -> list[Path]:
    """.flp files written after `since` (epoch seconds, see written_after), newest first."""
    found: list[tuple[int, Path]] = []
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
                    mtime_ns = path.stat().st_mtime_ns
                except OSError:
                    continue
                if written_after(mtime_ns, since):
                    found.append((mtime_ns, path))
    found.sort(reverse=True)
    unique, seen = [], set()
    for _, path in found:
        if str(path) not in seen:
            seen.add(str(path))
            unique.append(path)
    return unique


def temp_file(path: Path) -> Path:
    """FL's temporary file while it saves `path` ("<name>.flp~", renamed when done)."""
    return path.with_name(path.name + "~")


@dataclass
class SaveRequest:
    """A save asked of FL: when, and the state of the files it may write."""

    requested: float  # time.time() just before the request
    before: dict[str, tuple[int, int] | None] = field(default_factory=dict)
    temp_before: dict[str, tuple[int, int] | None] = field(default_factory=dict)
    path_hint: Path | None = None  # the file FL is expected to write, for messages
    # Whether pgrep saw FL running when the save was asked: only then does
    # "not running" mean FL died during the save.
    fl_was_running: bool = False


def _last_saved(path: Path | None) -> str:
    """"Last saved file <path> (modified <time>); MCP backups in <folder>." for messages."""
    if path is None:
        return "MCP backups are in the Backup folder next to the project."
    try:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(path.stat().st_mtime))
    except OSError:
        when = "unknown"
    return (f"The last saved file is {path} (modified {when}); MCP backups are in "
            f"{path.parent / 'Backup'}.")


def fl_stopped_error(path: Path | None) -> ToolError:
    """The error for an FL Studio that died or stopped answering during a save."""
    what = str(path) if path is not None else "the project"
    return ToolError(
        f"FL Studio stopped (it probably crashed) while saving {what}. {_last_saved(path)} "
        f"Relaunch FL Studio and reopen {what}."
    )


def fl_crashed_error(path: Path | None, log: Path | None = None) -> ToolError:
    """The error for FL's in-process crash: the process lives on, showing its crash window."""
    what = str(path) if path is not None else "the project"
    where = f" (crash log {log})" if log is not None else ""
    return errors.FLCrashedError(
        f"FL Studio crashed (its crash window is open{where}) while saving {what}; close "
        f"it, relaunch FL Studio and reopen {what}. {_last_saved(path)}"
    )


def fl_busy_or_crashed_error(path: Path | None, text: str) -> ToolError:
    """FL refused the save as unsafe and wrote no crash log: a modal window, or a crash."""
    what = str(path) if path is not None else "the project"
    return FLBusyError(
        f"FL Studio refused to save {what} ({text}): a modal window is open in FL Studio, "
        "or FL crashed and its crash window is open. Close it; after a crash, relaunch FL "
        f"Studio and reopen {what}. {_last_saved(path)}"
    )


def _new_crash_log(since: float) -> Path | None:
    logs = errors.crash_logs_since(since)
    return logs[0] if logs else None


def _fl_state(conn) -> str:
    """"ok", "gone" (FL does not answer) or "unsafe" (FL refuses commands: crash window)."""
    try:
        result = conn.send_command("system.info", None, 2.0)
    except RuntimeError:
        return "gone"
    if not result.get("success"):
        return "gone"
    # system.info answers even behind FL's crash window; a real command does not.
    try:
        status = conn.send_command("transport.getStatus", None, 2.0)
    except errors.FLCrashedError:
        return "unsafe"
    except RuntimeError as e:
        return "unsafe" if errors.UNSAFE_MARKER in str(e) else "ok"
    if errors.UNSAFE_MARKER in str(status.get("error") or ""):
        return "unsafe"
    return "ok"


def _candidates(project_path: str | None) -> list[Path]:
    if project_path:
        return [Path(project_path).expanduser()]
    return [_last_project] if _last_project is not None else []


def save_project(conn, new_version: bool = False,
                 project_path: str | None = None) -> SaveRequest:
    """Ask FL Studio to save; returns what locate_saved_project needs to find the file.

    Needs controller version 4 (see SAVE_CONTROLLER_VERSION). The controller
    refuses to save while FL says it is not safe (a modal window, FL busy);
    that is retried a few times before giving up. A save right after an
    MCP-triggered reload waits until SETTLE_AFTER_RELOAD has passed.
    """
    controller_info(conn, min_version=SAVE_CONTROLLER_VERSION)
    _settle_after_reload()
    candidates = [] if new_version else _candidates(project_path)
    request = SaveRequest(
        requested=time.time(),
        before={str(c): _stat(c) for c in candidates},
        temp_before={str(c): _stat(temp_file(c)) for c in candidates},
        path_hint=candidates[0] if candidates else _last_project,
        fl_was_running=fl_process_running() is True,
    )
    for attempt in range(SAVE_RETRIES):
        try:
            run(conn, "transport.save", {"new_version": new_version}, timeout=SAVE_TIMEOUT)
            return request
        except FLBusyError as e:
            # "Operation unsafe at current time": FL's crash window (or a modal) is open.
            log = (_new_crash_log(min(errors.last_ok(), request.requested))
                   or getattr(e, "log", None))
            if log is not None:
                raise fl_crashed_error(request.path_hint, log) from e
            raise fl_busy_or_crashed_error(request.path_hint, errors.UNSAFE_MARKER) from e
        except ToolError as e:
            if SAVE_BUSY_MARKER in str(e) and attempt < SAVE_RETRIES - 1:
                time.sleep(SAVE_RETRY_DELAY)
                continue
            if (SAVE_BUSY_MARKER not in str(e) and request.fl_was_running
                    and fl_process_running() is False):
                raise fl_stopped_error(request.path_hint) from e
            raise
    return request


def _saved_since(path: Path, request: SaveRequest) -> bool:
    """The file changed after the save request (not merely 'recent')."""
    state = _stat(path)
    if state is None:
        return False
    key = str(path)
    if key in request.before:
        return state != request.before[key]
    return written_after(state[0], request.requested)


def _save_finished(path: Path, request: SaveRequest) -> bool:
    """FL's temp file is gone (or untouched since the request) and the file is stable."""
    def temp_busy() -> bool:
        state = _stat(temp_file(path))
        if state is None:
            return False
        key = str(path)
        if key in request.temp_before:
            return state != request.temp_before[key]  # a stale ~ left by a crash is ignored
        return written_after(state[0], request.requested)

    if temp_busy():
        return False
    first = _stat(path)
    time.sleep(SAVE_STABLE_DELAY)
    second = _stat(path)
    return first is not None and first == second and second[1] > 0 and not temp_busy()


def locate_saved_project(request: SaveRequest, project_path: str | None = None,
                         conn=None, search_only: bool = False) -> Path:
    """The .flp FL Studio wrote for `request`, waiting until the save has finished.

    A file only counts once it changed after the request (another mtime or
    size than just before it) and FL's "<name>.flp~" is gone. With no known
    path, the search folders are walked for an .flp written after the request
    (search_only: only that, e.g. for "save new version"). The save is
    reported as a crash when FL Studio's process is gone (if it ran when the
    save was asked), when FL wrote a crash log after the request (FL then
    keeps running behind its crash window), or when FL stops answering.
    """
    global _last_project
    explicit = Path(project_path).expanduser() if project_path else None
    if explicit is not None:
        known = [explicit]
    elif search_only or _last_project is None:
        known = []
    else:
        known = [_last_project]
    deadline = time.time() + SAVE_TIMEOUT
    while True:
        for candidate in known:
            if _saved_since(candidate, request) and _save_finished(candidate, request):
                _last_project = candidate
                return candidate
        if explicit is None:
            recent = [p for p in find_recent_projects(request.requested)
                      if not known or p != known[0]]
            if recent and _save_finished(recent[0], request):
                _last_project = recent[0]
                return recent[0]
        log = _new_crash_log(request.requested)
        if log is not None:
            raise fl_crashed_error(explicit or request.path_hint, log)
        if request.fl_was_running and fl_process_running() is False:
            raise fl_stopped_error(explicit or request.path_hint)
        if time.time() > deadline:
            break
        time.sleep(0.3)
    if conn is not None:
        state = _fl_state(conn)
        if state == "gone":
            raise fl_stopped_error(explicit or request.path_hint)
        if state == "unsafe":
            log = _new_crash_log(request.requested) or errors.fresh_crash_log()
            if log is not None:
                raise fl_crashed_error(explicit or request.path_hint, log)
            raise fl_busy_or_crashed_error(explicit or request.path_hint,
                                           errors.UNSAFE_MARKER)
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


def save_and_locate(conn, project_path: str | None = None, new_version: bool = False) -> Path:
    """Save the open project and return the .flp FL wrote (always saves)."""
    request = save_project(conn, new_version=new_version, project_path=project_path)
    return locate_saved_project(request, None if new_version else project_path, conn,
                                search_only=new_version)


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
    return save_and_locate(conn, project_path)


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
            reload_finished()
            return info
        if sentinel is not None:
            try:
                status = conn.send_command("transport.getStatus", None, 2.0)
            except RuntimeError:
                continue
            if status.get("success") and status.get("position") != sentinel:
                time.sleep(0.5)  # let FL finish what follows the load
                reload_finished()
                return info
        elif time.time() - started >= MIN_RELOAD_WAIT:
            reload_finished()
            return info
    raise ToolError(
        f"FL Studio did not finish reopening the project within {timeout:.0f} s. Check FL "
        "Studio for a dialog (e.g. 'Save changes?') and answer it."
    )
