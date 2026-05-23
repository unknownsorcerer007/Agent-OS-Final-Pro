#!/usr/bin/env python3
"""
Agent-OS MCP Server — Complete (199 tools)
Model Context Protocol server for Agent-OS browser automation.
Allows Claude, Codex, and other MCP-compatible agents to control the browser.

Usage:
    python mcp_server.py                    # Starts MCP server on stdio
    AGENT_OS_URL=http://localhost:8001 python mcp_server.py
"""
import os
import json
import sys
import logging
from typing import Any, Dict, List, Optional

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from connectors._tool_registry import TOOLS, get_command_map, get_mcp_tools

def resolve_agent_token() -> str:
    # 1. Environment Variable
    token = os.environ.get("AGENT_OS_TOKEN")
    if token:
        return token

    # 2. Try .env in repo root or ~/.agent-os/.env
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = [
        os.path.join(repo_dir, ".env"),
        os.path.expanduser("~/.agent-os/.env")
    ]
    for path in paths:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k in ("AGENT_TOKEN", "AGENT_OS_TOKEN") and v:
                                return v
            except Exception:
                pass

    # 3. Try config.yaml in ~/.agent-os/ or repo root
    config_paths = [
        os.path.expanduser("~/.agent-os/config.yaml"),
        os.path.join(repo_dir, "config.yaml")
    ]
    for path in config_paths:
        if os.path.exists(path):
            try:
                import yaml
                with open(path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                    if cfg and isinstance(cfg, dict):
                        t = cfg.get("server", {}).get("agent_token")
                        if t:
                            return t
            except Exception:
                pass

    # 4. Fallback to generating a temp token
    import secrets
    fallback_token = secrets.token_urlsafe(32)
    print(f"WARNING: AGENT_OS_TOKEN not set or found. Generated temp token: {fallback_token}", file=sys.stderr)
    return fallback_token

AGENT_OS_URL = os.environ.get("AGENT_OS_URL", "http://localhost:8001")
AGENT_OS_TOKEN = resolve_agent_token()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger("agent-os-mcp")

# ─── Persistent HTTP Client ──────────────────────────────────

_client: Optional[httpx.AsyncClient] = None


async def _get_client() -> httpx.AsyncClient:
    """Get or create a persistent httpx.AsyncClient with connection pooling."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=60.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client

# Create MCP server
server = Server("agent-os")

# ─── Build Tool Definitions from Registry ─────────────────────

TOOLS_LIST: List[Tool] = []
_mcp_tools = get_mcp_tools()
for tool_def in _mcp_tools:
    TOOLS_LIST.append(Tool(
        name=tool_def["name"],
        description=tool_def["description"],
        inputSchema=tool_def["inputSchema"],
    ))

command_map = get_command_map()

logger.info(f"Loaded {len(TOOLS_LIST)} MCP tools from registry")


# ─── Agent-OS Communication ──────────────────────────────────

async def agent_os_command(command: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    """Send a command to Agent-OS server."""
    payload = {"token": AGENT_OS_TOKEN, "command": command}
    if params:
        payload.update(params)

    client = await _get_client()
    try:
        response = await client.post(
            f"{AGENT_OS_URL}/command",
            json=payload,
        )
        return response.json()
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def agent_os_status() -> Dict[str, Any]:
    """Check Agent-OS server status."""
    client = await _get_client()
    try:
        response = await client.get(f"{AGENT_OS_URL}/health")
        return response.json()
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ─── MCP Handlers ────────────────────────────────────────────

@server.list_tools()
async def list_tools() -> List[Tool]:
    """List all available Agent-OS tools."""
    return TOOLS_LIST


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """Execute an Agent-OS tool."""
    logger.info(f"Tool call: {name} with args: {list(arguments.keys())}")

    if name == "browser_status":
        result = await agent_os_status()
    elif name in command_map:
        cmd_name, param_keys = command_map[name]
        params = {k: arguments[k] for k in param_keys if k in arguments}
        result = await agent_os_command(cmd_name, params)
    else:
        result = {"status": "error", "error": f"Unknown tool: {name}"}

    # Format response
    output = json.dumps(result, indent=2)

    # Truncate large responses
    if len(output) > 10000:
        if "screenshot" in result:
            output = f"[Screenshot captured: {len(result.get('screenshot', ''))} bytes base64]"
        elif "html" in result:
            preview = result.get('text', '')[:2000]
            output = f"[HTML content: {len(result.get('html', ''))} chars]\n\nText preview:\n{preview}"
        else:
            output = output[:10000] + "\n... [truncated]"

    return [TextContent(type="text", text=output)]


# ─── Entry Point ──────────────────────────────────────────────

async def main():
    logger.info(f"Agent-OS MCP Server starting...")
    logger.info(f"Agent-OS URL: {AGENT_OS_URL}")
    logger.info(f"Agent Token: {AGENT_OS_TOKEN[:10]}...")
    logger.info(f"Tools available: {len(TOOLS_LIST)}")

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
