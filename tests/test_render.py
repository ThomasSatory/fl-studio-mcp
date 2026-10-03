import os
import stat
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastmcp.exceptions import ToolError

from fl_studio_mcp.utils import render

FAKE = """#!{py}
import os, sys, time
from pathlib import Path
mode = os.environ.get("FAKE_FL_MODE", "ok")
flp = Path(sys.argv[-1])
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
        f = out / f"old{i}.wav"
        f.write_bytes(b"x")
        os.utime(f, (now - 1000 + i, now - 1000 + i))
    new = render.render_wav(b"x", "s", 30, binary=fake)
    wavs = list(out.glob("*.wav"))
    assert len(wavs) == render.RENDER_KEEP
    assert new in wavs
    assert not (out / "old0.wav").exists()


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
