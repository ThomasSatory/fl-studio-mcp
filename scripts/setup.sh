#!/bin/bash
# FL Studio MCP Setup Script
#
# This script:
# 1. Installs Python dependencies (uv, or pip as a fallback)
# 2. Checks/guides virtual MIDI port setup (IAC Driver on Mac, loopMIDI on Windows)
# 3. Installs the FL Studio MIDI controller script
# 4. Installs the Piano Roll script (ComposeWithLLM)
#
# FL_MCP_YES=1 skips every prompt.

set -e

echo "=========================================="
echo "FL Studio MCP Setup"
echo "=========================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# FL Studio paths
FL_SETTINGS="$HOME/Documents/Image-Line/FL Studio/Settings"
FL_HARDWARE="$FL_SETTINGS/Hardware"
MCP_CONTROLLER_DIR="$FL_HARDWARE/FLStudioMCP"
FL_PIANO_SCRIPTS="$FL_SETTINGS/Piano roll scripts"

# Detect OS
if [[ "$OSTYPE" == "darwin"* ]]; then
    OS="mac"
elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" || "$OSTYPE" == "cygwin" ]]; then
    OS="windows"
else
    OS="linux"
fi

# Step 1: Python dependencies (needed below to list the MIDI ports)
echo "Step 1: Installing Python dependencies..."
echo "----------------------------------------------"

cd "$PROJECT_DIR"

if command -v uv &> /dev/null; then
    echo "Using uv to install dependencies..."
    uv sync
    echo -e "${GREEN}✓ Dependencies installed with uv${NC}"
elif command -v pip &> /dev/null; then
    echo "Using pip to install dependencies..."
    pip install -e .
    echo -e "${GREEN}✓ Dependencies installed with pip${NC}"
else
    echo -e "${YELLOW}⚠ Neither uv nor pip found. Please install dependencies manually:${NC}"
    echo "  pip install -e ."
fi

# Step 2: Virtual MIDI Setup
echo ""
echo "Step 2: Checking Virtual MIDI Setup..."
echo "----------------------------------------------"

if [[ "$OS" == "mac" ]]; then
    # Ask CoreMIDI (through mido) for the real list of output ports: the audio
    # device list does not show MIDI ports, and the IAC bus name is localised.
    IAC_PORTS=""
    if command -v uv &> /dev/null; then
        IAC_PORTS="$(uv run python -c 'import mido; print("\n".join(p for p in mido.get_output_names() if "IAC" in p))' 2>/dev/null || true)"
    fi
    if [[ -n "$IAC_PORTS" ]]; then
        echo -e "${GREEN}✓ IAC bus online:${NC} $IAC_PORTS"
    else
        echo -e "${YELLOW}⚠ No IAC bus is online (the bus name is localised, e.g. 'Gestionnaire IAC Bus 1' in French).${NC}"
        echo ""
        echo "To enable it:"
        echo "  1. Open 'Audio MIDI Setup' (search in Spotlight)"
        echo "  2. Window > Show MIDI Studio"
        echo "  3. Double-click 'IAC Driver' and tick 'Device is online'"
        echo ""
        if [[ "${FL_MCP_YES:-}" != "1" ]]; then
            read -p "Press Enter to open Audio MIDI Setup, or 's' to skip: " choice
            if [[ "$choice" != "s" && "$choice" != "S" ]]; then
                open -a "Audio MIDI Setup"
                echo ""
                read -p "Press Enter when the IAC Driver is online..."
            fi
        fi
    fi
elif [[ "$OS" == "windows" ]]; then
    echo "Windows detected. Please ensure loopMIDI is installed and running."
    echo "Download from: https://www.tobias-erichsen.de/software/loopmidi.html"
    echo ""
    echo "Create a virtual MIDI port in loopMIDI (any name works)."
    echo ""
    if [[ "${FL_MCP_YES:-}" != "1" ]]; then
        read -p "Press Enter when loopMIDI is configured..."
    fi
fi

# Step 3: Install FL Studio MIDI Controller
echo ""
echo "Step 3: Installing FL Studio MIDI Controller..."
echo "----------------------------------------------"

if [[ ! -d "$FL_HARDWARE" ]]; then
    echo "Creating FL Studio Hardware directory..."
    mkdir -p "$FL_HARDWARE"
fi

if [[ ! -d "$MCP_CONTROLLER_DIR" ]]; then
    echo "Creating FLStudioMCP directory..."
    mkdir -p "$MCP_CONTROLLER_DIR"
fi

CONTROLLER_SOURCE="$PROJECT_DIR/fl_controller/device_FLStudioMCP.py"
CONTROLLER_DEST="$MCP_CONTROLLER_DIR/device_FLStudioMCP.py"

if [[ -f "$CONTROLLER_SOURCE" ]]; then
    cp "$CONTROLLER_SOURCE" "$CONTROLLER_DEST"
    echo -e "${GREEN}✓ Copied device_FLStudioMCP.py to FL Studio${NC}"
    echo "  Location: $MCP_CONTROLLER_DIR"
else
    echo -e "${RED}✗ Controller script not found at: $CONTROLLER_SOURCE${NC}"
    exit 1
fi

# Step 4: Install Piano Roll Script
echo ""
echo "Step 4: Installing Piano Roll Script..."
echo "----------------------------------------------"

if [[ ! -d "$FL_PIANO_SCRIPTS" ]]; then
    echo "Creating Piano roll scripts directory..."
    mkdir -p "$FL_PIANO_SCRIPTS"
fi

PIANO_SOURCE="$SCRIPT_DIR/ComposeWithLLM.pyscript"
PIANO_DEST="$FL_PIANO_SCRIPTS/ComposeWithLLM.pyscript"

if [[ -f "$PIANO_SOURCE" ]]; then
    cp "$PIANO_SOURCE" "$PIANO_DEST"
    echo -e "${GREEN}✓ Copied ComposeWithLLM.pyscript to FL Studio${NC}"
    echo "  Location: $FL_PIANO_SCRIPTS"
else
    echo -e "${YELLOW}⚠ Piano Roll script not found at: $PIANO_SOURCE${NC}"
    echo "  Piano Roll tools will not be available."
fi

echo ""
echo "=========================================="
echo "Setup Complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo ""
echo "1. ${YELLOW}Restart FL Studio${NC} (if it's running)"
echo ""
echo "2. Enable the MIDI controller in FL Studio:"
echo "   - Go to Options > MIDI Settings"
if [[ "$OS" == "mac" ]]; then
    echo "   - Under 'Input', find your IAC bus (e.g. 'IAC Driver Bus 1' or 'Gestionnaire IAC Bus 1')"
else
    echo "   - Under 'Input', find your loopMIDI port"
fi
echo "   - Set the controller type to: ${GREEN}FLStudioMCP${NC}"
echo "   - Enable the port (click to highlight)"
echo ""
echo "3. For Piano Roll features, first run in FL Studio:"
echo "   ${GREEN}Tools > Scripting > ComposeWithLLM${NC}"
echo ""
echo "4. Start the MCP server:"
echo "   ${GREEN}uv run fl-studio-mcp${NC}"
echo ""
echo "For troubleshooting, check:"
echo "  - FL Studio's View > Script output (for controller logs)"
echo "  - MCP server terminal output"
echo ""
