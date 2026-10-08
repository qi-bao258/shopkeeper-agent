"""
统一错误码与业务异常

参照企业级实践，把"错误"从散落的字符串提升为可编排的契约：
1. 错误码分段：不同层级的故障用不同号段，前端与告警系统可据此分流
2. 稳定消息 + 可选细节：message 面向用户保持稳定，detail 面向排障携带上下文
3. 与 HTTP 状态码解耦：业务码表达"是什么错"，状态码表达"怎么处理"

号段划分（与项目现有的四层依赖对应）：
    10000 段 —— 通用 / 参数 / 未知错误
    20000 段 —— LLM 相关（模型调用失败、输出解析失败）
    30000 段 —— 工具与检索相关（Qdrant / ES / Embedding）
    40000 段 —— SQL 与数据仓库相关（生成失败、护栏拦截、执行失败）
    50000 段 —— 会话与存储相关
"""

from enum import IntEnum
from typing import Any


class ErrorCode(IntEnum):
    """业务错误码。

    使用 IntEnum 而非裸常量，是为了让错误码在日志与响应体中都保持可读，
    并且能在类型检查阶段发现拼写错误——字符串错误码最容易在重构时静默失效。
    """

    # 10000 段：通用
    UNKNOWN = 10000
    INVALID_PARAM = 10001
    INTERNAL_ERROR = 10002

    # 20000 段：LLM
    LLM_ERROR = 20001
    LLM_PARSE_ERROR = 20002

    # 30000 段：检索与工具
    RETRIEVAL_ERROR = 30001
    EMBEDDING_ERROR = 30002

    # 40000 段：SQL 与数仓
    SQL_GENERATE_ERROR = 40001
    SQL_GUARD_REJECTED = 40002
    SQL_EXECUTE_ERROR = 40003

    # 50000 段：会话与存储
    SESSION_ERROR = 50001


# 每个错误码对应的默认文案。前端可直接展示，避免把内部异常原文暴露给用户。
_DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.UNKNOWN: "服务开小差了，请稍后重试",
    ErrorCode.INVALID_PARAM: "请求参数不合法",
    ErrorCode.INTERNAL_ERROR: "服务内部错误",
    ErrorCode.LLM_ERROR: "模型服务暂时不可用，请稍后重试",
    ErrorCode.LLM_PARSE_ERROR: "模型输出解析失败，请换一种问法",
    ErrorCode.RETRIEVAL_ERROR: "检索服务暂时不可用",
    ErrorCode.EMBEDDING_ERROR: "向量化服务暂时不可用",
    ErrorCode.SQL_GENERATE_ERROR: "未能生成可用的查询语句，请补充更多信息",
    ErrorCode.SQL_GUARD_REJECTED: "查询被安全策略拦截",
    ErrorCode.SQL_EXECUTE_ERROR: "查询执行失败",
    ErrorCode.SESSION_ERROR: "会话状态异常",
}


class AppError(Exception):
    """项目业务异常基类。

    service 与 repository 层抛出它，由 main.py 的全局异常处理器统一转换成
    带正确状态码与错误码的 JSON 响应——避免每个路由都写一遍 try/except。
    """

    # 子类可覆盖：默认视为服务端问题
    http_status: int = 500
    code: ErrorCode = ErrorCode.UNKNOWN

    def __init__(
        self,
        message: str | None = None,
        *,
        code: ErrorCode | None = None,
        detail: Any = None,
    ):
        self.code = code or self.code
        # 未显式传 message 时回落到错误码的默认文案，保证响应文案稳定可预期
        self.message = message or _DEFAULT_MESSAGES.get(self.code, "服务异常")
        # detail 只进日志，不进响应体，避免泄漏内部实现细节
        self.detail = detail
        super().__init__(self.message)


class ParamError(AppError):
    """入参校验失败（客户端问题）"""

    http_status = 400
    code = ErrorCode.INVALID_PARAM


class RetrievalError(AppError):
    """检索链路失败（Qdrant / ES / Embedding）"""

    http_status = 503
    code = ErrorCode.RETRIEVAL_ERROR


class LLMError(AppError):
    """LLM 调用或输出解析失败"""

    http_status = 503
    code = ErrorCode.LLM_ERROR


class SQLError(AppError):
    """SQL 生成、护栏拦截或执行失败"""

    http_status = 500
    code = ErrorCode.SQL_EXECUTE_ERROR


class SQLGuardRejected(SQLError):
    """SQL 未通过安全护栏（属于可预期的拒绝，非服务故障）"""

    http_status = 400
    code = ErrorCode.SQL_GUARD_REJECTED
