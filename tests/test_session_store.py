"""
会话记忆存储测试

覆盖多轮记忆存储的三类关键行为：
1. 读写正确性：追加的轮次能按顺序读回，且只返回最近 N 轮
2. 容量约束：单会话轮数上限、会话数上限（LRU 淘汰）、TTL 过期
3. 并发安全：同一会话的并发追加不丢轮次

内存存储的两个致命风险是「无界增长」与「并发覆盖历史」，
因此这两块是测试重点，也是面试时值得展开的工程细节。
"""

import asyncio
import time

import pytest

from app.services.session_store import InMemorySessionStore, Turn, new_session_id


def make_turn(question: str, **overrides) -> Turn:
    """构造一轮记录，默认 resolved_question 与 question 相同"""
    payload = {
        "question": question,
        "resolved_question": overrides.pop("resolved_question", question),
    }
    payload.update(overrides)
    return Turn(**payload)


@pytest.fixture
def store() -> InMemorySessionStore:
    return InMemorySessionStore(max_sessions=3, max_turns_per_session=3, ttl_seconds=60)


class TestBasicReadWrite:
    """基础读写"""

    @pytest.mark.asyncio
    async def test_append_and_read_back(self, store):
        await store.append_turn("s1", make_turn("统计华北地区的销售总额"))

        history = await store.get_history("s1")
        assert len(history) == 1
        assert history[0].question == "统计华北地区的销售总额"

    @pytest.mark.asyncio
    async def test_read_unknown_session_returns_empty(self, store):
        """未知会话应返回空列表而不是抛异常"""
        assert await store.get_history("not-exist") == []

    @pytest.mark.asyncio
    async def test_empty_session_id_is_noop(self, store):
        """空 session_id 视为无会话，读写都不应报错"""
        await store.append_turn("", make_turn("q"))
        assert await store.get_history("") == []

    @pytest.mark.asyncio
    async def test_limit_returns_most_recent(self, store):
        """limit 必须返回最近的 N 轮，顺序为时间正序"""
        store.max_turns_per_session = 10  # 放宽长度限制以单独验证 limit 语义
        for i in range(5):
            await store.append_turn("s1", make_turn(f"q{i}"))

        history = await store.get_history("s1", limit=2)
        assert [t.question for t in history] == ["q3", "q4"]

    @pytest.mark.asyncio
    async def test_limit_zero_returns_empty(self, store):
        await store.append_turn("s1", make_turn("q"))
        assert await store.get_history("s1", limit=0) == []

    @pytest.mark.asyncio
    async def test_sessions_are_isolated(self, store):
        """不同会话的历史必须完全隔离"""
        await store.append_turn("s1", make_turn("华北销售额"))
        await store.append_turn("s2", make_turn("华东销售额"))

        assert [t.question for t in await store.get_history("s1")] == ["华北销售额"]
        assert [t.question for t in await store.get_history("s2")] == ["华东销售额"]

    @pytest.mark.asyncio
    async def test_turn_fields_persisted(self, store):
        """SQL 与行数等字段必须完整落库，供下一轮追问引用"""
        await store.append_turn(
            "s1",
            make_turn(
                "那华东呢",
                resolved_question="统计华东地区的销售总额",
                sql="SELECT 1",
                row_count=42,
            ),
        )

        turn = (await store.get_history("s1"))[0]
        assert turn.question == "那华东呢"
        assert turn.resolved_question == "统计华东地区的销售总额"
        assert turn.sql == "SELECT 1"
        assert turn.row_count == 42


class TestCapacityLimits:
    """容量约束——防止内存无界增长"""

    @pytest.mark.asyncio
    async def test_turns_truncated_to_max(self, store):
        """单会话轮数超限时保留最近 N 轮，丢弃最旧的"""
        for i in range(6):
            await store.append_turn("s1", make_turn(f"q{i}"))

        history = await store.get_history("s1", limit=100)
        assert len(history) == store.max_turns_per_session
        # 保留的是最近的 3 轮
        assert [t.question for t in history] == ["q3", "q4", "q5"]

    @pytest.mark.asyncio
    async def test_sessions_evicted_lru(self, store):
        """会话数超限时淘汰最久未更新的会话"""
        await store.append_turn("s1", make_turn("a"))
        await store.append_turn("s2", make_turn("b"))
        await store.append_turn("s3", make_turn("c"))
        # 触碰 s1，使其成为最近使用；此后再加一个会话应淘汰 s2
        await store.append_turn("s1", make_turn("a2"))
        await store.append_turn("s4", make_turn("d"))

        sessions = await store.list_sessions()
        assert len(sessions) == store.max_sessions
        assert "s1" in sessions
        assert "s4" in sessions
        assert "s2" not in sessions

    @pytest.mark.asyncio
    async def test_expired_session_evicted_lazily(self):
        """空闲超过 TTL 的会话应在下次访问时被惰性清理"""
        store = InMemorySessionStore(max_sessions=10, ttl_seconds=0)
        await store.append_turn("s1", make_turn("q"))
        # ttl_seconds=0 使会话立即过期
        time.sleep(0.01)

        assert await store.get_history("s1") == []
        assert await store.list_sessions() == []

    @pytest.mark.asyncio
    async def test_stats_reports_usage(self, store):
        """stats 必须真实反映会话数与轮数，供运维观测内存水位"""
        await store.append_turn("s1", make_turn("q1"))
        await store.append_turn("s1", make_turn("q2"))
        await store.append_turn("s2", make_turn("q3"))

        stats = await store.stats()
        assert stats["session_count"] == 2
        assert stats["turn_count"] == 3
        assert stats["max_sessions"] == store.max_sessions
        assert stats["max_turns_per_session"] == store.max_turns_per_session


class TestClear:
    """会话清理"""

    @pytest.mark.asyncio
    async def test_clear_existing_returns_true(self, store):
        await store.append_turn("s1", make_turn("q"))
        assert await store.clear("s1") is True
        assert await store.get_history("s1") == []

    @pytest.mark.asyncio
    async def test_clear_missing_returns_false(self, store):
        assert await store.clear("not-exist") is False


class TestConcurrency:
    """并发安全"""

    @pytest.mark.asyncio
    async def test_concurrent_appends_do_not_lose_turns(self):
        """
        同一会话的并发追加不能丢轮次。

        若不加锁，`turns.append` 与长度截断之间存在竞态，
        并发的多轮问数会互相覆盖历史。这里用放宽的长度上限来验证。
        """
        store = InMemorySessionStore(
            max_sessions=10, max_turns_per_session=100, ttl_seconds=60
        )

        await asyncio.gather(
            *(store.append_turn("s1", make_turn(f"q{i}")) for i in range(50))
        )

        history = await store.get_history("s1", limit=100)
        assert len(history) == 50
        assert len({t.question for t in history}) == 50


class TestSessionId:
    """会话 id 生成"""

    def test_new_session_id_is_unique(self):
        ids = {new_session_id() for _ in range(100)}
        assert len(ids) == 100

    def test_new_session_id_is_hex_string(self):
        sid = new_session_id()
        assert isinstance(sid, str)
        assert len(sid) == 32
        int(sid, 16)  # 能按十六进制解析即说明格式正确
