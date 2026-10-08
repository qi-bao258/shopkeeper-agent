# 项目文件结构说明（PROJECT_STRUCTURE.md）

> 本文档介绍电商问数（shopkeeper-agent）项目的每个文件/目录的功能，帮助快速建立代码地图。
> 一句话串联：`frontend` 发请求 → `api` 路由接住 → `services` 驱动 → `agent/graph` 编排 14 个 `nodes`（入口先做追问改写）→ 节点通过 `repositories` 访问 `clients` 管理的四个存储 → 结果经 SSE 流回前端；`prompts/` 供给模型提示词，`conf/ + clients/` 管配置与连接，`scripts/` 负责知识库初始化，`session_store` 承担多轮上下文记忆。

---

## 一、根目录——入口与工程配置

| 文件 | 功能 |
|------|------|
| `main.py` | **后端启动入口**：创建 FastAPI 应用，注册 query 路由，挂载 lifespan 生命周期 |
| `pyproject.toml` | Python 依赖与工程配置（uv 管理，ruff lint/format） |
| `.python-version` | 固定 Python 版本（3.14） |
| `.env` / `.env.example` | 私密配置（LLM_API_KEY）/ 配置模板 |
| `.editorconfig` / `.gitignore` | 编辑器统一格式 / Git 忽略规则 |
| `.pre-commit-config.yaml` | 提交前钩子（ruff 检查） |
| `README.md` | 项目说明（架构图、教程索引） |
| `STARTUP.md` | 本机启动指南（端口、启动顺序、排错） |
| `LICENSE` | 开源协议 |

## 二、app/agent——智能体核心（项目心脏）

| 文件 | 功能 |
|------|------|
| `graph.py` | **LangGraph 图编排**：注册 14 个节点、追问改写入口边、三路并行召回边、双路过滤边、条件边（校验失败→修正） |
| `state.py` | **State 定义**：`DataAgentState`（节点间传递的业务数据：query/resolved_query/session_id/history/keywords/召回结果/sql/error/result）+ 各 InfoState 结构 |
| `context.py` | **Context 定义**：`DataAgentContext`（外部依赖：各仓储、客户端与 `session_store`，与 State 分离） |
| `llm.py` | LLM 实例化：`init_chat_model`，OpenAI 兼容接口，temperature=0，读 `.env` 的 Key |

### app/agent/nodes——14 个执行节点（按执行顺序）

| 节点文件 | 功能 |
|------|------|
| `resolve_followup.py` | **图入口节点**：读会话历史，把「那华东呢？」这类追问改写为独立问题写入 `resolved_query`；无历史直接透传，失败降级为原问题 |
| `extract_keywords.py` | jieba TF-IDF 抽词 + 词性白名单（n/nr/ns/nt/nz/v/vn/a/an/eng/i/l）+ 原始 query 兜底 |
| `recall_column.py` | 字段召回：LLM 扩展关键词 → 逐词向量化查 Qdrant → **按 column_id 去重** |
| `recall_metric.py` | 指标召回：扩展关键词 → Qdrant 指标 collection 检索 |
| `recall_value.py` | 取值召回：关键词查 Elasticsearch 全文索引 |
| `merge_retrieved_info.py` | **合并枢纽**（7 步）：id 去重 → 补指标依赖字段 → 取值回填 examples → 按表组织 → 补主外键 → 生成表/指标上下文 |
| `filter_table.py` | LLM 从候选表中挑真正需要的，程序执行结构裁剪 |
| `filter_metric.py` | 同上，过滤指标 |
| `add_extra_context.py` | 补充 date_info（当前日期）和 db_info（方言/版本，从 DW MySQL 读） |
| `generate_sql.py` | 四类上下文 YAML dump 进提示词 → StrOutputParser 产出纯文本 SQL |
| `validate_sql.py` | `EXPLAIN` 预校验；失败**写回 state["error"]** 而非抛异常 |
| `correct_sql.py` | 带着原 SQL + 报错信息 + 全量上下文做最小必要修正（修正后直接执行，不回校验） |
| `run_sql.py` | 在真实数仓执行 SQL，返回结果（`result` 事件附带 `sql` 与 `resolved_query`） |
| `give_up.py` | 修正次数耗尽后的终止节点，结构化返回最后一次数据库错误 |

## 三、app/api——接口层

| 文件 | 功能 |
|------|------|
| `routers/query_router.py` | `POST /api/query`（SSE）+ 三个会话接口 `/api/session/{id}`（GET/DELETE）与 `/api/sessions`（GET），只管路由不碰业务 |
| `schemas/query_schema.py` | 请求体 Pydantic 模型（`{"query": "...", "session_id": "..."}`）与会话历史响应模型 |
| `dependencies.py` | **依赖注入入口**：`get_query_service` 递归组装仓储、客户端与 `session_store` |
| `lifespan.py` | 应用启动/关闭钩子：初始化与关闭各客户端管理器 |

## 四、app/clients——外部客户端管理（4 个单例）

| 文件 | 功能 |
|------|------|
| `mysql_client_manager.py` | 双实例：meta（元数据库）和 dw（数仓）两个引擎与会话工厂 |
| `qdrant_client_manager.py` | Qdrant 向量库客户端 |
| `es_client_manager.py` | Elasticsearch 客户端 |
| `embedding_client_manager.py` | Embedding 客户端（连本地 TEI 服务的 BGE 模型） |

## 五、配置体系（conf/ + app/conf/）

| 文件 | 功能 |
|------|------|
| `conf/app_config.yaml` | 运行时配置：MySQL(3307)/Qdrant/ES/Embedding 地址、LLM model_name/base_url |
| `conf/meta_config.yaml` | 知识库构建配置：从哪些库表抽取元数据、写入哪个 collection/index |
| `app/conf/app_config.py` / `meta_config.py` | OmegaConf 加载 YAML + `load_dotenv` 注入环境变量，导出强类型配置对象 |

## 六、app/core——基础设施

| 文件 | 功能 |
|------|------|
| `log.py` | loguru 日志 + **ContextVar 注入 request_id**，并发请求链路追踪 |
| `context.py` | 定义 request_id 的 ContextVar 变量 |

## 七、数据层：entities / models / repositories

**app/entities——业务实体**（介于 ORM 和 State 之间的内存对象）：
- `column_info.py`（字段）、`metric_info.py`（指标）、`table_info.py`（表）、`value_info.py`（字段取值）、`column_metric.py`（字段-指标关联）

**app/models——SQLAlchemy ORM 模型**（映射 meta 库的 4 张表）：
- `base.py`（declarative base）+ `table_info.py` / `column_info.py` / `metric_info.py` / `column_metric.py`

**app/repositories——仓储层**（数据访问与业务解耦）：

| 文件 | 功能 |
|------|------|
| `qdrant/column_qdrant_repository.py` | 字段向量检索 |
| `qdrant/metric_qdrant_repository.py` | 指标向量检索 |
| `es/value_es_repository.py` | 字段取值全文检索 |
| `mysql/meta/meta_mysql_repository.py` | 元数据权威查询：按 id 查字段/表、查主外键字段 |
| `mysql/meta/mappers/`（4 个） | ORM 模型 → 业务实体的转换器 |
| `mysql/dw/dw_mysql_repository.py` | 数仓操作：EXPLAIN 校验、执行 SQL、读数据库版本 |

## 八、知识库构建与提示词

| 文件 | 功能 |
|------|------|
| `app/scripts/build_meta_knowledge.py` | **构建脚本 CLI 入口**：`uv run python -m app.scripts.build_meta_knowledge -c conf/meta_config.yaml` |
| `app/services/meta_knowledge_service.py` | 构建逻辑：从数仓抽取表/字段/指标/取值 → 分别写 MySQL、Qdrant、ES |
| `app/prompt/prompt_loader.py` | 按名称加载 prompts/ 目录的模板文件 |
| `prompts/`（8 个 .prompt） | `resolve_followup`（追问改写）、`generate_sql`、`correct_sql`、`filter_table_info`、`filter_metric_info`、`extend_keywords_for_column/metric/value_recall`（三路各自的关键词扩展） |

## 九、app/services——业务服务层

| 文件 | 功能 |
|------|------|
| `query_service.py` | **SSE 核心逻辑**：读历史注入 State → 执行 `graph.astream(stream_mode="custom")` → 把节点事件转成 `data: {...}\n\n` 异步生成器 → `finally` 中回写会话历史 |
| `session_store.py` | **多轮会话记忆存储**：`Turn` / `Session` 数据对象、`SessionStore` Protocol（依赖倒置，便于换 Redis）、`InMemorySessionStore`（LRU 淘汰 + 轮数上限 + TTL + `asyncio.Lock` 并发安全） |

## 十、docker——基础设施编排

| 文件 | 功能 |
|------|------|
| `docker-compose.yaml` | 一键编排 5 容器：MySQL(3307)、Elasticsearch(9200)、Kibana(5601)、Qdrant(6333)、TEI Embedding(8081) |
| `mysql/dw.sql` / `meta.sql` | 容器首启自动执行的建库脚本（数仓 5 表 + 元数据库 4 表） |
| `elasticsearch/Dockerfile` + `plugins/ik.zip` | 自定义 ES 镜像，安装 IK 中文分词插件 |

## 十一、frontend——React 前端

| 文件 | 功能 |
|------|------|
| `vite.config.ts` | Vite 配置，**/api 代理到后端 8000** |
| `src/App.tsx` | 主组件：消息列表状态、SSE 事件分发、**session_id 的 localStorage 持久化与追问示例** |
| `src/lib/agentApi.ts` | **SSE 请求封装**：fetch 流式解析后端事件；另含 `fetchSessionHistory` / `clearSession` 两个会话接口 |
| `src/types/agent.ts` | SSE 消息 TS 类型定义（session/progress/result/error） |
| `src/components/StepRail.tsx` | 步骤条：逐节点展示执行进度 |
| `src/components/ResultTable.tsx` | 查询结果表格渲染 |
| `src/components/MessageBubble.tsx` | 消息气泡：含**追问改写提示**（「已结合上文理解为」）与**可折叠 SQL 展示** |
| `src/components/Composer.tsx` / `EmptyState.tsx` | 输入框 / 空态页 |
| `src/lib/format.ts` | 格式化工具函数 |
| `package.json` / `tailwind.config.ts` / `tsconfig*.json` / `index.html` | 依赖、Tailwind 样式、TS 编译、HTML 入口 |

## 十二、其余

| 路径 | 功能 |
|------|------|
| `tests/` | **单元测试**：sql_guard / 节点逻辑 / 图编排 / 会话存储 / API 层，全部 mock 外部依赖 |
| `eval/` | **评测集与评测脚本**：`golden_set.yaml` + `run_eval.py`，输出召回率与通过率 |
| `Dockerfile` | 后端应用镜像（两阶段构建 + 非 root + 健康检查） |
| `.github/workflows/ci.yaml` | CI：ruff 检查 → pytest 覆盖率 → Docker 构建验证 |
| `docs/images/` | README 用的系统架构图和效果截图 |
| `examples/qdrant_quickstart_demo.py` | Qdrant 学习示例脚本（教程配套） |
| `logs/app.log` | 运行时日志输出 |
| `.vscode/` | 调试启动配置和编辑器设置 |

## 十三、工程质量模块（新增）

### app/core/sql_guard.py —— SQL 安全护栏

与 LLM 无关的程序化防线，在 SQL 抵达数仓前拦截风险：

| 规则 | 说明 |
|------|------|
| 语句白名单 | 仅放行 `SELECT` / `WITH`，拒绝 DDL / DML / 授权语句 |
| 多语句拦截 | 拒绝分号拼接，防止注入 |
| 强制 LIMIT | 无 LIMIT 自动补齐，超界 LIMIT 收紧到上限 |
| 长度上限 | 拒绝异常巨大的语句 |
| 字面量感知 | 先剥离字符串字面量再扫描关键字，避免合法值被误杀 |

### app/agent/nodes/give_up.py —— 有界失败

修正次数耗尽后的终止节点，把最后一次数据库错误结构化推送给前端，
避免无限重试与请求悬挂。

### 修正闭环（graph.py）

```
generate_sql → validate_sql → (通过) → run_sql → END
                    ↓ (失败且未达上限)
                correct_sql → validate_sql   ← 有界重试
                    ↓ (失败且已达上限)
                 give_up → END
```

---

## 十四、多轮会话记忆模块（新增）

### 数据流

```
POST /api/query { query, session_id? }
        │
        ▼
QueryService.query()
        │  ① 读历史：session_store.get_history(session_id, limit=5)
        │     历史以 { question, resolved_question } 列表注入 State
        ▼
   graph.astream(...)
        │  START → resolve_followup（改写）→ extract_keywords → … → run_sql
        │  下游统一读取 state["resolved_query"]
        ▼
      finally: ② 落库：session_store.append_turn(session_id, Turn(...))
        │
        ▼
   SSE 事件流：session → progress* → result(含 sql / resolved_query)
```

### app/services/session_store.py —— 会话存储

| 组成 | 说明 |
|------|------|
| `Turn` | 单轮记录：`question`（原始）/ `resolved_question`（改写后）/ `sql` / `row_count` / `created_at` |
| `Session` | 一个会话的 `Turn` 列表，提供 `recent(limit)` 取最近 N 轮 |
| `SessionStore`（Protocol） | 接口契约：`get_history` / `append_turn` / `clear` / `list_sessions` / `stats`——**依赖倒置，换 Redis 不用改业务代码** |
| `InMemorySessionStore` | 默认实现，三重容量约束 + `asyncio.Lock` |

### 三重容量约束（防止内存无界增长）

| 约束 | 默认值 | 作用 |
|------|--------|------|
| `max_sessions` | 500 | 会话总数上限，超出按 LRU 淘汰最久未访问的会话 |
| `max_turns_per_session` | 10 | 单会话保留轮数，超出丢弃最早的轮次 |
| `ttl_seconds` | 3600 | 惰性过期：读写时判断，超过 TTL 的会话视为不存在 |

### app/agent/nodes/resolve_followup.py —— 追问改写节点

| 设计点 | 说明 |
|--------|------|
| 图入口 | 先于 `extract_keywords` 执行，改写只发生一次 |
| 首轮透传 | 无历史时直接返回原问题，不发起 LLM 调用 |
| 降级优先 | 改写失败或空值 → 回退原始问题，并照常上报 success 进度 |
| 测试接缝 | `build_rewrite_chain()` 独立成函数，便于测试 monkeypatch 替换 LLM |

```
resolve_followup → resolved_query="统计华东地区的销售总额"
                          ↑
                  用户追问："那华东呢？"
```

### 会话管理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/session/{session_id}` | 查询历史轮次（`turns` 列表） |
| `DELETE` | `/api/session/{session_id}` | 清空会话，不存在返回 `404` |
| `GET` | `/api/sessions` | 会话列表 + 存储统计（`session_count` / `turn_count` / 上限参数） |
