"""
健康检查接口测试

覆盖 liveness 与 readiness 的职责分离：
1. /health 只反映进程存活，不触碰任何下游依赖，必须恒返回 200
2. /health/ready 真实探测依赖，全部可用返回 200，任一不可用返回 503
3. 未预期异常不会以 FastAPI 默认格式泄漏，而是收敛成统一信封

测试通过 monkeypatch 替换各 client manager 的 probe()，
在不启动真实 Qdrant / ES / MySQL / Embedding 的前提下验证分支逻辑。
"""

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import main as main_module
from app.clients import embedding_client_manager as emb_mod
from app.clients import es_client_manager as es_mod
from app.clients import mysql_client_manager as mysql_mod
from app.clients import qdrant_client_manager as qdrant_mod


@asynccontextmanager
async def _noop_lifespan(app):
    """跳过真实外部客户端初始化"""
    yield


@pytest.fixture
def client():
    """不依赖外部服务的 TestClient"""
    app = main_module.app
    original = app.router.lifespan_context
    app.router.lifespan_context = _noop_lifespan
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.router.lifespan_context = original


@pytest.fixture
def set_probe(monkeypatch):
    """
    批量设置各 manager 的 probe 返回值。

    用法：set_probe(qdrant=True, es=False) —— 未指定的依赖默认为 True。
    """

    def _apply(**overrides):
        default = True
        mapping = {
            "qdrant": qdrant_mod.qdrant_client_manager,
            "embedding": emb_mod.embedding_client_manager,
            "es": es_mod.es_client_manager,
            "meta_mysql": mysql_mod.meta_mysql_client_manager,
            "dw_mysql": mysql_mod.dw_mysql_client_manager,
        }
        for name, manager in mapping.items():
            result = overrides.get(name, default)

            async def _probe(_result=result):
                return _result

            monkeypatch.setattr(manager, "probe", _probe)

    return _apply


class TestLiveness:
    """存活探针"""

    def test_health_returns_ok(self, client):
        """/health 恒返回 200，不因依赖不可用而失败"""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_health_does_not_touch_dependencies(self, client, monkeypatch):
        """存活探针不应调用任何依赖探活，否则会被误判为需要重启"""
        called = []

        async def _spy():
            called.append(1)
            return False

        monkeypatch.setattr(qdrant_mod.qdrant_client_manager, "probe", _spy)
        client.get("/health")
        assert called == [], "/health 不应探测下游依赖"


class TestReadiness:
    """就绪探针"""

    def test_ready_when_all_dependencies_up(self, client, set_probe):
        """全部依赖可用时返回 200"""
        set_probe()  # 全部 True
        resp = client.get("/health/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert all(body["dependencies"].values())

    @pytest.mark.parametrize(
        "broken",
        ["qdrant", "embedding", "es", "meta_mysql", "dw_mysql"],
    )
    def test_not_ready_when_any_dependency_down(self, client, set_probe, broken):
        """任一依赖不可用时必须返回 503，让编排系统摘掉流量"""
        set_probe(**{broken: False})
        resp = client.get("/health/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "not_ready"
        assert body["dependencies"][broken] is False

    def test_reports_all_dependency_states(self, client, set_probe):
        """响应体应逐项列出依赖状态，便于定位是哪一项未就绪"""
        set_probe(es=False, dw_mysql=False)
        body = client.get("/health/ready").json()
        expected_keys = {"qdrant", "embedding", "es", "meta_mysql", "dw_mysql"}
        assert set(body["dependencies"]) == expected_keys


class TestGlobalExceptionHandler:
    """统一异常出口"""

    def test_business_error_uses_envelope(self, client, monkeypatch):
        """
        业务异常应转换成带 code/message/request_id 的统一信封，
        而不是 FastAPI 默认的 {"detail": ...} 结构。
        """
        from app.core.exception import ParamError

        @main_module.app.get("/_test_biz_error")
        async def _boom():
            raise ParamError("测试用的入参错误")

        try:
            resp = client.get("/_test_biz_error")
            assert resp.status_code == 400
            body = resp.json()
            assert body["code"] == 10001
            assert body["message"] == "测试用的入参错误"
            assert "request_id" in body
        finally:
            # 移除临时路由，避免污染后续测试
            main_module.app.router.routes = [
                r
                for r in main_module.app.router.routes
                if getattr(r, "path", "") != "/_test_biz_error"
            ]
