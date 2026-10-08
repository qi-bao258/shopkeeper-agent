"""
SQL 标识符护栏单元测试

guard_sql 守的是"整条 SQL 的形状"，但 `show columns from {table}` 这类语句
既以非查询关键字开头、又不含任何禁止关键字，无法被 guard_sql 覆盖。
表名与字段名一旦来自配置或模型输出就是真实注入面，因此需要独立的
标识符白名单作为第二道防线。本文件覆盖该函数的行为边界。
"""

import pytest

from app.core.sql_guard import SQLGuardError, guard_identifier


class TestGuardIdentifierAccepts:
    """合法标识符放行"""

    @pytest.mark.parametrize(
        "name",
        [
            "fact_order",
            "dwd_order_detail",
            "dim_region",
            "t",
            "_internal",
            "col_1",
            "A" * 64,  # 长度刚好到达上限
        ],
    )
    def test_accepts_valid_names(self, name):
        """常见表名与字段名应原样返回"""
        assert guard_identifier(name) == name

    def test_strips_surrounding_whitespace(self):
        """前后空白应被清理，避免 ` order_id ` 这类写入误判"""
        assert guard_identifier("  order_id  ") == "order_id"


class TestGuardIdentifierRejects:
    """非法标识符拒绝"""

    @pytest.mark.parametrize(
        "name",
        [
            "t; drop table x --",  # 注入拼接
            "order`id",  # 反引号
            "order id",  # 空格
            "order-id",  # 连字符
            "1abc",  # 数字开头
            "",
            "   ",
            "用户表",  # 非 ASCII
            "t.*",  # 通配符
            "A" * 65,  # 超长
        ],
    )
    def test_rejects_invalid_names(self, name):
        """任何含非法字符或超长的标识符都必须拒绝"""
        with pytest.raises(SQLGuardError):
            guard_identifier(name)

    def test_error_message_includes_kind(self):
        """错误信息应带上 kind，便于定位是表名还是字段名出问题"""
        with pytest.raises(SQLGuardError, match="表名"):
            guard_identifier("bad name", kind="表名")

    def test_rejects_non_string(self):
        """非字符串入参直接拒绝，避免 int 被静默接受"""
        with pytest.raises(SQLGuardError):
            guard_identifier(None)  # type: ignore[arg-type]
