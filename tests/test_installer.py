"""The Claude configuration script, run against fake `uv` and `claude` binaries."""

from __future__ import annotations

import json
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
    # A closed PATH: only the fakes plus the system binaries the script needs
    # (bash, python3, mkdir...). The real `claude` or `uv` must never be found.
    env = {
        "HOME": str(home),
        "PATH": f"{bindir}:/usr/bin:/bin",
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
        "run",
        "--directory",
        str(REPO),
        "fl-studio-mcp",
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
