"""
问数查询接口路由

负责定义前端访问的 `/api/query` 接口，把 HTTP 请求交给 QueryService，
并把问数智能体执行过程以 SSE 形式持续返回给客户端。
同时提供会话历史的查询、清空与会话列表接口，支撑前端的多轮对话管理。

路由层只处理请求体、依赖声明和响应类型，不直接创建 Repository 或执行图节点。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import StreamingResponse

from app.api.dependencies import get_query_service, get_session_store
from app.api.schemas.query_schema import (
    QuerySchema,
    SessionHistoryItem,
    SessionHistoryResponse,
)
from app.services.query_service import QueryService
from app.services.session_store import SessionStore

# 当前模块只维护查询相关接口，避免后续所有 API 都挤在 main.py 中
query_router = APIRouter()


@query_router.post("/api/query")
async def query_handler(
    # 请求体参数：FastAPI 会把前端 JSON 自动解析成 QuerySchema
    query: QuerySchema,
    # 服务依赖：FastAPI 会调用 get_query_service，递归组装它所需的仓储和客户端
    query_service: Annotated[QueryService, Depends(get_query_service)],
):
    """
    接收用户自然语言问题，并流式返回 LangGraph 工作流输出。

    多轮对话协议：
    1. 首轮不传 session_id，服务端生成后通过 `session` 事件回传；
    2. 后续轮次把 session_id 带回，服务端据此加载历史实现追问理解。
    """

    return StreamingResponse(
        # query_service.query 返回异步生成器供响应逐段消费
        query_service.query(query.query, query.session_id),
        media_type="text/event-stream",
    )


@query_router.get(
    "/api/session/{session_id}",
    response_model=SessionHistoryResponse,
    tags=["session"],
)
async def get_session_history(
    session_id: str,
    store: Annotated[SessionStore, Depends(get_session_store)],
):
    """查询指定会话的历史轮次，用于前端刷新后恢复对话"""
    turns = await store.get_history(session_id, limit=100)
    return SessionHistoryResponse(
        session_id=session_id,
        turns=[
            SessionHistoryItem(
                question=t.question,
                resolved_question=t.resolved_question,
                sql=t.sql,
                row_count=t.row_count,
                created_at=t.created_at,
            )
            for t in turns
        ],
    )


@query_router.delete("/api/session/{session_id}", tags=["session"])
async def delete_session(
    session_id: str,
    store: Annotated[SessionStore, Depends(get_session_store)],
):
    """清空指定会话的历史，等价于前端点击「新会话」"""
    existed = await store.clear(session_id)
    if not existed:
        raise HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
    return {"session_id": session_id, "deleted": True}


@query_router.get("/api/sessions", tags=["session"])
async def list_sessions(
    store: Annotated[SessionStore, Depends(get_session_store)],
):
    """
    列出当前活跃会话与存储占用情况。

    暴露会话数量与总轮数，是为了让运维能观测内存中会话存储是否健康增长
    （内存实现的存储必须能看见容量水位，否则无法判断是否需要换 Redis）。
    """
    stats = await store.stats()
    # stats 中的 session_count / turn_count 是计数，与 sessions 列表语义不同，并列返回不会冲突
    return {"sessions": await store.list_sessions(), **stats}
