# 架构与目录结构

技术选型的来龙去脉在[设计文档第四节](../design/2026-08-19-mvp-design.md)，这里只讲
**代码怎么摆、依赖往哪个方向走**。

> **进度**：D1–D2 完成，D3 过半 —— 骨架（配置、三个客户端、健康检查）之外，
> `models/` + Alembic 迁移、`schemas/`、`services/` 都已落地，客户与账户这个领域
> 端到端可用。下面仍标 🚧 的目录还不存在 —— 先把位置定下来，是为了三个人
> （或三个 agent）各写各的时候不会摆出三套结构。

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
  providers/    🚧  ReportProvider 适配器注册表（文件导入 / Meta / TikTok）
  rules/        🚧  规则引擎：余额可撑天数、断货预警、指标异动。纯函数，好测
  llm/          🚧  LLM 适配器与提示词，输出必须过 Pydantic 校验
  tasks/        🚧  Celery 任务：导入、归一化、日报生成、告警巡检
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
db / providers / llm      ← 基础设施：连接、外部系统适配
      ↑
models → schemas          ← 数据形状
      ↑
services / rules          ← 业务逻辑。**不认识 HTTP**
      ↑
api                       ← 路由、依赖注入、状态码
      ↑
main                      ← 组装
```

只允许自下而上依赖，四条硬规矩：

1. **`services/` 不 import `api/`。** 业务逻辑不该知道 HTTP 层存在 —— 不返回状态
   码、不碰 `Request`、不抛 `HTTPException`，只抛领域异常。理由不是洁癖：Celery
   任务和规则巡检会调同一批服务函数，那些调用方没有请求对象可给。
2. **`api/` 不写业务判断。** 只做：解析入参 → 调服务 → 把领域异常翻成状态码。
   一个 handler 里出现 `if` 套 `if` 的业务分支，说明那段该在 `services/`。
3. **`rules/` 只依赖数据，不依赖 IO。** 规则引擎收的是已经查好的数据结构，返回
   判定结果，**不自己查库**。这条是为了让「余额还能撑几天」这类计算能用一张表格
   式的参数化测试覆盖完，而不必起数据库。
4. **`llm/` 不做决策。** 只把结构化输入变成结构化输出，边界见
   [设计文档第五节](../design/2026-08-19-mvp-design.md)。

> ⚠️ **这四条目前没有机器强制**，靠 review 守着 —— 和本仓库「规矩要写成门禁」的
> 主张有出入，属于已知欠账。落地 `services/` 的那个 PR 里补
> [import-linter](https://import-linter.readthedocs.io/) 并挂进 CI，契约就是上面
> 这张图。在那之前，改动跨了层要在 PR 描述里说明。

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
  → lifespan 退出：Redis → Mongo → PG 逐个关闭
```

规划中的异步链路（D6 起）是同一套服务函数的另一个调用方：

```
Celery worker（RabbitMQ 取任务）
  → providers.fetch() 拉平台原始响应
    → Mongo raw_reports 落快照（append-only，永不更新）
      → normalize：字段映射 → PG daily_metrics（可拿快照重跑）
        → rules 巡检 → 告警
        → llm 撰写日报 → PG reports
```

**两条链路共用 `services/`，不共用 `api/`。** 所以服务函数的签名里不能有请求对象、
不能有 `Depends`。

---

## 资源生命周期：为什么是 `app.state` 而不是模块级全局

三个客户端都是**进程级建一次、请求间共享**的连接池，理由与代价都写在
`resources.py` 与 `main.py` 的 docstring 里，这里只强调调用侧的规矩：

- **handler 一律通过 `deps.py` 的依赖取句柄**，不 `from adpilot.resources import ...`。
  模块级全局在测试里只能 monkeypatch，而依赖可以整个替换掉 —— `conftest.py` 的
  `offline_*` 夹具正是靠这个把「所有依赖都连不上」造出来的。
- **构造客户端不等于连上去。** 三个驱动都是懒连接，这是故意的：某个依赖短暂挂掉
  时进程仍要能起来，「连不上」该由就绪探针报出来，而不是由启动过程报出来。
- **加一个新的外部系统**（如 RabbitMQ、LLM 供应商）= 在 `db/` 或对应目录写
  `create_client(settings)`，在 `Resources` 加一个字段，在 `open_resources` 里
  建与关，在就绪探针里加一条探测。四处，缺一处就会漏关或漏报。

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
| 一条判定规则（阈值、告警） | `rules/<rule>.py` | 纯函数：数据进、判定出，不查库 |
| 一个数据源 | `providers/<platform>.py` | 实现 `ReportProvider` 协议，注册进注册表 |
| 一个后台任务 | `tasks/<domain>.py` | 任务体只做编排，逻辑仍在 `services/` |

**判定规则一句话：带 HTTP 语义的进 `api/`，带外部系统的进 `db/`/`providers/`，
其余都是 `services/` 或 `rules/`。**
