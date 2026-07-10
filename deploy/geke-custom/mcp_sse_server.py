#!/usr/bin/env python3
"""
MemOS SSE MCP Server + Config Sync
====================================
Serves the MemOS MCP bridge over SSE protocol (remote-capable + local).
Also provides a config-sync endpoint so the Windows backup machine can
pull the latest OpenCode/Claude configs via OpenCode.

Port: 48001 (0.0.0.0)

Endpoints:
  GET  /mcp        → SSE MCP server (for remote OpenCode clients)
  POST /mcp        → MCP message handler
  GET  /health     → Health check
  GET  /sync/pull   → Pull config bundle as JSON (latest version)
"""

import json
import os
import sys
import shutil
import tarfile
import io
import httpx
import datetime

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse, StreamingResponse
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MEMOS_URL = os.getenv("MEMOS_URL", "http://127.0.0.1:38001")
MEMOS_USER = os.getenv("MEMOS_USER_ID", "opencode")
SERVER_PORT = int(os.getenv("MCP_SSE_PORT", "48001"))
SERVER_HOST = os.getenv("MCP_SSE_HOST", "0.0.0.0")

# Paths to sync (relative to $HOME)
SYNC_PATHS = [
    # ── 全局配置 (→ ~/) ──
    ".claude/CLAUDE.md",
    ".claude/settings.json",
    ".claude/skills/superpowers/SKILL.md",
    ".claude/skills/google-search/SKILL.md",
    ".claude/skills/memos/SKILL.md",
    ".config/opencode/opencode.json",
    ".config/opencode/oh-my-openagent.json",
    ".config/opencode/opencode-image-proxy.json",
    ".config/opencode/opencode-mem.jsonc",
    ".config/opencode/zh-think-prompt.txt",
    ".config/opencode/package.json",
    # ── 项目配置 (→ ~/work/task/) ──
    "work/task/CLAUDE.md",
    "work/task/rules/analysis-common.md",
    "work/task/rules/code-standards.md",
    "work/task/rules/commit-standards.md",
    "work/task/rules/memory-standards.md",
    "work/task/rules/sdd-template.md",
    "work/task/rules/sql-script-standards.md",
    "work/task/双机协同-Win端接入指南.md",
]

HOME_DIR = os.path.expanduser("~")

# ---------------------------------------------------------------------------
# MCP Server Setup
# ---------------------------------------------------------------------------
server = Server("memos-mcp-sse")


@server.list_tools()
async def list_tools():
    """Expose the same tools as the stdio bridge, plus sync tools."""
    return [
        Tool(
            name="memos_add",
            description="Add a memory to MemOS for long-term storage. Supports structured messages and tags for graph relationship building.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Memory content to store"},
                    "user_id": {"type": "string", "description": "User ID (default: opencode)"},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for categorization and filtering (e.g. ['bug-fix', 'Neo4j', 'MES'])"},
                    "session_id": {"type": "string", "description": "Session ID for grouping related memories"},
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="memos_search",
            description="Search memories by semantic similarity",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "user_id": {"type": "string", "description": "User ID"},
                    "limit": {"type": "integer", "description": "Max results (default: 5)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memos_list",
            description="List all memories for a user. Specify memory_type to filter (default: text_mem).",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User ID"},
                    "memory_type": {"type": "string", "description": "Memory type filter: text_mem, act_mem, param_mem, para_mem (default: text_mem)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="memos_get",
            description="Get a specific memory by ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "Memory ID"},
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="sync_pull",
            description="Pull latest OpenCode/Claude configs from this server. Returns a JSON bundle of all config files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "version": {"type": "string", "description": "Current local version hash (optional, server returns 304 if same)"},
                },
            },
        ),
        Tool(
            name="sync_status",
            description="Show current config version/timestamp and what files would be synced.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name.startswith("memos_"):
        return await _handle_memos_tool(name, arguments)
    elif name == "sync_pull":
        return await _handle_sync_pull(arguments)
    elif name == "sync_status":
        return await _handle_sync_status()
    else:
        raise ValueError(f"Unknown tool: {name}")


async def _handle_memos_tool(name: str, arguments: dict):
    """Proxy to the local MemOS HTTP API."""
    api_map = {
        "memos_add": ("POST", "/product/add"),
        "memos_search": ("POST", "/product/search"),
        "memos_list": ("POST", "/product/get_all"),
        "memos_get": ("GET", "/product/get_memory/{memory_id}"),
    }
    method, path = api_map[name]
    url = f"{MEMOS_URL}{path}"

    # ── 统一兜底 user_id：处理客户端传 None/不传的情况 ──
    arguments["user_id"] = arguments.get("user_id") or MEMOS_USER

    # ── memos_add: 参数转换（与 stdio bridge 保持一致） ──
    if name == "memos_add":
        content = arguments.get("content", "")
        tags = arguments.get("tags")
        payload = {
            "user_id": arguments.get("user_id", MEMOS_USER),
            "messages": [{"role": "user", "content": content}],
            "session_id": arguments.get("session_id", "default_session"),
            "async_mode": "async",
        }
        if tags:
            payload["custom_tags"] = tags
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{MEMOS_URL}/product/add", json=payload)
            result = resp.json()
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

    # ── memos_list: 补 memory_type 默认值 ──
    if name == "memos_list":
        arguments.setdefault("memory_type", "text_mem")

    # ── memos_search: bump recall by lowering threshold and expanding candidates ──
    if name == "memos_search":
        arguments.setdefault("limit", 10)
        arguments.setdefault("relativity", 0)    # disable threshold filtering
        arguments.setdefault("top_k", 20)         # more candidates
        # translate 'limit' param for the API
        if "limit" in arguments:
            arguments["top_k"] = max(arguments.pop("limit", 10), arguments.get("top_k", 20))

    if "{memory_id}" in path:
        path = path.replace("{memory_id}", str(arguments["memory_id"]))
        arguments = {k: v for k, v in arguments.items() if k != "memory_id"}
    url = f"{MEMOS_URL}{path}"

    async with httpx.AsyncClient(timeout=30) as client:
        if method == "POST":
            resp = await client.post(url, json=arguments)
        else:
            resp = await client.get(url)
        result = resp.json()
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


def _version_hash() -> str:
    """Compute a hash of all tracked config files to detect changes."""
    import hashlib
    h = hashlib.sha256()
    for rel_path in SYNC_PATHS:
        full_path = os.path.join(HOME_DIR, rel_path)
        try:
            with open(full_path, "rb") as f:
                h.update(f.read())
        except FileNotFoundError:
            pass
    return h.hexdigest()[:16]


def _read_config_files() -> dict:
    """Read all sync paths and return as dict of {rel_path: content}."""
    files = {}
    for rel_path in SYNC_PATHS:
        full_path = os.path.join(HOME_DIR, rel_path)
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                files[rel_path] = f.read()
        except FileNotFoundError:
            files[rel_path] = None
        except Exception as e:
            files[rel_path] = f"# ERROR reading file: {e}"
    return files


async def _handle_sync_pull(arguments: dict):
    """Return all tracked config files as a JSON bundle."""
    version = _version_hash()
    files = _read_config_files()
    result = {
        "version": version,
        "timestamp": datetime.datetime.now().isoformat(),
        "server_hostname": os.uname().nodename,
        "files": files,
    }
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


async def _handle_sync_status():
    """Show what configs are tracked and their current version."""
    version = _version_hash()
    files_status = []
    for rel_path in SYNC_PATHS:
        full_path = os.path.join(HOME_DIR, rel_path)
        exists = os.path.exists(full_path)
        mtime = None
        size = None
        if exists:
            st = os.stat(full_path)
            mtime = datetime.datetime.fromtimestamp(st.st_mtime).isoformat()
            size = st.st_size
        files_status.append({
            "path": rel_path,
            "exists": exists,
            "mtime": mtime,
            "size": size,
        })
    result = {
        "version": version,
        "timestamp": datetime.datetime.now().isoformat(),
        "files": files_status,
    }
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


# ---------------------------------------------------------------------------
# HTTP (SSE) Transport
# ---------------------------------------------------------------------------
sse = SseServerTransport(endpoint="/mcp")

async def handle_sse(request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())

async def handle_mcp_post(request):
    from starlette.responses import Response
    try:
        response = await sse.handle_post_message(request.scope, request.receive, request._send)
        if response is not None:
            return response
        # handle_post_message may handle the response internally (SSE streaming),
        # in which case we return an empty response to satisfy Starlette's ASGI contract.
        return Response(status_code=202)
    except RuntimeError as e:
        if "http.response.start" in str(e):
            # SseServerTransport 已经发过响应了，不再重复发送
            return Response(status_code=200)
        raise

async def health_check(request):
    return JSONResponse({
        "status": "ok",
        "service": "memos-sse-mcp",
        "version": _version_hash(),
    })

async def sync_pull_http(request):
    """HTTP endpoint: GET /sync/pull returns config bundle as JSON."""
    version = _version_hash()
    files = _read_config_files()
    result = {
        "version": version,
        "timestamp": datetime.datetime.now().isoformat(),
        "server_hostname": os.uname().nodename,
        "files": files,
    }
    return JSONResponse(result, media_type="application/json; charset=utf-8")

async def sync_status_http(request):
    """HTTP endpoint: GET /sync/status returns current config version info."""
    version = _version_hash()
    files_status = []
    for rel_path in SYNC_PATHS:
        full_path = os.path.join(HOME_DIR, rel_path)
        exists = os.path.exists(full_path)
        mtime = None
        size = None
        if exists:
            st = os.stat(full_path)
            mtime = datetime.datetime.fromtimestamp(st.st_mtime).isoformat()
            size = st.st_size
        files_status.append({
            "path": rel_path,
            "exists": exists,
            "mtime": mtime,
            "size": size,
        })
    return JSONResponse({
        "version": version,
        "timestamp": datetime.datetime.now().isoformat(),
        "server_hostname": os.uname().nodename,
        "files": files_status,
    })


# ── Sync Script Download ──
SYNC_SCRIPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync.ps1")

async def serve_sync_script(request):
    """GET /sync/sync.ps1 → return the Windows PowerShell sync script."""
    from starlette.responses import PlainTextResponse
    from starlette.responses import Response
    if os.path.exists(SYNC_SCRIPT_PATH):
        with open(SYNC_SCRIPT_PATH, "r", encoding="utf-8-sig") as f:
            content = f.read()
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=sync.ps1",
                "X-Server-Version": _version_hash(),
            }
        )
    return PlainTextResponse("sync.psn not found", status_code=404)


# ---------------------------------------------------------------------------
# Starlette App
# ---------------------------------------------------------------------------
app = Starlette(
    middleware=[
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
    ],
    routes=[
        Route("/mcp", endpoint=handle_sse, methods=["GET"]),
        Route("/mcp", endpoint=handle_mcp_post, methods=["POST"]),
        Route("/health", endpoint=health_check),
        Route("/sync/pull", endpoint=sync_pull_http),
        Route("/sync/status", endpoint=sync_status_http),
        Route("/sync/sync.ps1", endpoint=serve_sync_script),
    ],
)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    print(f"[memos-sse-mcp] Starting on {SERVER_HOST}:{SERVER_PORT}")
    print(f"[memos-sse-mcp] Memos API: {MEMOS_URL}")
    print(f"[memos-sse-mcp] Endpoints:")
    print(f"  MCP (SSE):  http://0.0.0.0:{SERVER_PORT}/mcp")
    print(f"  Health:     http://0.0.0.0:{SERVER_PORT}/health")
    print(f"  Sync Pull:  http://0.0.0.0:{SERVER_PORT}/sync/pull")
    print(f"  Sync Status:http://0.0.0.0:{SERVER_PORT}/sync/status")
    print(f"  Sync Script:http://0.0.0.0:{SERVER_PORT}/sync/sync.ps1")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")
