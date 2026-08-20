# 架构与目录结构

技术选型的来龙去脉在[设计文档第四节](../design/2026-08-19-mvp-design.md)，这里只讲
**代码怎么摆、依赖往哪个方向走**。

> **进度**：D1–D8 完成 —— 骨架、四个客户端、`models/` + Alembic 迁移、`schemas/`、
> `services/`、`providers/`、`tasks/`（含每小时的告警巡检）、`rules/`（余额可撑
> 天数、指标周同比异动）和 `notifiers/` 都已落地。只剩 `llm/` 还标着 🚧 —— 先把
> 位置定下来，是为了三个人（或三个 agent）各写各的时候不会摆出三套结构。

---

## 目录结构

```
src/adpilot/
  config.py         配置：环境变量 → Settings，凭据一律 SecretStr，无默认值
  logging.py        structlog 配置：prod 出 JSON，dev 出彩色键值对
  resources.py      进程级资源容器：连接池在这里开、在这里关
  main.py           应用工厂 + lifespan，把 Resources 挂进 app.state
  db/
    postgres.py     引擎、会话工厂、session_scope（提交/回滚的唯一出口）
    mongo.py        原始快照客户端；**双库边界写在这个模块的 docstring 里**
    redis.py        限流令牌桶与热点缓存的客户端
    broker.py       Celery 应用的构造、队列与死信队列、任务名常量
  api/
    deps.py         FastAPI 依赖 + Annotated 别名（ResourcesDep/SessionDep/…）
    errors.py       领域异常 → HTTP 状态码，**唯一的翻译点**
    pagination.py   分页入参（page / page_size，硬上限 100）
    health.py       存活与就绪探针 —— 加接口时照着它的写法抄
  models/           SQLAlchemy ORM 模型，一个领域一个文件 —— **表结构的真相源**
    types.py        自定义列类型（StrEnum 存 varchar，不用 PG 原生 ENUM）
    mixins.py       共用列（created_at / updated_at）
  schemas/          对外的 Pydantic 出入参（与 ORM 模型分开，理由见下）
    errors.py       错误响应的形状，与 FastAPI 自带的 detail 对齐
  services/         业务逻辑，不认识 HTTP
    exceptions.py   领域异常 —— 只抛这一族，状态码不在这里决定
  providers/        ReportProvider 适配器注册表（文件导入 / Meta / TikTok）
  tasks/            Celery 任务：任务体只做编排，逻辑仍在 services/
    app.py          worker 入口（`celery -A adpilot.tasks.app`）+ 重试策略基类
    runtime.py      同步的 Celery ↔ async 业务代码；**两个致命坑写在它的 docstring 里**
    alerts.py       定时巡检；排期在 db/broker.py 的 beat_schedule，要单起一个 beat 进程
  rules/            规则引擎：纯函数，数据进、判定出。**够不着数据库，契约保证**
    balance.py      余额可撑天数与告警阈值
    anomaly.py      指标周同比异动（库存断货还没做）
  notifiers/        出站通知：只管送出去，不决定要不要送
    webhook.py      通用 webhook；🔴 URL 本身是凭据
  llm/          🚧  LLM 适配器与提示词，输出必须过 Pydantic 校验
tests/
  conftest.py       夹具：offline_*（不连外部服务）与 live_*（连真实服务）
  test_*.py         单元测试；需要真实服务的挂 @pytest.mark.integration
migrations/         Alembic 版本化迁移 —— schema 演进历史的真相源
  env.py            DSN 从 Settings 取（不进 ini）；自定义类型的渲染规则也在这
  versions/         每一份迁移；删表/删列要写 # DESTRUCTIVE-OK
docs/
  design/           设计文档 —— 范围与决策的真相源
  code-rules/       本目录：怎么写
  business/         业务领域：写的是什么
```

**为什么按技术职责分目录，不按业务领域分。** 这套代码的业务实体（客户、账户、
日指标、日报）之间关系紧密，几乎每个用例都要横跨两三个实体；按领域切会让一次
改动散在四五个目录里。按职责切之后，一个实体的模型、schema、服务分别落在三个
固定位置，**文件名以实体名开头**（`models/ad_account.py` ↔ `services/ad_account.py`），
grep 一次就能找齐。

---

## 分层与依赖方向

```
config / logging          ← 谁都能 import，它们谁都不 import
      ↑
rules                     ← 纯函数：数据进、判定出。**够不着上面任何一层**
      ↑
db / providers            ← 基础设施：连接、外部系统适配
notifiers / llm           ← （同一层：入站 providers、出站 notifiers）
      ↑
models → schemas          ← 数据形状
      ↑
services                  ← 业务逻辑。**不认识 HTTP**；查好数据后调 rules
      ↑
resources                 ← 进程级资源容器
      ↑
api  ‖  tasks             ← 两个平级的入口：HTTP 请求 / RabbitMQ 消息。**互不 import**
      ↑
main                      ← 组装（只组装 api；worker 的入口是 tasks/app.py）
```

只允许自下而上依赖，五条硬规矩：

1. **`services/` 不 import `api/`。** 业务逻辑不该知道 HTTP 层存在 —— 不返回状态
   码、不碰 `Request`、不抛 `HTTPException`，只抛领域异常。理由不是洁癖：Celery
   任务和规则巡检会调同一批服务函数，那些调用方没有请求对象可给。
2. **`api/` 不写业务判断。** 只做：解析入参 → 调服务 → 把领域异常翻成状态码。
   一个 handler 里出现 `if` 套 `if` 的业务分支，说明那段该在 `services/`。
3. **`rules/` 只依赖数据，不依赖 IO。** 规则引擎收的是已经查好的数据结构，返回
   判定结果，**不自己查库**。这条是为了让「余额还能撑几天」这类计算能用一张表格
   式的参数化测试覆盖完，而不必起数据库。所以它在图上被压到了很低的位置 ——
   低到够不着 `models` / `schemas` / `db`，写一句 `select(...)` 会被契约当场拦下。
   「规则和业务逻辑是一层」说的是职责，依赖方向上是 `services` 调 `rules`。
4. **`llm/` 不做决策。** 只把结构化输入变成结构化输出，边界见
   [设计文档第五节](../design/2026-08-19-mvp-design.md)。
5. **`api/` 与 `tasks/` 互不 import。** `tasks/` 认识 `api/` 意味着 worker 里出现
   了请求对象；`api/` 认识 `tasks/` 意味着 Web 进程要把 worker 那一侧的初始化代码
   （事件循环、连接池管理）也 import 进来。接口投递任务走 `Celery.send_task`
   **按名字发**，任务名常量在 `db/broker.py`（两层都在它之上，够得着）。

> ✅ **这五条有机器强制了**：[import-linter](https://import-linter.readthedocs.io/)
> 的 layers 契约跑在 CI 里（本地是 `make imports`），配置在 `pyproject.toml`，
> **层的顺序就是上面这张图** —— 改了图就要去改那份配置，反过来也一样。
>
> 契约开着 `exhaustive`：`adpilot` 下新增一个顶层模块必须先在分层表里占个位置，
> 否则契约本身就红。这是刻意的 —— `rules/` `llm/` 都还没建，建之前先想清楚它摆在
> 哪一层，比建完之后再来考古依赖便宜得多。

---

## 数据流

```
uvicorn
  → lifespan：open_resources() 建连接池 → 挂在 app.state.resources
    → FastAPI 路由匹配 + Pydantic 校验入参
      → deps.get_resources / get_session 取句柄（不是 import 全局变量）
        → services.*（业务逻辑唯一所在）
          → models + session_scope：成功提交、异常回滚
      ← response_model 序列化出参
  → lifespan 退出：Celery → Redis → Mongo → PG 逐个关闭
```

异步链路是同一套服务函数的另一个调用方（`▸` 是已经通了的部分）：

```
celery -A adpilot.tasks.app worker
  → tasks/runtime.py：第一个任务到来时建连接池（必须在 fork 之后）
▸   → 从 adpilot 队列取消息 → 任务体解参数、开事务
▸     → services.normalize：快照 → PG daily_metrics（可拿快照重跑）
▸     → 失败：瞬时故障退避重试；数据不对 → reject 进 adpilot.dead 死信队列
      → rules 巡检 → 告警          （D7–D8）
      → llm 撰写日报 → PG reports  （D12–D13）
  → worker_process_shutdown：关连接池、关事件循环
```

**两条链路共用 `services/`，不共用 `api/`。** 所以服务函数的签名里不能有请求对象、
不能有 `Depends`。

投递方向是**单向**的：`api/` 把任务放进队列，worker 取出来跑，跑完的结果落进
result backend 由接口去查（`GET /api/tasks/{id}`）。worker **不回调**接口。

---

## 资源生命周期：为什么是 `app.state` 而不是模块级全局

四个客户端都是**进程级建一次、请求间共享**的连接池，理由与代价都写在
`resources.py` 与 `main.py` 的 docstring 里，这里只强调调用侧的规矩：

- **handler 一律通过 `deps.py` 的依赖取句柄**，不 `from adpilot.resources import ...`。
  模块级全局在测试里只能 monkeypatch，而依赖可以整个替换掉 —— `conftest.py` 的
  `offline_*` 夹具正是靠这个把「所有依赖都连不上」造出来的。
- **构造客户端不等于连上去。** 四个驱动都是懒连接，这是故意的：某个依赖短暂挂掉
  时进程仍要能起来，「连不上」该由就绪探针报出来，而不是由启动过程报出来。
- **加一个新的外部系统**（如 LLM 供应商）= 在 `db/` 或对应目录写
  `create_client(settings)`，在 `Resources` 加一个字段，在 `open_resources` 里
  建与关，在就绪探针里加一条探测，**再去 `conftest.py` 的 `offline_settings`
  补上它的 host / port**。五处，缺一处就会漏关、漏报，或者让「哪儿都连不上」那个
  夹具悄悄连上开发机上真跑着的服务。
- **worker 进程不走 `app.state`**，它有自己一套（`tasks/runtime.py`）：连接池必须
  在 fork **之后**建、事件循环必须复用同一个。两个坑都不会当场报错，只会在压力上来
  之后变成偶发故障 —— 那个模块的 docstring 是这件事的真相源。

---

## 两个数据库的边界

**钱和关系走 PostgreSQL，未经解释的原始事实走 MongoDB。**

完整理由（含反例）在 `src/adpilot/db/mongo.py` 的模块 docstring 里，那是真相源。
落到写代码时是三句话：

- 平台回传的 payload **一个字段都不要动**就落 Mongo，`raw_reports` **append-only**
- 归一化是一次**单向转换**，产物进 PG；映射规则改了或发现 bug，拿快照重跑
- **金额和需要 JOIN 的东西一律 PG**，不要图省事塞进 Mongo

---

## 加一个功能：先问它属于哪一层

| 你要加的东西 | 落在哪 | 照抄谁 |
|---|---|---|
| 一个接口 | `api/<entity>.py` + `schemas/<entity>.py` | `api/health.py`，步骤见 [`api.md`](api.md) |
| 一张表 | `models/<entity>.py` + 一份 Alembic 迁移 | 见 [`conventions.md` 数据库一节](conventions.md#数据库与迁移) |
| 一段业务逻辑 | `services/<entity>.py` | 纯 async 函数，第一个参数是 session |
| 一条判定规则（阈值、告警） | `rules/<rule>.py` | `rules/balance.py`；纯函数：数据进、判定出，不查库。查库那半边写在 `services/<rule>.py` |
| 一个数据源 | `providers/<platform>.py` | 实现 `ReportProvider` 协议，注册进注册表 |
| 一个后台任务 | `tasks/<domain>.py` | `tasks/normalize.py`；任务体只做编排，逻辑仍在 `services/`。**别忘了在 `tasks/app.py` 的 `TASK_MODULES` 里加一行**，否则 worker 认不出这个任务（有门禁盯着） |
| 一个定时任务 | 同上 + `db/broker.py` 的 `beat_schedule` | 排期要显式指定 `queue`，否则消息进默认队列没人取。跑起来还要有一个 beat 进程 |
| 一个通知通道 | `notifiers/<channel>.py` | `notifiers/webhook.py`；只管送出去，失败返回 `False` 不抛 |

**判定规则一句话：带 HTTP 语义的进 `api/`，带外部系统的进 `db/`/`providers/`，
其余都是 `services/` 或 `rules/`。**
