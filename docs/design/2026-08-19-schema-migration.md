# 数据层 Schema 与迁移方案

> 2026-08-19 · 状态：**已落地**（三张表 + 首份迁移 + 第六节两道门禁）
>
> 深化 [MVP 设计文档](2026-08-19-mvp-design.md) 第四节选型表里「ORM：SQLAlchemy 2.0 + Alembic」那一行 —— 那里只有结论，这里记录候选、淘汰理由和使用边界。
>
> **不在这里抄表结构。** 表定义的真相源是 `src/adpilot/models/`，schema 历史的真相源是 `migrations/versions/`。

## 一、要解决的问题

诉求是：**改了 Python 代码，数据库自动跟着变，不用手写 DDL。**

拆开看是两类改动，价值和风险完全不对称：

| 改动类型 | 自动化的价值 | 自动化的风险 |
|---|---|---|
| 建表 / 加列 / 加索引 / 加约束 | **高**。二十个列的 `CREATE TABLE` 手敲容易错，从类型声明推导则不会 | 低。推错了迁移当场报错，改了重来 |
| 改名 / 删列 / 改类型 | 低。本来就没几行 | **高**。工具会把改名读成「删一列 + 加一列」，**数据静默丢失** |

**为什么第二类无解**：把 `spend` 改名成 `spend_usd`，与「删掉 `spend`、新增 `spend_usd`」在 schema diff 上是完全相同的两个状态差。工具读不到意图，只能猜。ent 的 auto migration 默认死活不删列（要显式 `WithDropColumn(true)`）就是在回避这件事。

**所以选型的判据不是「谁更自动」，而是「第二类出问题时谁能拦住我」。** 这决定了下面的取舍。

## 二、候选与淘汰

对照的是 Go 生态的 sqlc 和 ent —— 前者 SQL 优先、后者代码优先。

| 候选 | 方向 | 结论 |
|---|---|---|
| **sqlc-gen-python** | SQL → 代码 | ❌ **方向不对**。sqlc 只在编译期校验手写 SQL，**完全不管迁移**，数据库怎么变成 `schema.sql` 那个样子要另配工具。它假设你乐意手写 SQL，只帮你写对 |
| **prisma-client-py** | 代码 → 数据库 | ❌ **已弃用**。Prisma 核心从 Rust 重写为 TypeScript，Python client 跟进等于内部重写，作者已停止维护（Go / Rust / Dart client 同批弃用） |
| **Atlas + atlas-provider-sqlalchemy** | 代码 → 数据库 | 🟡 **能力更强，但先不上**。ent 背后同一套迁移引擎（ariga 出品），带 migration lint、drift 检测、触发器/RLS 支持。代价是引入 Go 二进制工具链。退出条件见第八节 |
| **SQLAlchemy 2.0 + Alembic** | 代码 → 数据库 | ✅ **选它**。零新增工具链，`Base` 已在 `src/adpilot/db/postgres.py` 就位，生态最成熟 |

**Atlas 之所以是「先不上」而不是「不用」**：两条路的真相源是同一份 `models/`，将来切换只是加一个 provider，model 一行不用改。**这不是一个锁死的决定**，所以按「最少工具链」起步是安全的。

**反例（不这么做）**：为了拿到 sqlc 那种「SQL 编译期校验」而引入 sqlc-gen-python。本项目只在报表聚合那一小块手写 SQL，摊不平「Go 工具链 + WASM 插件 + 生成物 drift 门禁」这三项成本；而它还不解决迁移，等于问题一个没少、工具多了一套。

## 三、选定方案

**真相源是 `src/adpilot/models/` 下的 SQLAlchemy 声明**，Alembic 负责把数据库 diff 到那个状态。

工作循环：

```text
改 models/ 下的声明
  → uv run alembic revision --autogenerate -m "..."
  → 人看一遍生成的 migrations/versions/*.py      ← 唯一不能省的一步
  → uv run alembic upgrade head
```

SQLAlchemy 2.0 的类型注解**同时就是** schema 声明，这是选它的核心理由：

```python
class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)

    # 金额一律 numeric/Decimal（CLAUDE.md 硬规矩 5）。这里是 Mapped[Decimal]
    # 而不是 float，所以 `budget * 0.5` 会被 mypy strict 当场拦下 ——
    # 「浮点混进金额计算」这条路在编译期就被堵死，不靠 review 盯。
    daily_budget: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
```

**nullability 由类型注解推导**：`Mapped[str]` → `NOT NULL`，`Mapped[str | None]` → 可空。Python 类型和数据库约束不可能不一致，因为它们是同一个声明 —— 这正是 1.x 时代要维护两份定义的痛点。

时间列一律 `DateTime(timezone=True)`（PG `timestamptz`）。`stat_date` 的时区口径不在这里定义，见 [glossary.md](../business/glossary.md)。

## 四、🔴 autogenerate 的能力边界

**这一节是本文最重要的部分。** 把 Alembic 当成「全自动同步」而不是「草稿生成器」是本方案唯一的翻车方式，Alembic 官方文档原文即 *"is not intended to be perfect"*。

**能可靠检测**：表增删、列增删、nullable 变化、列类型变化（`compare_type` 新版默认开）、索引增删、唯一约束增删、外键增删。日常九成改动落在这里。

**检测不到 —— 按本项目踩到的概率排序**：

| 盲区 | 后果 | 本项目会不会踩 |
|---|---|---|
| **列 / 表改名** | 生成 drop + create，**数据丢失** | 一定会。字段命名很少一次到位 |
| **PG 原生 ENUM 的值变更** | 基本管不了，得手写 | 很可能。广告状态、平台枚举 |
| **部分索引 / 表达式索引**（带 `WHERE`） | diff 不可靠 | 很可能。报表聚合迟早要 |
| `server_default` 变化 | 默认根本不比对 | 会。需显式开 `compare_server_default=True` |
| CHECK 约束、触发器、视图、存储过程 | 完全不管 | 目前用不到 |

## 五、绕开盲区的三条约定

1. **改名手工改写。** 把生成的 `drop_column` + `add_column` 两条并成一条
   `op.alter_column("campaigns", "spend", new_column_name="spend_usd")`。**没有自动解**，这就是「必须人看一遍」的全部理由。

2. **不用 PG 原生 ENUM**，用 `String` 列 + Python `StrEnum` 在应用层约束。PG 的 `ALTER TYPE ... ADD VALUE` 在事务里有限制，Alembic 处理起来很难受，而枚举加值在广告平台是常态。`config.py` 的 `Environment` 已是这个写法，风格一致。

3. **部分索引改动时手工确认。** 能用 `postgresql_where=` 声明，但 autogenerate 对它的 diff 不可信。

4. **迁移文件不 import 应用代码。**（落地时踩到才补的一条）自定义列类型会被
   autogenerate 渲染成 `adpilot.models.types.StrEnumType(length=16)`，而它**不会
   替你加那句 import** —— 生成出来的文件当场就是坏的。补上 import 只治了症状：
   迁移一旦提交就永不修改，而应用代码会被重构、改名、删掉，那天所有引用它的
   历史迁移一起崩。解法是 `env.py` 里的 `render_item`，把自定义类型渲染成
   SQLAlchemy 自带类型（`StrEnumType` 在库里本来就是 varchar，DDL 完全等价）。

## 六、门禁

按硬规矩 6（「重要就得能让构建失败」），两条进 CI：

| 门禁 | 拦什么 | 实现 |
|---|---|---|
| `alembic check` | **改了 model 但忘了生成迁移** | `ci.yml` 的集成 job（要连真实库，所以不在单元那一档） |
| `upgrade()` 里出现 `drop_column` / `drop_table` 就要求写 `# DESTRUCTIVE-OK: <理由>` | **自动生成的 DDL 悄悄删列** | `tests/test_migration_safety.py`，跟着 `pytest` 跑 |

第二条**只扫 `upgrade()`**：`downgrade()` 里的 `drop_table` 是回滚路径，每一个
建表迁移都有，一起扫的话门禁第一天就会变成人人跳过的噪音。

第二条是本方案最关键的补强：它把第一节说的那个高风险动作，从「靠人注意」变成「机器拦人」—— 也就是 Atlas 用 migration lint 内置提供、而 Alembic 缺失的那个能力。

另有两处工具链适配，第一次生成迁移前就要做，否则 CI 直接红：

- **mypy**：`migrations.*` 加 `ignore_errors`。Alembic 的迁移模板带不了 strict 级别的类型注解。
- **`alembic.ini` 的 `sqlalchemy.url` 必须留空**，DSN 在 `env.py` 里从 `get_settings().postgres_dsn` 读。`alembic.ini` 要进 git，而 DSN 带密码 —— 这直接撞硬规矩 1。

## 七、刻意不做

- ❌ **不做运行时 auto-migrate**（ent 的 `client.Schema.Create(ctx)` 那一档）。Alembic 也没有对应物。启动时自动改 schema 意味着你看不见它执行了什么，生产环境不可接受。
- ❌ **不引入第二套 SQL 代码生成**（sqlc 类）。理由见第二节反例。
- ❌ **不在 markdown 里维护表结构清单**。表定义会漂移，文档会过期，然后 agent 读文档、信文档、踩坑。

## 八、什么时候改用 Atlas

出现下面任一情况就重新评估 —— 写下退出条件是为了避免「当初选了就一直凑合」：

1. 第六节第二条门禁（破坏性变更审查）自写脚本开始变复杂 —— Atlas 的 migration lint 是现成的。
2. 需要触发器、RLS、自定义函数或物化视图 —— 这些 Alembic autogenerate 完全不管。
3. 出现过一次 schema drift 事故（数据库被手工改过，与迁移历史不一致）—— Atlas 有 drift 检测。

切换成本低：真相源仍是 `models/`，加一个 `atlas-provider-sqlalchemy` 即可。

## 参考

- [Alembic — Auto Generating Migrations](https://alembic.sqlalchemy.org/en/latest/autogenerate.html)（含官方的 not-detected 清单）
- [Atlas — Automatic SQLAlchemy Migrations](https://atlasgo.io/guides/orms/sqlalchemy)
- [prisma-client-py 弃用说明](https://github.com/RobertCraigie/prisma-client-py/issues/1073)
