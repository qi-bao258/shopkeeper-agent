"""
问数查询服务

负责把 API 层传入的自然语言问题转换成一次 LangGraph 工作流执行：
读取会话历史、创建初始 State、组装 Runtime Context、消费 graph.astream 的流式输出，
并统一包装成 SSE 文本返回给路由层。

多轮记忆的读写位置很关键：
- **读**发生在图执行之前（从 SessionStore 取最近 N 轮注入 State.history）
- **写**发生在图执行之后（把本轮的问题、改写后的问题、SQL 与结果行数落库）

这样设计而不是让节点自己去读写存储，是因为节点应该只关心「计算」，
会话生命周期属于编排职责，收敛在 Service 层更好维护与测试。
"""

import json

from langchain_huggingface import HuggingFaceEndpointEmbeddings

from app.agent.context import DataAgentContext
from app.agent.graph import graph
from app.agent.state import DataAgentState
from app.core.log import logger
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
from app.services.session_store import (
    SessionStore,
    Turn,
    new_session_id,
)
from app.services.session_store import (
    session_store as default_session_store,
)


class QueryService:
    """封装一次问数查询所需的业务编排逻辑"""

    # 注入给追问改写的最近历史轮数。
    # 取 5 轮是经验值：更早的对话对指代消解帮助有限，反而会稀释模型注意力并增加 token 成本。
    HISTORY_TURNS = 5

    def __init__(
        self,
        meta_mysql_repository: MetaMySQLRepository,
        embedding_client: HuggingFaceEndpointEmbeddings,
        dw_mysql_repository: DWMySQLRepository,
        column_qdrant_repository: ColumnQdrantRepository,
        metric_qdrant_repository: MetricQdrantRepository,
        value_es_repository: ValueESRepository,
        session_store: SessionStore | None = None,
    ):
        # MySQL 仓储分别负责元数据补全和真实数仓环境信息读取
        self.meta_mysql_repository = meta_mysql_repository
        self.dw_mysql_repository = dw_mysql_repository

        # 召回链路依赖的向量检索、Embedding 和全文检索能力由依赖层注入
        self.embedding_client = embedding_client
        self.column_qdrant_repository = column_qdrant_repository
        self.metric_qdrant_repository = metric_qdrant_repository
        self.value_es_repository = value_es_repository

        # 会话记忆存储；未显式传入时使用模块级单例
        self.session_store: SessionStore = session_store or default_session_store

    async def query(self, query: str, session_id: str | None = None):
        """
        执行一次问数工作流，并逐段产出 SSE 消息。

        Args:
            query: 用户本轮的原始提问
            session_id: 会话标识；为空时自动创建新会话，并通过 session 事件回传前端
        """

        # 前端首次请求不带 session_id，由服务端生成并把 id 推给前端，
        # 后续轮次前端带上它即可命中同一份历史
        session_id = session_id or new_session_id()

        # 图执行前读取历史：追问改写节点需要它来补全指代
        history = await self.session_store.get_history(
            session_id, limit=self.HISTORY_TURNS
        )
        history_payload = [
            {
                "question": turn.question,
                "resolved_question": turn.resolved_question,
                "sql": turn.sql,
            }
            for turn in history
        ]

        # State 只放会被图节点读写和合并的业务数据，外部工具对象不塞进 State
        state = DataAgentState(
            query=query,
            resolved_query=query,
            session_id=session_id,
            history=history_payload,
        )
        # Context 保存本次图执行需要复用的外部依赖，节点通过 runtime.context 读取
        context = DataAgentContext(
            column_qdrant_repository=self.column_qdrant_repository,
            embedding_client=self.embedding_client,
            metric_qdrant_repository=self.metric_qdrant_repository,
            value_es_repository=self.value_es_repository,
            meta_mysql_repository=self.meta_mysql_repository,
            dw_mysql_repository=self.dw_mysql_repository,
            session_store=self.session_store,
        )

        # 本轮的执行结果，用于图结束后落库
        resolved_query = query
        final_sql: str | None = None
        row_count = 0

        try:
            # 先把 session_id 推给前端，否则前端拿不到 id 就无法继续多轮
            yield self._sse({"type": "session", "session_id": session_id})

            # stream_mode="custom" 对应节点内部 writer(...) 写出的进度消息
            async for chunk in graph.astream(
                input=state, context=context, stream_mode="custom"
            ):
                # 从流式事件里抽取需要持久化的信息。
                # result 事件由 run_sql 写出，携带本轮最终 SQL 与改写后的问题。
                if isinstance(chunk, dict) and chunk.get("type") == "result":
                    final_sql = chunk.get("sql") or final_sql
                    resolved_query = chunk.get("resolved_query") or resolved_query
                    data = chunk.get("data")
                    if isinstance(data, list):
                        row_count = len(data)

                yield self._sse(chunk)

        except Exception as e:
            # 流式接口已经开始返回后不能再改 HTTP 状态码，因此把异常也包装成一条 SSE 消息
            yield self._sse({"type": "error", "message": str(e)})

        finally:
            # 无论成功失败都记录本轮，便于用户追问「刚才那个查询」时能被理解。
            # 失败轮次的 sql 记为 None，避免把无效 SQL 带入下一轮上下文。
            try:
                await self.session_store.append_turn(
                    session_id,
                    Turn(
                        question=query,
                        resolved_question=resolved_query,
                        sql=final_sql,
                        row_count=row_count,
                    ),
                )
            except Exception as e:  # 落库失败不应影响已经返回给用户的结果
                logger.error(f"会话历史落库失败：{e}")

    @staticmethod
    def _sse(payload: dict) -> str:
        """把事件包装成 SSE 帧：data: 开头 + 两个换行结尾"""
        # ensure_ascii=False 保留中文文案，default=str 兜底处理日期等非 JSON 类型
        return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
