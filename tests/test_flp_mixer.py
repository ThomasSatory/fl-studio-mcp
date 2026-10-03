"""Mixer routing and insert disabling in the .flp, used to render per-track stems."""

from __future__ import annotations

import struct

import pytest

from fl_studio_mcp.utils.flp import (
    Event,
    FLPError,
    Project,
    disable_inserts,
    downstream_inserts,
    insert_count,
    mixer_routes,
    stem_keep_set,
    stem_project_bytes,
    upstream_inserts,
    zero_routes,
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


def routing_bytes(project: Project) -> list[bytes]:
    return [bytes(e.value) for e in project.events if e.id == 235]


def small_mixer(routes: dict[int, list[int]], last: int = 6) -> Project:
    project = build_project()
    add_mixer(project, [routes.get(i, []) for i in range(last + 1)])
    return Project.parse(project.to_bytes())


def test_zero_routes_keeps_lengths_and_other_inserts():
    project = mixer_project()
    before = routing_bytes(project)
    zero_routes(project, {7: {1, 30}, 24: {19}})
    after = routing_bytes(Project.parse(project.to_bytes()))
    assert [len(b) for b in after] == [len(b) for b in before]
    routes = mixer_routes(Project.parse(project.to_bytes()))
    assert routes[7] == {8} and routes[24] == {2}
    for i in range(LAST + 1):
        if i not in (7, 24):
            assert after[i] == before[i]
    with pytest.raises(FLPError):
        zero_routes(project, {LAST + 1: {0}})


def test_upstream_and_downstream_inserts():
    routes = mixer_routes(mixer_project())
    assert upstream_inserts(routes, 1) == {4, 7, 8}
    assert downstream_inserts(routes, 7) == {0, 1, 8}
    # audio_routes replaces the file's routes for the inserts it lists
    assert downstream_inserts(routes, 7, {7: {8}, 8: {1}, 1: set()}) == {1, 8}


def test_sidechain_is_not_followed_downstream():
    # kick 3 -> Master + sidechain (level 0) into bass 5
    routes = mixer_routes(small_mixer({3: [0, 5], 5: [0]}))
    assert 5 in stem_keep_set(routes, 3)  # the file alone cannot tell
    keep = stem_keep_set(routes, 3, audio_routes={3: {0}, 0: set()})
    assert keep == {0, 3}
    data = small_mixer({3: [0, 5], 5: [0]}).to_bytes()
    flags = flags_of(Project.parse(stem_project_bytes(data, 3, {3: {0}, 0: set()})))
    assert flags[5] & 0x08 == 0 and flags[3] & 0x08


def test_send_source_loses_its_other_routes_in_the_send_stem():
    # vocal 3 -> Master + send to reverb 5; reverb 5 -> Master; 4 -> 3 (a feeder of vocal)
    project = small_mixer({3: [0, 5], 4: [3, 0], 5: [0]})
    data = project.to_bytes()
    stem = Project.parse(stem_project_bytes(data, 5))
    routes = mixer_routes(stem)
    assert routes[3] == {5}  # 3 -> 0 cut, 3 -> 5 kept
    assert routes[4] == {3}  # 4 -> 0 cut, 4 -> 3 (upstream of 5) kept
    assert routes[5] == {0}
    assert [len(b) for b in routing_bytes(stem)] == [len(b) for b in routing_bytes(project)]
    flags = flags_of(stem)
    assert [i for i, f in enumerate(flags) if f & 0x08] == [0, 3, 4, 5, 6]
    # the vocal's stem: its feeder 4 only reaches Master through 3; 3 itself is untouched
    vocal = mixer_routes(Project.parse(stem_project_bytes(data, 3)))
    assert vocal[4] == {3} and vocal[3] == {0, 5}


def test_short_flags_value_is_refused():
    project = mixer_project()
    flags = [e for e in project.events if e.id == 236]
    flags[5].value = bytes(6)
    with pytest.raises(FLPError, match="shorter than 8 bytes"):
        disable_inserts(project, [5])
    with pytest.raises(FLPError, match="shorter than 8 bytes"):
        stem_project_bytes(project.to_bytes(), 7)


def test_mismatched_routing_and_flags_counts_are_refused():
    project = mixer_project()
    project.events.append(Event(236, bytes(12)))
    with pytest.raises(FLPError, match="routing events"):
        stem_project_bytes(project.to_bytes(), 7)
