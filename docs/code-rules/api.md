# 接口约定与「加一个接口」

`src/adpilot/api/health.py` 是现成的范本，加接口前先读它。本篇写它没法自我说明的
部分：命名、契约、以及为什么某几件事必须做。

---

## OpenAPI 是对外产物，不是副产品

两个前端（uni-app 客户端、Vue 内部后台）都从 `/openapi.json` 生成请求代码和类型。
所以 schema 坏掉不是「文档不好看」，是**下游编译不过**。`tests/test_health.py` 里
有一条 `test_openapi_schema_is_generated` 守着这件事，CI 会红。

推论是三条硬要求：

1. **每个接口都要写 `response_model`。** 不写的话 OpenAPI 里那个接口没有响应
   schema，前端只能拿到 `any`，类型契约当场作废。
2. **面向客户端的接口显式写 `operation_id`**，用小驼峰动宾式（`listDailyMetrics`、
   `createImportJob`）。不写的话 FastAPI 会按函数名 + 路径 + 方法拼一个
   （`live_api_health_live_get`），生成出来的客户端方法名既丑又会随路径变动。
   健康探针是例外 —— 没有客户端会为它生成代码。
3. **入参出参一律 Pydantic 模型**，不用裸 `dict`。`dict` 在 OpenAPI 里是
   `additionalProperties: true`，等于没有契约。

---

## 命名

| 东西 | 约定 | 例 |
|---|---|---|
| 路由文件 | 一个领域一个文件，文件名即实体名 | `api/daily_metrics.py` |
| router 变量 | 固定叫 `router` | `router = APIRouter(tags=["metrics"])` |
| 出入参模型 | 实体名 + `Request` / `Response`，放 `schemas/` | `DailyMetricsQueryRequest` |
| 列表出参 | 实体名 + `ListResponse`，字段固定 `items` + `total` | `ClientListResponse` |
| handler 函数 | 动词开头、小写下划线 | `list_daily_metrics` |
| `tags` | 一个领域一个标签，小写 | `tags=["metrics"]` |

**`tags` 决定 Swagger 的分组，也决定[业务文档](../business/BUSINESS.md)按什么切分**
—— 新增一个标签就意味着要新增一篇业务文档。

**ORM 模型和出参 schema 分开写，不要让 ORM 对象直接当响应模型。** 两者变化的原因
不同：加一个内部字段（成本、供应商 ID）不该自动出现在客户端能看见的响应里。要从
ORM 对象构造出参就在 schema 上开 `model_config = ConfigDict(from_attributes=True)`。

---

## 依赖注入

**一律用 `api/deps.py` 里的 `Annotated` 别名**，不要在 handler 上手写
`Depends(...)`：

```python
from adpilot.api.deps import SessionDep, SettingsDep
```

理由是可替换性 —— 测试要替掉的是**依赖**，别名让替换点只有一处。新增外部系统时
在 `deps.py` 里加，不在 handler 里现取。

**认证也是依赖**，但两组接口挂法不同：

- **内部接口不写**认证依赖 —— `main.py` 按 router 统一挂 `require_operator`，
  加一组路由时去那个循环里加一行。写在 handler 上漏掉一个不会有任何报错。
- **客户端接口（`/api/portal/*`）每个 handler 都要写 `ClientScopeDep`**，因为它有
  返回值（`client_id`），handler 要用它去过滤。

两边都有门禁盯着（`tests/test_auth_guard.py`）：每个接口要么带 security、要么在
一份显式的豁免清单里；`/api/portal/` 下的还必须是 `ClientBearer`。

**handler 不自己开事务。** `SessionDep` 背后是 `db/postgres.py` 的 `session_scope`：
正常返回就提交、抛异常就回滚。业务代码里出现 `session.commit()` 通常说明走错了路。

---

## 状态码与错误

- 查询 200；创建 201 并在响应里带回创建出来的资源；删除 204
- 入参不合法 → Pydantic 自动 422，**不要手写校验分支**
- 资源不存在 → 404；业务规则不允许 → 409 或 422，由 `api/` 层从领域异常翻译，
  翻译规则见 [`conventions.md` 错误处理](conventions.md#错误处理)
- 没带 token / token 无效或过期 → 401，**响应体固定一句话**，不区分是哪一种
- 依赖不可用、或服务端没配 `AUTH_SECRET` → 503（就绪探针已经是这个语义）
- **越权一律 404，不是 403** —— 403 等于承认「那个资源存在，只是不给你看」
- **响应体里不放内部错误细节**，理由见 `api/health.py` 里 `_run_probe` 的 docstring

---

## 分页

> 🚧 **约定先行，等 D3 的第一个列表接口落地。** 先写在这里是因为两个前端要按同一
> 套形状写分页组件，各写各的就得返工。

- 入参 `page`（从 1 起）+ `page_size`（默认 20，**硬上限 100**，超限 422）
- 出参固定 `{"items": [...], "total": <int>}`
- 数据量涨到 offset 扛不住时，**加新接口做游标分页**，不改既有接口的形状

---

## 加一个接口

以「按账户查日指标」为例，四步，每步都有现成的东西可照抄：

**① `schemas/daily_metrics.py`** —— 出入参形状

```python
class DailyMetricsItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stat_date: date
    spend: Decimal  # 金额一律 Decimal，序列化出去是字符串
    impressions: int
    conversions: int


class DailyMetricsListResponse(BaseModel):
    items: list[DailyMetricsItem]
    total: int
```

**② `services/daily_metrics.py`** —— 业务逻辑，不认识 HTTP

```python
async def list_by_account(
    session: AsyncSession,
    account_id: int,
    start: date,
    end: date,
) -> tuple[list[DailyMetrics], int]: ...
```

第一个参数是 session，返回领域对象。**不返回状态码、不抛 `HTTPException`** ——
Celery 任务也会调它，那边没有请求对象。

**③ `api/daily_metrics.py`** —— 路由

```python
router = APIRouter(tags=["metrics"])


@router.get(
    "/accounts/{account_id}/daily-metrics",
    response_model=DailyMetricsListResponse,
    operation_id="listDailyMetrics",
)
async def list_daily_metrics(
    account_id: int,
    session: SessionDep,
    start: date,
    end: date,
) -> DailyMetricsListResponse:
    """按天返回某个账户的归一化指标。

    `stat_date` 是广告账户时区下的自然日，口径见 docs/business/glossary.md。
    """
    rows, total = await services.daily_metrics.list_by_account(session, account_id, start, end)
    return DailyMetricsListResponse(
        items=[DailyMetricsItem.model_validate(row) for row in rows],
        total=total,
    )
```

**④ `main.py`** —— 注册一行

```python
app.include_router(daily_metrics.router, prefix=settings.api_prefix)
```

然后：**写测试**（至少一条成功路径 + 一条失败路径），**跑门禁**（见
[`conventions.md` 改完必做](conventions.md#改完必做)），**如果引入了新的 `tags`，
去 [`docs/business/`](../business/BUSINESS.md) 加一篇业务文档**。

---

## 踩坑速查

| 症状 | 原因 |
|---|---|
| 前端拿到的类型是 `any` | 接口漏了 `response_model` |
| 客户端方法名叫 `list_..._get` | 漏了 `operation_id` |
| 金额到前端变成浮点、丢精度 | 出参 schema 用了 `float` 而不是 `Decimal` |
| 响应里多出了本不该暴露的字段 | 拿 ORM 模型直接当了 `response_model` |
| 提交了一半的数据 | handler 里手写了 `commit()`，绕开了 `session_scope` |
| 接口偶发全体变慢 | `async def` 里混进了阻塞调用，事件循环被卡住 |
| 日期差一天 | 用了服务器本地时区，没走账户时区的 `stat_date` 口径 |
