# 后端应用镜像
#
# 采用两阶段构建：builder 阶段用 uv 安装依赖，runtime 阶段只拷贝虚拟环境，
# 避免把编译工具链带进最终镜像，体积与攻击面都更小。
#
# 说明：MySQL / Qdrant / Elasticsearch / TEI Embedding 属于外部依赖服务，
# 由 docker-compose 或独立部署提供，不打进本镜像。

FROM python:3.14-slim AS builder

# uv 官方静态二进制，避免额外安装 pip 引导
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.14

WORKDIR /app

# 确认基础镜像的 Python 版本符合 pyproject 的 requires-python（>=3.14）
RUN python3.14 --version || python --version

# 先只拷贝依赖清单，让依赖层在源码变更时保持缓存
COPY pyproject.toml uv.lock ./

# --no-dev 排除测试依赖，--frozen 严格按 lock 安装保证可复现
RUN uv sync --frozen --no-dev --no-install-project

# 再拷贝源码并安装项目本身
COPY . .
RUN uv sync --frozen --no-dev


FROM python:3.14-slim AS runtime

# 非 root 用户运行，降低容器逃逸风险
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --create-home app

WORKDIR /app

# 仅携带虚拟环境与应用代码，不包含构建缓存
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/app /app/app
COPY --from=builder --chown=app:app /app/prompts /app/prompts
COPY --from=builder --chown=app:app /app/conf /app/conf
COPY --from=builder --chown=app:app /app/main.py /app/main.py

# 预建日志目录并确保工作目录可写：
# app/core/log.py 在导入期会执行 Path("logs").mkdir(parents=True, exist_ok=True)，
# 而 /app 由 COPY 后归属 root，非 root 的 app 用户无法创建该目录，会导致启动即崩。
RUN mkdir -p /app/logs && chown -R app:app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

EXPOSE 8000

# 容器级健康检查：探活 /docs 即可确认应用已完成 lifespan 初始化
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/docs')" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
