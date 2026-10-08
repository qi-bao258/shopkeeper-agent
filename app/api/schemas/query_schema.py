"""
问数接口请求体定义

集中声明 API 层输入输出的数据结构，让路由函数只处理业务流程，
字段校验和 OpenAPI 文档生成交给 Pydantic 与 FastAPI 完成。
"""

from pydantic import BaseModel, Field


class QuerySchema(BaseModel):
    """`/api/query` 请求体，承载用户输入的自然语言问题与可选的会话标识"""

    # 前端请求体中的 query 字段，例如 {"query": "统计华北地区销售额"}
    query: str = Field(
        min_length=1,
        max_length=2000,
        description="用户的自然语言问题；多轮追问时只需填写省略后的问法",
    )

    # 会话标识。首轮不传，服务端会生成并通过 session 事件回传；
    # 后续轮次把它带回来即可命中同一份对话历史，实现多轮追问
    session_id: str | None = Field(
        default=None,
        max_length=64,
        description="会话标识；首轮留空由服务端生成，后续轮次回传以延续上下文",
    )


class SessionHistoryItem(BaseModel):
    """会话历史中的一轮记录"""

    question: str = Field(description="用户当轮的原始提问")
    resolved_question: str = Field(description="追问改写后的独立问题")
    sql: str | None = Field(default=None, description="该轮生成的 SQL，失败时为 null")
    row_count: int = Field(default=0, description="该轮查询返回的行数")
    created_at: float = Field(description="记录时间戳（Unix 秒）")


class SessionHistoryResponse(BaseModel):
    """会话历史查询响应"""

    session_id: str
    turns: list[SessionHistoryItem]
