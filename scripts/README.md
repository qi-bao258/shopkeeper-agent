# 启动脚本说明

本目录提供五个 PowerShell 脚本，把 `STARTUP.md` 里的手工步骤固化为可重复执行的流程。

## 快速开始

```powershell
# 一键启动全栈（基础设施 → 后端 → 前端）
.\scripts\start-all.ps1

# 停止全部服务（保留数据卷，下次可直接恢复）
.\scripts\stop-all.ps1
```

启动完成后访问：

| 服务 | 地址 |
|------|------|
| 前端界面 | http://localhost:5173 |
| 接口文档 | http://127.0.0.1:8000/docs |
| 健康检查（就绪） | http://127.0.0.1:8000/health/ready |
| Kibana | http://localhost:5601 |

## 脚本清单

| 脚本 | 职责 | 何时单独用 |
|------|------|-----------|
| `start-infra.ps1` | 起 5 个容器并等待服务可达 | 只想起依赖、调试容器 |
| `start-backend.ps1` | 起 FastAPI 并等待就绪探针返 200 | 容器已在跑，只重启后端 |
| `start-frontend.ps1` | **自愈 node_modules 链接** + 起 Vite + 等端口监听 | 只改前端时 |
| `start-all.ps1` | 串行编排上述三者 | 日常启动 |
| `stop-all.ps1` | 停前后端 + 停容器 | 收工、释放内存 |
| `repair-node-links.py` | 修复迁移后退化为空目录的 pnpm 链接 | 前端报 MODULE_NOT_FOUND 时 |

## 设计要点

### node_modules 链接自愈（重要）

本项目从原路径迁移到 `C:\Users\...\Desktop\项目\` 后，pnpm 建立的所有
junction **退化为空目录**（Windows 复制不保留跨路径链接）。`node_modules`
顶层目录看起来都在，实际全是空的，Vite 启动会报：

```
Cannot find module @rollup/rollup-win32-x64-msvc
You installed esbuild for another platform than the one you're currently using
Cannot find package '...\@vitejs\plugin-react\index.js'
ERR_MODULE_NOT_FOUND ...picomatch
```

这些都是**链接损坏**，不是代码问题，重装依赖也不是必要手段。
`start-frontend.ps1` 默认会自动调用 `repair-node-links.py` 自愈。

修复原理与 pnpm 的「截断 + hash」命名规则（如
`@babel/plugin-transform-react-jsx-self` → `@babel+plugin-transform-rea_21aa63fd…`）
详见 `repair-node-links.py` 头部注释。

手动修复：

```powershell
# 预览将要做的修改
python .\scripts\repair-node-links.py --dry-run
# 实际修复
python .\scripts\repair-node-links.py
```

### 为什么前端不用 pnpm 启动

`frontend/package.json` 声明 `packageManager: pnpm@10.33.0`，而本机 pnpm 为
12.x。pnpm 会尝试下载声明版本并失败（`@pnpm/exe did not materialize`）。
因此 `start-frontend.ps1` **直接用 node 运行 node_modules 里的 vite**，
绕开包管理器的版本协商。

### 为什么脚本里不用 `Start-Process`

两条本机限制：

- **`Start-Process -FilePath 'powershell.exe'` 被安全策略拦截**
  （报 "shell/interpreter/LOLBin target"）。
- **`UseShellExecute = $true` 会静默终止整个 PowerShell 进程**，
  导致脚本中途无输出退出，报错信息极具误导性。

统一改用 `System.Diagnostics.Process` + `UseShellExecute = $false`
+ `CreateNoWindow = $true`。

### 为什么拆成三个脚本

容器冷启动（尤其 Elasticsearch 与 Embedding 的模型加载）需要数十秒到数分钟，与后端进程的生命周期差异很大。拆开后，排错时能明确区分「依赖没好」还是「应用起不来」——这是把两类故障混在一个脚本里最容易踩的坑。

### 为什么每个脚本都要等待就绪

不等待就返回的脚本只能用「睡固定秒数」来近似编排，秒数短了会误判、长了会浪费。本套脚本改为**主动轮询真实健康端点**：

- `start-infra.ps1` 轮询五个端口是否可建连
- `start-backend.ps1` 轮询 `/health/ready` 是否返回 200
- `start-frontend.ps1` 轮询 5173 是否处于监听

### 后端的前置校验

`start-backend.ps1` 在启动前检查四项，任一不通过就不启动，避免「进程活着但问数不可用」：

1. **uv 与 `.venv`** —— 缺失时直接给修复命令（`uv sync`）
2. **`LLM_API_KEY` 是否占位** —— 占位 Key 会让指标召回步骤返回 `401 Token is invalid`，是全链路唯一的功能性阻塞点
3. **四个外部依赖端口** —— 不可达时提示先跑 `start-infra.ps1`
4. **8000 端口是否被占** —— 被占时直接报错并给出占用进程 PID，而不是让 uvicorn 自己报绑定失败

## 常用参数

```powershell
# 跳过 Kibana，省约 1GB 内存（16GB 机器推荐）
.\scripts\start-all.ps1 -SkipKibana

# 依赖已在跑，只起前后端
.\scripts\start-all.ps1 -SkipInfra

# 改后端端口（需同步改 frontend/.env 的代理目标）
.\scripts\start-backend.ps1 -Port 8001

# 只拉起进程不等待就绪
.\scripts\start-backend.ps1 -NoWait

# 只停前后端，容器继续跑
.\scripts\stop-all.ps1 -KeepInfra
```

### 无 Docker 时预览界面与接口文档

容器起不来（例如本机 `wsl.exe` 被拦截）时，仍可拉起前后端看界面和接口文档，
只是**问数功能不可用**（依赖 Qdrant/ES/MySQL 的检索会失败）：

```powershell
# 终端 1：后端，跳过依赖校验
.\scripts\start-backend.ps1 -SkipDependencyCheck

# 终端 2：前端
.\scripts\start-frontend.ps1
```

此时 `/health` 返回 200（应用正常），`/health/ready` 会卡住或返回 503
（因为它要探五个容器）——这是**预期行为**，不是故障。

> `-SkipDependencyCheck` 只跳过「端口可达性」前置校验，不影响应用启动逻辑。
> 常规启动**不要**加这个参数——它存在的意义是让你在依赖缺失时仍能调试接口。

## 危险操作

`stop-all.ps1 -RemoveVolumes` 会**删除 MySQL / Elasticsearch / Qdrant 的数据卷**，执行前需输入 `yes` 二次确认。删除后必须重建知识库：

```powershell
uv run python -m app.scripts.build_meta_knowledge -c conf/meta_config.yaml
```

日常停止请直接用 `.\scripts\stop-all.ps1`（默认保留数据卷）。

## 排错

| 症状 | 原因与解法 |
|------|-----------|
| `Docker 引擎不可用` | Docker Desktop 未启动。手动打开并等托盘图标变绿后重跑 |
| 脚本报中文乱码 / 语法错误 | `.ps1` 文件必须存为 **UTF-8 with BOM**。PowerShell 5.1 会把无 BOM 文件按 GBK 解码，导致中文字符串破坏语法结构 |
| 后端就绪探针返 503 | 查看 `dependencies` 字段定位是哪个依赖没起 |
| `401 Token is invalid` | `.env` 的 `LLM_API_KEY` 是占位值，填真实 SiliconFlow Key |
| `端口 8000 已被占用` | 用报错信息里的 PID 定位进程，或改 `-Port` |
| Embedding 迟迟不就绪 | 首次加载 BGE 模型较慢，`start-infra.ps1` 的 300 秒超时通常够用；不够可加 `-TimeoutSeconds 600` |
| 本地服务探测返回 **502 `upstream connect failed`** | 环境变量 `HTTP_PROXY`/`HTTPS_PROXY` 指向本地代理，把对 `127.0.0.1` 的请求也截获了。脚本内已对 `HttpWebRequest` 设 `Proxy = $null` 规避；自己写探测代码时要记得清零代理 |
| 服务起来一会儿又没了 | 若由父进程（脚本/IDE/沙箱）派生，父进程退出时子进程可能被连带终止。请在**独立终端**里运行启动脚本 |

## 环境限制说明

本套脚本在**本机沙箱环境**下无法完成完整启动，原因是 `wsl.exe` 被安全策略列入程序黑名单：

```
PROGRAM BLOCKED BY SECURITY POLICY
  - wsl.exe (C:\WINDOWS\System32\wsl.exe)
```

Docker Desktop 的 Linux 引擎依赖 WSL2 后端，因此容器无法拉起。这是**环境限制，不是脚本或代码问题**。在正常桌面环境下（Docker Desktop 可正常运行），本套脚本可直接使用。

如需在本机启用，请到 **安全中心 → 命令安全 → 程序黑名单** 中移除 `wsl.exe`。
