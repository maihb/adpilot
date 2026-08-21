# 业务领域文档索引

**这一层的用途：先读这里，判断要不要打开源码。**

[`code-rules/`](../code-rules/) 讲**怎么写**，这一层讲**写的是什么**：每个领域的
能力、规则，以及「哪几段必须读源码」。

**先读 [`glossary.md`](glossary.md)** —— 指标口径、时区、数据回填、告警公式全在那里，
它是所有领域共用的地基。

---

## 领域

> **D13 完成**：日报的两块前置就位了 —— [操作记录](actions.md)（「本期做了什么」
> 的唯一数据来源，`reason` 必填）和 [LLM 层](llm.md)（输出契约里没有数字字段，
> 调用成本与每日闸门都在）。整条链现在是 客户与账户 → 文件导入 → Mongo 原始快照
> →（RabbitMQ）→ 归一化 upsert → 按天查询 →（每小时 beat）→ 规则巡检 → 告警状态机
> → webhook，外面套着一层认证（内部接口全要运营身份，客户扫邀请码换一张 7 天的票
> 只能看自己那份），前面摆着两个前端。**下一段是 D14** 的日报 —— draft → 人工修订
> → published，客户端只看得到已发布的那份；库存断货那条规则仍然欠着（它需要一个
> 全新的数据域）。下表是
> [设计文档第六、七节](../design/2026-08-19-mvp-design.md)定下的范围，落一个
> 勾一个。**「状态」这一列不许提前打勾** —— 一张说自己有东西但其实没有的表，
> 比没有这张表更糟。

**`tag` 那一列是机器校验的锚点**：`tests/test_business_docs.py` 会拿 OpenAPI 里
出现的每个 tag 来查这张表，查不到就红。所以新增一个 tag 就必须在这里加一行。
没有对外接口的领域填 `—`。

| 领域 | tag | 覆盖什么 | 代码 | 状态 |
|---|---|---|---|---|
| [glossary](glossary.md) | — | 术语、指标口径、时区、回填与重述、规则公式 | `rules/` | ✅ 口径已定；余额那两个参数已按倾向值实现，仍待业务定论 |
| [认证与作用域](auth.md) | `auth` | 两种身份、token 的签发与校验、运营登录、有效期与续期 | `auth/` `api/auth.py` `api/deps.py` | ✅ D9：内部接口已全部要认证；客户端那半边随 `portal` 一起 |
| [客户自助端](portal.md) | `portal` | 客户看自己：账户、时间线、余额可撑天数、告警。**全只读，作用域锁死** | `api/portal.py` `schemas/portal.py` `services/*_for_client` | ✅ D9：接口已通；uni-app 前端 D10–D11 |
| [内部操作台](admin.md) | — | 运营的五屏、写操作的三级分类、邀请码只显示一次、导入的两段 | `admin/src/` | ✅ D12：登录、导入、告警、客户与邀请码、账户明细已通 |
| [客户端界面](client-app.md) | — | 客户在手机上看到的四屏、以及「显示错了不会报错」的四条口径规则 | `client/src/` | ✅ D10–D11：H5 已跑通；微信小程序端只验到编译通过 |
| [客户与账户](clients.md) | `clients` | 客户、广告账户（平台/币种/**时区**）、账户与客户的归属 | `client.py` `ad_account.py`（model / schema / service / api 四层同名） | ✅ D3：建、查、改与分页已落地，无删除 |
| [数据接入](imports.md) | `imports` | `ReportProvider` 适配器注册表、文件导入、原始快照落盘 | `providers/` `services/imports.py` | ✅ D3：CSV 导入与 append-only 落盘已通，Excel 与拉取调度未做 |
| [日指标](metrics.md) | `metrics` | 平台字段 → 统一口径、唯一键 upsert、按天查询与派生指标 | `services/field_maps.py` `services/normalize.py` `services/daily_metric.py` | ✅ D3–D5：归一化与按天查询已通；聚合与环比未做 |
| [异步任务](tasks.md) | `tasks` | Celery + RabbitMQ、重试与死信队列、任务状态查询、定时排期 | `db/broker.py` `tasks/` `services/task.py` | ✅ D6：归一化已异步化；D8：告警巡检已进 `beat_schedule`（**要另起一个 beat 进程**） |
| [余额与账户](balances.md) | `balances` | 余额快照录入、可撑天数 | `rules/balance.py` `services/balance.py` | ✅ D7 |
| [告警与巡检](alerts.md) | `alerts` | 状态机去重、定时巡检、指标异动、webhook 推送 | `rules/anomaly.py` `services/alert.py` `tasks/alerts.py` `notifiers/` | ✅ D8：巡检自动跑、同一件事只报一次；库存断货未做 |
| [操作记录](actions.md) | `actions` | 「本期做了什么」的唯一数据来源，**`reason` 必填** —— 平台变更日志补得上「改了什么」，补不上「为什么」 | `models/action.py` `services/action.py` `api/action.py` | ✅ D13：登记与查询已通；自动抓平台变更日志未做，[发布前校验非空](../design/2026-08-19-mvp-design.md#4-投放操作记录mvp-手动登记必填)随日报走 |
| 日报 | — | 生成、人工修订、发布、**快照固定** | `services/report.py`（🚧） | 🚧 D14：前置的[操作记录](actions.md)与 [LLM 层](llm.md)已就位 |
| [LLM](llm.md) | — | 适配器、输出契约（**没有数字字段**）、提示词版本、调用成本与每日闸门 | `llm/` `services/llm.py` `models/llm_call.py` | ✅ D13：假 provider 下走通「结构化输入 → 校验 → 落 `llm_calls`」；日报与诊断本身是 D14 |
| 健康检查 | `health` | 存活与就绪探针 | `api/health.py` | ✅ 已落地 |

---

## 怎么用

**做需求前**：读对应领域那篇。文档里「能读文档就够的部分」覆盖到的场景，照着写即可，
不必打开源码。

**碰到「⛔ 必须读源码」标记的**：那几段的正确性依赖执行顺序、事务边界或安全考量，
看签名和文档推不出来，动手前先读代码。

**改完之后**：动的是**业务规则**（不是重构、不是改文案）就回来更新对应那篇。
文档腐烂的唯一原因是改了代码不改它 —— 而这一层一旦不可信，就没人敢照着它跳过源码，
等于白写。

**发现口径没定义**（阈值、取哪个转化事件、回溯几天）：按最合理的假设实现，
**收口到一个函数或一个常量**，然后回 [`glossary.md`](glossary.md) 把它记成「⚠️ 待定」
并写清现在取的是什么值。散在五处的假设，等口径定了也改不动。

---

## 加一个领域时

1. 在本目录加一篇 `<tag>.md`，照 [`_template.md`](_template.md) 的骨架写
2. 在上面那张表里加一行，**tag 和状态如实填**
3. 接口的 `tags` 与这里的 `tag` 列一一对应（命名约定见 [`../code-rules/api.md`](../code-rules/api.md#命名)）

> ✅ **第 2、3 步有机器强制了**（`tests/test_business_docs.py`，跟着 `pytest` 跑）：
> OpenAPI 里出现的每个 tag 都必须在上表登记，且指向一篇真实存在的文档。以 `tag`
> 为锚点是因为它不可能忘记填 —— 忘了接口就注册不出来。
>
> 🚧 **第 1 步的内容质量仍然靠 review**：机器只能验「文件在不在」，验不了「写得对
> 不对」。骨架里那几节没有内容时写「暂无」，别删。

写的时候注意两件事：

- **第 3 节（能读文档就够的规则）一行一条**，写多了会变成源码的劣质副本
- **第 4 节（必须读源码）才是这套文档真正的价值**，只列真正推不出来的，并写明
  **为什么**不看代码会写错
