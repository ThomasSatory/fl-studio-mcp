"""Shared test setup: fresh project-file state, and no real FL process check."""

from __future__ import annotations

import pytest

from fl_studio_mcp.utils import errors, project_files, render


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
    monkeypatch.setattr(render, "shared_fl_files", lambda: [])
