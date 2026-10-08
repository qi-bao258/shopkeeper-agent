"""
Agent 节点单元测试

对不依赖外部存储的纯逻辑节点做隔离测试，重点覆盖：
- extract_keywords：关键词抽取与原始问题兜底
- add_extra_context：日期上下文的季度/星期计算
- resolve_followup：多轮追问改写（含首轮透传与失败降级）

所有 LLM 与仓储依赖均通过 monkeypatch 替换为轻量桩对象，
测试不产生任何网络请求，可在 CI 中稳定运行。
"""

from datetime import date
from types import SimpleNamespace

import pytest

from app.agent.nodes.extract_keywords import extract_keywords


class FakeWriter:
    """收集节点写出的 SSE 进度事件，便于断言状态机流转"""

    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    @property
    def steps(self):
        return [e["step"] for e in self.events]

    @property
    def final_status(self):
        return self.events[-1]["status"] if self.events else None


@pytest.fixture
def fake_runtime():
    """构造一个最小可用的 Runtime 替身，只暴露节点真正会用到的方法"""

    def _make(context=None, writer=None):
        writer = writer or FakeWriter()
        return SimpleNamespace(stream_writer=writer, context=context or {}), writer

    return _make


def make_state(**overrides) -> dict:
    """
    构造最小可用 State。

    `resolved_query` 与 `query` 默认相同——单轮场景两者本就一致，
    只有多轮追问才会出现差异。这样各节点测试无需反复构造该字段。
    """
    query = overrides.pop("query", "统计华北地区的销售总额")
    state = {
        "query": query,
        "resolved_query": overrides.pop("resolved_query", query),
        "session_id": overrides.pop("session_id", "test-session"),
        "history": overrides.pop("history", []),
    }
    state.update(overrides)
    return state


class TestExtractKeywords:
    """关键词抽取节点"""

    @pytest.mark.asyncio
    async def test_extracts_business_keywords_and_keeps_original_query(
        self, fake_runtime
    ):
        runtime, writer = fake_runtime()
        query = "统计华北地区的销售总额"

        result = await extract_keywords(make_state(query=query), runtime)

        keywords = result["keywords"]
        # 原始问题必须保留，作为关键词切分不准时的兜底检索入口
        assert query in keywords
        # 业务实体应被抽出来（华北 / 销售总额 至少命中一个）
        assert any(k in keywords for k in ("华北", "销售总额", "地区"))
        # 进度事件必须是 running -> success 两个事件
        assert writer.steps == ["抽取关键词", "抽取关键词"]
        assert writer.events[0]["status"] == "running"
        assert writer.final_status == "success"

    @pytest.mark.asyncio
    async def test_uses_resolved_query_not_raw_query(self, fake_runtime):
        """
        多轮场景的关键契约：关键词必须基于改写后的问题抽取。

        「那华东呢」本身不含任何业务实体，若节点误读 state["query"]，
        抽出的关键词会是空的，整条召回链路直接失效。
        """
        runtime, _ = fake_runtime()
        state = make_state(query="那华东呢", resolved_query="统计华东地区的销售总额")

        result = await extract_keywords(state, runtime)

        assert "统计华东地区的销售总额" in result["keywords"]
        assert any(k in result["keywords"] for k in ("华东", "销售总额"))

    @pytest.mark.asyncio
    async def test_empty_query_still_returns_original(self, fake_runtime):
        runtime, _ = fake_runtime()
        result = await extract_keywords(make_state(query=""), runtime)
        assert "" in result["keywords"]

    @pytest.mark.asyncio
    async def test_result_is_deduplicated(self, fake_runtime):
        runtime, _ = fake_runtime()
        result = await extract_keywords(
            make_state(query="销售额 销售额 销售额"), runtime
        )
        assert len(result["keywords"]) == len(set(result["keywords"]))


class TestAddExtraContext:
    """额外上下文补全节点"""

    @pytest.mark.asyncio
    async def test_builds_date_and_db_info(self, fake_runtime, monkeypatch):
        from app.agent.nodes import add_extra_context as module

        class FakeDWRepo:
            async def get_db_info(self):
                return {"dialect": "mysql", "version": "8.0.36"}

        runtime, writer = fake_runtime(context={"dw_mysql_repository": FakeDWRepo()})
        result = await module.add_extra_context(make_state(query="q"), runtime)

        date_info = result["date_info"]
        today = date.today()
        assert date_info["date"] == today.strftime("%Y-%m-%d")
        assert date_info["quarter"] == f"Q{(today.month - 1) // 3 + 1}"
        assert result["db_info"]["dialect"] == "mysql"
        assert writer.final_status == "success"

    @pytest.mark.asyncio
    async def test_raises_when_db_unavailable(self, fake_runtime):
        from app.agent.nodes import add_extra_context as module

        class BrokenRepo:
            async def get_db_info(self):
                raise RuntimeError("connection refused")

        runtime, writer = fake_runtime(context={"dw_mysql_repository": BrokenRepo()})
        with pytest.raises(RuntimeError):
            await module.add_extra_context(make_state(query="q"), runtime)
        # 失败时必须把 error 状态推给前端，否则进度条会一直卡在 running
        assert writer.final_status == "error"


class TestResolveFollowup:
    """追问改写节点"""

    @pytest.mark.asyncio
    async def test_first_turn_passes_through_without_llm(
        self, fake_runtime, monkeypatch
    ):
        """首轮没有历史可指代，必须原样透传且不触发模型调用"""
        from app.agent.nodes import resolve_followup as module

        def _fail_if_called():
            raise AssertionError("首轮不应调用 LLM")

        monkeypatch.setattr(module, "build_rewrite_chain", _fail_if_called)

        runtime, writer = fake_runtime()
        result = await module.resolve_followup(
            make_state(query="统计华北地区的销售总额", history=[]), runtime
        )

        assert result["resolved_query"] == "统计华北地区的销售总额"
        assert writer.final_status == "success"

    @pytest.mark.asyncio
    async def test_rewrites_followup_with_history(self, fake_runtime, monkeypatch):
        """带历史的追问应被改写为独立问题，并把历史带入提示词"""
        from app.agent.nodes import resolve_followup as module

        captured = {}

        class FakeChain:
            async def ainvoke(self, payload):
                captured.update(payload)
                return "统计华东地区的销售总额"

        monkeypatch.setattr(module, "build_rewrite_chain", lambda: FakeChain())

        history = [
            {
                "question": "统计华北地区的销售总额",
                "resolved_question": "统计华北地区的销售总额",
                "sql": "SELECT ...",
            }
        ]
        runtime, writer = fake_runtime()
        result = await module.resolve_followup(
            make_state(query="那华东呢", history=history), runtime
        )

        assert result["resolved_query"] == "统计华东地区的销售总额"
        # 提示词必须同时拿到历史与当前追问
        assert "统计华北地区的销售总额" in captured["history"]
        assert captured["query"] == "那华东呢"
        assert writer.final_status == "success"

    @pytest.mark.asyncio
    async def test_idempotent_when_question_already_standalone(
        self, fake_runtime, monkeypatch
    ):
        """独立问题经改写后若与原问题一致，不应发生任何变化"""
        from app.agent.nodes import resolve_followup as module

        class FakeChain:
            async def ainvoke(self, payload):
                return "统计华东地区的销售总额"

        monkeypatch.setattr(module, "build_rewrite_chain", lambda: FakeChain())

        history = [{"question": "统计华北地区的销售总额"}]
        runtime, _ = fake_runtime()
        result = await module.resolve_followup(
            make_state(
                query="统计华东地区的销售总额",
                resolved_query="统计华东地区的销售总额",
                history=history,
            ),
            runtime,
        )

        assert result["resolved_query"] == "统计华东地区的销售总额"

    @pytest.mark.asyncio
    async def test_falls_back_to_raw_query_on_error(self, fake_runtime, monkeypatch):
        """LLM 异常时降级为原始问题，多轮记忆不能成为主链路单点故障"""
        from app.agent.nodes import resolve_followup as module

        def _boom():
            raise RuntimeError("LLM 服务不可用")

        monkeypatch.setattr(module, "build_rewrite_chain", _boom)

        history = [{"question": "统计华北地区的销售总额"}]
        runtime, writer = fake_runtime()
        result = await module.resolve_followup(
            make_state(query="那华东呢", history=history), runtime
        )

        assert result["resolved_query"] == "那华东呢"
        # 降级路径也应返回 success，避免前端进度条显示为红色失败
        assert writer.final_status == "success"

    @pytest.mark.asyncio
    async def test_empty_llm_output_falls_back(self, fake_runtime, monkeypatch):
        """模型返回空串时必须回退，否则空问题会静默传到下游"""
        from app.agent.nodes import resolve_followup as module

        class FakeChain:
            async def ainvoke(self, payload):
                return "   "

        monkeypatch.setattr(module, "build_rewrite_chain", lambda: FakeChain())

        history = [{"question": "统计华北地区的销售总额"}]
        runtime, _ = fake_runtime()
        result = await module.resolve_followup(
            make_state(query="那华东呢", history=history), runtime
        )

        assert result["resolved_query"] == "那华东呢"

    def test_format_history_empty(self):
        """无历史时应返回「无」，而不是空字符串导致提示词结构缺块"""
        from app.agent.nodes.resolve_followup import _format_history

        assert _format_history([]) == "无"

    def test_format_history_prefers_resolved(self):
        """格式化历史时优先使用改写后的问题，保证上下文本身完整可读"""
        from app.agent.nodes.resolve_followup import _format_history

        text = _format_history(
            [
                {"question": "那华东呢", "resolved_question": "统计华东地区的销售总额"},
                {"question": "统计华北地区的销售总额"},
            ]
        )
        assert "统计华东地区的销售总额" in text
        assert "统计华北地区的销售总额" in text
        assert "那华东呢" not in text
