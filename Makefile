# adpilot 的命令入口：把常用命令收成短名字，不引入任何新逻辑。
#
# agent 也能跑这里的一部分 target —— .claude/settings.json 的 allow 里**逐条**列着
# 哪些能跑。那是精确匹配，不是 `Bash(make:*)` 通配，判据只有一条：
#
#   只有「展开后的原命令本身已经在 allow 里」的 target 才进白名单。
#
# 于是 make 只是换了个短名字，没有放大 agent 的实际权限。两个推论，别绕过：
#
# 1. 新加的 target **不会自动获得授权**。这是刻意的：要给 agent 用就去 settings.json
#    里补一条，顺手也就逼着人想一遍这条命令到底该不该给。
# 2. env / bootstrap **刻意不在白名单里** —— 它们碰 .env，而且跑完还得有人去填密码。
#
# 之所以不肯图省事写 `Bash(make:*)`：target 里能写任意命令，而 .claude/bash_guard.py
# 只看得见命令行上的 `make xxx`、看不见 Makefile 里这些行。一条通配规则就等于给
# 「密钥不进上下文」「依赖只经 uv 装」那几道拦截开了后门。
#
# check 的真相源是 .github/workflows/ci.yml —— 五道门禁必须与它同序同命令。

UV      ?= uv
COMPOSE ?= docker compose

# check 的五道门禁按 CI 的顺序串行跑，-j 下也不许打乱：先 lint 后 test，
# 才能在格式问题上快速失败，而不是等跑完测试再报一个空行。
.NOTPARALLEL:

.DEFAULT_GOAL := help

.PHONY: help bootstrap env setup dev lint fmt types imports test test-int check migrate revision up down logs ps

help: ## 显示这份清单
	@awk 'BEGIN {FS = ":.*##"} \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next } \
		/^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

##@ 新机器上手

bootstrap: env setup ## 生成 .env + 装依赖（新电脑就跑这一条）
	@echo ""
	@echo "接下来："
	@echo "  1. 把 .env 里空着的密码填上（openssl rand -base64 24 生成一个）"
	@echo "  2. make up    起 PostgreSQL / MongoDB / Redis / RabbitMQ"
	@echo "  3. make dev   起热重载的接口服务"

env: ## 从 .env.example 生成 .env（已存在就不动它）
	@if [ -f .env ]; then \
		echo "==> .env 已存在，保持不动"; \
	else \
		cp .env.example .env; \
		echo "==> 已生成 .env"; \
		echo "    现在去把空着的密码填上 —— 不填这套栈起不来，这是故意的："; \
		echo "    本仓库里没有任何服务带默认凭据。"; \
	fi

setup: ## 按 uv.lock 装依赖（含 dev）
	$(UV) sync --all-extras

##@ 开发

# --reload-dir 限定到 src：不加的话 watchfiles 盯整个项目目录，改 tests/、
# migrations/ 乃至 .claude/ 下的钩子脚本都会白重启一次，每次都要重连三个后端。
dev: ## 起接口服务，热重载（依赖得先 make up）
	$(UV) run uvicorn adpilot.main:app --reload --reload-dir src

fmt: ## 就地格式化 + 自动修可修的 lint（会改文件）
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

##@ 门禁（与 CI 同序同命令）

lint: ## ruff 检查 + 格式核对，不改文件
	$(UV) run ruff check .
	$(UV) run ruff format --check .

types: ## mypy strict
	$(UV) run mypy src tests

imports: ## 分层依赖契约：services 不许 import api 那张图
	$(UV) run lint-imports

test: ## 单元测试，不需要任何外部服务
	$(UV) run pytest

test-int: ## 集成测试，需要 make up 那套环境 + 先 make migrate
	RUN_INTEGRATION=1 $(UV) run pytest -m integration

check: lint types imports test ## 推送前跑这条：CI 卡的五道门禁，一次跑完

##@ 数据库迁移

migrate: ## 把数据库升到最新的 schema
	$(UV) run alembic upgrade head

# 生成的是**草稿**：autogenerate 认不出改名，它会给你一对 drop + add。
# 盲区清单见 docs/design/2026-08-19-schema-migration.md 第四节。
revision: ## 按 models/ 的改动生成迁移草稿：make revision m='说明'
	@test -n "$(m)" || { echo "用法：make revision m='加一列 spend_usd'"; exit 1; }
	$(UV) run alembic revision --autogenerate -m "$(m)"
	@echo ""
	@echo "==> 生成完了，现在去 migrations/versions/ 把它读一遍再提交。"

##@ 容器

up: ## 起依赖服务（后台）
	$(COMPOSE) up -d

down: ## 停容器。数据卷保留 —— 这里永远不会加 -v
	$(COMPOSE) down

logs: ## 跟 api 的日志
	$(COMPOSE) logs -f api

ps: ## 看容器状态
	$(COMPOSE) ps
