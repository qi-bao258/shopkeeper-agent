"""
统一错误码与业务异常单元测试

覆盖三层契约：
1. 错误码分段：不同层级的故障落在不同号段，前端与告警可据此分流
2. 状态码映射：业务异常携带的 HTTP 状态码符合语义（客户端错 4xx / 服务侧错 5xx）
3. 消息稳定性：未传 message 时回落到错误码默认文案，避免响应文案随机漂移

这些测试不依赖 FastAPI 实例，属于纯逻辑回归。
"""

import pytest

from app.core.exception import (
    AppError,
    ErrorCode,
    LLMError,
    ParamError,
    RetrievalError,
    SQLError,
    SQLGuardRejected,
)


class TestErrorCodeSegments:
    """错误码号段划分"""

    def test_segments_are_disjoint(self):
        """各层号段不应重叠，否则前端无法按号段分流"""
        segments = {
            "general": (10000, 19999),
            "llm": (20000, 29999),
            "retrieval": (30000, 39999),
            "sql": (40000, 49999),
            "session": (50000, 59999),
        }
        for lower, upper in segments.values():
            members = [c for c in ErrorCode if lower <= int(c) <= upper]
            assert members, f"号段 {lower}-{upper} 内没有错误码"

    def test_sql_codes_land_in_sql_segment(self):
        """SQL 相关错误码必须落在 40000 段"""
        assert 40000 <= int(ErrorCode.SQL_GENERATE_ERROR) < 50000
        assert 40000 <= int(ErrorCode.SQL_GUARD_REJECTED) < 50000
        assert 40000 <= int(ErrorCode.SQL_EXECUTE_ERROR) < 50000


class TestAppErrorDefaults:
    """默认文案与继承关系"""

    def test_falls_back_to_default_message(self):
        """未传 message 时使用错误码默认文案"""
        err = ParamError()
        assert err.message == "请求参数不合法"
        assert str(err) == "请求参数不合法"

    def test_explicit_message_overrides_default(self):
        """显式传入的 message 优先于默认文案"""
        err = ParamError("session_id 格式非法")
        assert err.message == "session_id 格式非法"

    def test_all_concrete_errors_are_app_errors(self):
        """所有具体异常都应可被 AppError 基类统一捕获"""
        for cls in (ParamError, RetrievalError, LLMError, SQLError, SQLGuardRejected):
            assert issubclass(cls, AppError)


class TestStatusMapping:
    """HTTP 状态码语义"""

    @pytest.mark.parametrize(
        ("exc_cls", "expected_status"),
        [
            (ParamError, 400),  # 客户端入参问题
            (SQLGuardRejected, 400),  # 可预期的拒绝，非服务故障
            (RetrievalError, 503),  # 下游依赖不可用，应重试
            (LLMError, 503),
            (SQLError, 500),  # 服务侧执行失败
        ],
    )
    def test_status_codes(self, exc_cls, expected_status):
        """每类异常携带的状态码应符合其在调用链中的语义"""
        assert exc_cls().http_status == expected_status

    def test_detail_is_not_exposed_in_message(self):
        """detail 用于日志排障，不应混入面向用户的 message"""
        err = SQLError(detail="connection refused to dw-mysql:3306")
        assert "connection refused" not in err.message
        assert err.detail == "connection refused to dw-mysql:3306"


class TestErrorCodeOverride:
    """错误码可按调用点覆盖"""

    def test_can_override_code(self):
        """同一异常类可在具体调用点携带更精确的错误码"""
        err = AppError(code=ErrorCode.EMBEDDING_ERROR)
        assert err.code == ErrorCode.EMBEDDING_ERROR
        assert err.message == "向量化服务暂时不可用"
