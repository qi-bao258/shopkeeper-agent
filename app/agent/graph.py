"""
电商问数 Agent 图编排

使用 LangGraph 把问数智能体的各个节点串成一条可观测的执行链路
当前链路已经落地关键词抽取和多路召回，字段和指标走 Qdrant 向量检索，字段取值走 ES 全文检索
多轮场景下最前端会先做追问改写，把依赖上下文的提问还原成独立完整的问题，
随后抽取关键词、并行召回字段 字段取值和指标信息，
再合并召回结果 过滤候选表和指标 补充额外上下文，最后生成 校验 修正并执行 SQL
"""

import asyncio

from langgraph.constants import END, START
from langgraph.graph import StateGraph

from app.agent.context import DataAgentContext
from app.agent.nodes.add_extra_context import add_extra_context
from app.agent.nodes.correct_sql import correct_sql
from app.agent.nodes.extract_keywords import extract_keywords
from app.agent.nodes.filter_metric import filter_metric
from app.agent.nodes.filter_table import filter_table
from app.agent.nodes.generate_sql import generate_sql
from app.agent.nodes.give_up import give_up
from app.agent.nodes.merge_retrieved_info import merge_retrieved_info
from app.agent.nodes.recall_column import recall_column
from app.agent.nodes.recall_metric import recall_metric
from app.agent.nodes.recall_value import recall_value
from app.agent.nodes.resolve_followup import resolve_followup
from app.agent.nodes.run_sql import run_sql
from app.agent.nodes.validate_sql import validate_sql
from app.agent.state import DataAgentState
from app.clients.embedding_client_manager import embedding_client_manager
from app.clients.es_client_manager import es_client_manager
from app.clients.mysql_client_manager import (
    dw_mysql_client_manager,
    meta_mysql_client_manager,
)
from app.clients.qdrant_client_manager import qdrant_client_manager
from app.core.log import logger
from app.repositories.es.value_es_repository import ValueESRepository
from app.repositories.mysql.dw.dw_mysql_repository import DWMySQLRepository
from app.repositories.mysql.meta.meta_mysql_repository import MetaMySQLRepository
from app.repositories.qdrant.column_qdrant_repository import ColumnQdrantRepository
from app.repositories.qdrant.metric_qdrant_repository import MetricQdrantRepository
from app.services.session_store import session_store

# StateGraph 声明整张图使用的状态结构和运行时上下文结构
graph_builder = StateGraph(state_schema=DataAgentState, context_schema=DataAgentContext)

# 注册节点：每个节点负责问数链路中的一个清晰步骤
graph_builder.add_node("resolve_followup", resolve_followup)
graph_builder.add_node("extract_keywords", extract_keywords)
graph_builder.add_node("recall_column", recall_column)
graph_builder.add_node("recall_value", recall_value)
graph_builder.add_node("recall_metric", recall_metric)
graph_builder.add_node("merge_retrieved_info", merge_retrieved_info)
graph_builder.add_node("filter_metric", filter_metric)
graph_builder.add_node("filter_table", filter_table)
graph_builder.add_node("add_extra_context", add_extra_context)
graph_builder.add_node("generate_sql", generate_sql)
graph_builder.add_node("validate_sql", validate_sql)
graph_builder.add_node("correct_sql", correct_sql)
graph_builder.add_node("run_sql", run_sql)
graph_builder.add_node("give_up", give_up)

# 多轮场景下第一步先做追问改写，把依赖上下文的提问补全成独立问题，
# 后续所有召回与 SQL 生成节点都基于改写后的问题，而不是用户的原始输入
graph_builder.add_edge(START, "resolve_followup")
graph_builder.add_edge("resolve_followup", "extract_keywords")

# 关键词抽取后并行进入三类召回，分别面向字段 字段值和业务指标
graph_builder.add_edge("extract_keywords", "recall_column")
graph_builder.add_edge("extract_keywords", "recall_value")
graph_builder.add_edge("extract_keywords", "recall_metric")

# 三路召回都完成后，再进入统一的信息合并节点
graph_builder.add_edge("recall_column", "merge_retrieved_info")
graph_builder.add_edge("recall_value", "merge_retrieved_info")
graph_builder.add_edge("recall_metric", "merge_retrieved_info")

# 合并后的候选信息继续拆成表过滤和指标过滤两条线
graph_builder.add_edge("merge_retrieved_info", "filter_table")
graph_builder.add_edge("merge_retrieved_info", "filter_metric")

# 表和指标都过滤完成后，统一补充生成 SQL 所需的上下文
graph_builder.add_edge("filter_table", "add_extra_context")
graph_builder.add_edge("filter_metric", "add_extra_context")
graph_builder.add_edge("add_extra_context", "generate_sql")
graph_builder.add_edge("generate_sql", "validate_sql")

# SQL 修正的最大重试次数。超过上限仍在报错时，不再让模型继续瞎改，
# 而是直接把失败结果返回给用户，避免无限修正导致的 token 消耗与请求悬挂
MAX_SQL_RETRY = 2


def route_after_validate(state: DataAgentState) -> str:
    """
    SQL 校验后的分支决策。

    - 校验通过（error 为空）：直接执行
    - 校验失败且未达重试上限：进入修正节点
    - 校验失败且已达重试上限：放弃修正，直接结束流程

    注意：统一使用 state.get(...) 而非 state[...] 取值。LangGraph 在并行分支或
    中断恢复场景下可能只带回部分字段，直接下标会在缺键时抛 KeyError，
    把一次可降级的校验失败升级成节点崩溃。缺键按"无错误"处理，与初始状态语义一致。
    """
    if state.get("error") is None:
        return "run_sql"
    if state.get("retry_count", 0) >= MAX_SQL_RETRY:
        return "give_up"
    return "correct_sql"


# SQL 校验通过就直接执行，校验失败则先进入修正节点（受重试上限约束）
graph_builder.add_conditional_edges(
    source="validate_sql",
    path=route_after_validate,
    path_map={
        "run_sql": "run_sql",
        "correct_sql": "correct_sql",
        "give_up": "give_up",
    },
)
graph_builder.add_edge("correct_sql", "validate_sql")
graph_builder.add_edge("run_sql", END)
graph_builder.add_edge("give_up", END)

# 编译后的 graph 是对外使用的 Agent 执行入口
graph = graph_builder.compile()


def export_mermaid() -> str:
    """
    导出当前编排的 Mermaid 状态图文本。

    面试与文档场景下需要一张能反映真实编排的流程图，手工绘制容易与代码漂移；
    这里直接由 LangGraph 编译产物生成，保证图与代码始终一致。
    原先是注释掉的 print，改为显式导出函数，避免模块导入时就产生副作用输出。
    """
    return graph.get_graph().draw_mermaid()


if __name__ == "__main__":

    async def test():
        """本地调试关键词抽取和字段 指标 取值三路召回链路"""

        # 多路召回和上下文补全会访问 Qdrant、Embedding、ES、Meta MySQL 和 DW MySQL
        qdrant_client_manager.init()
        embedding_client_manager.init()
        es_client_manager.init()
        meta_mysql_client_manager.init()
        dw_mysql_client_manager.init()

        # Meta MySQL 用来补齐元数据，DW MySQL 用来读取数据库方言和版本
        async with (
            meta_mysql_client_manager.session_factory() as meta_session,
            dw_mysql_client_manager.session_factory() as dw_session,
        ):
            meta_mysql_repository = MetaMySQLRepository(meta_session)
            dw_mysql_repository = DWMySQLRepository(dw_session)

            # 字段和指标分别使用不同 Qdrant collection，取值检索使用 ES index
            column_qdrant_repository = ColumnQdrantRepository(
                qdrant_client_manager.client
            )
            metric_qdrant_repository = MetricQdrantRepository(
                qdrant_client_manager.client
            )
            value_es_repository = ValueESRepository(es_client_manager.client)

            # 首次问数：query 与 resolved_query 都填原始问题，session_id 用于后续多轮串联
            session_id = "local-debug-session"
            question = "统计华北地区的销售总额"
            state = DataAgentState(
                query=question,
                resolved_query=question,
                session_id=session_id,
                history=[],
            )
            context = DataAgentContext(
                column_qdrant_repository=column_qdrant_repository,
                embedding_client=embedding_client_manager.client,
                metric_qdrant_repository=metric_qdrant_repository,
                value_es_repository=value_es_repository,
                meta_mysql_repository=meta_mysql_repository,
                dw_mysql_repository=dw_mysql_repository,
                session_store=session_store,
            )

            # stream_mode="custom" 会接收各节点通过 runtime.stream_writer 写出的进度信息
            async for chunk in graph.astream(
                input=state, context=context, stream_mode="custom"
            ):
                # 本地调试入口统一走 logger，避免 print 绕过日志格式与 request_id 注入
                logger.info(f"graph chunk: {chunk}")

        # 关闭显式创建的异步客户端，避免本地调试时连接资源悬挂
        await qdrant_client_manager.close()
        await es_client_manager.close()
        await embedding_client_manager.close()
        await meta_mysql_client_manager.close()
        await dw_mysql_client_manager.close()

    asyncio.run(test())
