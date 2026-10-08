<div align="center">
  <h1>「电商问数」智能数据分析 Agent</h1>
  <p>
    <img src="https://img.shields.io/badge/Python-3.14-3776AB?logo=python&logoColor=white" alt="Python">
    <img src="https://img.shields.io/badge/LangGraph-Agentic_Workflow-1C3C3C" alt="LangGraph">
    <img src="https://img.shields.io/badge/FastAPI-SSE-009688?logo=fastapi&logoColor=white" alt="FastAPI">
    <img src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white" alt="Docker">
  </p>
</div>

## 项目介绍

「电商问数」是一个面向电商数据仓库场景的自然语言问数智能体。业务人员无需掌握 SQL 与数据模型细节，用自然语言提问即可得到结构化的数据查询结果，全过程以流式方式实时反馈推理进度与最终结论。

系统围绕「准确召回 → 严谨生成 → 安全执行」三个阶段构建：

- **元数据知识库**：将数据仓库的表结构、字段、指标口径与字段取值预构建为元数据，分别以向量与全文形式索引到 Qdrant 与 Elasticsearch，供查询时精准召回。
- **多阶段问数流程**：基于 LangGraph 编排 14 个节点的智能体工作流，完成关键词抽取、字段/指标/取值的并行召回、召回结果合并、候选过滤、上下文补全、SQL 生成、EXPLAIN 校验、错误修正与最终执行。
- **SQL 安全护栏**：执行前对生成 SQL 做标识符白名单与危险语句校验，杜绝表名/字段名注入与越权访问。
- **流式接口**：基于 FastAPI 的 SSE 接口向前端推送节点进度、会话标识与最终结果，支持带会话上下文的多轮追问。

后端、前端与全部基础设施（MySQL / Elasticsearch / Qdrant / Embedding）均由 Docker Compose 编排，通过 `启动电商问数.ps1` 一键拉起。

## 项目技术栈

| 层次 | 技术 |
| ---- | ---- |
| 智能体编排 | LangGraph、LangChain |
| Web 框架 | FastAPI（SSE 流式响应） |
| 向量检索 | Qdrant |
| 全文检索 | Elasticsearch（IK 中文分词） |
| 关系存储 | MySQL（元数据库 + 教学数仓，SQLAlchemy 异步 ORM） |
| Embedding | TEI（`BAAI/bge-large-zh-v1.5`） |
| 中文分词 | Jieba |
| 配置管理 | OmegaConf + python-dotenv |
| 前端 | React + TypeScript + Vite + Tailwind CSS |
| 部署 | Docker Compose + Nginx + PowerShell 一键启停脚本 |
| 工程规范 | Ruff、pre-commit、pytest、pytest-asyncio、pytest-cov |

## 项目结构

```text
shopkeeper-agent/
├── app/
│   ├── agent/                  # LangGraph 问数智能体
│   │   ├── graph.py            # 工作流骨架与节点编排
│   │   ├── state.py            # 智能体状态定义
│   │   ├── llm.py              # 大模型客户端
│   │   └── nodes/              # 14 个问数流程节点
│   │       ├── extract_keywords.py      # 关键词抽取
│   │       ├── recall_*.py              # 字段/指标/取值并行召回
│   │       ├── merge_retrieved_info.py  # 召回结果合并
│   │       ├── filter_*.py              # 候选表字段与指标过滤
│   │       ├── add_extra_context.py     # 日期与数据库上下文补全
│   │       ├── generate_sql.py          # SQL 生成
│   │       ├── validate_sql.py          # EXPLAIN 校验
│   │       ├── correct_sql.py           # 错误修正
│   │       └── run_sql.py               # SQL 执行
│   ├── api/                    # FastAPI 接口层
│   │   ├── routers/            # 问数路由（SSE 流式）
│   │   ├── schemas/            # 请求/响应模型
│   │   ├── dependencies.py     # 依赖注入
│   │   └── lifespan.py         # 应用生命周期管理
│   ├── clients/                # 外部服务客户端管理（MySQL/ES/Qdrant/Embedding）
│   ├── conf/                   # 应用与元数据配置（OmegaConf dataclass）
│   ├── core/                   # SQL 护栏、异常、日志等核心能力
│   ├── entities/               # 领域实体定义
│   ├── models/                 # SQLAlchemy ORM 模型
│   ├── repositories/           # 仓储层（mysql/es/qdrant）
│   ├── services/               # 业务服务（问数、元数据构建、会话存储）
│   ├── prompt/                 # 提示词加载器
│   └── scripts/                # 元数据知识库构建脚本
├── conf/                       # YAML 配置文件
├── docker/                     # Docker Compose 编排与基础服务
│   ├── docker-compose.yaml     # 基础设施（MySQL/ES/Qdrant/Embedding）
│   ├── docker-compose.net.yaml # 容器网络接入
│   ├── docker-compose.app.yaml # 应用（后端 + 前端）
│   ├── mysql/                  # 数仓初始化 SQL
│   └── elasticsearch/          # IK 分词插件镜像
├── frontend/                   # React 前端
├── tests/                      # 单元与集成测试
├── main.py                     # FastAPI 应用入口
├── pyproject.toml              # 依赖与工具配置
├── Dockerfile                  # 后端镜像
├── 启动电商问数.ps1             # 一键启动脚本
└── 停止电商问数.ps1             # 一键停止脚本
```

## 快速开始

1. 安装并启动 [Docker Desktop](https://www.docker.com/products/docker-desktop/)。
2. 复制环境变量模板并填入大模型密钥：

```bash
cp .env.example .env   # 编辑 .env，设置 LLM_API_KEY
```

3. 双击 `启动电商问数.ps1`，脚本会自动拉起 Docker 引擎、启动全部容器、等待后端就绪并打开浏览器：

```text
前端界面：http://localhost:5173
接口文档：http://localhost:8000/docs
健康检查：http://localhost:8000/health/ready
```

4. 停止服务：双击 `停止电商问数.ps1`。

> 首次运行会构建镜像（约 1-3 分钟），之后日常启动直接复用镜像。容器由 Docker 守护进程托管，关闭脚本窗口不会停止服务。

## License

[MIT](LICENSE)
