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

精确加载，不要一上来铺开整棵树。

| 任务 | 读 |
|---|---|
| 范围、里程碑、哪些是**刻意不做**的 | `docs/design/2026-08-19-mvp-design.md` |
| 为什么同时用 PostgreSQL 和 MongoDB | `src/adpilot/db/mongo.py` 模块 docstring |
| 配置、密钥、环境护栏 | `src/adpilot/config.py` |
| 连接生命周期、`app.state` 里有什么 | `src/adpilot/resources.py` |
| 加一个接口 | 先看 `src/adpilot/api/health.py` 的写法，再看 `src/adpilot/api/deps.py` |
| 本地环境、端口、服务凭据 | `docker-compose.yml` + `.env.example` |
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

## 工作方式

- **动结构先出设计文档**，再落代码，两者进同一个 PR。
- **文档指向真相源，不复述它。** 不要把表结构、env 名、接口清单抄进 markdown ——
  它们会漂移，然后 agent 读文档、信文档，最后踩坑。文档只写**为什么**和**坑**，
  **是什么**交给代码。
- **注释解释不显然的部分。** 为什么是这个超时、为什么用两个库、为什么是这个顺序。
  不要复述代码本身已经说清楚的事。
- **语言约定**：一律中文 —— 注释、docstring、本文件、设计文档、`README.md`。
  英文版是 `README.en.md`，它是**译本不是真相源**：改了 `README.md` 才去同步它，
  两边内容必须一致。

## 常用命令

```bash
uv sync --all-extras
uv run uvicorn adpilot.main:app --reload

uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run pytest                                    # 单元测试，不需要外部服务
RUN_INTEGRATION=1 uv run pytest -m integration   # 需要 compose 那套环境

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
