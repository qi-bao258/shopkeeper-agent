"""
数仓 MySQL 仓储

这一层对应文档里的 DW Repository，职责是到真实数仓中补齐配置文件里
没有显式维护的信息，例如字段类型和字段示例值。Service 层只关心
“需要哪些信息”，具体怎样查数仓由仓储层统一封装
SQL 生成闭环中的数据库环境读取 SQL 校验和最终查询执行也集中放在这里
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.log import logger
from app.core.sql_guard import guard_identifier, guard_sql


class DWMySQLRepository:
    """负责查询数仓真实表结构和字段样例值"""

    # 单次查询允许返回的最大行数，作为防止全表拉取的最后一道程序化防线
    MAX_RESULT_ROWS = 1000

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_column_types(self, table_name: str) -> dict[str, str]:
        """查询整张表的字段类型，作为 ColumnInfo.type 的真实来源"""
        # 表名来自配置与模型输出，拼进 SQL 前先过标识符白名单，
        # 避免 `show columns from` 这类语句绕过 guard_sql 的整句校验
        safe_table = guard_identifier(table_name, kind="表名")
        sql = f"show columns from {safe_table}"
        result = await self.session.execute(text(sql))
        result_dict = result.mappings().fetchall()
        return {row["Field"]: row["Type"] for row in result_dict}

    async def get_column_values(
        self, table_name: str, column_name: str, limit: int = 10
    ) -> list:
        """抽样查询字段示例值，供元数据入库和后续检索链路复用"""
        safe_table = guard_identifier(table_name, kind="表名")
        safe_column = guard_identifier(column_name, kind="字段名")
        # limit 是数值，直接强制转为 int 并夹紧区间，杜绝字符串拼接
        safe_limit = max(1, min(int(limit), self.MAX_RESULT_ROWS))
        sql = f"select distinct {safe_column} from {safe_table} limit {safe_limit}"
        result = await self.session.execute(text(sql))
        return [row[0] for row in result.fetchall()]

    async def get_db_info(self):
        """读取当前数仓数据库的方言和版本，供 SQL 生成提示词使用"""

        sql = "select version()"
        result = await self.session.execute(text(sql))
        version = result.scalar()

        # dialect 来自 SQLAlchemy 当前绑定的数据库方言，例如 mysql
        dialect = self.session.bind.dialect.name
        return {"dialect": dialect, "version": version}

    async def validate(self, sql: str):
        """
        用 EXPLAIN 让数据库提前解析 SQL，发现语法 表名 字段名等错误。

        EXPLAIN 只做计划解析、不真正取数，因此这里不做 LIMIT 注入，
        让模型有机会根据原始语义修正，行数约束统一在 run() 中施加。
        """
        sql = f"explain {sql}"
        await self.session.execute(text(sql))

    async def run(self, sql: str) -> list[dict]:
        """
        执行最终 SQL，并把 SQLAlchemy 行对象转换成前端更易消费的字典列表。

        执行前统一经过 SQLGuard：
        - 拦截非查询语句，避免 LLM 幻觉产生写操作
        - 强制行数上限，避免大表查询拖垮服务
        """
        guarded = guard_sql(sql, max_rows=self.MAX_RESULT_ROWS)
        if guarded.limit_injected:
            logger.info(f"执行前已对结果集施加 {self.MAX_RESULT_ROWS} 行上限")

        result = await self.session.execute(text(guarded.sql))
        return [dict(row) for row in result.mappings().fetchall()]
