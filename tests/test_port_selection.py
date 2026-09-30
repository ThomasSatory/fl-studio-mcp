"""select_output_port never picks a hardware port by accident."""

from __future__ import annotations

import pytest

from fl_studio_mcp.utils.midi_connection import (
    MIDI_PORT_ENV,
    PortSelectionError,
    select_output_port,
)

HARDWARE = ["Arturia KeyStep 32", "LUMI Keys BLOCK", "FLkey 37 MIDI"]


def test_requested_exact_name_wins():
    assert select_output_port(["Gestionnaire IAC Bus 1", "loopMIDI Port"], "loopMIDI Port") == (
        "loopMIDI Port"
    )


def test_requested_substring_is_case_insensitive():
    assert select_output_port(["Gestionnaire IAC Bus 1"], "iac bus") == "Gestionnaire IAC Bus 1"


def test_requested_name_not_found_lists_ports():
    with pytest.raises(PortSelectionError) as excinfo:
        select_output_port(HARDWARE, "IAC Driver Bus 1")
    message = str(excinfo.value)
    assert MIDI_PORT_ENV in message
    assert "Arturia KeyStep 32" in message


@pytest.mark.parametrize(
    "ports, expected",
    [
        (["Arturia KeyStep 32", "Gestionnaire IAC Bus 1"], "Gestionnaire IAC Bus 1"),
        (["IAC Driver Bus 1", "IAC Driver Bus 2"], "IAC Driver Bus 1"),
        (["LUMI Keys BLOCK", "loopMIDI Port"], "loopMIDI Port"),
        (["Midi Through Port-0", "USB MIDI"], "Midi Through Port-0"),
    ],
)
def test_virtual_port_hints(ports, expected):
    assert select_output_port(ports, None) == expected


def test_hardware_only_ports_are_refused():
    with pytest.raises(PortSelectionError) as excinfo:
        select_output_port(HARDWARE, None)
    message = str(excinfo.value)
    assert "FLkey 37 MIDI" in message
    assert "IAC" in message and MIDI_PORT_ENV in message


def test_no_ports_at_all_is_refused():
    with pytest.raises(PortSelectionError):
        select_output_port([], None)
