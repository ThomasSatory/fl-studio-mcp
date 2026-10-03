"""Render a .flp to WAV by running FL Studio's executable in command-line mode.

`OsxFL -R -Ewav <file.flp>` (FL64.exe on Windows) writes `<file>.wav` next to
the project and exits. Only one render can run at a time, so renders are
serialized with a lock shared between threads and processes. A failed render
may exit 0 without writing anything, so the output file is what counts. The
renderer also crashes now and then (killed by a signal); that is retried once.

The render instance is a second FL Studio: when it exits it rewrites FL's
shared settings files (reg.xml, the browser's recent files and search cache,
note colour presets, velocity curves), and the FL Studio the user works in
then crashed at its next save (EAccessViolation in FLEngine, 4 runs in 5
live; none in 6 with the guard). So the bytes of those files are taken
before each render and every file the render changed is put back (deleted
if it did not exist) as soon as the render process has exited.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from fastmcp.exceptions import ToolError

from fl_studio_mcp.utils.midi_connection import get_fl_settings_base

RENDER_KEEP = 30
MIN_WAV_BYTES = 44  # a WAV header alone
MIN_TIMEOUT, MAX_TIMEOUT = 90.0, 1200.0
BINARY_ENV = "FL_STUDIO_MCP_FL_BINARY"
RENDER_DIR_ENV = "FL_STUDIO_MCP_RENDER_DIR"
# FL's command-line renderer sometimes segfaults (exit -11); a killed render
# that wrote nothing is retried this many times.
SIGNAL_RETRIES = 1

_thread_lock = threading.Lock()


def shared_fl_files() -> list[tuple[Path, str]]:
    """(folder, glob pattern) of FL's shared files a render instance rewrites on exit.

    Seen live on macOS (FL 2025) with find -newer after a render. The
    Settings files are found through get_fl_settings_base (the default user
    data folder, or FL_STUDIO_MCP_SETTINGS_DIR); on Windows the same Settings
    layout is assumed, and FL's registry settings (macOS: reg.xml) are not
    guarded. A moved user data folder that the server does not know is
    skipped silently: missing folders simply match nothing.
    """
    settings = get_fl_settings_base()
    files = [
        (settings / "Browser", "Recent files.scr"),
        (settings / "Browser", "SearchCache"),
        (settings / "Browser", "SearchCacheDate"),
        (settings / "Note color presets", "Auto saved.ncp"),
        (settings / "Mapping", "*.env"),
    ]
    if platform.system() == "Darwin":
        files.insert(0, (Path.home() / "Library" / "Preferences" / "Image-Line", "reg.xml"))
    return files


# path -> (bytes, (atime_ns, mtime_ns)), or None when the file did not exist.
Snapshot = dict[Path, "tuple[bytes, tuple[int, int]] | None"]


def snapshot_shared_files() -> Snapshot:
    """The current bytes of FL's shared files (see shared_fl_files)."""
    snap: Snapshot = {}
    for folder, pattern in shared_fl_files():
        if any(ch in pattern for ch in "*?["):
            paths = sorted(folder.glob(pattern)) if folder.is_dir() else []
            snap[folder / pattern] = None  # marks the pattern: new matches get deleted
        else:
            paths = [folder / pattern]
        for path in paths:
            try:
                st = path.stat()
                snap[path] = (path.read_bytes(), (st.st_atime_ns, st.st_mtime_ns))
            except OSError:
                snap[path] = None
    return snap


def restore_shared_files(snap: Snapshot) -> list[Path]:
    """Put back every shared file whose bytes changed since `snap`; returns them.

    A file that did not exist before (or a new match of a guarded pattern)
    is deleted. Nothing is printed: the server talks MCP over stdout.
    """
    changed: list[Path] = []
    for folder, pattern in shared_fl_files():
        if any(ch in pattern for ch in "*?[") and folder.is_dir():
            for path in folder.glob(pattern):
                if path not in snap:
                    try:
                        path.unlink()
                        changed.append(path)
                    except OSError:
                        pass
    for path, saved in snap.items():
        if any(ch in path.name for ch in "*?["):
            continue
        try:
            current = path.read_bytes() if path.exists() else None
        except OSError:
            continue
        if saved is None:
            if current is not None:
                try:
                    path.unlink()
                    changed.append(path)
                except OSError:
                    pass
            continue
        data, times = saved
        if current == data:
            continue
        try:
            with open(path, "wb") as fh:  # in place: the user's FL may hold it open
                fh.write(data)
            os.utime(path, ns=times)
            changed.append(path)
        except OSError:
            pass
    return changed


def find_fl_binary() -> Path:
    """Locate FL Studio's executable (env override, then standard install paths)."""
    override = os.environ.get(BINARY_ENV)
    if override:
        path = Path(override).expanduser()
        if not path.exists():
            raise ToolError(f"{BINARY_ENV} points to {path}, which does not exist.")
        return path

    system = platform.system()
    candidates: list[Path] = []
    if system == "Darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            candidates += sorted(
                base.glob("FL Studio*.app/Contents/MacOS/OsxFL"), reverse=True
            )
    elif system == "Windows":
        root = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        candidates += sorted(
            (root / "Image-Line").glob("FL Studio*/FL64.exe"), reverse=True
        )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise ToolError(
        "FL Studio's executable was not found. Set the environment variable "
        f"{BINARY_ENV} to its full path."
    )


def render_command(binary: Path, flp: Path, system: str | None = None) -> list[str]:
    """Command line that renders `flp` to a WAV next to it."""
    system = system or platform.system()
    if system == "Windows":
        return [str(binary), "/R", "/Ewav", str(flp)]
    return [str(binary), "-R", "-Ewav", str(flp)]


def renders_dir() -> Path:
    """Folder where finished renders are kept (created if needed)."""
    override = os.environ.get(RENDER_DIR_ENV)
    system = platform.system()
    if override:
        path = Path(override).expanduser()
    elif system == "Darwin":
        path = Path.home() / "Library" / "Caches" / "fl-studio-mcp" / "renders"
    elif system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "fl-studio-mcp" / "renders"
    else:
        path = Path.home() / ".cache" / "fl-studio-mcp" / "renders"
    path.mkdir(parents=True, exist_ok=True)
    return path


def render_timeout(song_seconds: float | None) -> float:
    """Time allowed for a render: 90 s plus twice the song length, clamped."""
    if song_seconds is None:
        return 600.0
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, 90.0 + 2.0 * song_seconds))


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9 _.-]", "_", name or "")[:60].strip()
    return cleaned or "render"


class _RenderLock:
    """Thread lock plus an OS-level lock file shared between processes."""

    def __init__(self, path: Path):
        self._path = path
        self._fh = None

    def __enter__(self):
        _thread_lock.acquire()
        try:
            self._fh = open(self._path, "a+b")
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                while True:
                    try:
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        continue
            else:
                import fcntl

                fcntl.flock(self._fh, fcntl.LOCK_EX)
        except BaseException:
            if self._fh:
                self._fh.close()
                self._fh = None
            _thread_lock.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self._fh:
                try:
                    if os.name == "nt":
                        import msvcrt

                        self._fh.seek(0)
                        msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(self._fh, fcntl.LOCK_UN)
                finally:
                    self._fh.close()
        finally:
            _thread_lock.release()


def _stop(proc: subprocess.Popen) -> None:
    """Kill the child if it is still running and reap it."""
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def _purge(directory: Path, keep: Path | None = None) -> None:
    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    wavs = sorted(directory.glob("*.wav"), key=mtime, reverse=True)
    for old in wavs[RENDER_KEEP:]:
        if keep is not None and old == keep:
            continue
        try:
            old.unlink()
        except OSError:
            pass


def _signal_name(number: int) -> str:
    try:
        return f", {signal.Signals(number).name}"
    except ValueError:
        return ""


def _wrote_wav(wav: Path) -> bool:
    return wav.exists() and wav.stat().st_size >= MIN_WAV_BYTES


def _run_renderer(binary: Path, flp: Path, timeout: float) -> int | None:
    """Run one render of `flp`; returns the exit code (negative: killed by a signal)."""
    try:
        proc = subprocess.Popen(
            render_command(binary, flp),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        raise ToolError(f"Could not launch FL Studio ({binary}): {e}") from e
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _stop(proc)
            raise ToolError(
                f"FL Studio did not finish rendering within {timeout:g} s and "
                "was stopped. It may be waiting on a dialog (missing plugin, "
                "missing sample, demo limitation): the render runs on a copy "
                "in a temp folder, so samples FL finds relative to the "
                "project folder may be missing; open the project in FL to check."
            ) from None
    finally:
        _stop(proc)
    return proc.returncode


def render_wav(
    flp_data: bytes, name: str, timeout: float, binary: Path | None = None
) -> Path:
    """Render project bytes to a WAV and return the kept file's path."""
    binary = binary or find_fl_binary()
    name = _safe_name(name)
    out_dir = renders_dir()
    with _RenderLock(out_dir / ".render.lock"):
        work = Path(tempfile.mkdtemp(prefix="flmcp-render-"))
        try:
            flp = work / f"{name}.flp"
            flp.write_bytes(flp_data)
            wav = work / f"{name}.wav"
            for attempt in range(SIGNAL_RETRIES + 1):
                shared = snapshot_shared_files()
                try:
                    returncode = _run_renderer(binary, flp, timeout)
                finally:
                    # Also after a timeout (the renderer was killed) and before a retry.
                    restore_shared_files(shared)
                if _wrote_wav(wav):
                    break
                if returncode is None or returncode >= 0:
                    raise ToolError(
                        f"FL Studio wrote no audio file (exit code {returncode}). "
                        "Another render may have been running, or the project could "
                        "not be rendered; open it in FL to check."
                    )
                if attempt == SIGNAL_RETRIES:
                    raise ToolError(
                        f"FL Studio's command-line renderer crashed (signal {-returncode}"
                        f"{_signal_name(-returncode)}), {SIGNAL_RETRIES + 1} times in a "
                        "row. This happens intermittently and retrying usually works. "
                        "macOS may show a crash dialog for the renderer (a separate FL "
                        "Studio process); it can be dismissed."
                    )
                wav.unlink(missing_ok=True)  # a partial file from the crash
            stamp = time.strftime("%Y-%m-%d %Hh%Mm%S")
            dest = out_dir / f"{name} {stamp}.wav"
            n = 1
            while dest.exists():
                n += 1
                dest = out_dir / f"{name} {stamp}-{n}.wav"
            shutil.move(str(wav), str(dest))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _purge(out_dir, keep=dest)
        return dest
