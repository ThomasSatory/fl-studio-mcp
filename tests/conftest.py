"""Shared test setup: fresh project-file state, and no real FL process check."""

from __future__ import annotations

import threading

import pytest

from fl_studio_mcp.utils import cache, errors, project_files, render


@pytest.fixture(autouse=True)
def _fresh_project_file_state(monkeypatch, tmp_path):
    monkeypatch.setattr(project_files, "_last_project", None)
    monkeypatch.setattr(project_files, "_reload_finished_at", None)
    # pgrep would see the developer's FL Studio, or none on CI.
    monkeypatch.setattr(project_files, "fl_process_running", lambda: True)
    # FL's real crash logs (and its shared settings files) stay out of the tests.
    crash_dir = tmp_path / "fl-user-data" / "Support" / "Logs" / "Crash"
    monkeypatch.setattr(errors, "crash_logs_dir", lambda: crash_dir)
    monkeypatch.setattr(errors, "_last_ok", None)
    monkeypatch.setattr(errors, "fl_started_at", lambda: None)  # no real ps
    monkeypatch.setattr(errors, "_render_windows", [])
    monkeypatch.setattr(errors, "_last_change_ok", None)
    monkeypatch.setattr(errors, "_started_cache", None)
    monkeypatch.setattr(errors, "_crash_check", None)
    monkeypatch.setattr(errors, "_warned_no_process", False)
    # The render lock, persisted render windows and pending snapshot stay out of ~/Library.
    cache_root = tmp_path / "fl-studio-mcp-cache"

    def test_cache_dir():
        cache_root.mkdir(exist_ok=True)
        return cache_root

    monkeypatch.setattr(cache, "cache_dir", test_cache_dir)
    monkeypatch.setattr(render, "_active", None)
    monkeypatch.setattr(render, "_stopping", threading.Event())
    monkeypatch.setattr(render, "shared_fl_files", lambda: [])
