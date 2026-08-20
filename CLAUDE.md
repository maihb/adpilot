# CLAUDE.md

给 Claude Code（以及其它编码 agent）在本仓库工作时的指引。

## 这是什么

自托管的 Meta / TikTok 广告投放数据中台。不是 SaaS：每个使用者部署自己的实例，
连自己的广告账户。

**范围与决策的真相源是设计文档：**
[`docs/design/2026-08-19-mvp-design.md`](docs/design/2026-08-19-mvp-design.md)。
动结构之前先读它。如果一处改动与它冲突，在同一个 commit 里把文档一起改掉 ——
过期的设计文档比没有更糟。

## 任务 → 读哪个文件

精确加载，不要一上来铺开整棵树。文档分三层：`design/` 是**为什么这么定**，
`code-rules/` 是**怎么写**，`business/` 是**写的是什么**。

| 任务 | 读 |
|---|---|
| 范围、里程碑、哪些是**刻意不做**的 | [`docs/design/2026-08-19-mvp-design.md`](docs/design/2026-08-19-mvp-design.md) |
| **加表 / 改字段、迁移怎么生成、autogenerate 有哪些盲区** | [`docs/design/2026-08-19-schema-migration.md`](docs/design/2026-08-19-schema-migration.md) |
| **代码摆在哪、依赖往哪个方向走** | [`docs/code-rules/architecture.md`](docs/code-rules/architecture.md) |
| 金额 / 时区 / 类型 / 异步 / 测试 / 日志 / 配置密钥 / **改完必做清单** | [`docs/code-rules/conventions.md`](docs/code-rules/conventions.md) |
| **加一个接口**（含可照抄的四步与踩坑速查） | [`docs/code-rules/api.md`](docs/code-rules/api.md) |
| 分支 / 提交信息 / **推送前自检** / 需明确指令的操作 | [`docs/code-rules/git-workflow.md`](docs/code-rules/git-workflow.md) |
| **加一个后台任务**（重试与死信怎么分流、worker 里的两个致命坑） | [`docs/business/tasks.md`](docs/business/tasks.md) |
| **加一条规则或告警**（状态机怎么去重、通知怎么不重复打扰） | [`docs/business/alerts.md`](docs/business/alerts.md) |
| **指标口径、时区、数据回填、告警公式** | [`docs/business/glossary.md`](docs/business/glossary.md) |
| 某个业务领域现在做到哪、规则是什么 | [`docs/business/BUSINESS.md`](docs/business/BUSINESS.md) |
| 为什么同时用 PostgreSQL 和 MongoDB | `src/adpilot/db/mongo.py` 模块 docstring |
| 配置、密钥、环境护栏 | `src/adpilot/config.py` |
| 连接生命周期、`app.state` 里有什么 | `src/adpilot/resources.py` |
| 接口怎么写 | 先看 `src/adpilot/api/health.py`，再看 `src/adpilot/api/deps.py` |
| 本地环境、端口、服务凭据 | `docker-compose.yml` + `.env.example` |
| 新机器上手、有哪些快捷命令（人用，非 agent） | `Makefile`，或 `make help` |
| CI 到底卡哪些门禁 | `.github/workflows/ci.yml` |

## 不可协商的规矩

这几条之所以立成硬规矩，是因为破了代价大、而且 review 时很难发现。

1. **任何密钥都不进仓库。** 凭据、token、真实客户名、广告账户 ID、导出的报表，
   一律不进 git —— 代码里不行，测试里不行，fixture 里不行，文档里也不行。
   这是公开仓库。「以后再清理」不成立：git 历史会留住你删掉的东西，真要清干净
   得重写每一个 commit hash。

2. **不给凭据留默认值。** 密码缺失就必须让启动大声失败。永远不要写
   `password or "postgres"` 这种兜底 —— 一个空密码也能起来的栈，迟早会被谁
   那样发到线上。

3. **LLM 只建议，不执行。** 改预算、关广告、调出价 —— 一律只产出建议，绝不代为
   执行。凡是确定性规则能算出来的，就必须写成规则而不是提示词。见设计文档第五节。

4. **原始快照 append-only。** Mongo 里 `raw_reports` 的文档永不更新、永不删除。
   它既是审计留痕，也是重跑归一化的输入。

5. **金额一律 `numeric` / `Decimal`。** 凡是沾钱的地方，永远不要用浮点。

6. **每道门禁都要跑在 CI 里。** 一条规矩重要，就把它写成 lint、类型检查或测试。
   只活在文字里的约定一定会腐化。

## 护栏一览

上面那几条规矩之所以立得住，是因为它们大多不靠自觉 —— 已经变成了机器判定。
**被拦到属正常，读拦截提示照做即可，不要绕。**

| 约束 | 强制机制 |
|---|---|
| ruff / format / mypy / lint-imports / pytest 五道门禁 | `.github/workflows/ci.yml`；改过 `.py` 或 `.md` 时 [`.claude/stop-hook.sh`](.claude/stop-hook.sh) 在本地先跑一遍 |
| 刚写完的文件保持格式 | [`.claude/format-hook.sh`](.claude/format-hook.sh) 就地跑 `ruff format`。**`.md` 也在内** —— ruff 连 Markdown 里的 Python 代码块一起格式化，漏了它同样让 CI 红 |
| **凭据不进对话上下文** | `.claude/settings.json` 的 `Read(.env)` deny + 命令守卫拦下 `cat`/`head`/`less` 读 `.env`，`gh auth token` 同样不放行。要看有哪些配置项去 `.env.example` |
| **凭据不进公开历史** | `.gitignore` + 命令守卫拦下 `git add .env` 与 `git add -f`。**挡不住「把密钥粘进代码里」**，那只能靠提交前看一眼 `git diff --staged` |
| 依赖只经 uv 装 | 守卫拦下 `pip install`，并给出 `uv add` 的写法 |
| 命令只在项目环境里跑 | 守卫拦下裸 `pytest` / `mypy` / `ruff` / `uvicorn`，提示走 `uv run` |
| **make 只放已授权的 target** | `settings.json` 逐条精确列出；`Bash(make:*)` 刻意不加 —— target 里能写任意命令，通配一条就等于给守卫开后门。新 target 默认不授权 |
| **celery 只放 worker / beat 子命令** | `settings.json` 里逐条列出，不是 `uv run celery:*` —— 同一个 CLI 底下还有 `purge`（清空队列）和 `control`（远程指挥 worker） |
| **worker / beat 的启动参数不许被删** | `tests/test_tasks.py` 同时盯着 Makefile 和 compose：`--without-mingle` / `--without-gossip`（少了 RabbitMQ 4 上起不来）、`-Q adpilot`（少了一条消息都不处理）、以及「有没有 beat 的启动方式」 |
| **不监听端口的服务要显式关掉 healthcheck** | 同上那个文件。`HEALTHCHECK` 在 Dockerfile 里是**镜像级**的，三个进程共用一个镜像 —— worker / beat 不禁用就永远 unhealthy，而一个恒为红的健康灯会让人对整列输出脱敏 |
| **示例数据一眼看得出是假的** | `tests/test_seed.py`：客户名要 `示例｜` 前缀、账户与系列 ID 要 `demo-` 前缀。同一个文件还盯着「跑完恰好两条告警」—— 那个数字写在 README 里 |
| **迁移不悄悄删数据** | `tests/test_migration_safety.py`：`upgrade()` 里出现删表/删列，就必须在文件里写一行 `# DESTRUCTIVE-OK: <理由>`。只扫 `upgrade()` —— `downgrade()` 里的 drop 是回滚，每个建表迁移都有 |
| **改了 model 别忘了生成迁移** | `alembic check` 跑在 CI 的集成 job 里（要连真实库） |
| **分层依赖不许倒流** | `lint-imports`（import-linter）跑在 CI 里，契约就是 [`architecture.md`](docs/code-rules/architecture.md#分层与依赖方向) 那张图，配置在 `pyproject.toml`。`exhaustive` 开着 —— 新增顶层模块必须先在分层图里占个位置，不能先建了再说 |
| **加了接口别忘了业务文档** | `tests/test_business_docs.py`：OpenAPI 里的每个 tag 都要在 [`BUSINESS.md`](docs/business/BUSINESS.md) 的索引表里登记，**双向都查**（登记了却没接口也红），外加一条死链检查 |
| 数据卷不被顺手删掉 | 守卫拦下 `docker compose down -v` |
| **push 要人明确说** | `settings.json` 里 `Bash(git push:*)` 是 deny。用户说「提交」只意味着 commit |
| 只读命令不该反复弹窗 | 守卫按「拆出每个命令位置的可执行名，全部只读才放行」自动放行，边界写在 [`.claude/bash_guard.py`](.claude/bash_guard.py) 的模块 docstring 里 |
| 守卫自己判错 | `tests/test_bash_guard.py` 跟着 CI 跑 —— 误放一次写操作和误拦一次正常提交，两个方向都有用例 |

**仍然只能靠 review 的两件事**（写在这里是为了不假装它们已经被管住）：业务文档
**写得对不对** —— 机器只验得了「tag 登记了没有」和「文件在不在」，验不了内容；
以及**把密钥粘进代码里** —— `.gitignore` 拦得住文件，拦不住字符串，那只能靠提交
前看一眼 `git diff --staged`。

## 工作方式

- **动结构先出设计文档**，再落代码，两者进同一个 PR。
- **文档指向真相源，不复述它。** 不要把表结构、env 名、接口清单抄进 markdown ——
  它们会漂移，然后 agent 读文档、信文档，最后踩坑。文档只写**为什么**和**坑**，
  **是什么**交给代码。
- **注释解释不显然的部分。** 为什么是这个超时、为什么用两个库、为什么是这个顺序。
  不要复述代码本身已经说清楚的事。
- **改了业务规则，回头更新 [`docs/business/`](docs/business/) 里对应那篇**；改了
  计算口径（阈值、取哪个转化事件、回溯几天）更新
  [`glossary.md`](docs/business/glossary.md)。那一层一旦不可信，就没人敢照着它跳过
  源码，等于白写。
- **语言约定**：一律中文 —— 注释、docstring、本文件、设计文档、`README.md`。
  英文版是 `README.en.md`，它是**译本不是真相源**：改了 `README.md` 才去同步它，
  两边内容必须一致。

## 常用命令

[`Makefile`](Makefile) 是同一批命令的短名字（`make help` 看清单），下面是它展开成的
原命令。两者都能用，但**改了任一边就要同步另一边**。

能跑哪些 make target，`.claude/settings.json` 里**逐条**列着 —— 精确匹配，不是
`Bash(make:*)` 通配。判据是「展开后的原命令本身已获授权」，所以 make 没有放大权限。
两个推论：**新加的 target 不会自动获授权**（要用就去 settings.json 补一条），
**`make env` / `make bootstrap` 刻意不在里面**（碰 `.env`，且跑完要人填密码）。

```bash
uv sync --all-extras
uv run uvicorn adpilot.main:app --reload --reload-dir src   # 只盯 src，改测试不白重启

# Celery worker。三个参数一个都不能省，理由见 db/broker.py：-Q 不指定就去消费默认
# 的 celery 队列（于是一条消息都不处理）；mingle/gossip 会建 RabbitMQ 4 拒绝声明的
# 队列（于是 worker 疯狂重连后死掉）。
uv run celery -A adpilot.tasks.app worker --loglevel=info -Q adpilot \
    --without-mingle --without-gossip

# 定时排期。**worker 和 beat 两个都要起** —— 只起 worker 的症状是「告警一条都不来」，
# 而那看起来跟「一切正常」一模一样。只能有一个 beat 实例，起两个就是每个排期投两遍。
uv run celery -A adpilot.tasks.app beat --loglevel=info \
    --schedule=/tmp/adpilot-celerybeat-schedule

uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run lint-imports                              # 分层依赖契约，图见 architecture.md
uv run pytest                                    # 单元测试，不需要外部服务
RUN_INTEGRATION=1 uv run pytest -m integration   # 需要 compose 那套环境 + 先迁移

# 脱敏示例数据。只添不改（重复跑安全），ENVIRONMENT=prod 时拒绝执行且**没有
# --force**。四个示例账户各演示一种规则结局，跑完巡检应当恰好两条告警 ——
# 那个数字有测试盯着（tests/test_seed.py）。
uv run python -m adpilot.seed

uv run alembic upgrade head                      # 把库升到最新 schema
uv run alembic revision --autogenerate -m "..."  # 生成迁移草稿，**必须人看一遍**
uv run alembic check                             # 改了 model 却忘了生成迁移就报错

docker compose up -d
docker compose logs -f api
```

## 已经踩过的坑（别再踩一遍）

- **`postgres:18` 挪了数据目录。** 卷要挂在 `/var/lib/postgresql`，**不是** 17
  及以前的 `/var/lib/postgresql/data`。挂错了容器能起来，但自检失败报
  "unhealthy"，真正的原因埋在日志里。
- **`README.md` 是包元数据。** `pyproject.toml` 的 `readme` 字段指着它，所以
  Docker 构建必须 `COPY` 它，否则 hatchling 报 `Readme file does not exist`。
- **宿主机端口容易撞。** 5432 / 6379 / 27017 / 5672 经常已经被别的栈或 SSH 隧道
  占了。这些都能在 `.env` 里改，容器之间走服务名通信，不受影响。
