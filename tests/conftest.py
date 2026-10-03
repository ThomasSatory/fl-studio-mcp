"""Shared test setup: fresh project-file state, and no real FL process check."""

from __future__ import annotations

import pytest

from fl_studio_mcp.utils import project_files


@pytest.fixture(autouse=True)
def _fresh_project_file_state(monkeypatch):
    monkeypatch.setattr(project_files, "_last_project", None)
    monkeypatch.setattr(project_files, "_known_files", {})
    monkeypatch.setattr(project_files, "_mcp_changes", False)
    monkeypatch.setattr(project_files, "_reload_finished_at", None)
    # pgrep would see the developer's FL Studio, or none on CI.
    monkeypatch.setattr(project_files, "fl_process_running", lambda: True)
