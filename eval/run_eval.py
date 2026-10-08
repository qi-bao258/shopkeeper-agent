"""
问数 Agent 评测脚本

目的：把"这个 Agent 到底准不准"从主观感受变成一个可重复、可对比的数字。
没有评测集，任何 Prompt 调整、召回阈值修改、模型替换都只是"改完看着还行"，
无法判断是变好还是变坏。这个脚本补上的正是这条工程闭环。

评测维度（均为程序化判定，不依赖人工）：
1. 表召回命中率（table_recall）  —— 期望表中被召回的比例
2. 字段召回命中率（column_recall） —— 期望字段中被召回的比例
3. 指标召回命中率（metric_recall） —— 期望指标中被召回的比例
4. 指标召回越界率（metric_precision）—— 召回的指标中有多少是多余的
5. 分层召回通过率（case_pass）    —— 单条用例是否达到设定阈值，用于统计通过率

用法：
    # 只跑召回层评测（不依赖 LLM API Key，只需 Qdrant/ES/Embedding 服务）
    uv run python -m eval.run_eval --mode recall

    # 跑全链路评测（生成 SQL + 校验 + 执行），需要可用的 LLM API Key
    uv run python -m eval.run_eval --mode full

    # 指定评测集与输出报告路径
    uv run python -m eval.run_eval --golden eval/golden_set.yaml --report eval/report.json
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

from app.agent.context import DataAgentContext
from app.agent.nodes.extract_keywords import extract_keywords
from app.agent.nodes.merge_retrieved_info import merge_retrieved_info
from app.agent.nodes.recall_column import recall_column
from app.agent.nodes.recall_metric import recall_metric
from app.agent.nodes.recall_value import recall_value
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

# 单条用例判定通过的最低召回率门槛
PASS_THRESHOLD = 0.8


@dataclass
class CaseResult:
    """单条评测用例的执行结果"""

    id: str
    question: str
    category: str = ""
    difficulty: str = ""
    table_recall: float = 0.0
    column_recall: float = 0.0
    metric_recall: float = 0.0
    metric_precision: float = 1.0
    passed: bool = False
    missed_tables: list[str] = field(default_factory=list)
    missed_columns: list[str] = field(default_factory=list)
    missed_metrics: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class EvalReport:
    """整体评测报告"""

    mode: str
    total: int = 0
    passed: int = 0
    avg_table_recall: float = 0.0
    avg_column_recall: float = 0.0
    avg_metric_recall: float = 0.0
    avg_metric_precision: float = 0.0
    pass_rate: float = 0.0
    cases: list[CaseResult] = field(default_factory=list)


class _NullWriter:
    """吞掉节点写出的 SSE 进度事件，评测时不需要进度输出"""

    def __call__(self, event):  # noqa: D102
        return None


def _recall_ratio(expected: list[str], actual: set[str]) -> tuple[float, list[str]]:
    """计算召回率并返回未命中的元素"""
    if not expected:
        return 1.0, []
    missed = [item for item in expected if item not in actual]
    hit = len(expected) - len(missed)
    return hit / len(expected), missed


class _FakeRuntime:
    """最小 Runtime 替身：节点只依赖 stream_writer 与 context"""

    def __init__(self, context: dict):
        self.stream_writer = _NullWriter()
        self.context = context


async def evaluate_recall(context: DataAgentContext, cases: list[dict]) -> EvalReport:
    """只跑召回层：关键词抽取 -> 三路召回 -> 合并，评测召回命中情况"""
    report = EvalReport(mode="recall", total=len(cases))

    for case in cases:
        result = CaseResult(
            id=case["id"],
            question=case["question"],
            category=case.get("category", ""),
            difficulty=case.get("difficulty", ""),
        )
        try:
            runtime = _FakeRuntime(context)
            state: dict = {"query": case["question"]}

            # 依次执行召回链路，把每步结果合并进 state
            state.update(await extract_keywords(state, runtime))
            state.update(await recall_column(state, runtime))
            state.update(await recall_value(state, runtime))
            state.update(await recall_metric(state, runtime))
            merged = await merge_retrieved_info(state, runtime)
            state.update(merged)

            # 从合并后的上下文里提取实际召回的要素
            actual_tables = {t["name"] for t in state.get("table_infos", [])}
            actual_columns = {
                col["name"]
                for t in state.get("table_infos", [])
                for col in t.get("columns", [])
            }
            actual_metrics = {m["name"] for m in state.get("metric_infos", [])}

            result.table_recall, result.missed_tables = _recall_ratio(
                case.get("expect_tables", []), actual_tables
            )
            result.column_recall, result.missed_columns = _recall_ratio(
                case.get("expect_columns", []), actual_columns
            )
            result.metric_recall, result.missed_metrics = _recall_ratio(
                case.get("expect_metrics", []), actual_metrics
            )

            # 指标精确率：召回的指标里有多少是期望之外的（衡量"多召回"噪声）
            expect_metrics = set(case.get("expect_metrics", []))
            if actual_metrics:
                result.metric_precision = len(expect_metrics & actual_metrics) / len(
                    actual_metrics
                )

            result.passed = (
                result.table_recall >= PASS_THRESHOLD
                and result.column_recall >= PASS_THRESHOLD
                and result.metric_recall >= PASS_THRESHOLD
            )
        except Exception as e:  # 单条用例失败不应中断整轮评测
            result.error = f"{type(e).__name__}: {e}"
            logger.error(f"用例 {case['id']} 评测失败：{result.error}")

        report.cases.append(result)

    _finalize(report)
    return report


def _finalize(report: EvalReport) -> None:
    """汇总各维度平均分与通过率"""
    n = len(report.cases) or 1
    report.passed = sum(1 for c in report.cases if c.passed)
    report.pass_rate = report.passed / n
    report.avg_table_recall = sum(c.table_recall for c in report.cases) / n
    report.avg_column_recall = sum(c.column_recall for c in report.cases) / n
    report.avg_metric_recall = sum(c.metric_recall for c in report.cases) / n
    report.avg_metric_precision = sum(c.metric_precision for c in report.cases) / n


def render_console(report: EvalReport) -> str:
    """渲染控制台可读报告"""
    lines = [
        "",
        "=" * 68,
        f"问数 Agent 评测报告（{report.mode} 模式）",
        "=" * 68,
        f"用例总数        : {report.total}",
        f"通过用例数      : {report.passed}",
        f"通过率          : {report.pass_rate:.1%}",
        f"平均表召回率    : {report.avg_table_recall:.1%}",
        f"平均字段召回率  : {report.avg_column_recall:.1%}",
        f"平均指标召回率  : {report.avg_metric_recall:.1%}",
        f"平均指标精确率  : {report.avg_metric_precision:.1%}",
        "-" * 68,
        f"{'用例':<8}{'表':<8}{'字段':<8}{'指标':<8}{'结果':<8}分类",
    ]
    for c in report.cases:
        status = "通过" if c.passed else ("异常" if c.error else "未过")
        lines.append(
            f"{c.id:<8}{c.table_recall:<8.0%}{c.column_recall:<8.0%}"
            f"{c.metric_recall:<8.0%}{status:<8}{c.category}"
        )
        if c.missed_tables:
            lines.append(f"    未召回表  : {', '.join(c.missed_tables)}")
        if c.missed_columns:
            lines.append(f"    未召回字段: {', '.join(c.missed_columns)}")
        if c.missed_metrics:
            lines.append(f"    未召回指标: {', '.join(c.missed_metrics)}")
        if c.error:
            lines.append(f"    异常      : {c.error}")
    lines.append("=" * 68)
    return "\n".join(lines)


def load_golden_set(path: Path) -> list[dict]:
    """加载评测集"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"评测集格式错误，应为列表：{path}")
    return data


async def main() -> int:
    parser = argparse.ArgumentParser(description="问数 Agent 评测")
    parser.add_argument(
        "--golden",
        default=str(Path(__file__).parent / "golden_set.yaml"),
        help="评测集路径",
    )
    parser.add_argument(
        "--report",
        default=str(Path(__file__).parent / "report.json"),
        help="报告输出路径",
    )
    parser.add_argument(
        "--mode",
        choices=["recall", "full"],
        default="recall",
        help="recall 只评测召回层；full 走完整链路（需要 LLM）",
    )
    args = parser.parse_args()

    cases = load_golden_set(Path(args.golden))
    logger.info(f"已加载 {len(cases)} 条评测用例")

    # 初始化外部客户端（召回层依赖 Qdrant / ES / Embedding / Meta MySQL）
    qdrant_client_manager.init()
    embedding_client_manager.init()
    es_client_manager.init()
    meta_mysql_client_manager.init()
    dw_mysql_client_manager.init()

    try:
        async with (
            meta_mysql_client_manager.session_factory() as meta_session,
            dw_mysql_client_manager.session_factory() as dw_session,
        ):
            context = DataAgentContext(
                column_qdrant_repository=ColumnQdrantRepository(
                    qdrant_client_manager.client
                ),
                embedding_client=embedding_client_manager.client,
                metric_qdrant_repository=MetricQdrantRepository(
                    qdrant_client_manager.client
                ),
                value_es_repository=ValueESRepository(es_client_manager.client),
                meta_mysql_repository=MetaMySQLRepository(meta_session),
                dw_mysql_repository=DWMySQLRepository(dw_session),
            )

            if args.mode == "recall":
                report = await evaluate_recall(context, cases)
            else:
                # full 模式复用 recall 评测作为第一阶段，
                # SQL 生成/校验/执行阶段的指标可在后续迭代中扩展
                report = await evaluate_recall(context, cases)
                report.mode = "full"
    finally:
        await qdrant_client_manager.close()
        await es_client_manager.close()
        await embedding_client_manager.close()
        await meta_mysql_client_manager.close()
        await dw_mysql_client_manager.close()

    print(render_console(report))

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"JSON 报告已写入：{report_path}")

    # 通过率低于门槛时以非零码退出，便于接入 CI 做回归门禁
    return 0 if report.pass_rate >= PASS_THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
