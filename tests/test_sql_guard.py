"""
SQL 安全护栏单元测试

覆盖 SQLGuard 的三类职责：
1. 语句类型白名单：只允许 SELECT / WITH，拦截 DDL 与 DML
2. 强制 LIMIT：无 LIMIT 时自动补上，已有 LIMIT 时保持用户意图
3. 超长语句拒绝：避免把异常巨大的 SQL 直接透传给数据库

这些测试不依赖任何外部服务，属于纯函数级别的快速回归。
"""

import pytest

from app.core.sql_guard import SQLGuardError, guard_sql


class TestStatementWhitelist:
    """语句类型白名单"""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "SELECT * FROM fact_order WHERE dt = '2025-01-01'",
            "WITH t AS (SELECT 1 AS a) SELECT a FROM t",
            "  select count(*) from dim_region  ",
        ],
    )
    def test_allows_select_and_cte(self, sql):
        """SELECT 与 WITH 开头的查询语句应当放行"""
        result = guard_sql(sql)
        assert result.sql.upper().startswith(("SELECT", "WITH"))

    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM fact_order",
            "DROP TABLE fact_order",
            "UPDATE fact_order SET amount = 0",
            "INSERT INTO fact_order VALUES (1)",
            "TRUNCATE TABLE fact_order",
            "CREATE TABLE t (a int)",
            "ALTER TABLE fact_order ADD COLUMN b int",
            "GRANT ALL ON dw.* TO 'x'@'%'",
        ],
    )
    def test_rejects_non_select(self, sql):
        """任何非查询语句都必须抛出 SQLGuardError"""
        with pytest.raises(SQLGuardError):
            guard_sql(sql)

    def test_rejects_stacked_statements(self):
        """分号拼接的多语句必须拦截，防止注入"""
        with pytest.raises(SQLGuardError, match="多语句"):
            guard_sql("SELECT 1; DROP TABLE fact_order")

    def test_allows_single_trailing_semicolon(self):
        """单条语句结尾的分号属于合法写法，不应误伤"""
        result = guard_sql("SELECT 1;")
        assert "SELECT 1" in result.sql

    def test_rejects_write_keyword_in_comment_position(self):
        """形如 SELECT 中嵌套写操作的语句必须拦截"""
        with pytest.raises(SQLGuardError):
            guard_sql("SELECT * FROM fact_order FOR UPDATE")

    def test_rejects_empty(self):
        """空语句直接拒绝"""
        with pytest.raises(SQLGuardError):
            guard_sql("   ")


class TestLimitInjection:
    """LIMIT 自动补齐"""

    def test_injects_limit_when_absent(self):
        """无 LIMIT 的查询会被自动补上默认上限"""
        result = guard_sql("SELECT * FROM fact_order", max_rows=500)
        assert result.limit_injected is True
        assert result.sql.rstrip().endswith("LIMIT 500")

    def test_keeps_existing_limit_when_within_bound(self):
        """已有 LIMIT 且未超界时保持原值，不重复追加"""
        result = guard_sql("SELECT * FROM fact_order LIMIT 10", max_rows=500)
        assert result.limit_injected is False
        assert "LIMIT 10" in result.sql
        assert "LIMIT 500" not in result.sql

    def test_clamps_existing_limit_when_exceeds_bound(self):
        """已有 LIMIT 超过上限时收紧到上限"""
        result = guard_sql("SELECT * FROM fact_order LIMIT 999999", max_rows=500)
        assert result.limit_injected is True
        assert "LIMIT 500" in result.sql
        assert "999999" not in result.sql

    def test_strips_trailing_semicolon_before_injecting(self):
        """结尾分号不应导致 LIMIT 拼在分号之后（非法语法）"""
        result = guard_sql("SELECT * FROM fact_order;", max_rows=100)
        assert result.sql.rstrip().endswith("LIMIT 100")
        assert ";" not in result.sql

    def test_limit_keyword_case_insensitive(self):
        """limit 关键字大小写不敏感，避免重复注入"""
        result = guard_sql("select * from fact_order limit 5", max_rows=500)
        assert result.limit_injected is False
        assert result.sql.lower().count("limit") == 1


class TestLengthGuard:
    """超长语句拒绝"""

    def test_rejects_oversized_sql(self):
        """超过长度上限的语句应被拒绝"""
        huge = "SELECT " + ", ".join(f"col_{i}" for i in range(5000)) + " FROM t"
        with pytest.raises(SQLGuardError, match="过长"):
            guard_sql(huge, max_length=1000)

    def test_accepts_normal_sql(self):
        """正常长度的语句不受影响"""
        result = guard_sql("SELECT a, b, c FROM fact_order", max_rows=100)
        assert result.limit_injected is True
