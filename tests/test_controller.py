"""The controller script, executed under pytest with fake FL Studio modules."""

from __future__ import annotations

import json

import pytest

from tests.fakes import load_controller, make_fl_modules


@pytest.fixture
def fl(tmp_path, monkeypatch):
    modules = make_fl_modules()
    controller = load_controller(monkeypatch, tmp_path, modules)
    return controller, modules


def test_controller_dispatches_with_fake_modules(fl):
    controller, modules = fl
    modules["transport"].isPlaying.return_value = 1

    result = controller.dispatch_command("transport.getStatus", {})

    assert result["is_playing"] is True
    assert result["loop_mode"] == "pattern"


def test_execute_pending_command_writes_response_file(fl):
    controller, _ = fl
    controller.COMMAND_FILE.write_text(json.dumps({"action": "transport.getStatus", "params": {}}))

    controller.execute_pending_command()

    response = json.loads(controller.RESPONSE_FILE.read_text())
    assert response["success"] is True
    assert response["is_playing"] is False
