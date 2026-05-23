#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Agent-OS MCP Passthrough — One-Command Startup
# ─────────────────────────────────────────────────────────────
# Starts Agent-OS server + MCP passthrough wrapper.
# NO LLM API KEY NEEDED — MCP client's LLM handles reasoning.
#
# Usage:
#   ./run_mcp.sh                          # Auto-detect everything
#   ./run_mcp.sh --token my-secret        # Custom token
#   ./run_mcp.sh --port 9000              # Custom port
#   ./run_mcp.sh --headed                 # Show browser window
#   ./run_mcp.sh --mcp-only               # Only start MCP (server already running)
#   ./run_mcp.sh --server-only            # Only start server (for external MCP)
#
# After running, configure Claude Desktop:
#   {
#     "mcpServers": {
#       "agent-os": {
#         "command": "python3",
#         "args": ["/absolute/path/to/Agent-OS/connectors/mcp_passthrough.py"],
#         "env": {
#           "AGENT_OS_URL": "http://localhost:8001",
#           "AGENT_OS_TOKEN": "YOUR_TOKEN_HERE"
#         }
#       }
#     }
#   }
# ─────────────────────────────────────────────────────────────

set -e

# Redirect all helper outputs to stderr to prevent JSON-RPC corruption
echo() {
    builtin echo "$@" >&2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── Colors ──────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# ─── Defaults ────────────────────────────────────────────
PORT=8000
HEADED=""
MCP_ONLY=false
SERVER_ONLY=false
TOKEN=""

# Compression: "aggressive" (default, ~87% savings) | "normal" (~50%) | "off"
COMPRESS="${AGENT_OS_COMPRESS:-aggressive}"
MAX_OUTPUT="${AGENT_OS_MAX_OUTPUT:-8000}"

# ─── Parse Args ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --token)    TOKEN="$2"; shift 2 ;;
        --port)     PORT="$2"; shift 2 ;;
        --headed)   HEADED="--headed"; shift ;;
        --mcp-only) MCP_ONLY=true; shift ;;
        --server-only) SERVER_ONLY=true; shift ;;
        --compress) COMPRESS="$2"; shift 2 ;;
        --max-output) MAX_OUTPUT="$2"; shift 2 ;;
        *)          echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ─── Generate Token ──────────────────────────────────────
if [ -z "$TOKEN" ]; then
    TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
fi

export AGENT_OS_TOKEN="$TOKEN"
export AGENT_OS_URL="http://localhost:$((PORT + 1))"  # HTTP port = WS port + 1
export AGENT_OS_COMPRESS="$COMPRESS"
export AGENT_OS_MAX_OUTPUT="$MAX_OUTPUT"

# ─── Functions ───────────────────────────────────────────

check_deps() {
    echo -e "${BLUE}Checking dependencies...${NC}"
    
    if ! command -v python3 &>/dev/null; then
        echo -e "${RED}❌ python3 not found${NC}"
        exit 1
    fi
    
    # Check Python packages
    python3 -c "import mcp" 2>/dev/null || {
        echo -e "${YELLOW}Installing mcp package...${NC}"
        pip install mcp
    }
    
    python3 -c "import httpx" 2>/dev/null || {
        echo -e "${YELLOW}Installing httpx...${NC}"
        pip install httpx
    }
    
    echo -e "${GREEN}✅ Dependencies OK${NC}"
}

start_server() {
    echo -e "${BLUE}Starting Agent-OS server on port $PORT...${NC}"
    
    # Check if port is in use
    if lsof -Pi :$PORT -sTCP:LISTEN -t &>/dev/null 2>&1; then
        echo -e "${YELLOW}⚠️  Port $PORT already in use — trying $((PORT + 100))${NC}"
        PORT=$((PORT + 100))
        AGENT_OS_URL="http://localhost:$((PORT + 1))"
        export AGENT_OS_URL
    fi
    
    python3 main.py \
        --agent-token "$TOKEN" \
        --port "$PORT" \
        $HEADED \
        --json-logs \
        > agent_os_server.log 2>&1 &
    
    SERVER_PID=$!
    echo $SERVER_PID > .agent-os-server.pid
    echo -e "${GREEN}✅ Server started (PID: $SERVER_PID)${NC}"
    
    # Wait for server to be ready
    echo -e "${BLUE}Waiting for server...${NC}"
    for i in $(seq 1 30); do
        if curl -s "$AGENT_OS_URL/health" &>/dev/null; then
            echo -e "${GREEN}✅ Server is ready!${NC}"
            return 0
        fi
        sleep 1
    done
    
    echo -e "${RED}❌ Server failed to start in 30s${NC}"
    return 1
}

start_mcp() {
    echo -e "${BLUE}Starting MCP Passthrough Wrapper...${NC}"
    echo -e "${CYAN}  Agent-OS URL: $AGENT_OS_URL${NC}"
    echo -e "${CYAN}  Token: ${TOKEN:0:10}...${NC}"
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  MCP Server Ready — Connect via Claude Desktop${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "${YELLOW}Claude Desktop config:${NC}"
    echo '{'
    echo '  "mcpServers": {'
    echo '    "agent-os": {'
    echo '      "command": "python3",'
    echo "      \"args\": [\"$SCRIPT_DIR/connectors/mcp_passthrough.py\"],"
    echo '      "env": {'
    echo "        \"AGENT_OS_URL\": \"$AGENT_OS_URL\","
    echo "        \"AGENT_OS_TOKEN\": \"$TOKEN\""
    echo '      }'
    echo '    }'
    echo '  }'
    echo '}'
    echo ""
    echo -e "${BLUE}Press Ctrl+C to stop${NC}"
    echo ""
    
    # Run MCP server (foreground — it uses stdio)
    exec python3 "$SCRIPT_DIR/connectors/mcp_passthrough.py"
}

cleanup() {
    echo ""
    echo -e "${YELLOW}Shutting down...${NC}"
    
    if [ -f .agent-os-server.pid ]; then
        kill $(cat .agent-os-server.pid) 2>/dev/null || true
        rm -f .agent-os-server.pid
    fi
    
    echo -e "${GREEN}✅ Stopped${NC}"
}

trap cleanup EXIT

# ─── Main ────────────────────────────────────────────────

echo -e "${CYAN}"
echo "╔═══════════════════════════════════════════════════╗"
echo "║   Agent-OS MCP Passthrough                       ║"
echo "║   No API Key Needed — Your LLM Does the Work     ║"
echo "╚═══════════════════════════════════════════════════╝"
echo -e "${NC}"

check_deps

if [ "$MCP_ONLY" = true ]; then
    start_mcp
elif [ "$SERVER_ONLY" = true ]; then
    start_server
    echo ""
    echo -e "${GREEN}Server running. Start MCP separately with:${NC}"
    echo -e "  AGENT_OS_URL=$AGENT_OS_URL AGENT_OS_TOKEN=$TOKEN python3 connectors/mcp_passthrough.py"
    echo ""
    echo -e "${BLUE}Press Ctrl+C to stop${NC}"
    wait
else
    start_server
    echo ""
    start_mcp
fi
