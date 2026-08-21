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

.PHONY: help bootstrap env setup dev worker beat seed lint fmt types imports test test-int check migrate revision up rebuild down logs ps openapi client client-check

help: ## 显示这份清单
	@awk 'BEGIN {FS = ":.*##"} \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next } \
		/^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-13s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

##@ 新机器上手

bootstrap: env setup ## 生成 .env + 装依赖（新电脑就跑这一条）
	@echo ""
	@echo "接下来："
	@echo "  1. 把 .env 里空着的密码填上（openssl rand -base64 24 生成一个）"
	@echo "  2. make up      起 PostgreSQL / MongoDB / Redis / RabbitMQ"
	@echo "  3. make dev     起热重载的接口服务"
	@echo "  4. make worker  另开一个终端，起 Celery worker"
	@echo "  5. make beat    再开一个，起定时排期（告警巡检靠它）"

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

# 三个参数一个都不能省，理由各不相同：
#
#   -Q adpilot         不指定队列的话 Celery 去消费名为 celery 的默认队列，而任务
#                      全投在 adpilot 上 —— worker 安静地跑着、一条消息都不处理。
#   --without-mingle   这两步会创建「非持久 + 非独占」的队列，RabbitMQ 4 默认拒绝
#   --without-gossip   （废弃特性 transient_nonexcl_queues），worker 会疯狂重连后
#                      死在 RestartFreqExceeded 上。它们是命令行开关、进不了配置，
#                      所以只能写在这里 —— db/broker.py 那段注释是完整解释。
worker: ## 起 Celery worker，消费 adpilot 队列（依赖得先 make up）
	$(UV) run celery -A adpilot.tasks.app worker --loglevel=info -Q adpilot \
		--without-mingle --without-gossip

# beat 是**排期进程**，不是 worker：它只负责按点把任务投进队列，真正干活的还是
# worker。两个都要起 —— 只起 worker 的症状是「告警一条都不来」，而那看起来跟
# 「一切正常」一模一样。
#
# --schedule 落到 /tmp：beat 要记住「上次是什么时候投的」，默认写在当前目录下，
# 于是仓库里会多出一个 celerybeat-schedule 文件（.gitignore 兜着，但没必要生成）。
beat: ## 起 Celery beat，按排期投巡检任务（依赖得先 make up）
	$(UV) run celery -A adpilot.tasks.app beat --loglevel=info \
		--schedule=/tmp/adpilot-celerybeat-schedule

# 要先 make migrate —— 表还没建出来的时候跑它，报的是 UndefinedTable，跟「示例数据
# 有问题」看不出关系。
#
# 只添不改、重复跑安全；ENVIRONMENT=prod 时直接拒绝，且**没有 --force**。
# 完整说明在 src/adpilot/seed.py 的模块 docstring。
seed: ## 灌一批脱敏示例数据（先 make migrate）
	$(UV) run python -m adpilot.seed

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

##@ 客户端（uni-app）

# 后端改了出参形状之后**必须**跑它。CI 的 frontend job 会用 git diff --exit-code
# 判生成出来的 .ts 有没有跟着变，不同步就红 —— 这是前后端同仓库换来的主要好处，
# 分仓的话这条只能靠人盯。
#
# 导出走离线的 `app.openapi()`，**不起服务**。起服务再 curl /openapi.json 那条路
# 有两个问题：它要连数据库（lifespan 会开连接池），而且那个路由在生产环境本来就是
# 关掉的（main.py 里 openapi_url=None）。离线这条不触发 lifespan、也不读那个路由，
# 于是不需要任何依赖、任何凭据。
openapi: ## 导出 openapi.json 并重新生成前端 TS 类型
	$(UV) run python -c "import json,sys; from adpilot.main import create_app; json.dump(create_app().openapi(), sys.stdout, ensure_ascii=False)" > client/openapi.json
	npm --prefix client run gen:api

# H5 走 vite 的 dev proxy 连后端（见 client/vite.config.ts），所以要先 make dev。
# 微信小程序端是 npm --prefix client run dev:mp-weixin，产物用微信开发者工具打开。
client: ## 起客户端 H5 开发服务器（先 make up + make dev）
	npm --prefix client run dev:h5

# 客户端那两道门禁。type-check 吃的是上面生成的类型，所以后端改了形状而没跑
# make openapi 的话，会在这里表现成「前端用了不存在的字段」。
client-check: ## 客户端门禁：vue-tsc + 纯函数单测
	npm --prefix client run type-check
	npm --prefix client test

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

rebuild: ## 改了代码或依赖之后：重建镜像再换上去
	# `up -d` 认的是「容器在不在跑」，不是「镜像新不新」—— 代码改了它照样把旧镜像
	# 拉起来，症状是「我明明改了，接口还是老样子」，而且没有任何提示。
	# 三个应用进程共用一个镜像，所以这一条会把 api / worker / beat 一起换掉。
	$(COMPOSE) up -d --build

down: ## 停容器。数据卷保留 —— 这里永远不会加 -v
	$(COMPOSE) down

logs: ## 跟 api 的日志
	$(COMPOSE) logs -f api

ps: ## 看容器状态
	$(COMPOSE) ps
