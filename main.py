"""
FastAPI 应用入口

负责创建后端应用实例，注册应用生命周期函数，并把各业务模块中的 router
挂载到同一个 app 上。HTTP 请求会先进入这里创建的 app，再按路由分发到
具体的接口处理函数。

除路由装配外，这里还承担两项横切职责：
1. 请求级 request_id 注入（贯穿日志与响应头）
2. 统一异常出口（把任意异常收敛成带业务错误码的 JSON，避免泄漏堆栈）
"""

import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.lifespan import lifespan
from app.api.routers.query_router import query_router
from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.core.context import request_id_ctx_var
from app.core.exception import AppError, ErrorCode
from app.core.log import logger

# lifespan 交给 FastAPI 管理，用于在服务启动和关闭时统一初始化与释放外部客户端
app = FastAPI(
    title="电商问数智能数据分析 Agent",
    description="基于 LangGraph 的自然语言问数服务：混合检索 + 多阶段推理 + SQL 生成执行",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS 配置：生产环境应把 allow_origins 收敛为前端实际域名，
# 这里保留 localhost 便于本地联调，避免前后端分离部署时被浏览器拦截
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# 把查询路由注册进应用；没有挂载时，/docs 和真实 HTTP 请求都访问不到该接口
app.include_router(query_router)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """
    为每个请求注入 request_id。

    先在 ContextVar 中写入本次请求 id，下游所有节点与仓储的日志都会自动带上它；
    同时在响应头中回传，便于前端把用户反馈与服务端日志对应起来。
    """
    request_id = str(uuid.uuid4())
    token = request_id_ctx_var.set(request_id)
    try:
        response = await call_next(request)
    finally:
        # ContextVar 必须重置，否则并发请求之间会串号
        request_id_ctx_var.reset(token)

    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(AppError)
async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
    """
    业务异常统一出口。

    响应只暴露稳定的 message，detail 仅进日志——避免把堆栈或下游原文
    透传给前端。同时带上 code 与 trace 用的 request_id，前端可按 code 分支处理。
    """
    request_id = request_id_ctx_var.get()
    logger.warning(
        f"业务异常命中 code={int(exc.code)} message={exc.message} detail={exc.detail}"
    )
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "code": int(exc.code),
            "message": exc.message,
            "data": None,
            "request_id": request_id,
        },
    )


@app.exception_handler(Exception)
async def handle_unknown_error(request: Request, exc: Exception) -> JSONResponse:
    """
    兜底异常处理器。

    没有这一层时，未预期异常会以 FastAPI 默认格式返回，堆栈信息可能出现在
    响应体中；有这一层后，客户端始终拿到同一套信封结构，排障靠 request_id 回查日志。
    """
    request_id = request_id_ctx_var.get()
    logger.exception(f"未处理异常: {exc!r}")
    return JSONResponse(
        status_code=500,
        content={
            "code": int(ErrorCode.INTERNAL_ERROR),
            "message": "服务内部错误",
            "data": None,
            "request_id": request_id,
        },
    )


@app.get("/health", tags=["ops"])
async def health():
    """
    存活探针（liveness）。

    只反映进程是否存活，不探测下游依赖——
    依赖探活由 /health/ready 承担，避免把外部服务抖动
    误判成本服务不可用而导致被编排系统反复重启。
    """
    return {"status": "ok"}


@app.get("/health/ready", tags=["ops"])
async def readiness():
    """
    就绪探针（readiness）。

    真实探测四个外部依赖（Qdrant / Embedding / ES / MySQL）。
    任一依赖不可用时返回 503，让编排系统把流量摘掉而不是继续打进来，
    直到依赖恢复。这正是 liveness 与 readiness 必须分离的原因：
    前者决定"要不要重启"，后者决定"要不要接流量"。
    """
    checks = {
        "qdrant": await qdrant_client_manager.probe(),
        "embedding": await embedding_client_manager.probe(),
        "es": await es_client_manager.probe(),
        "meta_mysql": await meta_mysql_client_manager.probe(),
        "dw_mysql": await dw_mysql_client_manager.probe(),
    }
    all_ready = all(checks.values())

    return JSONResponse(
        status_code=200 if all_ready else 503,
        content={
            "status": "ready" if all_ready else "not_ready",
            "dependencies": checks,
        },
    )
