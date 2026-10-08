"""
API 层测试

覆盖 FastAPI 应用的对外契约：
1. /health 健康检查可直接访问
2. request_id 中间件会为响应注入 X-Request-ID
3. /api/query 返回 text/event-stream，且把用户问题与会话 id 转发给 QueryService
4. 会话管理接口：历史查询 / 清空 / 列表

测试通过覆盖依赖注入与 lifespan 来隔离外部服务，不需要启动真实容器。
"""

import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import main as main_module
from app.api.dependencies import get_query_service, get_session_store
from app.services.session_store import InMemorySessionStore, Turn


@asynccontextmanager
async def _noop_lifespan(app):
    """替换真实 lifespan，跳过 MySQL/ES/Qdrant 客户端初始化"""
    yield


@pytest.fixture
def client():
    """
    构造不依赖外部服务的 TestClient。

    关键点是替换 `app.router.lifespan_context`，绕开真实的 lifespan——
    否则启动时会尝试初始化 MySQL / Qdrant / ES 客户端，测试无法在 CI 中运行。
    """
    app = main_module.app
    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = _noop_lifespan
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.router.lifespan_context = original_lifespan
        app.dependency_overrides.clear()


@pytest.fixture
def session_store() -> InMemorySessionStore:
    """每个测试独立的内存会话存储，避免测试之间互相污染"""
    return InMemorySessionStore(max_sessions=10, max_turns_per_session=10)


@pytest.fixture
def client_with_store(client, session_store):
    """把会话存储依赖替换为测试实例"""

    async def fake_store():
        return session_store

    client.app.dependency_overrides[get_session_store] = fake_store
    return client


class TestHealth:
    def test_health_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestRequestIdMiddleware:
    def test_response_carries_request_id(self, client):
        """每个响应都应带上 X-Request-ID，便于把前端反馈对应到服务端日志"""
        resp = client.get("/health")
        request_id = resp.headers.get("X-Request-ID")
        assert request_id is not None
        # 必须是合法 UUID，而不是空串
        assert len(request_id) == 36

    def test_request_ids_are_unique(self, client):
        """不同请求的 request_id 必须不同，否则并发链路会串号"""
        ids = {client.get("/health").headers["X-Request-ID"] for _ in range(5)}
        assert len(ids) == 5


class TestQueryEndpoint:
    def test_query_streams_sse(self, client):
        """查询接口应返回 SSE，并包含 progress / result 两类事件"""

        class FakeQueryService:
            async def query(self, query: str, session_id: str | None = None):
                assert query == "统计华北地区的销售总额"
                # ensure_ascii=False 与 QueryService 的真实序列化行为保持一致，
                # 否则中文会被转义成 \uXXXX，无法断言可读文案
                event = {"type": "progress", "step": "抽取关键词", "status": "success"}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                result = {"type": "result", "data": [{"cnt": 1}]}
                yield f"data: {json.dumps(result, ensure_ascii=False)}\n\n"

        async def fake_get_query_service():
            return FakeQueryService()

        client.app.dependency_overrides[get_query_service] = fake_get_query_service

        resp = client.post("/api/query", json={"query": "统计华北地区的销售总额"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        body = resp.text
        assert "抽取关键词" in body
        assert '"type": "result"' in body
        # SSE 协议要求每条消息以两个换行结尾
        assert body.endswith("\n\n")

    def test_query_forwards_session_id(self, client):
        """前端回传的 session_id 必须原样透传给 Service，否则多轮上下文会断"""
        captured = {}

        class FakeQueryService:
            async def query(self, query: str, session_id: str | None = None):
                captured["query"] = query
                captured["session_id"] = session_id
                yield 'data: {"type": "result", "data": []}\n\n'

        async def fake_get_query_service():
            return FakeQueryService()

        client.app.dependency_overrides[get_query_service] = fake_get_query_service
        client.post("/api/query", json={"query": "那华东呢", "session_id": "abc123"})

        assert captured["query"] == "那华东呢"
        assert captured["session_id"] == "abc123"

    def test_query_accepts_missing_session_id(self, client):
        """首轮不传 session_id 时应正常受理，由服务端生成"""

        class FakeQueryService:
            async def query(self, query: str, session_id: str | None = None):
                yield 'data: {"type": "result", "data": []}\n\n'

        async def fake_get_query_service():
            return FakeQueryService()

        client.app.dependency_overrides[get_query_service] = fake_get_query_service
        resp = client.post("/api/query", json={"query": "统计华北地区的销售总额"})
        assert resp.status_code == 200

    def test_query_missing_field_returns_422(self, client):
        """
        请求体缺少 query 字段时，FastAPI 应返回 422 而不是 500。

        注意：请求体校验失败时仍会先尝试解析依赖（FastAPI 解依赖早于校验失败返回），
        因此这里必须把所有依赖都替换掉，否则会因未初始化的连接池报错。
        """

        async def fake_query_service():
            return object()

        client.app.dependency_overrides[get_query_service] = fake_query_service
        resp = client.post("/api/query", json={})
        assert resp.status_code == 422

    def test_query_blank_rejected_by_schema(self, client):
        """空字符串问题应被 Pydantic 拦下（min_length=1）"""

        async def fake_query_service():
            return object()

        client.app.dependency_overrides[get_query_service] = fake_query_service
        resp = client.post("/api/query", json={"query": ""})
        assert resp.status_code == 422


class TestSessionEndpoints:
    @pytest.mark.asyncio
    async def test_get_history(self, client_with_store, session_store):
        await session_store.append_turn(
            "s1",
            Turn(
                question="那华东呢",
                resolved_question="统计华东地区的销售总额",
                sql="SELECT 1",
                row_count=3,
            ),
        )

        resp = client_with_store.get("/api/session/s1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session_id"] == "s1"
        assert len(data["turns"]) == 1
        turn = data["turns"][0]
        assert turn["question"] == "那华东呢"
        assert turn["resolved_question"] == "统计华东地区的销售总额"
        assert turn["sql"] == "SELECT 1"
        assert turn["row_count"] == 3

    def test_get_unknown_history_returns_empty(self, client_with_store):
        """未知会话返回空轮次列表，而不是 404——前端刷新后恢复场景更需要空态"""
        resp = client_with_store.get("/api/session/not-exist")
        assert resp.status_code == 200
        assert resp.json()["turns"] == []

    @pytest.mark.asyncio
    async def test_delete_session(self, client_with_store, session_store):
        await session_store.append_turn("s1", Turn(question="q", resolved_question="q"))

        resp = client_with_store.delete("/api/session/s1")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert await session_store.get_history("s1") == []

    def test_delete_missing_session_returns_404(self, client_with_store):
        resp = client_with_store.delete("/api/session/not-exist")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_sessions_reports_stats(self, client_with_store, session_store):
        await session_store.append_turn(
            "s1", Turn(question="q1", resolved_question="q1")
        )
        await session_store.append_turn(
            "s2", Turn(question="q2", resolved_question="q2")
        )

        resp = client_with_store.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data["sessions"]) == {"s1", "s2"}
        # 计数与列表是两个不同语义的字段，必须能同时返回且不冲突
        assert data["session_count"] == 2
        assert data["turn_count"] == 2
        # 存储容量上限必须一并暴露，供运维判断内存水位
        assert data["max_turns_per_session"] == session_store.max_turns_per_session
