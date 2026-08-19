# syntax=docker/dockerfile:1

# --------------------------------------------------------------------------
# 构建阶段：把依赖装进一个自包含的 venv。
#
# 依赖装在拷源码之前，这样改代码不会让依赖层失效 —— 绝大多数重建里，最慢的
# 那一步始终命中缓存。
# --------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# --frozen 会在 uv.lock 缺失或过期时直接构建失败，这正是要的效果：镜像必须由
# 提交进仓库的 lockfile 复现出来，绝不允许在构建时现场解析依赖。
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# README 是包元数据（pyproject 的 readme 字段），构建后端要读它。放在这一层而
# 不是跟 pyproject 一起，是为了改文案时不打掉上面那层依赖缓存。
COPY README.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --------------------------------------------------------------------------
# 运行阶段：没有构建工具，没有包管理器，不用 root。
# --------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# 用非 root 账号跑，容器一旦被突破，落点也只是个无权限用户。
RUN groupadd --system --gid 1001 adpilot \
    && useradd --system --uid 1001 --gid adpilot --create-home adpilot

WORKDIR /app

COPY --from=builder --chown=adpilot:adpilot /app/.venv /app/.venv
COPY --from=builder --chown=adpilot:adpilot /app/src /app/src

# 迁移脚本和它的配置也得进镜像：升 schema 是在容器里跑的
# （`docker compose run --rm api alembic upgrade head`），而运行阶段这个镜像里
# 既没有包管理器也没有仓库源码。**启动时不自动迁移** —— 自动迁移意味着你看不见
# 它执行了什么，生产环境不可接受（Schema 方案第七节）。
COPY --chown=adpilot:adpilot alembic.ini ./
COPY --chown=adpilot:adpilot migrations/ ./migrations/

USER adpilot

EXPOSE 8000

# 这里探的是存活而不是就绪：数据库连不上意味着「暂时不能接流量」，
# 不等于「这个容器坏了，该换掉」。
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health/live', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "adpilot.main:app", "--host", "0.0.0.0", "--port", "8000"]
