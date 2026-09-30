#!/bin/bash
# Configure Claude Desktop and/or Claude Code to use the FL Studio MCP server.
#
# Environment (all optional, set by install.sh):
#   FL_MCP_CLIENT  desktop | code | both | none   (otherwise an interactive menu)
#   FL_MCP_YES     1 to skip every prompt
#   UV_BIN         absolute path to uv (otherwise resolved from PATH)

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo ""
echo -e "${BLUE}FL Studio MCP - Claude Configuration${NC}"
echo "======================================"
echo ""
echo -e "Project directory: ${CYAN}$PROJECT_DIR${NC}"

# GUI apps (Claude Desktop) do not inherit the shell PATH, so the config must
# carry uv's absolute path.
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "$UV_BIN" ]]; then
    echo -e "${YELLOW}Warning: uv is not installed.${NC} The MCP server needs it:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    UV_BIN="uv"
fi
echo -e "uv: ${CYAN}$UV_BIN${NC}"
echo ""

if [[ "$OSTYPE" == "darwin"* ]]; then
    CLAUDE_DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "win32" || "$OSTYPE" == "cygwin" ]]; then
    CLAUDE_DESKTOP_CONFIG="$APPDATA/Claude/claude_desktop_config.json"
else
    CLAUDE_DESKTOP_CONFIG="$HOME/.config/Claude/claude_desktop_config.json"
fi

print_manual_config() {
    echo ""
    echo -e "${CYAN}Manual configuration${NC}"
    echo "Claude Desktop config file: $CLAUDE_DESKTOP_CONFIG"
    echo "Claude Code: claude mcp add --scope user fl-studio -- \"$UV_BIN\" run --directory \"$PROJECT_DIR\" fl-studio-mcp"
    echo ""
    echo "JSON to add under \"mcpServers\":"
    echo '{'
    echo '  "mcpServers": {'
    echo '    "fl-studio": {'
    echo "      \"command\": \"$UV_BIN\","
    echo "      \"args\": [\"run\", \"--directory\", \"$PROJECT_DIR\", \"fl-studio-mcp\"]"
    echo '    }'
    echo '  }'
    echo '}'
    echo ""
}

configure_desktop() {
    echo -e "Configuring ${CYAN}Claude Desktop${NC}..."
    mkdir -p "$(dirname "$CLAUDE_DESKTOP_CONFIG")"
    if [[ ! -f "$CLAUDE_DESKTOP_CONFIG" ]]; then
        echo '{}' > "$CLAUDE_DESKTOP_CONFIG"
    fi
    if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$CLAUDE_DESKTOP_CONFIG" 2>/dev/null; then
        echo -e "  ${YELLOW}Invalid JSON in config: backing it up and starting fresh.${NC}"
        cp "$CLAUDE_DESKTOP_CONFIG" "${CLAUDE_DESKTOP_CONFIG}.backup"
        echo '{}' > "$CLAUDE_DESKTOP_CONFIG"
    fi
    python3 - "$CLAUDE_DESKTOP_CONFIG" "$UV_BIN" "$PROJECT_DIR" <<'PYEOF'
import json, sys
path, uv_bin, project_dir = sys.argv[1:4]
with open(path) as f:
    config = json.load(f)
config.setdefault("mcpServers", {})["fl-studio"] = {
    "command": uv_bin,
    "args": ["run", "--directory", project_dir, "fl-studio-mcp"],
}
with open(path, "w") as f:
    json.dump(config, f, indent=2)
PYEOF
    echo -e "  ${GREEN}✓${NC} fl-studio added to $CLAUDE_DESKTOP_CONFIG"
}

configure_code() {
    echo -e "Configuring ${CYAN}Claude Code${NC}..."
    if ! command -v claude >/dev/null 2>&1; then
        echo -e "  ${YELLOW}The 'claude' command is not installed; add the server manually:${NC}"
        print_manual_config
        return 0
    fi
    if claude mcp add --scope user fl-studio -- "$UV_BIN" run --directory "$PROJECT_DIR" fl-studio-mcp; then
        echo -e "  ${GREEN}✓${NC} fl-studio registered in Claude Code (user scope)"
    else
        echo -e "  ${YELLOW}'claude mcp add' refused (an organisation policy can forbid local MCP servers).${NC}"
        print_manual_config
    fi
}

CHOICE="${FL_MCP_CLIENT:-}"
if [[ -z "$CHOICE" ]]; then
    if [[ "${FL_MCP_YES:-}" == "1" ]]; then
        CHOICE="both"
    else
        echo "Select which Claude client(s) to configure:"
        echo "  1) Claude Desktop only"
        echo "  2) Claude Code only"
        echo "  3) Both"
        echo "  4) Show manual configuration"
        echo "  5) Skip"
        read -p "Enter choice [1-5]: " menu
        case "$menu" in
            1) CHOICE="desktop" ;;
            2) CHOICE="code" ;;
            3) CHOICE="both" ;;
            4) print_manual_config; exit 0 ;;
            *) CHOICE="none" ;;
        esac
    fi
fi

case "$CHOICE" in
    desktop) configure_desktop ;;
    code) configure_code ;;
    both) configure_desktop; echo ""; configure_code ;;
    none) echo "Skipping Claude configuration." ;;
    *) echo -e "${RED}Unknown FL_MCP_CLIENT '$CHOICE' (desktop|code|both|none)${NC}"; exit 1 ;;
esac

echo ""
echo -e "${GREEN}Configuration complete.${NC} Restart Claude Desktop/Code to load the server."
