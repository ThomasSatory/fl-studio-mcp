"""The server's per-user cache folder: renders, the render lock and render state.

The render lock, the persisted render windows (see errors.render_window) and
the pending snapshot of FL's shared files (see render.py) live here whatever
FL_STUDIO_MCP_RENDER_DIR says: every server process, whatever its
configuration, must see the same ones.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path


def cache_dir() -> Path:
    """~/Library/Caches/fl-studio-mcp (macOS), %LOCALAPPDATA%\\fl-studio-mcp (Windows),
    ~/.cache/fl-studio-mcp elsewhere; created if needed."""
    system = platform.system()
    if system == "Darwin":
        path = Path.home() / "Library" / "Caches" / "fl-studio-mcp"
    elif system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        path = Path(base) / "fl-studio-mcp"
    else:
        path = Path.home() / ".cache" / "fl-studio-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def pid_alive(pid: int) -> bool | None:
    """Whether process `pid` runs (POSIX); None when it cannot be told (Windows)."""
    if os.name == "nt" or pid <= 0:
        return None  # os.kill(pid, 0) would send CTRL_C_EVENT on Windows
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True
