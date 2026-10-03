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
if it did not exist) as soon as the render process has exited, through a
temp file and a rename, with its date; a file whose bytes the renderer
rewrote unchanged gets its date back too. Whatever the
main FL writes to those few files while a render runs is reverted with
them. A file that could not be read before the render is left alone; a file
that could not be put back is reported on stderr (never stdout: the server
talks MCP over it) and in the render's notes (render_wav's `problems`).

The snapshot is also written to the cache folder (pending-restore) before
the renderer starts, and removed once the files are back: when the server
dies mid-render, the next render (or the next server start) kills the
orphaned renderer and puts the files back from it. SIGTERM/SIGHUP and the
interpreter's exit stop a running renderer, so that the render's own
cleanup runs.

Before that guard, each render also added its temp project
(.../flmcp-render-XXXX/<name>.flp) to FL's recent files, pushing the user's
projects out of the list; after every render such lines are removed from
"Recent files.scr" (forget_render_paths).
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastmcp.exceptions import ToolError

from fl_studio_mcp.utils import cache, errors
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

# The render's temp folder (tempfile.mkdtemp prefix): recognisable in FL's recent files.
TEMP_PREFIX = "flmcp-render-"
RECENT_FILES = "Recent files.scr"
UTF8_BOM = b"\xef\xbb\xbf"

# What the server names a kept render: "<name> 2026-10-03 14h25m07.wav" or
# "... 14h25m07-2.wav". Only such files are ever purged from the renders folder,
# which the user may point at a folder holding their own audio.
RENDER_NAME = re.compile(r" \d{4}-\d{2}-\d{2} \d{2}h\d{2}m\d{2}(-\d+)?\.wav$")

# In the cache folder (cache.cache_dir), whatever FL_STUDIO_MCP_RENDER_DIR says.
LOCK_FILE = "render.lock"
PENDING_DIR = "pending-restore"
MANIFEST = "manifest.json"
# Leftover render temp folders (a server killed mid-render) older than this go.
TEMP_MAX_AGE = 3600.0

# The render running in this process: {"proc", "snapshot", "work", "done"} (see _at_exit).
_active: dict | None = None
# Set when the server is exiting (SIGTERM/SIGHUP, interpreter exit): no new render, no retry.
_stopping = threading.Event()
# Serializes putting the shared files back (the render's thread, the exit handler).
_restore_lock = threading.Lock()


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
        (settings / "Browser", RECENT_FILES),
        (settings / "Browser", "SearchCache"),
        (settings / "Browser", "SearchCacheDate"),
        (settings / "Note color presets", "Auto saved.ncp"),
        (settings / "Mapping", "*.env"),
    ]
    if platform.system() == "Darwin":
        files.insert(0, (Path.home() / "Library" / "Preferences" / "Image-Line", "reg.xml"))
    return files


# Snapshot value of a file that exists but could not be read: left alone on restore.
UNREADABLE = "unreadable"

# path -> (bytes, (atime_ns, mtime_ns)), None when the file did not exist, or
# UNREADABLE when it could not be read.
Snapshot = dict[Path, "tuple[bytes, tuple[int, int]] | str | None"]


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
            snap[path] = _snapshot_one(path)
    return snap


def _snapshot_one(path: Path) -> tuple[bytes, tuple[int, int]] | str | None:
    for attempt in range(2):
        try:
            st = path.stat()
            return (path.read_bytes(), (st.st_atime_ns, st.st_mtime_ns))
        except FileNotFoundError:
            if attempt == 0:
                continue  # look once more (a file being replaced) before saying "missing"
            return None
        except OSError:
            return UNREADABLE  # exists (maybe): never deleted nor rewritten
    return None


def write_atomic(path: Path, data: bytes) -> None:
    """Write `data` to `path` through a temp file next to it and os.replace.

    The file's mode is kept. Writing the temp file failing leaves `path`
    untouched (OSError raised); when only the replace fails (Windows: FL holds
    the file), the bytes are written in place instead.
    """
    tmp = path.with_name(path.name + ".flmcp-tmp")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        mode = None
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)


def restore_shared_files(snap: Snapshot, failed: list[Path] | None = None) -> list[Path]:
    """Put back every shared file whose bytes changed since `snap`; returns them.

    A file that did not exist before (or a new match of a guarded pattern)
    is deleted; one that could not be read then is skipped. Files that could
    not be put back are appended to `failed`. Nothing is printed here.
    """
    changed: list[Path] = []
    failed = [] if failed is None else failed
    for folder, pattern in shared_fl_files():
        if any(ch in pattern for ch in "*?[") and folder.is_dir():
            for path in folder.glob(pattern):
                if path not in snap:
                    try:
                        path.unlink()
                        changed.append(path)
                    except OSError:
                        failed.append(path)
    for path, saved in snap.items():
        if any(ch in path.name for ch in "*?[") or saved == UNREADABLE:
            continue
        try:
            current = path.read_bytes() if path.exists() else None
        except OSError:
            failed.append(path)
            continue
        if saved is None:
            if current is not None:
                try:
                    path.unlink()
                    changed.append(path)
                except OSError:
                    failed.append(path)
            continue
        data, times = saved
        if current == data:
            # Same bytes, but the renderer may have rewritten them: put the date back.
            try:
                if path.stat().st_mtime_ns != times[1]:
                    os.utime(path, ns=times)
            except OSError:
                failed.append(path)
            continue
        try:
            write_atomic(path, data)
            os.utime(path, ns=times)
            changed.append(path)
        except OSError:
            failed.append(path)
    return changed


def restore_problems_note(paths: list[Path]) -> str:
    """The render note for shared files that could not be put back."""
    return ("FL Studio's renderer changed shared settings files that could not be put "
            f"back: {', '.join(str(p) for p in paths)}. If FL Studio misbehaves, restart it.")


def _stderr(message: str) -> None:
    try:
        print(f"fl-studio-mcp: {message}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _recent_files_paths() -> list[Path]:
    return [folder / pattern for folder, pattern in shared_fl_files() if pattern == RECENT_FILES]


def _without_render_paths(raw: bytes, markers: list[str]) -> tuple[bytes, int]:
    """`raw` (FL's recent files) without the lines holding a marker; BOM and order kept."""
    bom = raw.startswith(UTF8_BOM)
    text = (raw[len(UTF8_BOM):] if bom else raw).decode("utf-8", "surrogateescape")
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if not any(m in line for m in markers)]
    data = (UTF8_BOM if bom else b"") + "".join(kept).encode("utf-8", "surrogateescape")
    return data, len(lines) - len(kept)


def forget_render_paths(work: Path | str | None = None) -> int:
    """Drop the render's temp projects from FL's recent files; returns the lines removed.

    A line goes when it holds the render temp folder prefix (TEMP_PREFIX) or
    `work`, this render's folder; never the renders folder, which the user may
    point at a project folder. The BOM, the order and every other line are
    kept; the file is re-read just before writing and only rewritten when a
    line went. Never raises: a problem is noted on stderr.
    """
    markers = [TEMP_PREFIX] + ([str(work)] if work else [])
    removed = 0
    for path in _recent_files_paths():
        try:
            _, count = _without_render_paths(path.read_bytes(), markers)
            if not count:
                continue
            # FL may have written it meanwhile: filter what is there now.
            data, count = _without_render_paths(path.read_bytes(), markers)
            if count:
                write_atomic(path, data)
                removed += count
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as e:
            _stderr(f"could not remove render paths from {path}: {e}")
    return removed


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
    path = Path(override).expanduser() if override else cache.cache_dir() / "renders"
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

    def __init__(self, path: Path, blocking: bool = True):
        self._path = path
        self._fh = None
        self._blocking = blocking

    def __enter__(self):
        """Take the lock; BlockingIOError when not blocking and it is held."""
        if not _thread_lock.acquire(blocking=self._blocking):
            raise BlockingIOError("a render runs in this process")
        try:
            self._fh = open(self._path, "a+b")
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                while True:
                    try:
                        msvcrt.locking(self._fh.fileno(),
                                       msvcrt.LK_LOCK if self._blocking else msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if not self._blocking:
                            raise BlockingIOError("a render runs in another process") from None
                        continue
            else:
                import fcntl

                fcntl.flock(self._fh, fcntl.LOCK_EX if self._blocking
                            else fcntl.LOCK_EX | fcntl.LOCK_NB)
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


def render_lock_path() -> Path:
    """The lock every server process takes to render (one fixed place per user)."""
    return cache.cache_dir() / LOCK_FILE


def _purge(directory: Path, keep: Path | None = None) -> None:
    """Delete the oldest renders past RENDER_KEEP: only files the server named
    (RENDER_NAME), never another WAV of the folder."""

    def mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    wavs = sorted((p for p in directory.glob("*.wav") if RENDER_NAME.search(p.name)),
                  key=mtime, reverse=True)
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


def _run_renderer(binary: Path, flp: Path, timeout: float, on_start=None) -> int | None:
    """Run one render of `flp`; returns the exit code (negative: killed by a signal).

    `on_start(proc)` is called as soon as the renderer runs.
    """
    try:
        proc = subprocess.Popen(
            render_command(binary, flp),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as e:
        raise ToolError(f"Could not launch FL Studio ({binary}): {e}") from e
    try:
        if on_start is not None:
            on_start(proc)
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


def _after_render(shared: Snapshot, work: Path, problems: list[Path] | None = None) -> None:
    """Put back FL's shared files and clean its recent files; never raises.

    Files that could not be put back are appended to `problems` (and noted on
    stderr). The pending snapshot on disk goes once the restore has run.
    """
    failed: list[Path] = []
    restored = False
    try:
        restore_shared_files(shared, failed)
        restored = True
    except Exception as e:  # noqa: BLE001 - a render must not fail on this
        _stderr(f"could not put back FL's shared files after a render: {e}")
    if failed:
        if problems is not None:
            problems.extend(failed)
        _stderr("could not put back after a render: " + ", ".join(str(p) for p in failed))
    try:
        forget_render_paths(work)
    except Exception as e:  # noqa: BLE001
        _stderr(f"could not clean FL's recent files after a render: {e}")
    if restored:
        clear_pending()


def _finish(state: dict, problems: list[Path] | None = None) -> None:
    """_after_render for the render `state`, once (its thread or the exit handler)."""
    if not _restore_lock.acquire(timeout=30):
        _stderr("could not put back FL's shared files: another restore is stuck")
        return
    try:
        if state.get("done"):
            return
        state["done"] = True
        _after_render(state["snapshot"], state["work"], problems)
    finally:
        _restore_lock.release()


# --- The snapshot on disk (a server that dies mid-render) -------------------


def _pending_dir() -> Path:
    return cache.cache_dir() / PENDING_DIR


def save_pending(snap: Snapshot, work: Path) -> dict:
    """Write `snap` to the cache folder before a render; returns its manifest.

    The bytes go to one file each, the manifest (written last, atomically) is
    what makes the snapshot pending. Raises OSError.
    """
    folder = _pending_dir()
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    entries = []
    for n, (path, saved) in enumerate(snap.items()):
        if saved is None:
            entries.append({"path": str(path), "state": "missing"})
        elif saved == UNREADABLE:
            entries.append({"path": str(path), "state": UNREADABLE})
        else:
            data, times = saved
            blob = f"{n}.bin"
            (folder / blob).write_bytes(data)
            entries.append({"path": str(path), "state": "file", "blob": blob,
                            "times": list(times)})
    manifest = {"version": 1, "created": time.time(), "pid": os.getpid(),
                "work": str(work), "renderer_pid": None, "entries": entries}
    _write_manifest(manifest)
    return manifest


def _write_manifest(manifest: dict) -> None:
    write_atomic(_pending_dir() / MANIFEST, json.dumps(manifest).encode("utf-8"))


def load_pending() -> tuple[Snapshot, dict] | None:
    """The pending snapshot and its manifest; None when there is none (or it is unreadable)."""
    folder = _pending_dir()
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
        snap: Snapshot = {}
        for entry in manifest["entries"]:
            path = Path(entry["path"])
            if entry["state"] == "missing":
                snap[path] = None
            elif entry["state"] == "file":
                try:
                    data = (folder / entry["blob"]).read_bytes()
                except OSError:
                    snap[path] = UNREADABLE  # its bytes are lost: leave the file alone
                    continue
                atime, mtime = entry["times"]
                snap[path] = (data, (int(atime), int(mtime)))
            else:
                snap[path] = UNREADABLE
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        _stderr(f"ignoring an unreadable pending snapshot in {folder}: {e}")
        return None
    return snap, manifest


def clear_pending() -> None:
    folder = _pending_dir()
    try:
        (folder / MANIFEST).unlink(missing_ok=True)
    except OSError:
        pass
    shutil.rmtree(folder, ignore_errors=True)


def _stop_orphan(pid: object, work: str | None) -> None:
    """Kill a renderer left running by a server that died (POSIX), if it is one."""
    if not isinstance(pid, int) or not work or pid == os.getpid() or os.name == "nt":
        return
    if not cache.pid_alive(pid):
        return
    try:
        command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return
    if work not in command:
        return  # the pid now belongs to another process
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        return
    deadline = time.monotonic() + 10
    while cache.pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)


def recover_pending(problems: list[Path] | None = None) -> bool:
    """Put back the shared files of a render whose server died; True if there was one.

    Call it holding the render lock. The orphaned renderer (if it still runs)
    is killed first, then the files are put back as after any render, its
    temp folder removed and the snapshot deleted. When the restore itself
    fails, the snapshot is kept for the next try.
    """
    loaded = load_pending()
    if loaded is None:
        if _pending_dir().exists():
            clear_pending()  # no manifest: never got as far as a render
        return False
    snap, manifest = loaded
    work = manifest.get("work")
    _stop_orphan(manifest.get("renderer_pid"), work)
    failed: list[Path] = []
    try:
        changed = restore_shared_files(snap, failed)
    except Exception as e:  # noqa: BLE001 - a render must not fail on this
        _stderr(f"could not put back FL's shared files left by an unfinished render: {e}")
        return True
    if failed:
        if problems is not None:
            problems.extend(failed)
        _stderr("could not put back after an unfinished render: "
                + ", ".join(str(p) for p in failed))
    if work:
        forget_render_paths(work)
        if Path(work).name.startswith(TEMP_PREFIX):
            shutil.rmtree(work, ignore_errors=True)
    clear_pending()
    if changed:
        _stderr("put back FL's shared files a render left changed (its server stopped "
                "mid-render): " + ", ".join(str(p) for p in changed))
    return True


def clean_temp_dirs(max_age: float = TEMP_MAX_AGE) -> None:
    """Remove render temp folders (TEMP_PREFIX) older than `max_age` seconds."""
    limit = time.time() - max_age
    for folder in Path(tempfile.gettempdir()).glob(TEMP_PREFIX + "*"):
        try:
            if folder.is_dir() and folder.stat().st_mtime < limit:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            continue


def recover_at_startup() -> None:
    """At server start: recover a pending snapshot unless a render runs; never raises."""
    try:
        with _RenderLock(render_lock_path(), blocking=False):
            recover_pending()
            clean_temp_dirs()
    except BlockingIOError:
        pass  # another server renders: it recovers before its next render
    except Exception as e:  # noqa: BLE001 - the server must start anyway
        _stderr(f"could not check for an unfinished render: {e}")


def _kill_active() -> None:
    _stopping.set()
    state = _active
    proc = state.get("proc") if state else None
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def _at_exit() -> None:
    """Interpreter exit: stop a running renderer and put the shared files back."""
    state = _active
    if state is None:
        return
    _kill_active()
    proc = state.get("proc")
    if proc is not None:
        try:
            proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            pass
    _finish(state)
    shutil.rmtree(state["work"], ignore_errors=True)


atexit.register(_at_exit)


def install_exit_handlers() -> None:
    """SIGTERM/SIGHUP stop a running renderer, then exit (atexit runs); POSIX only.

    The render's thread is not a daemon: once its renderer is dead it puts
    the shared files back itself, and the interpreter waits for it.
    """
    def handler(signum, frame):
        _kill_active()
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None or os.name == "nt":
            continue
        try:
            if signal.getsignal(sig) == signal.SIG_DFL:
                signal.signal(sig, handler)
        except (OSError, ValueError):
            pass  # not the main thread


def render_wav(
    flp_data: bytes, name: str, timeout: float, binary: Path | None = None,
    problems: list[Path] | None = None,
) -> Path:
    """Render project bytes to a WAV and return the kept file's path.

    Shared files that could not be put back are appended to `problems`.
    """
    global _active
    binary = binary or find_fl_binary()
    name = _safe_name(name)
    out_dir = renders_dir()
    with _RenderLock(render_lock_path()):
        recover_pending(problems)  # a server that died mid-render
        clean_temp_dirs()
        work = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
        try:
            flp = work / f"{name}.flp"
            flp.write_bytes(flp_data)
            wav = work / f"{name}.wav"
            for attempt in range(SIGNAL_RETRIES + 1):
                shared = snapshot_shared_files()
                try:
                    manifest = save_pending(shared, work)
                except OSError as e:
                    manifest = None
                    _stderr(f"could not write the render's snapshot to disk: {e}")
                state = {"snapshot": shared, "work": work, "proc": None, "done": False}

                def started(proc, state=state, manifest=manifest):
                    state["proc"] = proc
                    if manifest is not None:
                        manifest["renderer_pid"] = proc.pid
                        try:
                            _write_manifest(manifest)
                        except OSError:
                            pass

                _active = state
                try:
                    with errors.render_window():
                        returncode = _run_renderer(binary, flp, timeout, started)
                finally:
                    # Also after a timeout (the renderer was killed) and before a retry.
                    _finish(state, problems)
                    _active = None
                crashed = returncode is not None and returncode < 0
                if _stopping.is_set():
                    wav.unlink(missing_ok=True)
                    raise ToolError("The render was stopped: the MCP server is shutting down.")
                if not crashed and _wrote_wav(wav):
                    break
                if not crashed:
                    raise ToolError(
                        f"FL Studio wrote no audio file (exit code {returncode}). "
                        "Another render may have been running, or the project could "
                        "not be rendered; open it in FL to check."
                    )
                # Killed by a signal: whatever it wrote is partial, even a full header.
                wav.unlink(missing_ok=True)
                if attempt == SIGNAL_RETRIES:
                    raise ToolError(
                        f"FL Studio's command-line renderer crashed (signal {-returncode}"
                        f"{_signal_name(-returncode)}), {SIGNAL_RETRIES + 1} times in a "
                        "row. This happens intermittently and retrying usually works. "
                        "macOS may show a crash dialog for the renderer (a separate FL "
                        "Studio process); it can be dismissed."
                    )
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
