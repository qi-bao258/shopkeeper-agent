"""
追问改写节点

多轮问数的第一个节点，负责把依赖上下文的「追问」还原成独立完整的问题。

为什么必须在检索之前做这一步：
后续的三路召回（字段 / 取值 / 指标）与 SQL 生成全部只依赖用户问题文本。
如果直接把「那华东呢？」送进召回，关键词抽取拿不到任何有效业务实体，
整条链路会直接失效。因此在图的最前端插入本节点，把改写后的问题写入
`state["resolved_query"]`，下游节点统一改读这个字段。

两个关键的降级设计：
1. **无历史时不调用 LLM**：首轮提问不存在指代问题，直接透传，
   省掉一次模型调用（既省钱又降延迟）。
2. **改写失败时回退原始问题**：LLM 调用异常或返回空值时回退，
   保证「多轮记忆」这个增强能力不会成为整条链路的单点故障。
"""

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langgraph.runtime import Runtime

from app.agent.context import DataAgentContext
from app.agent.state import DataAgentState
from app.core.log import logger
from app.prompt.prompt_loader import load_prompt


def _format_history(history: list[dict]) -> str:
    """
    把历史轮次格式化成提示词可读的对话文本。

    只取最近若干轮，且只保留问题本身——SQL 对「问题改写」这个任务没有帮助，
    把它们塞进提示词反而会分散模型注意力并增加 token 消耗。
    """
    if not history:
        return "无"

    lines = []
    for index, turn in enumerate(history, start=1):
        # 优先用改写后的问题，保证传给模型的上下文本身就是完整可读的
        question = turn.get("resolved_question") or turn.get("question", "")
        lines.append(f"第 {index} 轮：{question}")
    return "\n".join(lines)


def build_rewrite_chain():
    """
    构造追问改写链（提示词 -> 模型 -> 纯文本解析）。

    把「构造链」单独抽成函数而不是写死在节点里，是为了给测试留一个替换点：
    单元测试可以直接 monkeypatch 本函数返回桩对象，
    从而在不配置 API Key、不发网络请求的前提下验证节点行为。
    """
    from app.agent.llm import llm

    prompt = PromptTemplate(
        template=load_prompt("resolve_followup"),
        input_variables=["history", "query"],
    )
    return prompt | llm | StrOutputParser()


async def resolve_followup(state: DataAgentState, runtime: Runtime[DataAgentContext]):
    """把多轮追问改写为独立完整的问题"""

    writer = runtime.stream_writer
    step = "理解追问"
    writer({"type": "progress", "step": step, "status": "running"})

    query = state["query"]
    history = state.get("history") or []

    # 首轮提问没有可指的上下文，直接透传，避免无意义的模型调用
    if not history:
        logger.info(f"首轮提问，无需改写：{query}")
        writer({"type": "progress", "step": step, "status": "success"})
        return {"resolved_query": query}

    try:
        chain = build_rewrite_chain()
        resolved = await chain.ainvoke(
            {"history": _format_history(history), "query": query}
        )
        resolved = (resolved or "").strip()

        if not resolved:
            # 模型返回空值时不能把空问题传给下游，否则整条链路会静默失效
            logger.warning("追问改写返回空值，回退为原始问题")
            resolved = query

        if resolved != query:
            logger.info(f"追问改写：{query} -> {resolved}")
        else:
            logger.info(f"提问已独立，无需改写：{query}")

        writer({"type": "progress", "step": step, "status": "success"})
        return {"resolved_query": resolved}

    except Exception as e:
        # 多轮记忆是增强能力，不应成为主链路单点故障：
        # 改写失败时降级为原始问题，让后续流程照常执行
        logger.error(f"追问改写失败，降级为原始问题：{e}")
        writer({"type": "progress", "step": step, "status": "success"})
        return {"resolved_query": query}
