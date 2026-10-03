import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.utils import render

FAKE = """#!{py}
import os, signal, sys, time
from pathlib import Path
mode = os.environ.get("FAKE_FL_MODE", "ok")
flp = Path(sys.argv[-1])
counter = os.environ.get("FAKE_FL_COUNTER")
if counter:
    calls = int(Path(counter).read_text() or 0) + 1 if Path(counter).exists() else 1
    Path(counter).write_text(str(calls))
if mode == "crash_once":
    if calls == 1:
        os.kill(os.getpid(), signal.SIGSEGV)
    mode = "ok"
if mode in ("partial_crash_once", "big_partial_crash_once"):
    wav = flp.with_suffix(".wav")
    if calls == 1:
        # FL's header (44 bytes and more) may be on disk when it crashes.
        wav.write_bytes(b"RIF" if mode == "partial_crash_once" else b"RIFF" + bytes(96))
        os.kill(os.getpid(), signal.SIGSEGV)
    if wav.exists():
        sys.exit(3)  # the partial file of the crash was left behind
    mode = "ok"
shared = os.environ.get("FAKE_FL_SHARED")
if shared:  # like FL's render instance: rewrite the shared settings files on the way out
    shared = Path(shared)
    (shared / "reg.xml").write_text("render instance")
    (shared / "Recent files.scr").write_text("recent + " + str(flp))
    (shared / "Mapping").mkdir(exist_ok=True)
    (shared / "Mapping" / "Vel curve 2.env").write_text("new")
    (shared / "Auto saved.ncp").write_text("created")
if mode == "crash":
    os.kill(os.getpid(), signal.SIGSEGV)
if mode == "big_partial_crash":
    flp.with_suffix(".wav").write_bytes(b"RIFF" + bytes(96))
    os.kill(os.getpid(), signal.SIGSEGV)
if mode == "sleep":
    time.sleep(60)
elif mode == "ok":
    flp.with_suffix(".wav").write_bytes(b"RIFF" + bytes(60))
elif mode == "tiny":
    flp.with_suffix(".wav").write_bytes(b"RIFF")
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = tmp_path / "fakefl"
    fake.write_text(FAKE.format(py=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    out = tmp_path / "renders"
    monkeypatch.setenv("FL_STUDIO_MCP_RENDER_DIR", str(out))
    monkeypatch.delenv("FAKE_FL_MODE", raising=False)
    return fake, out


def _tmp_dirs():
    return set(Path(tempfile.gettempdir()).glob("flmcp-render-*"))


def test_success(env):
    fake, out = env
    before = _tmp_dirs()
    path = render.render_wav(b"FLhd", "My: song", 30, binary=fake)
    assert path.parent == out and path.exists()
    assert path.name.startswith("My_ song ") and path.suffix == ".wav"
    assert path.read_bytes().startswith(b"RIFF")
    assert _tmp_dirs() <= before


def test_unique_names(env):
    fake, out = env
    a = render.render_wav(b"x", "s", 30, binary=fake)
    b = render.render_wav(b"x", "s", 30, binary=fake)
    assert a != b and a.exists() and b.exists()


def test_no_file(env, monkeypatch):
    fake, _ = env
    monkeypatch.setenv("FAKE_FL_MODE", "none")
    before = _tmp_dirs()
    with pytest.raises(ToolError, match="exit code 0"):
        render.render_wav(b"x", "s", 30, binary=fake)
    assert _tmp_dirs() <= before


def test_timeout_kills(env, monkeypatch):
    fake, _ = env
    monkeypatch.setenv("FAKE_FL_MODE", "sleep")
    started = []
    real_popen = render.subprocess.Popen

    def spy(*a, **k):
        p = real_popen(*a, **k)
        started.append(p)
        return p

    monkeypatch.setattr(render.subprocess, "Popen", spy)
    t0 = time.time()
    with pytest.raises(ToolError, match="did not finish rendering within 1 s"):
        render.render_wav(b"x", "s", 1, binary=fake)
    assert time.time() - t0 < 20
    assert started[0].poll() is not None


def test_purge(env):
    fake, out = env
    out.mkdir(parents=True)
    now = time.time()
    for i in range(35):
        f = out / f"old 2026-01-01 10h00m{i:02d}.wav"
        f.write_bytes(b"x")
        os.utime(f, (now - 1000 + i, now - 1000 + i))
    new = render.render_wav(b"x", "s", 30, binary=fake)
    wavs = list(out.glob("*.wav"))
    assert len(wavs) == render.RENDER_KEEP
    assert new in wavs
    assert not (out / "old 2026-01-01 10h00m00.wav").exists()


def test_purge_never_deletes_a_wav_the_server_did_not_write(env):
    # FL_STUDIO_MCP_RENDER_DIR may point at a folder holding the user's own audio.
    fake, out = env
    out.mkdir(parents=True)
    old = time.time() - 10_000
    foreign = [out / f"{name}.wav" for name in (
        "Kick 01", "My bounce 2026-01-01", "vocal take 3", "mix 2026-01-01 10h00m")]
    foreign += [out / f"Take {i}.wav" for i in range(40)]
    for f in foreign:
        f.write_bytes(b"user audio")
        os.utime(f, (old, old))
    for i in range(35):
        f = out / f"s 2026-01-01 10h00m{i:02d}-{i + 2}.wav"
        f.write_bytes(b"x")
        os.utime(f, (old + i, old + i))
    new = render.render_wav(b"x", "s", 30, binary=fake)
    assert all(f.read_bytes() == b"user audio" for f in foreign)
    ours = [p for p in out.glob("*.wav") if render.RENDER_NAME.search(p.name)]
    assert len(ours) == render.RENDER_KEEP and new in ours
    assert render.RENDER_NAME.search(new.name)


def test_render_command():
    b, f = Path("/x/OsxFL"), Path("/t/a.flp")
    assert render.render_command(b, f, "Darwin") == [str(b), "-R", "-Ewav", str(f)]
    assert render.render_command(b, f, "Windows") == [str(b), "/R", "/Ewav", str(f)]


def test_find_binary_env(tmp_path, monkeypatch):
    exe = tmp_path / "fl"
    exe.write_text("")
    monkeypatch.setenv("FL_STUDIO_MCP_FL_BINARY", str(exe))
    assert render.find_fl_binary() == exe
    monkeypatch.setenv("FL_STUDIO_MCP_FL_BINARY", str(tmp_path / "nope"))
    with pytest.raises(ToolError, match="FL_STUDIO_MCP_FL_BINARY"):
        render.find_fl_binary()


def test_render_timeout():
    assert render.render_timeout(None) == 600
    assert render.render_timeout(0) == 90
    assert render.render_timeout(100) == 290
    assert render.render_timeout(100000) == 1200


def test_tiny_wav_is_error(env, monkeypatch):
    fake, _ = env
    monkeypatch.setenv("FAKE_FL_MODE", "tiny")
    with pytest.raises(ToolError, match="no audio file"):
        render.render_wav(b"x", "s", 30, binary=fake)


def test_lock_released_after_failure(env, monkeypatch):
    fake, _ = env
    monkeypatch.setenv("FAKE_FL_MODE", "none")
    with pytest.raises(ToolError):
        render.render_wav(b"x", "s", 30, binary=fake)
    monkeypatch.setenv("FAKE_FL_MODE", "ok")
    assert render.render_wav(b"x", "s", 30, binary=fake).exists()


def test_renderer_crash_is_retried_once(env, monkeypatch, tmp_path):
    fake, out = env
    counter = tmp_path / "calls"
    monkeypatch.setenv("FAKE_FL_MODE", "crash_once")
    monkeypatch.setenv("FAKE_FL_COUNTER", str(counter))
    path = render.render_wav(b"x", "s", 30, binary=fake)
    assert path.exists() and path.parent == out
    assert counter.read_text() == "2"


def test_renderer_crashing_twice_says_it_crashed(env, monkeypatch, tmp_path):
    fake, _ = env
    counter = tmp_path / "calls"
    monkeypatch.setenv("FAKE_FL_MODE", "crash")
    monkeypatch.setenv("FAKE_FL_COUNTER", str(counter))
    with pytest.raises(ToolError) as excinfo:
        render.render_wav(b"x", "s", 30, binary=fake)
    message = str(excinfo.value)
    assert "renderer crashed (signal 11, SIGSEGV)" in message
    assert "crash dialog" in message and "retrying usually works" in message
    assert "Another render" not in message
    assert counter.read_text() == "2"


def test_normal_failure_is_not_retried(env, monkeypatch, tmp_path):
    fake, _ = env
    counter = tmp_path / "calls"
    monkeypatch.setenv("FAKE_FL_MODE", "none")
    monkeypatch.setenv("FAKE_FL_COUNTER", str(counter))
    with pytest.raises(ToolError, match="exit code 0"):
        render.render_wav(b"x", "s", 30, binary=fake)
    assert counter.read_text() == "1"


def test_partial_wav_of_a_crash_is_removed_before_the_retry(env, monkeypatch, tmp_path):
    fake, _ = env
    counter = tmp_path / "calls"
    monkeypatch.setenv("FAKE_FL_MODE", "partial_crash_once")
    monkeypatch.setenv("FAKE_FL_COUNTER", str(counter))
    path = render.render_wav(b"x", "s", 30, binary=fake)
    assert path.read_bytes().startswith(b"RIFF")
    assert counter.read_text() == "2"


def test_a_renderer_killed_after_writing_a_header_is_retried(env, monkeypatch, tmp_path):
    # 100 bytes passes the size check: a signal still means the file is partial.
    fake, _ = env
    counter = tmp_path / "calls"
    monkeypatch.setenv("FAKE_FL_MODE", "big_partial_crash_once")
    monkeypatch.setenv("FAKE_FL_COUNTER", str(counter))
    path = render.render_wav(b"x", "s", 30, binary=fake)
    assert path.read_bytes() == b"RIFF" + bytes(60)  # the retry's file, not the partial one
    assert counter.read_text() == "2"


def test_a_renderer_killed_twice_after_writing_audio_is_an_error(env, monkeypatch, tmp_path):
    fake, out = env
    counter = tmp_path / "calls"
    monkeypatch.setenv("FAKE_FL_MODE", "big_partial_crash")
    monkeypatch.setenv("FAKE_FL_COUNTER", str(counter))
    with pytest.raises(ToolError, match="renderer crashed"):
        render.render_wav(b"x", "s", 30, binary=fake)
    assert counter.read_text() == "2"
    assert not list(out.glob("*.wav"))


@pytest.fixture
def shared(tmp_path, monkeypatch):
    folder = tmp_path / "shared"
    (folder / "Mapping").mkdir(parents=True)
    (folder / "reg.xml").write_text("user settings")
    (folder / "Recent files.scr").write_text("recent")
    (folder / "Mapping" / "Vel curve 0.env").write_text("curve")
    (folder / "untouched").write_text("same")
    old = time.time() - 3600
    os.utime(folder / "reg.xml", (old, old))
    monkeypatch.setattr(render, "shared_fl_files", lambda: [
        (folder, "reg.xml"), (folder, "Recent files.scr"), (folder, "Auto saved.ncp"),
        (folder, "untouched"), (folder / "Mapping", "*.env"),
    ])
    monkeypatch.setenv("FAKE_FL_SHARED", str(folder))
    return folder


def _assert_restored(folder):
    assert (folder / "reg.xml").read_text() == "user settings"
    assert abs((folder / "reg.xml").stat().st_mtime - (time.time() - 3600)) < 60
    assert (folder / "Recent files.scr").read_text() == "recent"
    assert not (folder / "Auto saved.ncp").exists()  # did not exist before the render
    assert sorted(p.name for p in (folder / "Mapping").iterdir()) == ["Vel curve 0.env"]
    assert (folder / "untouched").read_text() == "same"


def test_render_puts_back_the_shared_files_fl_rewrote(env, shared):
    fake, _ = env
    assert render.render_wav(b"x", "s", 30, binary=fake).exists()
    _assert_restored(shared)


def test_shared_files_are_put_back_after_a_timeout(env, shared, monkeypatch):
    fake, _ = env
    monkeypatch.setenv("FAKE_FL_MODE", "sleep")
    with pytest.raises(ToolError, match="did not finish"):
        render.render_wav(b"x", "s", 1, binary=fake)
    _assert_restored(shared)


def test_shared_files_are_put_back_before_a_retry(env, shared, monkeypatch, tmp_path):
    fake, _ = env
    counter = tmp_path / "calls"
    monkeypatch.setenv("FAKE_FL_MODE", "crash_once")
    monkeypatch.setenv("FAKE_FL_COUNTER", str(counter))
    seen = []
    real = render._run_renderer

    def spy(*args, **kwargs):
        seen.append((shared / "reg.xml").read_text())  # what the next render starts from
        return real(*args, **kwargs)

    monkeypatch.setattr(render, "_run_renderer", spy)
    assert render.render_wav(b"x", "s", 30, binary=fake).exists()
    assert seen == ["user settings", "user settings"]
    _assert_restored(shared)


def test_unchanged_shared_files_are_not_rewritten(tmp_path, monkeypatch):
    folder = tmp_path / "s"
    folder.mkdir()
    (folder / "a").write_text("x")
    monkeypatch.setattr(render, "shared_fl_files", lambda: [(folder, "a"), (folder, "*.env")])
    snap = render.snapshot_shared_files()
    assert render.restore_shared_files(snap) == []
    (folder / "a").write_text("y")
    (folder / "b.env").write_text("new")
    assert sorted(p.name for p in render.restore_shared_files(snap)) == ["a", "b.env"]
    assert (folder / "a").read_text() == "x" and not (folder / "b.env").exists()


def test_shared_files_live_in_fl_user_data(monkeypatch, tmp_path):
    monkeypatch.undo()  # the conftest stub
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path / "Settings"))
    names = [(folder.name, pattern) for folder, pattern in render.shared_fl_files()]
    assert ("Browser", "Recent files.scr") in names and ("Mapping", "*.env") in names
    assert ("Note color presets", "Auto saved.ncp") in names


def test_a_file_unreadable_at_snapshot_is_never_deleted_or_rewritten(tmp_path, monkeypatch):
    folder = tmp_path / "s"
    folder.mkdir()
    locked = folder / "reg.xml"
    locked.write_text("user")
    monkeypatch.setattr(render, "shared_fl_files", lambda: [(folder, "reg.xml")])
    real = Path.read_bytes

    def unreadable(self):
        if self == locked:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    snap = render.snapshot_shared_files()
    monkeypatch.setattr(Path, "read_bytes", real)
    assert snap[locked] == render.UNREADABLE
    locked.write_text("changed by the main FL")
    failed: list[Path] = []
    assert render.restore_shared_files(snap, failed) == [] and failed == []
    assert locked.read_text() == "changed by the main FL"


def test_a_missing_file_is_recorded_as_missing_and_deleted_if_created(tmp_path, monkeypatch):
    folder = tmp_path / "s"
    folder.mkdir()
    monkeypatch.setattr(render, "shared_fl_files", lambda: [(folder, "new")])
    snap = render.snapshot_shared_files()
    assert snap[folder / "new"] is None
    (folder / "new").write_text("x")
    assert render.restore_shared_files(snap) == [folder / "new"]
    assert not (folder / "new").exists()


@pytest.fixture
def recent(tmp_path, monkeypatch):
    folder = tmp_path / "Browser"
    folder.mkdir()
    monkeypatch.setattr(render, "shared_fl_files", lambda: [(folder, "Recent files.scr")])
    monkeypatch.setenv("FL_STUDIO_MCP_RENDER_DIR", str(tmp_path / "renders"))
    return folder / "Recent files.scr"


def test_render_paths_are_removed_from_fl_recent_files(recent, tmp_path):
    work = tmp_path / "work dir"
    lines = [
        "/Users/me/Music/Song A.flp\r\n",
        "/var/folders/xy/T/flmcp-render-ab12/Song A.flp\r\n",
        "/Users/me/Music/Song B.flp\r\n",
        f"{work}/s.flp\r\n",
        "/Users/me/Music/Ünïcode.flp\r\n",
    ]
    recent.write_bytes(render.UTF8_BOM + "".join(lines).encode("utf-8"))
    recent.chmod(0o640)
    assert render.forget_render_paths(work) == 2
    assert recent.read_bytes() == render.UTF8_BOM + "".join(
        [lines[0], lines[2], lines[4]]).encode("utf-8")
    assert stat.S_IMODE(recent.stat().st_mode) == 0o640
    assert not list(recent.parent.glob("*.flmcp-tmp"))


def test_a_user_project_in_the_renders_folder_stays_in_recent_files(recent, tmp_path,
                                                                     monkeypatch):
    projects = tmp_path / "Projects"
    projects.mkdir()
    monkeypatch.setenv("FL_STUDIO_MCP_RENDER_DIR", str(projects))
    recent.write_bytes(render.UTF8_BOM + f"{projects}/My song.flp\n".encode())
    assert render.forget_render_paths(tmp_path / "flmcp-render-x") == 0
    assert recent.read_bytes() == render.UTF8_BOM + f"{projects}/My song.flp\n".encode()


def test_recent_files_without_render_paths_are_not_rewritten(recent):
    recent.write_bytes(render.UTF8_BOM + b"/Users/me/a.flp\n/Users/me/b.flp\n")
    old = time.time() - 3600
    os.utime(recent, (old, old))
    assert render.forget_render_paths() == 0
    assert abs(recent.stat().st_mtime - old) < 2


def test_recent_files_errors_never_raise(recent, monkeypatch, capsys):
    original = b"/Users/me/a.flp\n/tmp/flmcp-render-1/x.flp\n"
    recent.write_bytes(original)
    real_open = open

    def failing_open(path, mode="r", *args, **kwargs):
        if str(path).endswith(".flmcp-tmp") and "w" in mode:
            raise OSError(28, "No space left on device", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)
    assert render.forget_render_paths() == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "could not remove render paths" in captured.err
    assert recent.read_bytes() == original


def test_a_failing_restore_write_leaves_the_file_intact(tmp_path, monkeypatch):
    folder = tmp_path / "s"
    folder.mkdir()
    (folder / "reg.xml").write_text("user")
    monkeypatch.setattr(render, "shared_fl_files", lambda: [(folder, "reg.xml")])
    snap = render.snapshot_shared_files()
    (folder / "reg.xml").write_text("render instance")
    real_open = open

    def failing_open(path, mode="r", *args, **kwargs):
        if str(path).endswith(".flmcp-tmp"):
            fh = real_open(path, mode, *args, **kwargs)
            fh.write(b"par")
            fh.close()
            raise OSError(28, "No space left on device", str(path))
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", failing_open)
    failed: list[Path] = []
    assert render.restore_shared_files(snap, failed) == []
    assert failed == [folder / "reg.xml"]
    assert (folder / "reg.xml").read_text() == "render instance"  # never half-written
    assert sorted(p.name for p in folder.iterdir()) == ["reg.xml"]


def test_a_locked_file_is_written_in_place_when_the_rename_fails(tmp_path, monkeypatch):
    target = tmp_path / "reg.xml"
    target.write_text("old")

    def locked(src, dst):
        raise PermissionError(13, "in use", str(dst))

    monkeypatch.setattr(render.os, "replace", locked)
    render.write_atomic(target, b"new")
    assert target.read_bytes() == b"new" and sorted(tmp_path.iterdir()) == [target]


def test_the_date_of_a_file_rewritten_unchanged_is_put_back(tmp_path, monkeypatch):
    folder = tmp_path / "Mapping"
    folder.mkdir()
    curve = folder / "Vel curve 0.env"
    curve.write_text("curve")
    old = time.time() - 3600
    os.utime(curve, (old, old))
    monkeypatch.setattr(render, "shared_fl_files", lambda: [(folder, "*.env")])
    snap = render.snapshot_shared_files()
    curve.write_text("curve")  # the renderer rewrites the same bytes
    assert render.restore_shared_files(snap) == []
    assert abs(curve.stat().st_mtime - old) < 1


def test_snapshot_looks_twice_before_recording_a_file_as_missing(tmp_path, monkeypatch):
    folder = tmp_path / "s"
    folder.mkdir()
    target = folder / "reg.xml"
    monkeypatch.setattr(render, "shared_fl_files", lambda: [(folder, "reg.xml")])
    real_stat = Path.stat
    calls = []

    def flaky_stat(self, *args, **kwargs):
        if self == target and not calls:
            calls.append(1)
            target.write_text("being replaced")  # appears right after the first look
            raise FileNotFoundError(2, "No such file", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    snap = render.snapshot_shared_files()
    assert snap[target][0] == b"being replaced"


def test_a_render_removes_its_temp_project_from_recent_files(env, recent, monkeypatch):
    fake, _ = env
    # The fake renderer writes its flp path into the file, like FL's render instance.
    monkeypatch.setenv("FAKE_FL_SHARED", str(recent.parent))
    recent.write_bytes(render.UTF8_BOM + b"/Users/me/song.flp\n/T/flmcp-render-old/s.flp\n")
    assert render.render_wav(b"x", "s", 30, binary=fake).exists()
    assert recent.read_bytes() == render.UTF8_BOM + b"/Users/me/song.flp\n"


def test_a_render_that_cannot_clean_recent_files_still_succeeds(env, monkeypatch):
    fake, _ = env

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(render, "forget_render_paths", boom)
    assert render.render_wav(b"x", "s", 30, binary=fake).exists()


def test_files_that_could_not_be_put_back_are_reported_on_stderr(env, monkeypatch, capsys):
    fake, _ = env

    def restore(snap, failed=None, since=None):
        failed.append(Path("/fl/reg.xml"))
        return []

    monkeypatch.setattr(render, "restore_shared_files", restore)
    problems: list[Path] = []
    assert render.render_wav(b"x", "s", 30, binary=fake, problems=problems).exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1 and "/fl/reg.xml" in captured.err
    assert problems == [Path("/fl/reg.xml")]
    # Each render reports into its caller's list: nothing is kept between calls.
    other: list[Path] = []
    monkeypatch.setattr(render, "restore_shared_files", lambda snap, failed=None, since=None: [])
    assert render.render_wav(b"x", "s", 30, binary=fake, problems=other).exists()
    assert other == [] and problems == [Path("/fl/reg.xml")]


def test_a_crash_log_written_during_a_render_is_the_renderers(env, monkeypatch):
    from fl_studio_mcp.utils import errors

    fake, _ = env
    folder = errors.crash_logs_dir()
    folder.mkdir(parents=True)
    real = render._run_renderer

    def crashing_renderer(*args, **kwargs):
        (folder / "Crash render.log").write_text("x")
        return real(*args, **kwargs)

    monkeypatch.setattr(render, "_run_renderer", crashing_renderer)
    start = time.time() - 1
    assert render.render_wav(b"x", "s", 30, binary=fake).exists()
    assert errors.crash_logs_since(start) == []
    log = folder / "Crash main.log"
    log.write_text("x")
    later = time.time() + errors.RENDER_WINDOW_MARGIN + 1
    os.utime(log, (later, later))
    assert errors.crash_logs_since(start) == [log]


# --- Render state shared by every server process (lock, pending snapshot) ---


def test_the_render_lock_ignores_the_renders_folder_setting(env, monkeypatch, tmp_path):
    from fl_studio_mcp.utils import cache

    fake, out = env
    seen = []
    real = render._RenderLock

    class Spy(real):
        def __init__(self, path, blocking=True):
            seen.append(path)
            super().__init__(path, blocking)

    monkeypatch.setattr(render, "_RenderLock", Spy)
    render.render_wav(b"x", "s", 30, binary=fake)
    monkeypatch.setenv("FL_STUDIO_MCP_RENDER_DIR", str(tmp_path / "elsewhere"))
    render.render_wav(b"x", "s", 30, binary=fake)
    assert seen == [cache.cache_dir() / render.LOCK_FILE] * 2
    assert not (out / ".render.lock").exists()


def test_the_snapshot_is_on_disk_while_the_renderer_runs_and_gone_after(env, shared,
                                                                         monkeypatch):
    fake, _ = env
    seen = {}
    real = render._run_renderer

    def spy(binary, flp, timeout, on_start=None):
        def started(proc):
            on_start(proc)
            seen["pending"] = render.load_pending()

        return real(binary, flp, timeout, started)

    monkeypatch.setattr(render, "_run_renderer", spy)
    assert render.render_wav(b"x", "s", 30, binary=fake).exists()
    snap, manifest = seen["pending"]
    assert snap[shared / "reg.xml"][0] == b"user settings"
    assert snap[shared / "Auto saved.ncp"] is None
    assert isinstance(manifest["renderer_pid"], int)
    assert render.load_pending() is None and not render._pending_dir().exists()
    _assert_restored(shared)


def _leave_a_pending_render(shared, work):
    """What a server killed mid-render leaves: its snapshot, then the renderer's changes."""
    snap = render.snapshot_shared_files()
    render.save_pending(snap, work)
    (shared / "reg.xml").write_text("render instance")
    (shared / "Recent files.scr").write_text(f"recent + {work}/s.flp")
    (shared / "Mapping" / "Vel curve 2.env").write_text("new")
    (shared / "Auto saved.ncp").write_text("created")


def test_the_next_render_puts_back_what_a_dead_servers_render_changed(env, shared,
                                                                       monkeypatch):
    fake, _ = env
    monkeypatch.delenv("FAKE_FL_SHARED")  # this render changes nothing itself
    work = Path(tempfile.mkdtemp(prefix=render.TEMP_PREFIX))
    _leave_a_pending_render(shared, work)
    assert render.render_wav(b"x", "s", 30, binary=fake).exists()
    _assert_restored(shared)
    assert not work.exists() and render.load_pending() is None


def test_server_start_puts_back_a_pending_snapshot(shared, tmp_path):
    work = Path(tempfile.mkdtemp(prefix=render.TEMP_PREFIX))
    _leave_a_pending_render(shared, work)
    render.recover_at_startup()
    _assert_restored(shared)
    assert not work.exists() and render.load_pending() is None
    render.recover_at_startup()  # nothing left: a no-op
    _assert_restored(shared)


def test_server_start_leaves_the_snapshot_to_a_render_that_holds_the_lock(shared):
    work = Path(tempfile.mkdtemp(prefix=render.TEMP_PREFIX))
    try:
        _leave_a_pending_render(shared, work)
        with render._RenderLock(render.render_lock_path()):
            render.recover_at_startup()  # must not block nor touch anything
            assert (shared / "reg.xml").read_text() == "render instance"
        assert render.load_pending() is not None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_a_snapshot_whose_restore_fails_is_kept_for_the_next_try(shared, monkeypatch):
    work = Path(tempfile.mkdtemp(prefix=render.TEMP_PREFIX))
    try:
        _leave_a_pending_render(shared, work)

        def boom(snap, failed=None, since=None):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(render, "restore_shared_files", boom)
        render.recover_at_startup()
        assert render.load_pending() is not None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_old_render_temp_folders_are_removed(tmp_path):
    old = Path(tempfile.mkdtemp(prefix=render.TEMP_PREFIX))
    fresh = Path(tempfile.mkdtemp(prefix=render.TEMP_PREFIX))
    other = Path(tempfile.mkdtemp(prefix="not-ours-"))
    try:
        (old / "s.flp").write_bytes(b"x")
        hours_ago = time.time() - 2 * 3600
        for folder in (old, other):
            os.utime(folder, (hours_ago, hours_ago))
        render.clean_temp_dirs()
        assert not old.exists() and fresh.exists() and other.exists()
    finally:
        for folder in (old, fresh, other):
            shutil.rmtree(folder, ignore_errors=True)


SERVER = """
import sys, threading
from pathlib import Path
from fl_studio_mcp.utils import cache, render
cache_root, shared = Path(sys.argv[1]), Path(sys.argv[2])
cache.cache_dir = lambda: cache_root
render.shared_fl_files = lambda: [
    (shared, "reg.xml"), (shared, "Recent files.scr"), (shared, "Auto saved.ncp"),
    (shared, "untouched"), (shared / "Mapping", "*.env")]
render.install_exit_handlers()
# Like the MCP server: the render runs in a worker thread, the main thread waits.
worker = threading.Thread(target=render.render_wav,
                          args=(b"x", "s", 300), kwargs={"binary": Path(sys.argv[3])})
worker.start()
while worker.is_alive():
    worker.join(0.1)
"""


def _start_server(env, shared, tmp_path, monkeypatch):
    import subprocess

    from fl_studio_mcp.utils import cache

    fake, _ = env
    monkeypatch.setenv("FAKE_FL_MODE", "sleep")  # rewrites the shared files, then hangs
    script = tmp_path / "server.py"
    script.write_text(SERVER)
    proc = subprocess.Popen([sys.executable, str(script), str(cache.cache_dir()),
                             str(shared), str(fake)], stderr=subprocess.PIPE)
    deadline = time.time() + 20
    while (shared / "reg.xml").read_text() != "render instance":
        assert time.time() < deadline and proc.poll() is None, "the render never started"
        time.sleep(0.05)
    manifest = None
    while manifest is None or manifest.get("renderer_pid") is None:
        assert time.time() < deadline
        loaded = render.load_pending()
        manifest = loaded[1] if loaded else None
        time.sleep(0.05)
    return proc, manifest


def _gone(pid, timeout=10.0):
    from fl_studio_mcp.utils import cache

    deadline = time.time() + timeout
    while cache.pid_alive(pid) and time.time() < deadline:
        time.sleep(0.05)
    return not cache.pid_alive(pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_a_server_killed_mid_render_is_recovered_at_the_next_start(env, shared, tmp_path,
                                                                    monkeypatch):
    import signal

    proc, manifest = _start_server(env, shared, tmp_path, monkeypatch)
    renderer = manifest["renderer_pid"]
    work = Path(manifest["work"])
    proc.send_signal(signal.SIGKILL)  # nothing runs: no finally, no atexit
    proc.wait(timeout=10)
    assert (shared / "reg.xml").read_text() == "render instance"
    render.recover_at_startup()
    assert _gone(renderer)  # the orphaned renderer was stopped
    _assert_restored(shared)
    assert not work.exists() and render.load_pending() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_sigterm_mid_render_stops_the_renderer_and_puts_the_files_back(env, shared, tmp_path,
                                                                        monkeypatch):
    import signal

    proc, manifest = _start_server(env, shared, tmp_path, monkeypatch)
    proc.send_signal(signal.SIGTERM)
    _, err = proc.communicate(timeout=30)
    assert proc.returncode == 128 + signal.SIGTERM, err
    assert _gone(manifest["renderer_pid"])
    _assert_restored(shared)
    assert not Path(manifest["work"]).exists() and render.load_pending() is None


# --- Re-review: real server exit, stale snapshots, other settings, unknown PID ---


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
def test_the_real_server_exits_at_once_on_a_signal_with_stdin_open(tmp_path, signame):
    import json
    import signal
    import subprocess

    sig = getattr(signal, signame)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home)}
    env.pop("FL_STUDIO_MCP_SETTINGS_DIR", None)
    env.pop("FL_STUDIO_MCP_RENDER_DIR", None)
    proc = subprocess.Popen(
        [sys.executable, "-c", "from fl_studio_mcp import server; server.main()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"}}}
        proc.stdin.write((json.dumps(hello) + "\n").encode())
        proc.stdin.flush()
        assert b'"result"' in proc.stdout.readline()  # the stdio loop is running
        sent = time.monotonic()
        proc.send_signal(sig)  # stdin stays open, like a client that only signals
        assert proc.wait(timeout=5) == 128 + sig
        assert time.monotonic() - sent < 5
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()


def test_a_stale_snapshot_is_discarded_not_put_back(shared, capsys):
    work = Path(tempfile.mkdtemp(prefix=render.TEMP_PREFIX))
    snap = render.snapshot_shared_files()
    render.save_pending(snap, work, time.time() - render.RECOVER_MAX_AGE - 60)
    # Days of the user's own changes since.
    (shared / "Recent files.scr").write_text("a week of projects")
    (shared / "Mapping" / "My curve.env").write_text("user curve")
    render.recover_at_startup()
    assert (shared / "Recent files.scr").read_text() == "a week of projects"
    assert (shared / "Mapping" / "My curve.env").read_text() == "user curve"
    assert render.load_pending() is None and not work.exists()
    assert "too old to put back" in capsys.readouterr().err


def test_files_untouched_since_the_snapshot_are_never_restored_or_deleted(shared):
    work = Path(tempfile.mkdtemp(prefix=render.TEMP_PREFIX))
    created = time.time()
    render.save_pending(render.snapshot_shared_files(), work, created)
    before = created - 600  # written before the snapshot: not the renderer's
    (shared / "Recent files.scr").write_text("other bytes, older date")
    os.utime(shared / "Recent files.scr", (before, before))
    (shared / "Mapping" / "Old curve.env").write_text("older than the snapshot")
    os.utime(shared / "Mapping" / "Old curve.env", (before, before))
    (shared / "reg.xml").write_text("render instance")  # the renderer's write: reverted
    (shared / "Mapping" / "Vel curve 2.env").write_text("new")  # the renderer's: deleted
    render.recover_at_startup()
    assert (shared / "Recent files.scr").read_text() == "other bytes, older date"
    assert (shared / "Mapping" / "Old curve.env").exists()
    assert (shared / "reg.xml").read_text() == "user settings"
    assert not (shared / "Mapping" / "Vel curve 2.env").exists()


def test_the_post_render_restore_still_reverts_every_renderer_write(tmp_path, monkeypatch):
    folder = tmp_path / "s"
    folder.mkdir()
    (folder / "a").write_text("x")
    (folder / "gone").write_text("kept")
    monkeypatch.setattr(render, "shared_fl_files",
                        lambda: [(folder, "a"), (folder, "gone"), (folder, "*.env")])
    taken = time.time()
    snap = render.snapshot_shared_files()
    (folder / "a").write_text("renderer")
    (folder / "gone").unlink()  # deleted by the renderer: put back
    (folder / "b.env").write_text("renderer")
    assert sorted(p.name for p in render.restore_shared_files(snap, since=taken)) == [
        "a", "b.env", "gone"]
    assert (folder / "a").read_text() == "x" and (folder / "gone").read_text() == "kept"
    assert not (folder / "b.env").exists()


def test_recovery_sweeps_the_recorded_folders_not_its_own_settings(tmp_path, monkeypatch,
                                                                   capsys):
    # Server A (default settings) died mid-render; server B, configured with
    # another settings folder, recovers its snapshot.
    a, b = tmp_path / "A" / "Mapping", tmp_path / "B" / "Mapping"
    for folder in (a, b):
        folder.mkdir(parents=True)
        (folder / "Default.env").write_text("default")
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path / "A"))
    monkeypatch.setattr(render, "shared_fl_files", lambda: [(a, "*.env")])
    work = Path(tempfile.mkdtemp(prefix=render.TEMP_PREFIX))
    render.save_pending(render.snapshot_shared_files(), work)
    (a / "Vel curve 2.env").write_text("the renderer's")
    (b / "B curve.env").write_text("B's own")
    monkeypatch.setenv("FL_STUDIO_MCP_SETTINGS_DIR", str(tmp_path / "B"))
    monkeypatch.setattr(render, "shared_fl_files", lambda: [(b, "*.env")])
    render.recover_at_startup()
    assert sorted(p.name for p in b.iterdir()) == ["B curve.env", "Default.env"]
    assert sorted(p.name for p in a.iterdir()) == ["Default.env"]
    assert "those files are put back" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="ps")
def test_a_renderer_whose_pid_was_never_recorded_is_found_and_stopped(shared):
    import subprocess

    work = Path(tempfile.mkdtemp(prefix=render.TEMP_PREFIX))
    _leave_a_pending_render(shared, work)
    assert render.load_pending()[1]["renderer_pid"] is None
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                               str(work / "s.flp")])
    try:
        render.recover_at_startup()
        assert orphan.wait(timeout=10) == -9
        _assert_restored(shared)
        assert render.load_pending() is None and not work.exists()
    finally:
        if orphan.poll() is None:
            orphan.kill()
        orphan.wait()


def test_recovery_waits_while_a_renderer_cannot_be_stopped(shared, monkeypatch, capsys):
    work = Path(tempfile.mkdtemp(prefix=render.TEMP_PREFIX))
    try:
        _leave_a_pending_render(shared, work)
        monkeypatch.setattr(render, "_processes_running_in", lambda w: [999_999_999])
        render.recover_at_startup()
        assert (shared / "reg.xml").read_text() == "render instance"  # untouched
        assert render.load_pending() is not None and work.exists()
        assert "still runs" in capsys.readouterr().err
    finally:
        shutil.rmtree(work, ignore_errors=True)
