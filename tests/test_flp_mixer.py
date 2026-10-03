"""Mixer routing and insert disabling in the .flp, used to render per-track stems."""

from __future__ import annotations

import struct

import pytest

from fl_studio_mcp.utils.flp import (
    FLPError,
    Project,
    disable_inserts,
    insert_count,
    mixer_routes,
    stem_keep_set,
    stem_project_bytes,
)
from tests.flp_builder import add_mixer, build_project

ROUTES = {1: [0], 2: [0], 3: [0], 4: [1], 7: [1, 8], 8: [1], 19: [3], 24: [2, 19]}
LAST = 26


def mixer_project() -> Project:
    project = build_project()
    add_mixer(project, [ROUTES.get(i, []) for i in range(LAST + 1)])
    return Project.parse(project.to_bytes())


def flags_of(project: Project) -> list[int]:
    return [struct.unpack_from("<I", e.value, 4)[0] for e in project.events if e.id == 236]


def test_routes_and_count_round_trip():
    project = mixer_project()
    assert insert_count(project) == LAST + 1
    routes = mixer_routes(project)
    for i in range(LAST + 1):
        assert routes[i] == set(ROUTES.get(i, []))


def test_stem_keep_set():
    routes = mixer_routes(mixer_project())
    assert stem_keep_set(routes, 7) == {0, 7, 1, 8}
    assert stem_keep_set(routes, 1) == {0, 1, 4, 7, 8}
    assert stem_keep_set(routes, 3) == {0, 3, 19, 24}


def test_disable_inserts_clears_only_enabled_bit():
    project = mixer_project()
    disable_inserts(project, [5, 6])
    flags = flags_of(project)
    assert flags[5] == flags[6] == 0x4C & ~0x08
    assert flags[4] == 0x4C


def test_disable_inserts_refuses_bad_indices():
    project = mixer_project()
    for bad in (0, LAST, LAST + 1, -1):
        with pytest.raises(FLPError):
            disable_inserts(project, [bad])
    with pytest.raises(FLPError):
        disable_inserts(build_project(), [1])


def test_stem_project_bytes():
    data = mixer_project().to_bytes()
    keep = {0, 7, 1, 8}
    flags = flags_of(Project.parse(stem_project_bytes(data, 7)))
    for i, value in enumerate(flags):
        if i in keep or i == LAST:
            assert value == 0x4C
        else:
            assert value == 0x4C & ~0x08
    for bad in (0, LAST, LAST + 5):
        with pytest.raises(FLPError):
            stem_project_bytes(data, bad)
