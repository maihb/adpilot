# 编码规范与项目约定

只写**不显然的部分**和**踩过的坑**。工具能管的（行宽、import 排序、引号）交给
`ruff`，配置在 `pyproject.toml`，不在这里复述。

---

## 语言

- **注释、docstring、文档、提交信息一律中文。** `README.en.md` 是译本不是真相源。
- **标识符、日志事件名、字段名一律英文 snake_case。** 日志事件名要能 grep：
  写 `log.info("import_completed", rows=1200)`，不要写
  `log.info(f"导入完成，共 {n} 行")` —— 后者一旦拼进自然语言就搜不到了。
- ruff 的 `RUF001/002/003`（易混淆字符）已在 `pyproject.toml` 里关掉：那三条本意
  是防西里尔字母冒充拉丁字母，对中文全角标点是纯误报。

---

## 类型

`mypy --strict` 跑在 CI 上，`src` 和 `tests` 都要过。

- **每个文件首行 `from __future__ import annotations`**，注解不在运行时求值，
  循环 import 和前向引用都省事。
- **`# type: ignore` 必须带理由**：`# type: ignore[arg-type]  # motor 的存根缺这个重载`。
  裸 ignore 会把一整类错误永久藏起来。
- **第三方库缺存根**在 `pyproject.toml` 的 `[[tool.mypy.overrides]]` 里显式列出
  （目前只有 `motor.*`），不要用全局 `ignore_missing_imports`。
- **不用 `Any` 兜底**。真的动态就用 `object` 再收窄，至少编译器会逼你写检查。

---

## 异步

整套栈是 async 的（asyncpg / motor / redis.asyncio），所以：

- **`async def` 里不许出现阻塞调用。** `requests`、`time.sleep`、同步驱动、大文件
  读写 —— 事件循环被卡住时症状是**全局变慢**，不是某个接口变慢，排查起来很贵。
  非用不可就 `await asyncio.to_thread(...)`。
- **CSV / Excel 解析是 CPU 密集**，导入任务跑在 Celery worker 里而不是请求线程里，
  正是为了这个。
- **并发探测用 `asyncio.gather`**，并且每个分支自己吞异常 —— `api/health.py` 的
  `_run_probe` 是范本：一个依赖挂掉应该表现为一条 unhealthy 记录，而不是把整个
  响应带崩。

---

## 金额

**一律 `decimal.Decimal`，禁用 `float`。** 广告花费、单价、结算金额、汇率都算。

- 数据库列用 `Numeric(20, 4)`，不用 `Float`/`Double`
- **不要用 `float` 中转**。`Decimal(str(x))` 而不是 `Decimal(x)`；从 CSV 读进来的
  数字**直接给 `Decimal(原始字符串)`**，别先 `float()` 一下
- **JSON 里金额是字符串。** Pydantic 序列化 `Decimal` 出来是 `"12.34"`，前端按
  字符串处理，不能假设固定小数位，更不要用浮点解析
- **PostgreSQL 的 `numeric(20,4)` 对超出精度的写入做四舍五入**（round half away
  from zero），不报错也不截断。ROAS、CPA 这类除法结果会乘出多位小数，注意它

理由不必展开：钱算错一次，这个项目输出的所有数字就都不可信了。

---

## 时间与时区

三个时区可以互不相同：**广告账户时区、店铺时区、看报表的人所在时区**。这不是假设，
是真实会遇到的情况（设计文档第六节记了一个日切点差 1 小时的例子）。

- **`stat_date` 一律是「广告账户时区下的自然日」**，类型 `date`，账户时区记在
  `ad_accounts` 上。口径的完整定义在
  [`docs/business/glossary.md`](../business/glossary.md)
- **所有 `datetime` 必须带时区**（aware），存 `TIMESTAMP WITH TIME ZONE`。
  裸 naive datetime 一律视为 bug
- **不用 `datetime.utcnow()`**（它返回 naive），用 `datetime.now(UTC)`
- **日报必须注明口径**。不注明的话，客户拿他自己后台的数字来对，永远差一截 ——
  而这是解释不清的差异，不是数据错误

---

## 配置与密钥

真相源是 `src/adpilot/config.py`，它的 docstring 写了那两条规矩的理由。使用侧：

- **不加包级全局配置变量**，配置经 `Settings` 显式传参或走依赖注入
- **凭据一律 `SecretStr`，且不给默认值。** 缺了就让启动大声失败。永远不要写
  `password or "postgres"` —— 一个空密码也能起来的栈，迟早会被谁那样发到线上
- **读值必须显式 `.get_secret_value()`**，这样 review 时一 grep 就能找全所有
  接触明文凭据的地方
- **连接串一律由零件拼**（`*_HOST` / `*_PORT` / 账号密码），`Settings` 里不存在
  「整条 URI」那种配置项。收整条 URI 会让同一个端口有两处真相，而不一致的症状是
  「compose 里跑得好好的，本机连到别的服务上去了」—— 不报错，只是结果不对
- **拼串时密码要 percent-encode**。README 让人用 `openssl rand -base64 24` 生成
  密码，base64 里有 `/`，一个斜杠就能把 DSN 从那里截断。拼装统一走 `config.py`
  的 property，别在调用点自己拼
- 那几个 property 的返回值**带着明文密码**，只喂给驱动，不进日志、不进异常消息

**加一个配置项要动四处**，漏一处的症状各不相同：

| 动哪 | 漏了会怎样 |
|---|---|
| `config.py` 的 `Settings` | 代码里根本读不到 |
| `.env.example` | 别人 clone 下来不知道要填什么；**本机直接跑时也读它** —— 漏了的项会静默落到 `Settings` 里的默认值，连到一个你没想到的地方去 |
| `docker-compose.yml` | 容器里没有，本机跑得通、compose 起不来 |
| `.github/workflows/ci.yml`（若集成测试要用） | CI 红，本地绿 |

**🔴 凭据一个字都不进仓库** —— 代码、测试、fixture、文档里都不行。这是公开仓库，
完整边界见[设计文档第八节](../design/2026-08-19-mvp-design.md)。CI 里那几个
`ci-not-a-secret` 是跑完即销毁的一次性容器凭据，不是例外，别照着它往别处填值。

---

## 错误处理

- **`services/` 抛领域异常**（自定义异常类），不抛 `HTTPException`，不返回状态码
- **`api/` 负责翻译**：领域异常 → HTTP 状态码 + 响应体。这一层不写业务判断
- **不把内部错误信息回给客户端。** `api/health.py` 的就绪探针是范本：失败只上报
  异常类名，因为驱动的报错信息里可能带着 DSN 或主机名，而探针接口通常不需要认证
- 入参校验交给 Pydantic，自动 422，不要手写校验分支

---

## 日志

- **结构化调用**：`log.info("event_name", key=value)`，事件名是稳定的英文标识符
- **不写凭据、token、完整 payload、客户可识别信息**。要留痕就记 ID 和计数
- **异常用 `log.exception` 或带 `exc_info=True`**，别只记 `str(exc)` —— 丢了栈就
  只剩一句没有上下文的报错

---

## 测试

两类，界限清楚（`tests/conftest.py` 的 docstring 是真相源）：

- **单元测试**：不碰任何外部服务，`uv run pytest` 在一台只装了 Python 的机器上
  必须全绿。用 `offline_*` 夹具
- **集成测试**：挂 `@pytest.mark.integration`，需要 compose 那套环境，本地默认跳过，
  `RUN_INTEGRATION=1` 才跑。用 `live_*` 夹具。**默认跳过是刻意的**：不然「没起
  数据库」看起来会像「测试挂了」

什么必须有测试：

| 一定要 | 为什么 |
|---|---|
| `rules/` 的每条规则 | 它决定要不要发告警，且是纯函数，参数化测试成本极低 |
| 归一化的字段映射 | 平台字段会漂移，这里是漂移的唯一收口点 |
| 金额相关的往返（写库 → 读回） | 验的是「`Decimal` + `numeric` 这条链路不丢精度」 |
| 每个新接口的失败路径 | 成功路径一跑就知道，失败路径不写就没人跑过 |

**不为覆盖率写测试。** 断言要落在「行为对不对」上，不是「这行执行过没有」。

---

## 数据库与迁移

- **ORM 模型是表结构的真相源**，schema 变更一律经 Alembic 生成迁移，**不手改
  数据库**、不手写 DDL
- **生成的迁移要人工 review 再提交** —— autogenerate 认不出重命名，它会给你一对
  drop + add，跑下去数据就没了
- **`raw_reports` append-only**：只 insert，永不 update、永不 delete。它既是审计
  留痕，也是重跑归一化的输入
- 唯一键该建就建。`daily_metrics` 的 `(account_id, level, object_id, stat_date)`
  是幂等重导的依据 —— 没有它，同一天导两次就是双倍花费
- **删表 / 删列要在迁移文件里写一行 `# DESTRUCTIVE-OK: <理由>`**，否则
  `tests/test_migration_safety.py` 会红。被它拦到时先想清楚：这真是删，还是一次
  **改名**被 autogenerate 拆成了 drop + add？后者要手工并回
  `op.alter_column(..., new_column_name=...)`
- **迁移文件不许 import 应用代码。** 迁移一旦提交就是历史、永不修改，而应用代码
  会被重构改名 —— 那天所有引用它的历史迁移一起崩。自定义列类型经
  `migrations/env.py` 的 `render_item` 渲染成 SQLAlchemy 自带类型

命令见 [`../../CLAUDE.md`](../../CLAUDE.md) 的「常用命令」，工作循环和
autogenerate 的盲区清单见
[Schema 与迁移方案](../design/2026-08-19-schema-migration.md)。

---

## 依赖管理

- **加依赖用 `uv add`**（开发依赖 `uv add --optional dev`），**不用 pip**。pip 装
  进当前解释器却不写 `pyproject.toml`，本机能跑、CI 装不到
- **`uv.lock` 进 git**，CI 用 `uv sync --frozen` —— 锁文件的意义就是 CI 和本机装
  的是同一份
- 跑项目里的命令一律 `uv run <cmd>`，理由同上

这两条有[命令守卫](../../.claude/bash_guard.py)拦着，写错会被当场拦下并告诉你正确
写法。

---

## 改完必做

| 场景 | 必须执行 |
|---|---|
| 改了任意 `.py` | `uv run ruff format .` → `uv run ruff check .` |
| 上一步过了 | `uv run mypy src tests` |
| 上一步过了 | `uv run pytest` |
| 改了 ORM 模型 | 生成 Alembic 迁移并**人工 review 生成的 SQL** |
| 改了业务规则 | 更新 [`docs/business/`](../business/) 里对应的那篇 |
| 改动与设计文档冲突 | **在同一个 commit 里改掉设计文档** —— 过期的设计文档比没有更糟 |
| 加了配置项 | 上面「配置与密钥」那张四处清单 |

前四条在 Stop 钩子里会自动跑一遍（`.claude/stop-hook.sh`），也全都跑在 CI 里。
