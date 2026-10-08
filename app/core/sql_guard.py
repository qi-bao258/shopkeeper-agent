"""
SQL 安全护栏

在生成的 SQL 真正抵达数仓之前，做一道与 LLM 无关的程序化防线。
大模型输出的 SQL 本质上不可信：它可能生成写操作、可能漏掉 LIMIT 导致全表拉取、
也可能因为上下文注入而带上危险语句。仅靠 Prompt 约束不足以作为安全边界。

本模块只做三件事，且全部是确定性规则，便于单元测试与审计：
1. 语句类型白名单：只放行 SELECT / WITH，拦截 DDL / DML / 授权类语句
2. 强制行数上限：无 LIMIT 时自动补齐，超界的 LIMIT 收紧到上限
3. 长度上限：拒绝异常巨大的语句，避免把压力透传给数据库

设计原则是"默认拒绝"：任何无法明确判断为安全查询的语句都会被拦下。
"""

import re
from dataclasses import dataclass
from typing import Final

from app.core.log import logger

# 允许的查询起始关键字：普通查询与 CTE
_ALLOWED_PREFIXES: Final[tuple[str, ...]] = ("select", "with")

# 合法标识符：字母或下划线开头，后接字母/数字/下划线，最长 64 字符。
# 表名与字段名在拼进 SQL 前必须先过这道校验，避免配置或模型输出被当作
# SQL 片段直接执行（例如表名为 `t; drop table x --`）。
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
)

# 明确禁止的语句类型与关键字，命中任意一项即拒绝
_FORBIDDEN_KEYWORDS: Final[tuple[str, ...]] = (
    "insert",
    "update",
    "delete",
    "drop",
    "truncate",
    "alter",
    "create",
    "replace",
    "grant",
    "revoke",
    "rename",
    "load_file",
    "outfile",
    "dumpfile",
    "for update",
    "lock in share mode",
)

# 默认行数上限，可通过 guard_sql 参数覆盖
_DEFAULT_MAX_ROWS: Final[int] = 1000
# 默认语句长度上限（字符数）
_DEFAULT_MAX_LENGTH: Final[int] = 20_000


class SQLGuardError(ValueError):
    """SQL 未通过安全校验时抛出"""


@dataclass(frozen=True)
class GuardedSQL:
    """护栏处理结果"""

    sql: str
    """可直接交给数据库执行的 SQL（已补齐 LIMIT）"""

    limit_injected: bool
    """是否对 LIMIT 做过改写，用于日志与审计"""


def _normalize(sql: str) -> str:
    """去掉首尾空白与结尾分号，统一成单条语句形态"""
    return sql.strip().rstrip(";").strip()


def _strip_string_literals(sql: str) -> str:
    """
    把字符串字面量替换成占位符，再做关键字扫描。

    否则形如 `WHERE name = 'delete'` 的合法查询会因值里含有 delete 被误杀，
    同时 `; DROP TABLE x` 这类真实注入又能被正确识别。
    """
    # 单引号与双引号字符串整体折叠为 ''
    return re.sub(r"'[^']*'|\"[^\"]*\"", "''", sql)


def _check_statement_type(normalized: str, scanned: str) -> None:
    """校验语句类型：必须以 SELECT/WITH 开头，且不含禁止关键字"""
    lowered = scanned.lower().strip()

    if not any(lowered.startswith(prefix) for prefix in _ALLOWED_PREFIXES):
        first_word = normalized.split()[0] if normalized.split() else "空语句"
        raise SQLGuardError(f"仅允许执行查询语句，当前语句以 `{first_word}` 开头")

    for keyword in _FORBIDDEN_KEYWORDS:
        # 用词边界匹配，避免 `update_time` 之类字段名被误判
        if re.search(rf"\b{re.escape(keyword)}\b", lowered):
            raise SQLGuardError(f"SQL 中包含被禁止的关键字：{keyword}")


def _check_single_statement(scanned: str) -> None:
    """拦截分号拼接的多语句注入"""
    if ";" in scanned:
        raise SQLGuardError("检测到多语句拼接，已拒绝执行")


def _check_length(scanned: str, max_length: int) -> None:
    """拦截超长语句"""
    if len(scanned) > max_length:
        raise SQLGuardError(
            f"SQL 语句过长（{len(scanned)} 字符，上限 {max_length}），已拒绝执行"
        )


def _extract_existing_limit(scanned: str) -> int | None:
    """提取语句末尾已有的 LIMIT 数值，没有则返回 None"""
    match = re.search(r"\blimit\s+(\d+)\s*$", scanned, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _apply_limit(normalized: str, max_rows: int) -> GuardedSQL:
    """
    应用行数上限。

    - 无 LIMIT：追加 `LIMIT max_rows`
    - 有 LIMIT 且未超界：保持原样，尊重用户显式意图
    - 有 LIMIT 但超界：收紧到 max_rows
    """
    existing = _extract_existing_limit(normalized)

    if existing is None:
        return GuardedSQL(sql=f"{normalized} LIMIT {max_rows}", limit_injected=True)

    if existing <= max_rows:
        return GuardedSQL(sql=normalized, limit_injected=False)

    clamped = re.sub(
        r"\blimit\s+\d+\s*$",
        f"LIMIT {max_rows}",
        normalized,
        flags=re.IGNORECASE,
    )
    return GuardedSQL(sql=clamped, limit_injected=True)


def guard_identifier(name: str, *, kind: str = "标识符") -> str:
    """
    校验表名 / 字段名等 SQL 标识符，拒绝任何可能改变语句结构的字符。

    与 guard_sql 的分工：guard_sql 守的是"整条 SQL 的形状"，本函数守的是
    "拼进 SQL 的单个名字"。二者缺一不可——`show columns from {table_name}`
    这类语句无法用 guard_sql 覆盖（它以 show 开头且不含禁止关键字），
    但表名一旦来自不可信输入就是真实注入面。

    Args:
        name: 待校验的标识符
        kind: 用于错误信息的名称，便于定位是表名还是字段名

    Returns:
        原样返回通过校验的标识符

    Raises:
        SQLGuardError: 标识符为空或含非法字符
    """
    if not name or not isinstance(name, str):
        raise SQLGuardError(f"{kind}为空")

    candidate = name.strip()
    if not _IDENTIFIER_PATTERN.match(candidate):
        raise SQLGuardError(f"{kind}含非法字符：{name!r}（仅允许字母、数字、下划线）")

    return candidate


def guard_sql(
    sql: str,
    max_rows: int = _DEFAULT_MAX_ROWS,
    max_length: int = _DEFAULT_MAX_LENGTH,
) -> GuardedSQL:
    """
    对 LLM 生成的 SQL 做安全校验与行数约束。

    Args:
        sql: 待校验的 SQL 文本
        max_rows: 允许返回的最大行数，超过则收紧
        max_length: 允许的最大语句长度（字符）

    Returns:
        处理后的 GuardedSQL；调用方应使用其中的 sql 字段执行查询

    Raises:
        SQLGuardError: 语句类型非法、包含禁止关键字、多语句或超长
    """
    if not sql or not sql.strip():
        raise SQLGuardError("SQL 语句为空")

    normalized = _normalize(sql)
    scanned = _strip_string_literals(normalized)

    _check_length(scanned, max_length)
    _check_single_statement(scanned)
    _check_statement_type(normalized, scanned)

    guarded = _apply_limit(normalized, max_rows)

    if guarded.limit_injected:
        logger.info(f"SQLGuard 已应用行数上限 {max_rows}：{guarded.sql}")

    return guarded
