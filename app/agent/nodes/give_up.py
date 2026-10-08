"""
SQL 修正放弃节点

当 SQL 在重试上限内仍无法通过校验时进入本节点。
它的作用不是"失败"，而是"有界地失败"：把最后一次的数据库错误
包装成结构化结果推送给前端，让用户看到明确的失败原因，
而不是让图执行抛异常、让请求悬挂或让模型无限重试消耗 token。
"""

from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger


async def give_up(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """在修正次数耗尽后，返回结构化失败信息并结束流程"""

    writer = runtime.stream_writer
    step = "SQL修正失败"
    writer({"type": "progress", "step": step, "status": "error"})

    error = state.get("error")
    retry_count = state.get("retry_count", 0)

    logger.warning(f"SQL 修正 {retry_count} 次后仍未通过校验：{error}")

    writer(
        {
            "type": "error",
            "message": f"SQL 在 {retry_count} 次修正后仍无法通过校验：{error}",
        }
    )

    # retry_count 计数自增为 retry_count，说明 error 字段仍保留最后一次报错
    return {"retry_count": retry_count}
