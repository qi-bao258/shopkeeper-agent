"""
会话记忆存储

为多轮问数提供对话历史。

设计取舍说明：
本项目是单机可运行的教学工程，因此这里选择**进程内内存存储**而不是
Redis / MySQL。原因有三点，也是面试时值得说明的工程判断：

1. 问数场景的会话历史是短生命周期的（分钟级），不需要持久化到磁盘；
2. 引入外部依赖会抬高「跑起来」的门槛，与本项目的定位冲突；
3. 存储被抽象成 SessionStore 接口，未来替换成 Redis 实现时，
   Service 层与节点完全不需要改动——这是依赖倒置带来的收益。

两个必须处理的工程问题：
- **无界增长**：会话数量与会话长度都必须有上限，否则进程内存会被打爆。
  这里用「会话容量上限 + 每会话轮数上限 + TTL 过期」三重约束。
- **并发安全**：FastAPI 是异步并发模型，同一会话的读写需要加锁，
  否则并发的两轮问数会互相覆盖历史。
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from app.core.log import logger


@dataclass
class Turn:
    """一轮完整的问答记录"""

    question: str
    """用户原始提问（未经改写）"""

    resolved_question: str
    """追问改写后的独立问题，首轮与 question 相同"""

    sql: str | None = None
    """本轮生成的 SQL，失败时为 None"""

    row_count: int = 0
    """结果行数，用于给后续轮次提供「上一轮查了多少行」的线索"""

    created_at: float = field(default_factory=time.time)


@dataclass
class Session:
    """一个会话的完整历史"""

    session_id: str
    turns: list[Turn] = field(default_factory=list)
    updated_at: float = field(default_factory=time.time)

    def recent(self, limit: int) -> list[Turn]:
        """取最近 limit 轮记录，供追问改写时构建上下文"""
        if limit <= 0:
            return []
        return self.turns[-limit:]


class SessionStore(Protocol):
    """
    会话存储接口。

    定义成 Protocol 而不是基类，是为了让 Redis 等实现无需继承本模块，
    只要结构上满足即可（结构化子类型）。
    """

    async def get_history(self, session_id: str, limit: int = 5) -> list[Turn]:
        """读取指定会话的最近 N 轮历史"""
        ...

    async def append_turn(self, session_id: str, turn: Turn) -> None:
        """向指定会话追加一轮记录"""
        ...

    async def clear(self, session_id: str) -> bool:
        """清空指定会话，返回是否存在过"""
        ...

    async def list_sessions(self) -> list[str]:
        """列出当前所有活跃会话 id"""
        ...


class InMemorySessionStore:
    """
    基于进程内存的会话存储实现。

    三重容量约束：
    - max_sessions: 最多保留的会话数，超出时淘汰最久未更新的会话（LRU）
    - max_turns_per_session: 单个会话最多保留的轮数，超出时丢弃最旧的轮次
    - ttl_seconds: 会话空闲超时时间，读取时惰性清理

    惰性清理策略：不启动后台任务扫描，而是在每次访问时顺带清理过期会话。
    对于教学项目这个取舍是合理的——避免引入定时任务的生命周期管理复杂度。
    """

    def __init__(
        self,
        max_sessions: int = 500,
        max_turns_per_session: int = 10,
        ttl_seconds: int = 3600,
    ):
        self.max_sessions = max_sessions
        self.max_turns_per_session = max_turns_per_session
        self.ttl_seconds = ttl_seconds

        self._sessions: dict[str, Session] = {}
        # 同一会话的并发读写需要串行化，避免历史互相覆盖
        self._lock = asyncio.Lock()

    def _is_expired(self, session: Session, now: float) -> bool:
        """判断会话是否已超过空闲 TTL"""
        return (now - session.updated_at) > self.ttl_seconds

    def _evict_expired(self, now: float) -> None:
        """惰性清理所有过期会话"""
        expired = [
            sid
            for sid, session in self._sessions.items()
            if self._is_expired(session, now)
        ]
        for sid in expired:
            del self._sessions[sid]
        if expired:
            logger.info(f"已清理 {len(expired)} 个过期会话")

    def _evict_lru(self) -> None:
        """会话数超限时，淘汰最久未更新的会话"""
        if len(self._sessions) <= self.max_sessions:
            return
        # 按 updated_at 升序排列，删除最旧的若干个
        overflow = len(self._sessions) - self.max_sessions
        oldest = sorted(self._sessions.items(), key=lambda kv: kv[1].updated_at)
        for sid, _ in oldest[:overflow]:
            del self._sessions[sid]
        logger.info(f"会话数超限，已淘汰 {overflow} 个最久未使用的会话")

    async def get_history(self, session_id: str, limit: int = 5) -> list[Turn]:
        """读取最近 N 轮历史（不含当前这一轮）"""
        if not session_id:
            return []
        async with self._lock:
            now = time.time()
            self._evict_expired(now)
            session = self._sessions.get(session_id)
            if session is None:
                return []
            return session.recent(limit)

    async def append_turn(self, session_id: str, turn: Turn) -> None:
        """追加一轮记录，并施加长度与容量约束"""
        if not session_id:
            return
        async with self._lock:
            now = time.time()
            self._evict_expired(now)

            session = self._sessions.get(session_id)
            if session is None:
                session = Session(session_id=session_id)
                self._sessions[session_id] = session

            session.turns.append(turn)
            # 单会话轮数超限时保留最近的 N 轮
            if len(session.turns) > self.max_turns_per_session:
                session.turns = session.turns[-self.max_turns_per_session :]
            session.updated_at = now

            self._evict_lru()

    async def clear(self, session_id: str) -> bool:
        """清空指定会话"""
        async with self._lock:
            return self._sessions.pop(session_id, None) is not None

    async def list_sessions(self) -> list[str]:
        """列出当前所有未过期会话"""
        async with self._lock:
            self._evict_expired(time.time())
            return list(self._sessions.keys())

    async def stats(self) -> dict:
        """返回存储运行状态，供运维观测内存占用是否健康"""
        async with self._lock:
            self._evict_expired(time.time())
            return {
                # 命名上刻意与 list_sessions() 的返回值区分开，
                # 避免与接口响应中的 sessions 列表字段冲突
                "session_count": len(self._sessions),
                "turn_count": sum(len(s.turns) for s in self._sessions.values()),
                "max_sessions": self.max_sessions,
                "max_turns_per_session": self.max_turns_per_session,
                "ttl_seconds": self.ttl_seconds,
            }


def new_session_id() -> str:
    """生成新的会话 id"""
    return uuid.uuid4().hex


# 模块级单例：应用生命周期内共享同一份会话记忆
session_store = InMemorySessionStore()
