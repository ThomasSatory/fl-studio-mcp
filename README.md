# FL Studio MCP Server

An MCP (Model Context Protocol) server that enables AI assistants to control FL Studio through MIDI communication and Piano Roll scripts.

### Quick Demo
dont mind the scuffed audio, i had to clip with my mic bc apple wouldnt let me record desktop audio lol

https://github.com/user-attachments/assets/d4fc668f-9fe5-4ab4-9f18-76cd661029c6

### Original Audio
If you wanted to hear the better audio

https://github.com/user-attachments/assets/c2b1a5e7-1640-41fa-82bc-18ca7cbae9e8

## Features

### Transport Control

- Play, pause, stop playback
- Toggle recording
- Set playback position
- Get song length and position
- Control loop mode (pattern/song)
- Adjust playback speed

### Mixer Control

- Get/set track volume and pan
- Mute/solo tracks
- Arm tracks for recording
- Set track names and colors
- Stereo separation control

### Channel Rack Control

- List all channels
- Get/set channel properties (volume, pan, name, color)
- Mute/solo channels
- Route channels to mixer tracks
- Trigger MIDI notes in real-time
- Step sequencer control (get/set grid bits)

### Plugin Control

- List plugin parameters
- Get/set parameter values
- Navigate presets (next/previous)
- Query plugin info

### Piano Roll Control

- **Add notes** by MIDI number or name (`"C4"`, `"F#3"`, `"Bb2"` — C4 = 60; FL Studio's UI shows MIDI 60 as "C5")
- **Add chords** by symbol (`"Am7"`, `"Cmaj9/E"`, `"F#m7b5"`) with octave and inversion, or as a list of notes
- Per-note velocity, pan, colour group, slide and portamento
- **Delete specific notes**, **clear** the piano roll, **read** its state
- Every write is verified: the tool reports how many notes FL Studio applied and fails loudly otherwise
- Auto-triggering via keystroke (Cmd+Opt+Y on macOS, Ctrl+Alt+Y on Windows/Linux)

## Important Limitations

### Cannot Load Plugins

The FL Studio scripting API does **not** support loading new VST/AU plugins. You can only control parameters of plugins that are already loaded in your project.

## Requirements

- **FL Studio 20.7+** (MIDI Controller Scripting API)
- **Python 3.10+**
- **macOS**, **Windows**, or **Linux** (FL Studio running under Wine)
  - macOS: IAC Driver (built-in, needs to be enabled)
  - Windows: [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html)
  - Linux: no virtual-MIDI package needed - the kernel's "Midi Through" ALSA port bridges into Wine; [xdotool](https://man.archlinux.org/man/xdotool.1) is required for Piano Roll auto-triggering on X11 (see [Linux (FL Studio under Wine)](#linux-fl-studio-under-wine))

## Which AI Clients Work With This?

This is a standard [MCP](https://modelcontextprotocol.io) server that talks to clients over stdio — it isn't hardcoded to any one AI vendor. In principle, **any MCP-compatible client can connect**: Claude Desktop, Claude Code, Cursor, Windsurf, Gemini CLI/Gemini's MCP support, OpenAI's Codex CLI/Agents SDK MCP support, etc.

**What's actually tested and auto-configured:** only **Claude Desktop** and **Claude Code**, via `scripts/install_mcp_for_claude.sh` (macOS/Linux) and `scripts/install_mcp_for_claude.ps1` (Windows). Other clients (Gemini, OpenAI-based tools, etc.) are not tested against this server and have no installer support — you'd need to manually add an equivalent MCP server entry to that client's own config, pointing at:

```json
{
  "command": "uv",
  "args": ["run", "--directory", "/path/to/fl-studio-mcp", "fl-studio-mcp"]
}
```

(adjust `/path/to/fl-studio-mcp` to your local clone). If you try this with a non-Claude client, please open an issue with what worked/didn't — the protocol should support it, but it hasn't been verified here.

## Quick Installation

The easiest way to install is using the provided setup script for your platform.

### macOS / Linux

```bash
# Clone the repository
git clone https://github.com/karl-andres/fl-studio-mcp.git
cd fl-studio-mcp

# Run the one-command installer
./install.sh
```

### Windows

```powershell
# Clone the repository
git clone https://github.com/karl-andres/fl-studio-mcp.git
cd fl-studio-mcp

# Allow running local scripts for this user (one-time)
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force

# Run the one-command installer
.\install.ps1
```

`install.ps1` is the Windows counterpart to `install.sh` — same steps, PowerShell instead of bash, and it installs Python 3.12 specifically (required for prebuilt `python-rtmidi` wheels on Windows). It has not been updated for the `--client`/`--yes` options or the absolute `uv` path.

Both installers will:

1. Install [uv](https://github.com/astral-sh/uv) if not present
2. Install Python dependencies
3. Guide you through enabling virtual MIDI ports (IAC Driver on Mac, loopMIDI on Windows)
4. Install the FL Studio MIDI controller script
5. Install the Piano Roll script (ComposeWithLLM)
6. Configure Claude Desktop or Claude Code automatically (`scripts/install_mcp_for_claude.sh` / `scripts/install_mcp_for_claude.ps1`)

> **Windows piano-roll auto-trigger note:** the Piano Roll script is launched by sending FL Studio a keystroke (`Ctrl+Alt+Y`). On Windows this requires briefly foregrounding the FL Studio window, so **FL Studio will pop to the front for a moment** each time a tool like `fl_send_notes` runs. This is expected. See [Piano Roll script not triggering](#piano-roll-script-not-triggering) if it doesn't fire at all.

## Manual Installation

### 1. Install Python Dependencies

```bash
# Using uv (recommended)
uv sync

# Or using pip
pip install -e .
```

### 2. Enable Virtual MIDI Ports

#### macOS (IAC Driver)

1. Open **Audio MIDI Setup** (search in Spotlight)
2. Press **Cmd+2** or go to **Window > Show MIDI Studio**
3. Double-click on **IAC Driver**
4. Check **"Device is online"**
5. Click **Apply**

#### Windows (loopMIDI)

1. Download and install [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html)
2. Create a virtual port (any name works)
3. Keep loopMIDI running while using FL Studio

#### Linux (nothing to install)

Wine's ALSA MIDI driver exposes Linux ALSA sequencer ports to Windows
applications, so the built-in kernel port **"Midi Through Port-0"**
(`snd-seq-dummy`, almost always loaded) serves as the virtual cable:

```bash
# should list: client 14: 'Midi Through' ... 'Midi Through Port-0'
aconnect -l | grep -A1 "Midi Through"
```

### 3. Install FL Studio Scripts

Copy the controller script to FL Studio's Hardware folder:

```bash
# macOS
mkdir -p ~/Documents/Image-Line/FL\ Studio/Settings/Hardware/FLStudioMCP
cp fl_controller/device_FLStudioMCP.py ~/Documents/Image-Line/FL\ Studio/Settings/Hardware/FLStudioMCP/

# Windows
mkdir "%USERPROFILE%\Documents\Image-Line\FL Studio\Settings\Hardware\FLStudioMCP"
copy fl_controller\device_FLStudioMCP.py "%USERPROFILE%\Documents\Image-Line\FL Studio\Settings\Hardware\FLStudioMCP\"

# Linux (FL Studio under Wine) - ~/.wine/drive_c/users/<user>/Documents is
# typically a symlink to your XDG documents folder, so this is the same
# place FL Studio writes to
mkdir -p ~/.wine/drive_c/users/$USER/Documents/Image-Line/FL\ Studio/Settings/Hardware/FLStudioMCP
mkdir -p ~/.wine/drive_c/users/$USER/Documents/Image-Line/FL\ Studio/Settings/Piano\ roll\ scripts
cp fl_controller/device_FLStudioMCP.py ~/.wine/drive_c/users/$USER/Documents/Image-Line/FL\ Studio/Settings/Hardware/FLStudioMCP/
cp scripts/ComposeWithLLM.pyscript ~/.wine/drive_c/users/$USER/Documents/Image-Line/FL\ Studio/Settings/Piano\ roll\ scripts/
```

Copy the Piano Roll script:

```bash
# macOS
cp scripts/ComposeWithLLM.pyscript ~/Documents/Image-Line/FL\ Studio/Settings/Piano\ roll\ scripts/

# Windows
copy scripts\ComposeWithLLM.pyscript "%USERPROFILE%\Documents\Image-Line\FL Studio\Settings\Piano roll scripts\"
```

### 4. Configure FL Studio

1. **Restart FL Studio** (if it's running)
2. Go to **Options > MIDI Settings**
3. Under **Input**, find your virtual MIDI port (e.g., "IAC Driver Bus 1", "loopMIDI", or - on Linux/Wine - "Midi Through Port-0")
4. Set the **Controller type** to **FLStudioMCP**
5. Enable the port (click to highlight it)

### 5. Configure Claude (or another MCP client)

Add to your Claude Desktop config:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "fl-studio": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/fl-studio-mcp", "fl-studio-mcp"]
    }
  }
}
```

Or for Claude Code, add to your MCP settings (`~/.claude.json`, or run `claude mcp add`).

Using a different MCP-compatible client (Gemini, an OpenAI-based tool, Cursor, etc.)? The same `command`/`args` pair above is all any MCP host needs — add it to that client's own MCP config in whatever format it expects. See [Which AI Clients Work With This?](#which-ai-clients-work-with-this) for what's actually been tested.

### Configuration (environment variables)

| Variable | Purpose |
|---|---|
| `FL_STUDIO_MCP_MIDI_PORT` | Name (or case-insensitive substring) of the MIDI output port to use. Without it the server only accepts a port containing `IAC`, `loopMIDI` or `Midi Through`, and refuses to send anything to another device (a hardware synth would otherwise receive the trigger note). |
| `FL_STUDIO_MCP_SETTINGS_DIR` | FL Studio's `Settings` folder when it is not the default (Options > File settings > User data folder). |

On a French macOS the IAC bus is called `Gestionnaire IAC Bus 1`; it contains `IAC`, so it is picked up automatically.

`./install.sh --client desktop|code|both|none --yes` runs without prompts. Claude Code is configured through `claude mcp add`; if your organisation forbids local MCP servers the script prints the configuration to apply by hand. The Claude Desktop entry carries uv's absolute path because GUI apps do not inherit the shell PATH.

Tool failures (FL Studio not reachable, invalid argument, script that did not run) are reported as MCP errors (`isError`), not as text starting with "Error:".

## Linux (FL Studio under Wine)

The MCP server runs natively on Linux and drives FL Studio running under
[Wine](https://www.winehq.org/) (verified with FL Studio 2026 / Wine 11).
No Windows Python and no loopMIDI required - the pieces line up like this:

- **MIDI bridge**: the server sends its trigger note to the kernel's
  **"Midi Through Port-0"** ALSA port. Wine's ALSA MIDI driver enumerates
  ALSA sequencer ports as Windows MIDI devices, so FL Studio lists it as a
  regular MIDI input. Enable it in FL Studio under *Options > MIDI Settings*
  with controller type **FLStudioMCP**. The port is provided by the kernel,
  so nothing has to keep running in the background.
- **Shared files**: FL Studio resolves its settings folder through
  `~/.wine/drive_c/users/<user>/Documents/Image-Line/FL Studio/Settings`,
  which Wine typically symlinks to your XDG documents folder. The server
  resolves the same location from the Linux side, so the JSON
  command/response files match up automatically.
- **Piano Roll trigger**: same Ctrl+Alt+Y keystroke as on Windows, sent via
  [xdotool](https://man.archlinux.org/man/xdotool.1) - the FL Studio window
  is briefly foregrounded first, exactly like on Windows. This requires an
  **X11 session**. On Wayland the keystroke cannot be synthesized; run the
  script manually from the Piano Roll's *Tools > Scripting* menu instead.

The one-command `install.sh` is macOS-oriented; on Linux follow the
[Manual Installation](#manual-installation) steps above.

Two environment variables are available if autodetection does not fit your
setup:

| Variable | Purpose |
|----------|---------|
| `FL_STUDIO_MCP_SETTINGS_DIR` | Full path to FL Studio's `Settings` folder (overrides Wine-prefix autodetection) |
| `FL_STUDIO_MCP_MIDI_PORT` | MIDI output port name (exact or substring match) to use instead of "Midi Through" |

> **Port selection note:** on Linux the server deliberately only opens a
> port with "Midi Through" in its name instead of falling back to the first
> available output - the first port could be a real hardware device (your
> keyboard or audio interface). Set `FL_STUDIO_MCP_MIDI_PORT` to override.

## Usage

### Running the Server Manually

```bash
# Using uv
uv run fl-studio-mcp

# Or after installation
fl-studio-mcp
```

### Piano Roll Workflow

1. Open FL Studio and select a channel
2. Open the Piano Roll (F7 or double-click the channel)
3. Once per FL Studio session, manually run the script from the piano roll: **Tools > Scripting > ComposeWithLLM**
4. After that, the MCP tools will auto-trigger the script

The trigger keystroke only re-runs the *last* piano roll script, so after every
FL Studio start (and possibly after loading another project) step 3 is needed
again. The note tools report whether the script actually ran; if the request
stays queued, repeat step 3 and call the tool again.

Before sending the keystroke the server brings FL Studio to the front and asks
the controller script to focus the piano roll, so the trigger works even when
the playlist or mixer was FL's focused window. An open piano roll does not
follow channels selected via scripting; call `fl_open_piano_roll(channel)` to
retarget it before writing notes to a specific channel.

> On Linux the auto-trigger uses `xdotool` (X11 sessions only) and sends the
> same Ctrl+Alt+Y keystroke as on Windows.

## Available Tools

### Connection

| Tool | Description |
|------|-------------|
| `fl_connection_status` | Connection status; reconnect=True to reconnect |

### Transport

| Tool | Description |
|------|-------------|
| `fl_play` | Start/pause playback |
| `fl_stop` | Stop playback |
| `fl_record` | Toggle recording |
| `fl_get_transport_status` | Get playback/recording state |
| `fl_set_song_position` | Move the playhead by 1-based bar, seconds or ticks |
| `fl_get_song_length` | Song length in seconds, ms, ticks and bars |
| `fl_set_loop_mode` | Switch between pattern/song mode |
| `fl_set_playback_speed` | Adjust playback speed (0.25x-4x) |
| `fl_get_tempo` | Get the project tempo (BPM) |
| `fl_set_tempo` | Set the project tempo (10-522 BPM), verified by reading it back |

### Mixer

| Tool | Description |
|------|-------------|
| `fl_get_mixer_track_info` | Get track details |
| `fl_get_all_mixer_tracks` | Track count and used tracks |
| `fl_set_track_volume` | Set track volume as fader position or in dB, verified by reading it back |
| `fl_set_track_pan` | Set track pan, verified |
| `fl_mute_track` | Mute/unmute track |
| `fl_solo_track` | Solo/unsolo track |
| `fl_arm_track` | Arm track for recording |
| `fl_set_track_name` | Rename track |
| `fl_set_track_color` | Set track color |
| `fl_set_stereo_separation` | Adjust stereo width |
| `fl_set_send` | Route a track to another one (sends / buses) with a level |
| `fl_get_mixer_routing` | Where a track sends its signal |
| `fl_list_effects` | Effects loaded in a track's slots |
| `fl_get_mixer_levels` | Peak dBFS per track while playing; flags clipping and silent tracks |

### Channels

| Tool | Description |
|------|-------------|
| `fl_get_channel_info` | Get channel details |
| `fl_get_all_channels` | List channels (volume, pan, mute, solo, selection, mixer target) |
| `fl_select_channel` | Select/deselect a channel; exclusive=True deselects the others |
| `fl_trigger_note` | Trigger MIDI note (real-time) |
| `fl_set_channel_volume` | Set channel volume |
| `fl_set_channel_pan` | Set channel pan |
| `fl_mute_channel` | Mute/unmute channel |
| `fl_solo_channel` | Solo/unsolo channel |
| `fl_set_channel_name` | Rename channel |
| `fl_set_channel_color` | Set channel color |
| `fl_route_channel_to_mixer` | Route to mixer track |
| `fl_set_grid_bit` | Set step sequencer step |
| `fl_get_step_sequence` | Get full pattern |
| `fl_set_step_sequence` | Set full pattern |
| `fl_write_steps` | Step-sequencer notes with pitch, velocity, pan and shift, in any channel and pattern - no piano roll, no keystroke |
| `fl_get_steps` | Active steps with their pitch/velocity/pan/shift |
| `fl_add_sample_channels` | Add Sampler channels playing audio files from disk (project file edit + reload) |

### Samples

| Tool | Description |
|------|-------------|
| `fl_find_samples` | Search FL's browser folders (or a folder) by words and kind (kick, 808, hihat...) |
| `fl_analyze_audio` | Peak/RMS dBFS, clipping, stereo width, low/mid/high balance, pitch (to tune 808s) |
| `fl_preview_sample` | Play a file on the computer's speakers (macOS) |

### Plugins

| Tool | Description |
|------|-------------|
| `fl_get_plugin_params` | Plugin name, validity, presets, colour and parameters |
| `fl_get_plugin_param_value` | Get parameter value |
| `fl_set_plugin_param_value` | Set parameter value by index |
| `fl_set_plugin_param` | Set a parameter by its name ("Mix", "Cutoff") |
| `fl_next_preset` | Next preset |
| `fl_prev_preset` | Previous preset |

### Project

| Tool | Description |
|------|-------------|
| `fl_get_project_snapshot` | Transport, tempo, patterns, channels and mixer tracks in one call |
| `fl_save_project` | Save (Ctrl+S) and return the .flp path |
| `fl_open_project` | Open a .flp in the running FL Studio (e.g. a backup) |

### Arrangement (playlist)

| Tool | Description |
|------|-------------|
| `fl_get_arrangement` | Every clip (pattern, track, bar, length), markers and automation clips, read from the saved project |
| `fl_edit_arrangement` | One batch of place / delete / copy / move clips and create or edit automation clips |
| `fl_get_playlist_tracks` | Playlist track names, colours, mute, solo |
| `fl_set_playlist_track` | Rename, colour, mute or solo a playlist track |
| `fl_add_marker` | Add a time marker at a bar |

FL Studio's scripting API cannot read or place playlist clips, create
automation clips or load samples. `fl_get_arrangement`, `fl_edit_arrangement`
and `fl_add_sample_channels` therefore work on the project file: they save
the project (Ctrl+S), read the `.flp`, and for edits write the change into it
and make FL Studio reopen the project (`open` on macOS, the file association
on Windows). Expect a few seconds per edit, a reset undo history and a copy of
the previous file in the project's `Backup` folder (`<name> (before MCP edit
HHhMMmSS).flp`, reopen it with `fl_open_project`). The project must have been
saved once (an untitled project makes FL show a Save As dialog). Files the
parser cannot rewrite byte for byte are refused. Automation targets: tempo,
mixer volume/pan/stereo/EQ, effect mix level, any effect or instrument
parameter, channel volume/pan/pitch.

### Windows / focus

| Tool | Description |
|------|-------------|
| `fl_get_focused_window` | Which FL windows are visible and which one has focus, plus the focused window's caption |
| `fl_show_window` | Show an FL window (mixer, channel_rack, playlist, piano_roll, browser) and optionally focus it |
| `fl_hide_window` | Hide an FL window |
| `fl_focus_window` | Give an FL window FL-internal focus |
| `fl_open_piano_roll` | Retarget the piano roll to a channel (hide, select, show) and focus it - use before `fl_send_notes` |

### Patterns

| Tool | Description |
|------|-------------|
| `fl_list_patterns` | List patterns and the active one |
| `fl_select_pattern` | Select/activate a pattern |
| `fl_new_pattern` | Create a pattern (next empty one), optionally named |
| `fl_clone_pattern` | Clone a pattern (closes the piano roll - FL behaviour) |
| `fl_rename_pattern` | Rename a pattern |
| `fl_set_pattern_color` | Set a pattern's colour |

Requires FL Studio 2024+ (the `patterns` scripting module); older versions
get a clear error message.

### Undo / Redo

| Tool | Description |
|------|-------------|
| `fl_save_undo_point` | Create a labelled undo checkpoint before batch edits |
| `fl_undo` | Undo (Ctrl+Z equivalent) |
| `fl_redo` | Redo |
| `fl_get_undo_status` | Undo history count/position, project-changed flag |

Note: FL's undo system does not track every scripting mutation (pattern
renames, for example, are not revertible); user actions and piano-roll edits
are.

### Piano Roll

| Tool | Description |
|------|-------------|
| `fl_send_notes` | Add notes by MIDI number or name (C4 = 60), with velocity, pan, colour, slide, porta; `channel` and `pattern` pick where they go |
| `fl_send_chord` | Add a chord by symbol ("Am7", "Cmaj9/E") or list of notes; same `channel` / `pattern` |
| `fl_delete_notes` | Delete specific notes |
| `fl_clear_piano_roll` | Clear all notes |
| `fl_get_piano_roll_state` | Read current piano roll notes |
| `fl_get_pr_context` | Read piano roll context: time signature, PPQ, snap-to-scale, selected channel, active pattern |
| `fl_trigger_script` | Manually trigger the FL Studio script |
| `fl_get_piano_roll_info` | Get piano roll system info |
| `fl_clear_request_queue` | Cancel pending queued changes |

### Changed in 0.4.0

`fl_set_song_position(position, mode)` became `fl_set_song_position(bar |
seconds | ticks)`: the old modes were off by one (mode 2, "seconds", sent
ticks). `fl_get_song_length` read the wrong units (ticks were bars, seconds
were ticks) and now returns seconds, milliseconds, ticks, PPQ and bars.
`fl_set_track_volume` takes `volume` or `volume_db`, and says 0.8 is 0 dB (it
claimed 1.0). The controller script is at version 3: copy
`fl_controller/device_FLStudioMCP.py` into FL Studio again (setup.sh) and
restart FL Studio; `fl_connection_status` reports a stale one.

### Removed in 0.3.0

| Removed | Use instead |
|---|---|
| `fl_connect` | `fl_connection_status(reconnect=True)` |
| `fl_get_channel_count`, `fl_get_selected_channel` | `fl_get_all_channels` |
| `fl_select_one_channel` | `fl_select_channel(index, exclusive=True)` |
| `fl_get_grid_bit` | `fl_get_step_sequence` |
| `fl_get_mixer_track_count` | `fl_get_all_mixer_tracks` (`track_count`) |
| `fl_is_plugin_valid`, `fl_get_plugin_name`, `fl_get_plugin_param_count`, `fl_get_preset_count`, `fl_get_plugin_color` | `fl_get_plugin_params` |
| `fl_get_current_pattern` | `fl_list_patterns` (`current`) |

`fl_send_chord` now takes `chord` (symbol or list) instead of `midi_notes`.
`fl_get_all_mixer_tracks` and `fl_get_plugin_params` now return an object
instead of a list. Note objects in `fl_send_notes` reject unknown fields;
`fl_delete_notes` accepts notes copied from `fl_get_piano_roll_state` as is.
`fl_connect` raised an error when the connection failed;
`fl_connection_status(reconnect=True)` returns the status instead - check its
`connected` and `error` fields.
After updating, rerun `scripts/setup.sh` so FL Studio gets the new controller
and ComposeWithLLM scripts.

## Example Workflows

### Adjusting a Mix

```text
"Set the volume of mixer track 1 to 80% and pan it slightly left"
```

### Creating a Drum Pattern

```text
"Create a basic kick pattern on channel 0 with kicks on steps 0, 4, 8, and 12"
```

### Adding a Melody to Piano Roll

```text
"Add a C major arpeggio starting at beat 0: C4, E4, G4, C5 - each note quarter duration"
```

### Adding Chords

```text
"Add a C major chord at beat 0, then F major at beat 2, then G major at beat 4"
```

### Automating Plugin Parameters

```text
"List the parameters of the plugin on channel 0 and set the filter cutoff to 50%"
```

### Arranging a Song

```text
"Put Intro on track 1 for bars 1-8, the Hook pattern from bar 9 repeated 4 times,
copy bars 9-25 to bar 41, and fade the master volume out over the last 8 bars"
```

## Troubleshooting

### "Not connected to FL Studio"

1. Ensure FL Studio is running
2. Check that the FLStudioMCP controller is enabled in MIDI Settings
3. On Mac, verify IAC Driver is enabled in Audio MIDI Setup
4. On Windows, verify loopMIDI is running
5. On Linux/Wine, run `aconnect -l` - while FL Studio is running, "Midi Through Port-0" should show a connection to the Wine MIDI client
6. Restart FL Studio after enabling the controller

### "Timeout waiting for FL Studio response"

1. Make sure FL Studio is in focus
2. Check the Script output window in FL Studio (View > Script output)
3. Verify the controller is receiving MIDI (look for activity in MIDI Settings)

### Piano Roll script not triggering

1. Once per FL Studio session: manually run **Tools > Scripting > ComposeWithLLM** from the piano roll (the keystroke only re-runs the last script, so this is needed after every FL Studio start)
2. On macOS: the app that runs the MCP server (Claude Desktop, Terminal, Cursor...) needs **Accessibility** permission (System Settings > Privacy & Security > Accessibility) to send the keystroke; the tools now say so explicitly when macOS refuses it. `fl_write_steps` needs no keystroke at all
3. On Windows: the MCP server foregrounds the FL Studio window automatically before sending the hotkey — if FL Studio isn't running or is minimized to the system tray, the trigger can't find it and will fall back to a warning telling you to press the hotkey manually
4. Try pressing Cmd+Opt+Y (macOS) or Ctrl+Alt+Y (Windows/Linux) manually to confirm the hotkey itself is bound to the script in FL Studio
5. On Linux: `xdotool` must be installed and the session must be X11 - on Wayland, run the script manually from Tools > Scripting
6. If you just updated the server code (e.g. pulled a fix to the trigger logic), **fully restart** your MCP client (Claude Desktop/Code) — reconnecting the MCP server alone does not respawn the underlying process, so it can keep running stale code

### "FL Studio is not running"

The piano roll tools check for an FL Studio process before activating it and never launch it (AppleScript's `activate` would). Start FL Studio yourself, open a piano roll, run ComposeWithLLM once, then retry; a queued request is processed on the next successful trigger.

If the message says *Could not determine whether FL Studio is running*, the check itself failed: on macOS the app running the MCP server (Claude Desktop, a terminal) needs Automation access to System Events in System Settings > Privacy & Security > Automation.

### "controller script is outdated"

The server only accepts a response that echoes its request id. An older `device_FLStudioMCP.py` answers without one: re-run `scripts/setup.sh` (or copy the file from `fl_controller/`) and restart FL Studio.

### No MIDI ports available

- **macOS**: Enable IAC Driver in Audio MIDI Setup
- **Windows**: Install and run loopMIDI
- **Linux**: check `aconnect -l` - the kernel "Midi Through" port (client 14) should always be present; if not, load it with `modprobe snd-seq-dummy`

## Architecture

This MCP server uses a hybrid approach:

```text
┌─────────────────┐     ┌─────────────────────────────────────────┐
│   MCP Client    │────▶│           FastMCP Server                │
│  (Claude, etc)  │     │                                         │
└─────────────────┘     │  ┌─────────────────┐  ┌──────────────┐  │
                        │  │ MIDI + JSON     │  │ Piano Roll   │  │
                        │  │ Tools           │  │ Tools (JSON) │  │
                        │  └────────┬────────┘  └──────┬───────┘  │
                        └───────────┼──────────────────┼──────────┘
                                    │                  │
                               MIDI + JSON        JSON Files +
                                    │              Keystroke
                                    ▼                  ▼
                        ┌─────────────────────────────────────────┐
                        │              FL Studio                   │
                        │  ┌──────────────┐  ┌──────────────────┐ │
                        │  │FLStudioMCP   │  │ Piano Roll Script│ │
                        │  │(MIDI Ctrl)   │  │ (ComposeWithLLM) │ │
                        │  └──────────────┘  └──────────────────┘ │
                        └─────────────────────────────────────────┘
```

### How It Works

Every command carries a request id and the server ignores any response that does not echo it, so a late answer to a timed-out command can never be mistaken for the current one. Commands are serialised (one at a time), and the command file is written atomically on the server side; FL Studio writes its response in place (its embedded Python cannot rename files safely).

1. **Transport/Mixer/Channels/Plugins**:
   - MCP server writes command to JSON file
   - Sends MIDI trigger note to FL Studio
   - FL Studio controller script reads JSON, executes API, writes response
   - MCP server reads response

2. **Piano Roll**:
   - MCP server writes note requests to JSON file
   - Sends keystroke (Cmd+Opt+Y on macOS, Ctrl+Alt+Y on Windows/Linux) to trigger FL Studio script — on Windows and Linux, the FL Studio window is foregrounded first so the keystroke actually reaches it
   - Piano Roll script reads JSON and modifies notes

## Development

### Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) (recommended)

### Setup

```bash
# Install all dependencies including dev extras
uv sync --dev

# Or with pip
pip install -e ".[dev]"
```

### Available Commands

| Command | Description |
|---------|-------------|
| `uv run fl-studio-mcp` | Run the MCP server |
| `uv run ruff check .` | Lint the codebase |
| `uv run ruff check --fix .` | Lint and auto-fix |
| `uv run pytest` | Run tests |

### Project Structure

```
fl-studio-mcp/
├── fl_controller/
│   └── device_FLStudioMCP.py    # FL Studio MIDI controller script (runs inside FL Studio)
├── scripts/
│   ├── setup.sh                  # FL Studio script installer
│   ├── install_mcp_for_claude.sh # Claude config installer (macOS/Linux)
│   ├── install_mcp_for_claude.ps1 # Claude config installer (Windows)
│   └── ComposeWithLLM.pyscript   # Piano Roll script (runs inside FL Studio)
├── src/fl_studio_mcp/
│   ├── server.py                # FastMCP server entry point
│   ├── tools/                   # MCP tool implementations
│   │   ├── channels.py
│   │   ├── mixer.py
│   │   ├── piano_roll.py
│   │   ├── plugins.py
│   │   └── transport.py
│   └── utils/
│       ├── connection.py        # FL Studio connection wrapper
│       ├── fl_trigger.py        # Piano roll keystroke trigger
│       └── midi_connection.py   # MIDI + JSON communication layer
├── install.sh                   # One-command installer (macOS/Linux)
└── install.ps1                  # One-command installer (Windows)
```

## Credits

- [FL Studio API Stubs](https://github.com/IL-Group/FL-Studio-API-Stubs) - API documentation
- [FastMCP](https://github.com/jlowin/fastmcp) - MCP server framework
- [mido](https://github.com/mido/mido) - MIDI library for Python
- [calvinw/fl-studio-mcp](https://github.com/calvinw/fl-studio-mcp) - Piano Roll integration approach
- [Image-Line](https://www.image-line.com/) - FL Studio

## License

MIT
