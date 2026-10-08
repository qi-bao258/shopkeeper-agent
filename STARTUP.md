# 项目启动手册（STARTUP.md）

> 电商问数 —— 自然语言数据分析 Agent。本文件记录从零启动项目的完整流程，基于本项目特有的环境（Windows、3307 端口、aiomysql、BGE 本地 CPU 推理）。

---

## 一、依赖清单

| 组件 | 说明 | 是否已装 |
|------|------|----------|
| Docker Desktop | 运行 MySQL / ES / Qdrant / Kibana / Embedding | ✅ 已装 |
| uv | Python 包管理（3.14.3） | ✅ 已装 |
| Node.js + pnpm | 前端 | ✅ 已装 |
| 环境依赖 | 已通过 `uv sync` 安装 | ✅ 已装 |
| BGE 模型 | 已下载到 `docker/embedding/bge-large-zh-v1.5` | ✅ 已装 |
| `.env` | 配置文件（含 API Key 占位） | ✅ 已建 |
| **SiliconFlow API Key** | 唯一缺失项，需真实填入 `.env` | ⚠️ 待填 |

---

## 二、端口规划（本项目特有）

| 服务 | 端口 | 说明 |
|------|------|------|
| MySQL（meta + dw） | **3307** | 因本机 3306 被本地 MySQL 占用而改 |
| Elasticsearch | 9200 | |
| Kibana | 5601 | 可选，内存紧张可停 |
| Qdrant | 6333 | |
| Embedding (TEI + BGE) | 8081 | CPU 推理，无需 GPU |
| 前端 | 5173 | Vite dev server |
| 后端 | 8000 | FastAPI |

---

## 三、启动步骤

### 0. 前置：获取真实 API Key

`.env` 文件当前是占位 Key，问数在指标召回步骤会返回 `401 Token is invalid`。这是唯一阻塞完整问数的点。

```bash
# 去 https://siliconflow.cn 注册并创建 API 密钥
# 编辑项目根目录 .env，替换占位值
LLM_API_KEY=sk-你的真实Key
```

模型配置见 `conf/app_config.yaml`：LLM 为 `Pro/zai-org/GLM-5.1`（硅基流动），Embedding 为 `BAAI/bge-large-zh-v1.5`（本地）。

### 1. 启动 Docker Desktop

双击启动，等待托盘图标变绿（引擎运行中）。

### 2. 启动基础设施容器

（项目根目录）

```powershell
docker compose -f docker/docker-compose.yaml up -d
```

验证：

```powershell
docker ps
```

应看到 5 个容器处于 Up 状态，说明 MySQL(3307)/ES/Qdrant/Embedding 全部就绪。

> 数据卷已持久化，**容器停止再启动不需要重建知识库**。只有当执行过 `docker compose down -v` 清空了数据卷，才需要运行：
> ```powershell
> uv run python -m app.scripts.build_meta_knowledge -c conf/meta_config.yaml
> ```

### 3. 启动后端

（项目根目录，新开终端）

```powershell
uv run fastapi dev main.py
```

看到 `Uvicorn running on http://127.0.0.1:8000` 即成功。接口文档：http://127.0.0.1:8000/docs

### 4. 启动前端

（另开终端）

```powershell
cd frontend
pnpm dev
```

访问 http://localhost:5173

---

## 四、停止与慎用命令

| 命令 | 作用 | 是否破坏数据 |
|------|------|--------------|
| `docker compose -f docker/docker-compose.yaml stop` | 停止容器，保留数据卷 | 否，可再次 `up` 恢复 |
| `docker compose -f docker/docker-compose.yaml down` | 停止并移除容器 | 保留数据卷 |
| `docker compose -f docker/docker-compose.yaml down -v` | 停止并删除容器 + **删除数据卷** | **是，需重建知识库**，慎用 |

关闭前端/后端只需在对应终端按 `Ctrl + C`。

---

## 五、排错速查

| 症状 | 原因与解法 |
|------|-----------|
| 容器起不来 | Docker Desktop 引擎未运行，先启动它 |
| `docker ps` 有容器但不健康 | 等待数秒后重查；Embedding 首次加载模型慢 |
| `uv run` 报环境不存在 | 先执行 `uv sync`（依赖已缓存，秒级完成） |
| 问数报 `401 Token is invalid` | `.env` 仍是占位 Key，填入真实 SiliconFlow Key |
| 报 3306 端口占用 | 正常现象，本机 MySQL 占用 3306；本项目用 3307，不影响 |
| 前端打不开 | 确认 Vite 已启动，且端口 5173 未被占用 |
| Kibana 占用内存过大 | 16GB 内存吃紧时可 `docker stop kibana`（不影响核心问数） |

---

## 六、关键配置文件

| 文件 | 作用 |
|------|------|
| `conf/app_config.yaml` | LLM 模型、检索、MySQL/Qdrant/ES 连接配置 |
| `docker/docker-compose.yaml` | 基础设施容器编排（含端口映射） |
| `.env` | LLM API Key 等密钥 |
| `frontend/.env` | 前端代理目标（默认 http://127.0.0.1:8000） |
| `pyproject.toml` | Python 依赖（已改用 aiomysql 替换 asyncmy） |
```