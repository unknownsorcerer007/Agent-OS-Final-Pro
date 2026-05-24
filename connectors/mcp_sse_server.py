#!/usr/bin/env python3
"""
Agent-OS MCP SSE Server
=======================
Exposes the Model Context Protocol (MCP) over HTTP/SSE.
Allows Claude Web version (Claude.ai) to connect directly using a public URL.

Usage:
    python connectors/mcp_sse_server.py
"""
import os
import sys
import logging
import json
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, Header
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
import mcp.types as types
from uuid import uuid4

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from connectors._tool_registry import get_command_map, get_mcp_tools

# Resolve token and url
def resolve_agent_token() -> str:
    token = os.environ.get("AGENT_OS_TOKEN")
    if token:
        return token
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(repo_dir, ".env")
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
    return "claude-web-token"

AGENT_OS_URL = os.environ.get("AGENT_OS_URL", "http://localhost:8001")
AGENT_OS_TOKEN = resolve_agent_token()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", stream=sys.stderr)
logger = logging.getLogger("agent-os-mcp-sse")

# Create MCP server
server = Server("agent-os")

# Build Tool Definitions
TOOLS_LIST: List[Tool] = []
_mcp_tools = get_mcp_tools()
for tool_def in _mcp_tools:
    TOOLS_LIST.append(Tool(
        name=tool_def["name"],
        description=tool_def["description"],
        inputSchema=tool_def["inputSchema"],
    ))

command_map = get_command_map()
logger.info(f"Loaded {len(TOOLS_LIST)} MCP tools")

_client: Optional[httpx.AsyncClient] = None

async def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=60.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client

async def agent_os_command(command: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    payload = {"token": AGENT_OS_TOKEN, "command": command}
    if params:
        payload.update(params)
    client = await _get_client()
    try:
        response = await client.post(f"{AGENT_OS_URL}/command", json=payload)
        return response.json()
    except Exception as e:
        return {"status": "error", "error": str(e)}

async def agent_os_status() -> Dict[str, Any]:
    client = await _get_client()
    try:
        response = await client.get(f"{AGENT_OS_URL}/health")
        return response.json()
    except Exception as e:
        return {"status": "error", "error": str(e)}

@server.list_tools()
async def list_tools() -> List[Tool]:
    return TOOLS_LIST

@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    logger.info(f"Tool call: {name}")
    if name == "browser_status":
        result = await agent_os_status()
    elif name in command_map:
        cmd_name, param_keys = command_map[name]
        params = {k: arguments[k] for k in param_keys if k in arguments}
        result = await agent_os_command(cmd_name, params)
    else:
        result = {"status": "error", "error": f"Unknown tool: {name}"}

    output = json.dumps(result, indent=2)
    if len(output) > 10000:
        if "screenshot" in result:
            output = f"[Screenshot captured: {len(result.get('screenshot', ''))} bytes base64]"
        elif "html" in result:
            preview = result.get('text', '')[:2000]
            output = f"[HTML content: {len(result.get('html', ''))} chars]\n\nText preview:\n{preview}"
        else:
            output = output[:10000] + "\n... [truncated]"

    return [TextContent(type="text", text=output)]

# ─── FastAPI app & SSE Transport ─────────────────────────────
from contextlib import asynccontextmanager
from starlette.types import Receive, Scope, Send
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
import anyio
from sse_starlette import EventSourceResponse

class DynamicSseServerTransport(SseServerTransport):
    @asynccontextmanager
    async def connect_sse(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            logger.error("connect_sse received non-HTTP request")
            raise ValueError("connect_sse can only handle HTTP requests")

        logger.debug("Setting up SSE connection")
        read_stream: MemoryObjectReceiveStream[types.JSONRPCMessage | Exception]
        read_stream_writer: MemoryObjectSendStream[types.JSONRPCMessage | Exception]

        write_stream: MemoryObjectSendStream[types.JSONRPCMessage]
        write_stream_reader: MemoryObjectReceiveStream[types.JSONRPCMessage]

        read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
        write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

        session_id = uuid4()
        
        # Build dynamic absolute URL using request details from scope
        headers = dict(scope.get("headers", []))
        host = headers.get(b"host", b"localhost").decode("utf-8")
        proto = headers.get(b"x-forwarded-proto", b"http").decode("utf-8")
        
        # If it has x-forwarded-host, use it (Cloudflare forwards this)
        forwarded_host = headers.get(b"x-forwarded-host", b"").decode("utf-8")
        if forwarded_host:
            host = forwarded_host
            
        base_url = f"{proto}://{host}"
        session_uri = f"{base_url}/messages?session_id={session_id.hex}"
        
        self._read_stream_writers[session_id] = read_stream_writer
        logger.info(f"Created new session with ID: {session_id}, absolute messages URL: {session_uri}")

        sse_stream_writer, sse_stream_reader = anyio.create_memory_object_stream[
            dict[str, Any]
        ](0)

        async def sse_writer():
            logger.debug("Starting SSE writer")
            async with sse_stream_writer, write_stream_reader:
                await sse_stream_writer.send({"event": "endpoint", "data": session_uri})
                logger.debug(f"Sent endpoint event: {session_uri}")

                async for message in write_stream_reader:
                    logger.debug(f"Sending message via SSE: {message}")
                    await sse_stream_writer.send(
                        {
                            "event": "message",
                            "data": message.model_dump_json(
                                by_alias=True, exclude_none=True
                            ),
                        }
                    )

        async with anyio.create_task_group() as tg:
            response = EventSourceResponse(
                content=sse_stream_reader, data_sender_callable=sse_writer
            )
            logger.debug("Starting SSE response task")
            tg.start_soon(response, scope, receive, send)

            logger.debug("Yielding read and write streams")
            yield (read_stream, write_stream)

app = FastAPI(title="Agent-OS MCP SSE Server")

# Add CORS middleware to allow the browser to connect
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

sse = DynamicSseServerTransport("/messages")

# ─── Mock OAuth 2.1 Provider Endpoints ────────────────────────

@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource(request: Request):
    headers = request.headers
    host = headers.get("x-forwarded-host") or headers.get("host") or "localhost:8002"
    proto = headers.get("x-forwarded-proto") or request.url.scheme or "http"
    base_url = f"{proto}://{host}"
    
    return JSONResponse({
        "resource": f"{base_url}/sse",
        "authorization_servers": [base_url],
        "scopes_supported": ["mcp"],
        "bearer_methods_supported": ["header"]
    })

@app.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server(request: Request):
    headers = request.headers
    host = headers.get("x-forwarded-host") or headers.get("host") or "localhost:8002"
    proto = headers.get("x-forwarded-proto") or request.url.scheme or "http"
    base_url = f"{proto}://{host}"
    
    return JSONResponse({
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/oauth/register",
        "scopes_supported": ["mcp"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"]
    })

@app.post("/oauth/register")
@app.post("/register")
async def oauth_register(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    
    redirect_uris = body.get("redirect_uris", ["https://claude.ai/oauth/callback"])
    
    return JSONResponse({
        "client_id": "mock-client-id",
        "client_secret": "mock-client-secret",
        "client_id_issued_at": 1600000000,
        "client_secret_expires_at": 0,
        "redirect_uris": redirect_uris
    })

@app.get("/oauth/authorize")
@app.get("/authorize")
async def oauth_authorize(
    request: Request,
    redirect_uri: str = "https://claude.ai/oauth/callback",
    state: str = "mock-state",
    code_challenge: str = None,
    code_challenge_method: str = None
):
    redirect_url = f"{redirect_uri}?code=mock-authorization-code&state={state}"
    logger.info(f"OAuth Authorize redirecting to: {redirect_url}")
    return RedirectResponse(url=redirect_url)

@app.post("/oauth/token")
@app.post("/token")
async def oauth_token(request: Request):
    return JSONResponse({
        "access_token": "mock-access-token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "mcp"
    })

@app.get("/sse")
async def handle_sse(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        headers = request.headers
        host = headers.get("x-forwarded-host") or headers.get("host") or "localhost:8002"
        proto = headers.get("x-forwarded-proto") or request.url.scheme or "http"
        base_url = f"{proto}://{host}"
        
        metadata_url = f"{base_url}/.well-known/oauth-protected-resource"
        logger.info(f"Unauthenticated request to /sse. Challenging with WWW-Authenticate pointing to {metadata_url}")
        
        return JSONResponse(
            status_code=401,
            content={"error": "unauthorized", "message": "Authentication required"},
            headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}"'}
        )
        
    logger.info("Authentication successful via Bearer token. Proceeding to SSE stream connection.")
    async with sse.connect_sse(
        request.scope,
        request.receive,
        request._send
    ) as streams:
        await server.run(
            streams[0],
            streams[1],
            server.create_initialization_options()
        )

app.mount("/messages", sse.handle_post_message)

if __name__ == "__main__":
    import uvicorn
    # Start on port 8002
    uvicorn.run(app, host="0.0.0.0", port=8002)
