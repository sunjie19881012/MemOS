"""
Server API Router for MemOS (Class-based handlers version).

This router demonstrates the improved architecture using class-based handlers
with dependency injection, providing better modularity and maintainability.

Comparison with function-based approach:
- Cleaner code: No need to pass dependencies in every endpoint
- Better testability: Easy to mock handler dependencies
- Improved extensibility: Add new handlers or modify existing ones easily
- Clear separation of concerns: Router focuses on routing, handlers handle business logic
"""

import os
import random as _random
import socket

from fastapi import APIRouter, HTTPException, Query

from memos.api import handlers
from memos.api.handlers.add_handler import AddHandler
from memos.api.handlers.base_handler import HandlerDependencies
from memos.api.handlers.chat_handler import ChatHandler
from memos.api.handlers.cube_handler import CubeHandler
from memos.api.handlers.feedback_handler import FeedbackHandler
from memos.api.handlers.search_handler import SearchHandler
from memos.api.product_models import (
    AllStatusResponse,
    APIADDRequest,
    APIChatCompleteRequest,
    APIFeedbackRequest,
    APISearchRequest,
    ChatBusinessRequest,
    ChatPlaygroundRequest,
    ChatRequest,
    CreateCubeRequest,
    CreateCubeResponse,
    DeleteMemoryByRecordIdRequest,
    DeleteMemoryByRecordIdResponse,
    DeleteMemoryRequest,
    DeleteMemoryResponse,
    ExistMemCubeIdRequest,
    ExistMemCubeIdResponse,
    GetMemoryDashboardRequest,
    DashboardConfigResponse,
    DashboardRequestsResponse,
    GetMemoryPlaygroundRequest,
    GetMemoryRequest,
    GetMemoryResponse,
    GetUserNamesByMemoryIdsRequest,
    GetUserNamesByMemoryIdsResponse,
    MemoryResponse,
    RecoverMemoryByRecordIdRequest,
    RecoverMemoryByRecordIdResponse,
    RegisterCubeRequest,
    RegisterCubeResponse,
    SearchResponse,
    StatusResponse,
    SuggestionRequest,
    SuggestionResponse,
    TaskQueueResponse,
)
from memos.log import get_logger
from memos.mem_scheduler.base_scheduler import BaseScheduler
from memos.mem_scheduler.utils.status_tracker import TaskStatusTracker


logger = get_logger(__name__)

router = APIRouter(prefix="/product", tags=["Server API"])

# Instance ID for identifying this server instance in logs and responses
INSTANCE_ID = f"{socket.gethostname()}:{os.getpid()}:{_random.randint(1000, 9999)}"

# Initialize all server components
components = handlers.init_server()

# Create dependency container
dependencies = HandlerDependencies.from_init_server(components)

# Initialize all handlers with dependency injection
search_handler = SearchHandler(dependencies)
add_handler = AddHandler(dependencies)
chat_handler = (
    ChatHandler(
        dependencies=dependencies,
        chat_llms=components["chat_llms"],
        playground_chat_llms=components.get("playground_chat_llms"),
        search_handler=search_handler,
        add_handler=add_handler,
        online_bot=components.get("online_bot"),
    )
    if os.getenv("ENABLE_CHAT_API", "false") == "true"
    else None
)
feedback_handler = FeedbackHandler(dependencies)
cube_handler = CubeHandler(dependencies)
# Extract commonly used components for function-based handlers
# (These can be accessed from the components dict without unpacking all of them)
mem_scheduler: BaseScheduler = components["mem_scheduler"]
llm = components["llm"]
naive_mem_cube = components["naive_mem_cube"]
redis_client = components["redis_client"]
status_tracker = TaskStatusTracker(redis_client=redis_client)
graph_db = components["graph_db"]


# =============================================================================
# Search API Endpoints
# =============================================================================


@router.post("/search", summary="Search memories", response_model=SearchResponse)
def search_memories(search_req: APISearchRequest):
    """
    Search memories for a specific user.

    This endpoint uses the class-based SearchHandler for better code organization.
    """
    search_results = search_handler.handle_search_memories(search_req)
    return search_results


# =============================================================================
# Add API Endpoints
# =============================================================================


@router.post("/add", summary="Add memories", response_model=MemoryResponse)
def add_memories(add_req: APIADDRequest):
    """
    Add memories for a specific user.

    This endpoint uses the class-based AddHandler for better code organization.
    """
    return add_handler.handle_add_memories(add_req)


# =============================================================================
# Cube Management API Endpoints
# =============================================================================


@router.post("/create_cube", summary="Create a new memory cube", response_model=CreateCubeResponse)
async def create_cube(request: CreateCubeRequest) -> CreateCubeResponse:
    """
    Create a new memory cube for a user.

    Memory cubes are containers that store different types of memories (textual, activation, parametric).
    Each cube can be owned by a user and shared with other users.

    **Note on cube_id vs mem_cube_id:**
    These terms are used interchangeably throughout the API:
    - `cube_id` is the canonical identifier for a cube
    - `mem_cube_id` appears in many legacy endpoints and means the same thing
    - When using other endpoints (search, add, chat), you can reference this cube using either term

    **Semantic Clarification:**
    - **Single mem_cube_id** (deprecated): Used in older endpoints to identify a single cube.
      New code should use `readable_cube_ids` / `writable_cube_ids` lists instead.
    - **readable_cube_ids**: List of cube IDs the user can read from (used in search/chat)
    - **writable_cube_ids**: List of cube IDs the user can write to (used in add/chat)
    """
    return await cube_handler.create_cube(request)


@router.post(
    "/register_cube",
    summary="Register an existing memory cube",
    response_model=RegisterCubeResponse,
)
async def register_cube(request: RegisterCubeRequest) -> RegisterCubeResponse:
    """
    Register an existing memory cube with the MOS system.

    This method loads and registers a memory cube from a file path or creates a new one
    if the path doesn't exist. The cube becomes available for memory operations.

    **Note on cube_id vs mem_cube_id:**
    These terms are used interchangeably throughout the API. The registered cube can then
    be referenced by its cube_id/mem_cube_id in other endpoints.

    **Current Status:**
    This endpoint validates the registration request. Full registration functionality
    requires architectural integration with MOSCore, which will be completed in a future update.
    """
    return await cube_handler.register_cube(request)


# =============================================================================
# Scheduler API Endpoints
# =============================================================================


@router.get(  # Changed from post to get
    "/scheduler/allstatus",
    summary="Get detailed scheduler status",
    response_model=AllStatusResponse,
)
def scheduler_allstatus():
    """Get detailed scheduler status including running tasks and queue metrics."""
    return handlers.scheduler_handler.handle_scheduler_allstatus(
        mem_scheduler=mem_scheduler, status_tracker=status_tracker
    )


@router.get(  # Changed from post to get
    "/scheduler/status", summary="Get scheduler running status", response_model=StatusResponse
)
def scheduler_status(
    user_id: str = Query(..., description="User ID"),
    task_id: str | None = Query(None, description="Optional Task ID to query a specific task"),
):
    """Get scheduler running status."""
    return handlers.scheduler_handler.handle_scheduler_status(
        user_id=user_id,
        task_id=task_id,
        status_tracker=status_tracker,
    )


@router.get(  # Changed from post to get
    "/scheduler/task_queue_status",
    summary="Get scheduler task queue status",
    response_model=TaskQueueResponse,
)
def scheduler_task_queue_status(
    user_id: str = Query(..., description="User ID whose queue status is requested"),
):
    """Get scheduler task queue backlog/pending status for a user."""
    return handlers.scheduler_handler.handle_task_queue_status(
        user_id=user_id, mem_scheduler=mem_scheduler
    )


@router.post("/scheduler/wait", summary="Wait until scheduler is idle for a specific user")
def scheduler_wait(
    user_name: str,
    timeout_seconds: float = 120.0,
    poll_interval: float = 0.5,
):
    """Wait until scheduler is idle for a specific user."""
    return handlers.scheduler_handler.handle_scheduler_wait(
        user_name=user_name,
        status_tracker=status_tracker,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
    )


@router.get("/scheduler/wait/stream", summary="Stream scheduler progress for a user")
def scheduler_wait_stream(
    user_name: str,
    timeout_seconds: float = 120.0,
    poll_interval: float = 0.5,
):
    """Stream scheduler progress via Server-Sent Events (SSE)."""
    return handlers.scheduler_handler.handle_scheduler_wait_stream(
        user_name=user_name,
        status_tracker=status_tracker,
        timeout_seconds=timeout_seconds,
        poll_interval=poll_interval,
        instance_id=INSTANCE_ID,
    )


# =============================================================================
# Chat API Endpoints
# =============================================================================


@router.post("/chat/complete", summary="Chat with MemOS (Complete Response)")
def chat_complete(chat_req: APIChatCompleteRequest):
    """
    Chat with MemOS for a specific user. Returns complete response (non-streaming).

    This endpoint uses the class-based ChatHandler.
    """
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )
    return chat_handler.handle_chat_complete(chat_req)


@router.post("/chat/stream", summary="Chat with MemOS")
def chat_stream(chat_req: ChatRequest):
    """
    Chat with MemOS for a specific user. Returns SSE stream.

    This endpoint uses the class-based ChatHandler which internally
    composes SearchHandler and AddHandler for a clean architecture.
    """
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )
    return chat_handler.handle_chat_stream(chat_req)


@router.post("/chat/stream/playground", summary="Chat with MemOS playground")
def chat_stream_playground(chat_req: ChatPlaygroundRequest):
    """
    Chat with MemOS for a specific user. Returns SSE stream.

    This endpoint uses the class-based ChatHandler which internally
    composes SearchHandler and AddHandler for a clean architecture.
    """
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )
    return chat_handler.handle_chat_stream_playground(chat_req)


# =============================================================================
# Suggestion API Endpoints
# =============================================================================


@router.post(
    "/suggestions",
    summary="Get suggestion queries",
    response_model=SuggestionResponse,
)
def get_suggestion_queries(suggestion_req: SuggestionRequest):
    """Get suggestion queries for a specific user with language preference."""
    return handlers.suggestion_handler.handle_get_suggestion_queries(
        user_id=suggestion_req.mem_cube_id,
        language=suggestion_req.language,
        message=suggestion_req.message,
        llm=llm,
        naive_mem_cube=naive_mem_cube,
    )


# =============================================================================
# Memory Retrieval Delete API Endpoints
# =============================================================================


@router.post("/get_all", summary="Get all memories for user", response_model=MemoryResponse)
def get_all_memories(memory_req: GetMemoryPlaygroundRequest):
    """
    Get all memories or subgraph for a specific user.

    If search_query is provided, returns a subgraph based on the query.
    Otherwise, returns all memories of the specified type.
    """
    if memory_req.search_query:
        return handlers.memory_handler.handle_get_subgraph(
            user_id=memory_req.user_id,
            mem_cube_id=(
                memory_req.mem_cube_ids[0] if memory_req.mem_cube_ids else memory_req.user_id
            ),
            query=memory_req.search_query,
            top_k=200,
            naive_mem_cube=naive_mem_cube,
            search_type=memory_req.search_type,
        )
    else:
        return handlers.memory_handler.handle_get_all_memories(
            user_id=memory_req.user_id,
            mem_cube_id=(
                memory_req.mem_cube_ids[0] if memory_req.mem_cube_ids else memory_req.user_id
            ),
            memory_type=memory_req.memory_type or "text_mem",
            naive_mem_cube=naive_mem_cube,
        )


@router.post("/get_memory", summary="Get memories for user", response_model=GetMemoryResponse)
def get_memories(memory_req: GetMemoryRequest):
    return handlers.memory_handler.handle_get_memories(
        get_mem_req=memory_req,
        naive_mem_cube=naive_mem_cube,
    )


@router.get("/get_memory/{memory_id}", summary="Get memory by id", response_model=GetMemoryResponse)
def get_memory_by_id(memory_id: str):
    return handlers.memory_handler.handle_get_memory(
        memory_id=memory_id,
        naive_mem_cube=naive_mem_cube,
    )


@router.post("/get_memory_by_ids", summary="Get memory by ids", response_model=GetMemoryResponse)
def get_memory_by_ids(memory_ids: list[str]):
    return handlers.memory_handler.handle_get_memory_by_ids(
        memory_ids=memory_ids,
        naive_mem_cube=naive_mem_cube,
    )


@router.post(
    "/delete_memory", summary="Delete memories for user", response_model=DeleteMemoryResponse
)
def delete_memories(memory_req: DeleteMemoryRequest):
    return handlers.memory_handler.handle_delete_memories(
        delete_mem_req=memory_req, naive_mem_cube=naive_mem_cube
    )


# =============================================================================
# Feedback API Endpoints
# =============================================================================


@router.post("/feedback", summary="Feedback memories", response_model=MemoryResponse)
def feedback_memories(feedback_req: APIFeedbackRequest):
    """
    Feedback memories for a specific user.

    This endpoint uses the class-based FeedbackHandler for better code organization.
    """
    return feedback_handler.handle_feedback_memories(feedback_req)


# =============================================================================
# Other API Endpoints (for internal use)
# =============================================================================


@router.post(
    "/get_user_names_by_memory_ids",
    summary="Get user names by memory ids",
    response_model=GetUserNamesByMemoryIdsResponse,
)
def get_user_names_by_memory_ids(request: GetUserNamesByMemoryIdsRequest):
    """Get user names by memory ids. Now unified to query from graph_db only."""
    result = graph_db.get_user_names_by_memory_ids(memory_ids=request.memory_ids)

    return GetUserNamesByMemoryIdsResponse(
        code=200,
        message="Successfully",
        data=result,
    )


@router.post(
    "/exist_mem_cube_id",
    summary="Check if mem cube id exists",
    response_model=ExistMemCubeIdResponse,
)
def exist_mem_cube_id(request: ExistMemCubeIdRequest):
    """(inner) Check if mem cube id exists."""
    return ExistMemCubeIdResponse(
        code=200,
        message="Successfully",
        data=graph_db.exist_user_name(user_name=request.mem_cube_id),
    )


@router.post("/chat/stream/business_user", summary="Chat with MemOS for business user")
def chat_stream_business_user(chat_req: ChatBusinessRequest):
    """(inner) Chat with MemOS for a specific business user. Returns SSE stream."""
    if chat_handler is None:
        raise HTTPException(
            status_code=503, detail="Chat service is not available. Chat handler not initialized."
        )

    return chat_handler.handle_chat_stream_for_business_user(chat_req)


@router.post(
    "/delete_memory_by_record_id",
    summary="Delete memory by record id",
    response_model=DeleteMemoryByRecordIdResponse,
)
def delete_memory_by_record_id(memory_req: DeleteMemoryByRecordIdRequest):
    """(inner) Delete memory nodes by mem_cube_id (user_name) and delete_record_id. Record id is inner field, just for delete and recover memory, not for user to set."""
    graph_db.delete_node_by_mem_cube_id(
        mem_cube_id=memory_req.mem_cube_id,
        delete_record_id=memory_req.record_id,
        hard_delete=memory_req.hard_delete,
    )

    return DeleteMemoryByRecordIdResponse(
        code=200,
        message="Called Successfully",
        data={"status": "success"},
    )


@router.post(
    "/recover_memory_by_record_id",
    summary="Recover memory by record id",
    response_model=RecoverMemoryByRecordIdResponse,
)
def recover_memory_by_record_id(memory_req: RecoverMemoryByRecordIdRequest):
    """(inner) Recover memory nodes by mem_cube_id (user_name) and delete_record_id. Record id is inner field, just for delete and recover memory, not for user to set."""
    graph_db.recover_memory_by_mem_cube_id(
        mem_cube_id=memory_req.mem_cube_id,
        delete_record_id=memory_req.delete_record_id,
    )

    return RecoverMemoryByRecordIdResponse(
        code=200,
        message="Called Successfully",
        data={"status": "success"},
    )


@router.post(
    "/get_memory_dashboard", summary="Get memories for dashboard", response_model=GetMemoryResponse
)
def get_memories_dashboard(memory_req: GetMemoryDashboardRequest):
    return handlers.memory_handler.handle_get_memories_dashboard(
        get_mem_req=memory_req,
        naive_mem_cube=naive_mem_cube,
    )


# =============================================================================
# Dashboard API Endpoints (审计日志 + 运行时配置)
# =============================================================================

# Dashboard 总开关:默认关闭,开启后才暴露 /requests、/config、/dashboard 静态资源
_DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "false").lower() == "true"

# 配置白名单:只返回这些非敏感字段,绝不 dump 全量 os.environ。
# 排除: *_API_KEY, *_PASSWORD, *_SECRET, NEO4J_URI, QDRANT_HOST/PORT, *_API_BASE 等。
_CONFIG_WHITELIST = [
    "MOS_CHAT_MODEL_PROVIDER",
    "MOS_CHAT_MODEL",
    "MOS_EMBEDDER_BACKEND",
    "MOS_EMBEDDER_MODEL",
    "EMBEDDING_DIMENSION",
    "MOS_TEXT_MEM_TYPE",
    "MOS_ENABLE_REORGANIZE",
    "ENABLE_CHAT_API",
]


@router.get("/requests", summary="Dashboard: 审计日志(最近请求)", response_model=DashboardRequestsResponse)
def dashboard_get_requests(limit: int = Query(100, ge=1, le=1000, description="返回条数上限")):
    """返回最近 N 条 API 调用记录(来自审计缓冲)。

    关闭时返回 404,假装端点不存在。
    多 worker 下只能看到当前 worker 的记录(单机个人使用默认单 worker)。
    """
    if not _DASHBOARD_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")

    from memos.api.middleware.audit_buffer import audit_buffer

    return {"code": 200, "message": "ok", "data": audit_buffer.get_recent(limit)}


@router.get("/config", summary="Dashboard: 运行时配置(白名单脱敏)", response_model=DashboardConfigResponse)
def dashboard_get_config():
    """返回白名单内的运行时配置项(模型/嵌入/存储开关等)。

    关闭时返回 404。只暴露非敏感字段,密钥/连接串/拓扑地址均不返回。
    """
    if not _DASHBOARD_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")

    return {
        "code": 200,
        "message": "ok",
        "data": {key: os.getenv(key) for key in _CONFIG_WHITELIST},
    }


# =============================================================================
# Dashboard Graph & Edit Endpoints
# =============================================================================


@router.post("/export_graph", summary="Dashboard: 导出图谱(节点+边)")
def dashboard_export_graph(
    user_name: str = Query(..., description="cube_id/user_name,按实体过滤"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(200, ge=1, le=500, description="每页条数,上限500防止OOM"),
):
    """导出图谱节点和边,供前端 vis-network 渲染。

    底层 export_graph 返回 {nodes, edges:[{source,target,type}], total_nodes, total_edges}。
    端点层做字段映射:edges 的 source/target → from/to(vis-network 要求)。
    默认排除已软删节点(status != deleted)和 embedding 字段。
    """
    if not _DASHBOARD_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")

    raw = graph_db.export_graph(
        page=page,
        page_size=page_size,
        user_name=user_name,
    )
    # 字段映射:source/target → from/to(vis-network edges 格式)
    mapped_edges = [
        {"from": e.get("source"), "to": e.get("target"), "type": e.get("type", "RELATED")}
        for e in raw.get("edges", [])
    ]
    # 节点精简:只保留渲染所需字段
    nodes = []
    for n in raw.get("nodes", []):
        meta = n.get("metadata", {}) or {}
        nodes.append({
            "id": n.get("id"),
            "label": (n.get("memory", "") or "")[:40],  # 截断防止长文本撑爆节点
            "full_memory": n.get("memory", ""),
            "memory_type": meta.get("memory_type", "text"),
            "tags": meta.get("tags", []),
            "created_at": meta.get("created_at", ""),
        })

    return {
        "code": 200,
        "message": "ok",
        "data": {
            "nodes": nodes,
            "edges": mapped_edges,
            "total_nodes": raw.get("total_nodes", 0),
            "total_edges": raw.get("total_edges", 0),
            "page": page,
            "page_size": page_size,
        },
    }


@router.post("/update_memory", summary="Dashboard: 编辑记忆(仅 tags/metadata)")
def dashboard_update_memory(
    memory_id: str = Query(..., description="记忆节点 ID"),
    tags: list[str] | None = Query(None, description="新标签列表(替换)"),
):
    """编辑记忆的 tags/metadata。

    ⚠️ 安全约束:禁止修改 memory 文本字段。
    原因:update_node 不更新 embedding,改文本会导致向量搜索失准(无法自愈)。
    如需修改内容,请删除后通过 /product/add 重新添加(会重新计算 embedding)。
    update_node 的 fields 必须扁平(Neo4j 节点是扁平存储,不能传嵌套 dict)。
    """
    if not _DASHBOARD_ENABLED:
        raise HTTPException(status_code=404, detail="Not Found")

    fields = {}
    if tags is not None:
        fields["tags"] = tags

    if not fields:
        raise HTTPException(status_code=400, detail="没有可更新的字段(仅支持 tags)")

    graph_db.update_node(id=memory_id, fields=fields)
    return {"code": 200, "message": "ok", "data": {"memory_id": memory_id, "updated_tags": tags}}
