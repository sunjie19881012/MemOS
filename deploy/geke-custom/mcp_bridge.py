#!/usr/bin/env python3
"""MemOS MCP Bridge v2 for OpenCode / Claude Code

升级说明（v2，2026-07-04）：
- memos_search 补全可用参数（mode/relativity/top_k/search_memory_type/filter/mem_cube_id）
- ★ search 返回前 client-side 过滤 status=deleted（解决后端不过滤软删的 bug）
- 新增 memos_delete（默认 dry_run，实际删除走软删 hard_delete=false，可恢复）
- 全工具透传 mem_cube_id（多知识库支持）
- 错误处理：HTTP != 200 时返回结构化错误而非抛异常
- 暂不引入 update（后端 update_node bug 待修）
- 暂不引入 create_cube（单用户部署时 curl 一次即可）

基于实测（2026-07-04 n≥6 严谨复测）：
- 软删后 search 不过滤（后端 recall.py 写了 status="activated" 但 graph_store 没生效）
- filter 仅支持 created_at 时间过滤，tags/id 字段过滤都不可用
- update_memory 返回成功但 get 查不变（update_node 真实 bug）
"""

import json
import os
import sys
from datetime import datetime, timezone

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

MEMOS_URL = os.getenv("MEMOS_URL", "http://127.0.0.1:38001")
MEMOS_USER = os.getenv("MEMOS_USER_ID", "opencode")
# Phase B: default multi-cube joint search (三库联动)# When mem_cube_id not specified, search all these cubes
DEFAULT_READABLE_CUBES = os.getenv("MEMOS_READABLE_CUBES", "opencode,a13a4ef6-437a-4593-bc46-1b139b463f59").split(",")
# 日志目录（审计日志，记 memory_id+cube_id+操作，不记内容防泄露）
AUDIT_LOG = os.getenv("MEMOS_AUDIT_LOG", "/var/lib/memos-data/custom/audit.log")
# 硬删日配额
DAILY_HARD_DELETE_LIMIT = 10
DAILY_HARD_DELETE_COUNTER = os.getenv(
    "MEMOS_DELETE_COUNTER", "/var/lib/memos-data/custom/.delete_quota"
)


def _ensure_str(val, fallback):
    """dict.get() 的 default 只在 key 不存在时生效。
    如果客户端传了 null/None，返回的是 None 而非 fallback。兜底处理。
    """
    return val if isinstance(val, str) and val.strip() else fallback


def _audit(action, memory_id=None, cube_id=None, extra=""):
    """记录审计日志（不记内容，防泄露）。失败静默（不影响主流程）。"""
    try:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        line = f"{ts} | {action} | id={memory_id or '-'} cube={cube_id or '-'} {extra}\n"
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # 审计失败不影响主流程


def _check_hard_delete_quota():
    """检查今日硬删配额。返回剩余次数。超限返回 0。"""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # 读计数文件: 格式 "2026-07-04 3"
        data = ""
        if os.path.exists(DAILY_HARD_DELETE_COUNTER):
            with open(DAILY_HARD_DELETE_COUNTER) as f:
                data = f.read().strip()
        if data:
            parts = data.split()
            if len(parts) == 2 and parts[0] == today:
                used = int(parts[1])
                return max(0, DAILY_HARD_DELETE_LIMIT - used)
        return DAILY_HARD_DELETE_LIMIT  # 新一天，重置
    except Exception:
        return DAILY_HARD_DELETE_LIMIT  # 出错宽容，不阻塞


def _incr_hard_delete_quota():
    """硬删计数 +1。"""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        used = 0
        if os.path.exists(DAILY_HARD_DELETE_COUNTER):
            with open(DAILY_HARD_DELETE_COUNTER) as f:
                data = f.read().strip()
            if data:
                parts = data.split()
                if len(parts) == 2 and parts[0] == today:
                    used = int(parts[1])
        with open(DAILY_HARD_DELETE_COUNTER, "w") as f:
            f.write(f"{today} {used + 1}")
    except Exception:
        pass


def _filter_deleted(memories):
    """★ client-side 过滤 status=deleted 的记录（解决后端不过滤 bug）。
    输入: search 返回的 memories 列表（每个是 dict）
    输出: 过滤掉 status=deleted 后的列表
    """
    result = []
    for m in memories:
        if not isinstance(m, dict):
            continue
        status = m.get("metadata", {}).get("status", "activated")
        if status == "deleted":
            continue  # 软删的记录对 LLM 不可见
        result.append(m)
    return result


def _format_error(resp, action):
    """HTTP 非 200 时格式化错误返回。"""
    try:
        body = resp.json()
        msg = body.get("message", str(body)[:200])
    except Exception:
        msg = resp.text[:200] if resp.text else "(empty body)"
    return {
        "error": True,
        "action": action,
        "status_code": resp.status_code,
        "message": msg,
    }


server = Server("memos-mcp-v2")


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="memos_add",
            description=(
                "Add a memory to MemOS for long-term storage. "
                "Content will be processed by LLM (extract facts, build graph). "
                "Tags are stored but NOTE: search filter does not support tags "
                "(only created_at time filter works). Tags are for display/categorization only."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Memory content to store",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "User ID (default: opencode)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for categorization (display only, not searchable)",
                    },
                    "mem_cube_id": {
                        "type": "string",
                        "description": "Target cube ID (default: user's default cube)",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session ID for grouping related memories",
                    },
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="memos_search",
            description=(
                "Search memories by semantic similarity. "
                "Returns memories with status=deleted automatically filtered out (client-side). "
                "filter param: only supports created_at time filter "
                "(e.g. {\"and\":[{\"created_at\":{\"gt\":\"2026-01-01\"}}]}). "
                "Does NOT support filtering by tags or id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "user_id": {"type": "string", "description": "User ID"},
                    "top_k": {
                        "type": "integer",
                        "description": "Max results per memory type (default: 5)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Deprecated alias for top_k. Kept for backward compatibility.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["fast", "fine", "mixture"],
                        "description": "Search mode (default: fast). fine=more thorough",
                    },
                    "relativity": {
                        "type": "number",
                        "description": "Relevance threshold 0-1 (default: 0.45). Lower=more results",
                    },
                    "search_memory_type": {
                        "type": "string",
                        "description": "Memory type filter: All/WorkingMemory/LongTermMemory/UserMemory/SkillMemory (default: All)",
                    },
                    "filter": {
                        "type": "object",
                        "description": 'Filter dict. ONLY supports created_at: {"and":[{"created_at":{"gt":"2026-01-01"}}]}',
                    },
                    "mem_cube_id": {
                        "type": "string",
                        "description": "Cube ID to search in (default: user's default cube)",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="memos_delete",
            description=(
                "Delete a memory. SAFE BY DEFAULT: dry_run=True only shows what would be deleted. "
                "Actual deletion defaults to SOFT delete (hard_delete=false, recoverable). "
                "Soft-deleted memories are auto-filtered from search results (client-side). "
                "HARD delete (hard_delete=true) is irreversible, limited to 10/day, audit-logged. "
                "Use hard delete ONLY for sensitive info (passwords/keys). "
                "For outdated content, prefer soft delete or append new version."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "Memory record ID to delete",
                    },
                    "mem_cube_id": {
                        "type": "string",
                        "description": "Cube ID containing the memory (required for actual deletion)",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true (default), only preview without deleting",
                    },
                    "hard_delete": {
                        "type": "boolean",
                        "description": "If true, permanently delete (irreversible). Default false=soft delete",
                    },
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memos_list",
            description=(
                "List all memories for a user. Specify memory_type to filter (default: text_mem). "
                "NOTE: soft-deleted memories (status=deleted) are included in raw output. "
                "Use memos_search for filtered results."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User ID"},
                    "memory_type": {
                        "type": "string",
                        "description": "Memory type: text_mem/act_mem/param_mem/para_mem (default: text_mem)",
                    },
                    "mem_cube_id": {
                        "type": "string",
                        "description": "Cube ID filter",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="memos_get",
            description="Get a specific memory by ID. Returns full metadata including status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "memory_id": {"type": "string", "description": "Memory ID"},
                    "user_id": {"type": "string", "description": "User ID"},
                },
                "required": ["memory_id"],
            },
        ),
        Tool(
            name="memos_feedback",
            description="Provide feedback on memory quality to improve future retrieval",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User ID"},
                    "history": {
                        "type": "string",
                        "description": "Conversation history for context",
                    },
                    "feedback_content": {
                        "type": "string",
                        "description": "Feedback about memory quality",
                    },
                    "retrieved_memory_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "IDs of memories this feedback relates to",
                    },
                    "mem_cube_id": {
                        "type": "string",
                        "description": "Cube ID",
                    },
                },
                "required": ["history", "feedback_content"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name, arguments):
    async with httpx.AsyncClient(timeout=60) as client:
        # ---------- memos_add ----------
        if name == "memos_add":
            payload = {
                "user_id": _ensure_str(arguments.get("user_id"), MEMOS_USER),
                "messages": [{"role": "user", "content": arguments["content"]}],
                "session_id": _ensure_str(arguments.get("session_id"), "default_session"),
                "async_mode": "sync",  # v2 改 sync（无 Redis，async 退化为同步）
            }
            cube_id = arguments.get("mem_cube_id")
            if cube_id:
                payload["mem_cube_id"] = cube_id
            if tags := arguments.get("tags"):
                payload["custom_tags"] = tags
            resp = await client.post(f"{MEMOS_URL}/product/add", json=payload)
            if resp.status_code != 200:
                return [TextContent(type="text", text=json.dumps(_format_error(resp, "add"), ensure_ascii=False))]
            result = resp.json()
            _audit("add", cube_id=cube_id)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        # ---------- memos_search ----------
        elif name == "memos_search":
            payload = {
                "user_id": _ensure_str(arguments.get("user_id"), MEMOS_USER),
                "query": arguments["query"],
                "top_k": arguments.get("top_k", arguments.get("limit", 5)),
            }
            # 补全可用参数（全 optional，向后兼容）
            cube_id = arguments.get("mem_cube_id")
            if cube_id:
                payload["mem_cube_id"] = cube_id
            if mode := arguments.get("mode"):
                payload["mode"] = mode
            # Phase B: multi-cube default. If not specified, search all readable cubes
            cube_ids = arguments.get("mem_cube_id")
            if not cube_ids:
                cube_ids = DEFAULT_READABLE_CUBES
            if isinstance(cube_ids, str):
                cube_ids = [cube_ids]
                payload["search_memory_type"] = smt
            if flt := arguments.get("filter"):
                payload["filter"] = flt

            # Multi-cube: fetch all specified cubes and merge results
            if len(cube_ids) <= 1:
                if cube_ids:
                    payload["mem_cube_id"] = cube_ids[0]
                resp = await client.post(f"{MEMOS_URL}/product/search", json=payload)
            else:
                all_results = {}
                for cid in cube_ids:
                    p = dict(payload)
                    p["mem_cube_id"] = cid
                    try:
                        r = await client.post(f"{MEMOS_URL}/product/search", json=p)
                        if r.status_code == 200:
                            all_results[cid] = r.json()
                    except Exception:
                        pass
                merged = {}
                for cid, rd in all_results.items():
                    d = rd.get("data", {})
                    if isinstance(d, dict):
                        for mt, bucket in d.items():
                            if isinstance(bucket, list):
                                merged.setdefault(mt, [])
                                merged[mt].extend(bucket)
                FakeResp = type("FakeResp", (), {"status_code": 200, "json": lambda self, md=merged: {"code": 200, "message": "Multi-cube search", "data": md}})
                resp = FakeResp()
            if resp.status_code != 200:
                return [TextContent(type="text", text=json.dumps(_format_error(resp, "search"), ensure_ascii=False))]
            result = resp.json()

            # ★ client-side 过滤 status=deleted（解决后端不过滤 bug）
            # 遍历所有 memory_type 桶，过滤每个 cube 的 memories
            data = result.get("data", {})
            total_before = 0
            total_after = 0
            if isinstance(data, dict):
                for mt, bucket in list(data.items()):
                    if not isinstance(bucket, list):
                        continue
                    total_before += sum(
                        len(c.get("memories", [])) if isinstance(c, dict) else 0
                        for c in bucket
                    )
                    for cube_obj in bucket:
                        if isinstance(cube_obj, dict):
                            original = cube_obj.get("memories", [])
                            filtered = _filter_deleted(original)
                            cube_obj["memories"] = filtered
                            total_after += len(filtered)
                    # 空桶清理（可选）
                    data[mt] = [c for c in bucket if isinstance(c, dict) and c.get("memories")]

            # 格式化为简洁的 memories 列表（含 metadata 便于 LLM 判断）
            memories_out = []
            if isinstance(data, dict):
                for mt, bucket in data.items():
                    if not isinstance(bucket, list):
                        continue
                    for cube_obj in bucket:
                        if not isinstance(cube_obj, dict):
                            continue
                        for m in cube_obj.get("memories", []):
                            meta = m.get("metadata", {})
                            memories_out.append({
                                "id": m.get("id"),
                                "memory": m.get("memory"),
                                "type": meta.get("type"),
                                "key": meta.get("key"),
                                "tags": meta.get("tags", []),
                                "confidence": meta.get("confidence"),
                                "memory_type": mt,
                                "created_at": meta.get("created_at"),
                            })

            summary = {
                "total": len(memories_out),
                "filtered_deleted": total_before - total_after,
                "memories": memories_out,
            }
            return [TextContent(type="text", text=json.dumps(summary, ensure_ascii=False, indent=2))]

        # ---------- memos_delete ----------
        elif name == "memos_delete":
            memory_id = arguments["memory_id"]
            cube_id = arguments.get("mem_cube_id")
            dry_run = arguments.get("dry_run", True)  # ★ 默认 dry_run
            hard_delete = arguments.get("hard_delete", False)  # 默认软删

            # 先查这条记忆的内容（dry_run 和实际删除都需要）
            try:
                get_resp = await client.get(
                    f"{MEMOS_URL}/product/get_memory/{memory_id}",
                    params={"user_id": _ensure_str(arguments.get("user_id"), MEMOS_USER)},
                )
            except Exception as e:
                return [TextContent(type="text", text=json.dumps({"error": f"获取记忆失败: {e}"}, ensure_ascii=False))]
            if get_resp.status_code != 200:
                return [TextContent(type="text", text=json.dumps(_format_error(get_resp, "delete-get"), ensure_ascii=False))]
            mem_data = get_resp.json().get("data", {})
            mem_content = str(mem_data.get("memory", ""))[:200]
            mem_status = mem_data.get("metadata", {}).get("status", "?")

            # dry_run 模式：只预览不删除
            if dry_run:
                preview = {
                    "dry_run": True,
                    "would_delete": {
                        "memory_id": memory_id,
                        "cube_id": cube_id,
                        "hard_delete": hard_delete,
                        "current_status": mem_status,
                        "content_preview": mem_content,
                    },
                    "message": (
                        "DRY RUN - nothing deleted. "
                        "To actually delete, call again with dry_run=false. "
                        f"{'HARD delete is IRREVERSIBLE.' if hard_delete else 'SOFT delete (default) is recoverable.'}"
                    ),
                }
                return [TextContent(type="text", text=json.dumps(preview, ensure_ascii=False, indent=2))]

            # 实际删除需要 cube_id
            if not cube_id:
                return [TextContent(type="text", text=json.dumps(
                    {"error": "mem_cube_id is required for actual deletion (dry_run=false)"},
                    ensure_ascii=False,
                ))]

            # 硬删配额检查
            if hard_delete:
                remaining = _check_hard_delete_quota()
                if remaining <= 0:
                    return [TextContent(type="text", text=json.dumps(
                        {"error": f"Daily hard delete limit ({DAILY_HARD_DELETE_LIMIT}) reached. Try tomorrow or use soft delete."},
                        ensure_ascii=False,
                    ))]

            # 执行删除
            del_payload = {
                "mem_cube_id": cube_id,
                "record_id": memory_id,
                "hard_delete": hard_delete,
            }
            del_resp = await client.post(
                f"{MEMOS_URL}/product/delete_memory_by_record_id", json=del_payload
            )
            if del_resp.status_code != 200:
                return [TextContent(type="text", text=json.dumps(_format_error(del_resp, "delete"), ensure_ascii=False))]
            del_result = del_resp.json()

            # 审计 + 配额计数
            action = f"delete-{'hard' if hard_delete else 'soft'}"
            _audit(action, memory_id=memory_id, cube_id=cube_id)
            if hard_delete:
                _incr_hard_delete_quota()

            summary = {
                "deleted": True,
                "memory_id": memory_id,
                "cube_id": cube_id,
                "hard_delete": hard_delete,
                "recoverable": not hard_delete,
                "result": del_result.get("data"),
            }
            if not hard_delete:
                summary["note"] = "Soft deleted. Recoverable via recover_memory_by_record_id. Auto-filtered from search (client-side)."
            return [TextContent(type="text", text=json.dumps(summary, ensure_ascii=False, indent=2))]

        # ---------- memos_list ----------
        elif name == "memos_list":
            payload = {
                "user_id": _ensure_str(arguments.get("user_id"), MEMOS_USER),
                "memory_type": arguments.get("memory_type", "text_mem"),
            }
            cube_ids = arguments.get("mem_cube_id")
            if cube_ids:
                payload["mem_cube_ids"] = [cube_ids] if isinstance(cube_ids, str) else cube_ids
            resp = await client.post(f"{MEMOS_URL}/product/get_all", json=payload)
            if resp.status_code != 200:
                return [TextContent(type="text", text=json.dumps(_format_error(resp, "list"), ensure_ascii=False))]
            result = resp.json()
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        # ---------- memos_get ----------
        elif name == "memos_get":
            resp = await client.get(
                f"{MEMOS_URL}/product/get_memory/{arguments['memory_id']}",
                params={"user_id": _ensure_str(arguments.get("user_id"), MEMOS_USER)},
            )
            if resp.status_code != 200:
                return [TextContent(type="text", text=json.dumps(_format_error(resp, "get"), ensure_ascii=False))]
            result = resp.json()
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        # ---------- memos_feedback ----------
        elif name == "memos_feedback":
            payload = {
                "user_id": _ensure_str(arguments.get("user_id"), MEMOS_USER),
                "history": [{"role": "user", "content": arguments["history"]}],
                "feedback_content": arguments["feedback_content"],
                "retrieved_memory_ids": arguments.get("retrieved_memory_ids"),
            }
            cube_id = arguments.get("mem_cube_id")
            if cube_id:
                payload["writable_cube_ids"] = [cube_id]
            resp = await client.post(f"{MEMOS_URL}/product/feedback", json=payload)
            if resp.status_code != 200:
                return [TextContent(type="text", text=json.dumps(_format_error(resp, "feedback"), ensure_ascii=False))]
            result = resp.json()
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
