"""The real FastMCP server: exact tool set and one annotation class per tool."""

from __future__ import annotations

import asyncio
import re

from fl_studio_mcp.server import mcp

READ_ONLY = {
    "fl_connection_status", "fl_get_transport_status", "fl_get_song_length", "fl_get_tempo",
    "fl_get_mixer_track_info", "fl_get_all_mixer_tracks", "fl_get_channel_info",
    "fl_get_all_channels", "fl_get_step_sequence", "fl_get_plugin_params",
    "fl_get_plugin_param_value", "fl_get_piano_roll_state", "fl_get_pr_context",
    "fl_get_piano_roll_info", "fl_list_patterns", "fl_get_undo_status",
    "fl_get_focused_window", "fl_get_project_snapshot",
}
IDEMPOTENT = {
    "fl_set_song_position", "fl_set_loop_mode", "fl_set_playback_speed", "fl_set_tempo",
    "fl_set_track_volume", "fl_set_track_pan", "fl_set_track_name", "fl_set_track_color",
    "fl_set_stereo_separation", "fl_set_channel_volume", "fl_set_channel_pan",
    "fl_set_channel_name", "fl_set_channel_color", "fl_route_channel_to_mixer",
    "fl_set_grid_bit", "fl_select_channel", "fl_set_plugin_param_value",
    "fl_select_pattern", "fl_rename_pattern", "fl_show_window", "fl_hide_window",
    "fl_focus_window", "fl_open_piano_roll",
}
DESTRUCTIVE = {
    "fl_clear_piano_roll", "fl_delete_notes", "fl_send_notes", "fl_set_step_sequence",
    "fl_undo", "fl_redo", "fl_clear_request_queue",
}
ACTION = {
    "fl_play", "fl_stop", "fl_record", "fl_trigger_note", "fl_send_chord",
    "fl_trigger_script", "fl_next_preset", "fl_prev_preset", "fl_new_pattern",
    "fl_clone_pattern", "fl_save_undo_point", "fl_mute_track", "fl_solo_track",
    "fl_arm_track", "fl_mute_channel", "fl_solo_channel",
}
REMOVED = {
    "fl_get_channel_count", "fl_get_selected_channel", "fl_select_one_channel",
    "fl_get_grid_bit", "fl_get_mixer_track_count", "fl_is_plugin_valid",
    "fl_get_plugin_name", "fl_get_plugin_param_count", "fl_get_preset_count",
    "fl_get_plugin_color", "fl_get_current_pattern", "fl_connect",
}


def registered():
    return {tool.name: tool for tool in asyncio.run(mcp.list_tools())}


def hints(tool):
    a = tool.annotations
    return (a.read_only_hint, a.destructive_hint, a.idempotent_hint)


def test_exact_tool_set():
    names = set(registered())
    assert len(names) == 64
    assert names == READ_ONLY | IDEMPOTENT | DESTRUCTIVE | ACTION
    assert not names & REMOVED


def test_every_tool_is_annotated():
    missing = [name for name, tool in registered().items() if tool.annotations is None]
    assert missing == []


def test_annotation_classes():
    tools = registered()
    for name in READ_ONLY:
        assert hints(tools[name])[0] is True, name
    for name in IDEMPOTENT:
        assert hints(tools[name]) == (False, False, True), name
    for name in DESTRUCTIVE:
        assert hints(tools[name])[:2] == (False, True), name
    for name in ACTION:
        assert hints(tools[name]) == (False, False, False), name


def test_instructions_match_the_tool_set():
    text = mcp.instructions
    assert "Cannot create new patterns" not in text
    assert "C4 = 60" in text and "C5" in text
    for name in ("fl_get_project_snapshot", "fl_open_piano_roll", "fl_send_chord",
                 "fl_save_undo_point", "fl_connection_status"):
        assert name in text, name
    for name in REMOVED:
        assert not re.search(rf"\b{name}\b", text), name
