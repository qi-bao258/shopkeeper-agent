"""
LangGraph 编排结构测试

不真正执行图，而是断言图的结构契约：
- 14 个节点全部注册
- 入口是追问改写节点，保证多轮追问先被补全
- 三路召回确实是从 extract_keywords 并行扇出
- 三路召回都汇聚到 merge_retrieved_info
- SQL 校验失败能走到 correct_sql 分支，且失败有界

这类"结构快照"测试能在重构图编排时第一时间发现连边被误删。
"""

import pytest

from app.agent.graph import MAX_SQL_RETRY, graph, route_after_validate

EXPECTED_NODES = {
    "resolve_followup",
    "extract_keywords",
    "recall_column",
    "recall_value",
    "recall_metric",
    "merge_retrieved_info",
    "filter_metric",
    "filter_table",
    "add_extra_context",
    "generate_sql",
    "validate_sql",
    "correct_sql",
    "run_sql",
    "give_up",
}


@pytest.fixture(scope="module")
def graph_repr():
    """把编译后的图转成 mermaid 文本，后续用字符串断言连边关系"""
    return graph.get_graph().draw_mermaid()


def test_all_nodes_registered(graph_repr):
    """所有节点都必须出现在图定义中"""
    for node in EXPECTED_NODES:
        assert node in graph_repr, f"节点缺失：{node}"


def test_entry_is_followup_resolution(graph_repr):
    """
    图入口必须是追问改写节点。

    这是多轮对话的关键契约：若入口直接是 extract_keywords，
    「那华东呢」会被原样送去抽词，整条链路静默失效。
    """
    assert "__start__ --> resolve_followup" in graph_repr
    assert "resolve_followup --> extract_keywords" in graph_repr
    # 不能有从 START 直连关键词抽取的捷径
    assert "__start__ --> extract_keywords" not in graph_repr


def test_three_way_parallel_recall(graph_repr):
    """关键词抽取后应并行扇出到字段/取值/指标三路召回"""
    assert "extract_keywords --> recall_column" in graph_repr
    assert "extract_keywords --> recall_value" in graph_repr
    assert "extract_keywords --> recall_metric" in graph_repr


def test_recall_branches_converge(graph_repr):
    """三路召回必须全部汇聚到合并节点，否则会有分支悬挂"""
    assert "recall_column --> merge_retrieved_info" in graph_repr
    assert "recall_value --> merge_retrieved_info" in graph_repr
    assert "recall_metric --> merge_retrieved_info" in graph_repr


def test_sql_loop_edges(graph_repr):
    """
    生成 -> 校验 -> 修正 -> 校验 的修正闭环存在。

    注意：validate_sql 的出边是条件边，mermaid 会渲染成虚线 `-.->`，
    与普通实线边语法不同，断言时必须区分。
    """
    assert "generate_sql --> validate_sql" in graph_repr
    # 条件边：校验结果决定进入执行 / 修正 / 放弃
    assert "validate_sql -.-> correct_sql" in graph_repr
    assert "validate_sql -.-> run_sql" in graph_repr
    assert "validate_sql -.-> give_up" in graph_repr
    # 修正后回到校验，形成有界重试闭环，而不是改完直接执行
    assert "correct_sql --> validate_sql" in graph_repr


def test_give_up_terminates(graph_repr):
    """放弃分支必须连到 END，否则重试耗尽后流程会悬挂"""
    assert "give_up --> __end__" in graph_repr
    assert "give_up -->" not in graph_repr.replace("give_up --> __end__", "")


def test_routing_decision_on_error_state():
    """条件边判定：校验通过走执行，失败未达上限走修正，达上限则放弃"""
    assert route_after_validate({"error": None}) == "run_sql"
    assert route_after_validate({"error": "Unknown column 'x'", "retry_count": 0}) == (
        "correct_sql"
    )


def test_routing_gives_up_after_retry_limit():
    """重试达上限后必须放弃修正，否则模型会无限重试消耗 token"""
    state = {"error": "Unknown column 'x'", "retry_count": MAX_SQL_RETRY}
    assert route_after_validate(state) == "give_up"

    # 超过上限同样放弃
    state = {"error": "Unknown column 'x'", "retry_count": MAX_SQL_RETRY + 3}
    assert route_after_validate(state) == "give_up"


def test_routing_tolerates_missing_retry_count():
    """retry_count 缺省时按 0 处理，不应抛 KeyError"""
    assert route_after_validate({"error": "boom"}) == "correct_sql"
