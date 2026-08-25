# 数据接入

**代码**：`providers/base.py`（协议与产物）· `providers/csv_file.py`（CSV 解析）·
`providers/registry.py`（适配器注册表）· `services/imports.py`（编排与落盘）·
`schemas/imports.py` · `api/imports.py`（路由）· `tests/test_csv_provider.py`
`tests/test_imports_api.py`

**范围出处**：[设计文档 §4](../design/2026-08-19-mvp-design.md) · 里程碑 D3

**OpenAPI tag**：`imports`

## 一句话职责

把外部报表**原样**变成 Mongo 里的快照，然后排一个归一化任务。

**不负责**：字段映射（归一化那一步的事）、去重（归一化按唯一键 upsert）、**从平台
API 拉数据**（D19 起在[自动拉取与凭据](credentials.md)那一篇 —— 两条路落到同一批
快照、走同一条归一化，区别只有数据是问来的还是传来的）。

## 接口

| operationId | 路径 | 说明 |
|---|---|---|
| `importReportFile` | `POST /api/imports` | multipart 上传一份报表，按天落成快照，并排一个归一化任务 |
| `listImportProviders` | `GET /api/imports/providers` | 可用的数据源适配器，给后台下拉框用 |

## 能读文档就够的部分

| 规则 | 一句话 |
|---|---|
| **快照 append-only** | 同一个 (账户, 日期) 导两次得到**两条**快照，不覆盖。平台数据若干天内还会变，「当时这个数是多少」只能从这里查 |
| **一个字段都不映射** | 列名原样存。提前改名等于把当时的映射规则永久烧进快照，重跑也救不回来 |
| **一天一条文档** | 按 `stat_date` 分组，`{provider, account_id, stat_date, fetched_at, payload}` |
| `stat_date` 存字符串 | ISO 格式（`"2026-08-18"`）而不是 BSON datetime —— 它是账户时区下的自然日、不是时刻。存 datetime 会诱导下游拿时区去解释它 |
| 日期列自动探测 | 候选：`Day` / `Date` / `日期` / `stat_time_day` / `Reporting starts`。认不出就报错并列出实际表头，**不猜** |
| 可以显式指定日期列 | `date_column` 参数。两列都像日期时用它定夺 |
| 日期格式只认三种 | `2026-08-18` / `2026/08/18` / `20260818`。`03/04/2026` 这种歧义格式一律拒绝 —— 美式欧式差一天，而错一天的数据看起来完全正常 |
| 末尾 Total 行跳过 | 平台导出常带一行没有日期的汇总。跳过但在响应里报 `skipped_rows`，正常应该是 0 或 1 |
| 列数多于表头 → 报错 | 文件被改坏了。报错带行号 |
| BOM 会被吃掉 | Excel 另存的 CSV 带 BOM，按 `utf-8-sig` 解码 |
| 文件上限 10 MiB | 超了 413。`UploadFile` 会整个读进内存 |
| **落完快照自动排队归一化** | 响应里回 `task_id`，拿它去 `GET /api/tasks/{id}` 看进度。不带日期 —— 一份文件常横跨多天，投一个「整个账户重跑」比按天投 N 条省事，而归一化本来就幂等 |
| **`task_id` 可能是 `null`** | 队列连不上。**快照已经落好了** —— 补触发一次归一化即可，别重新导文件（那只会多一条快照） |
| 账户不存在 → 422 | 快照的 `account_id` 是唯一的归属标记，落一条指向不存在账户的快照 = 造了一份永远不会被归一化的数据 |
| provider 名字不能改 | 它落进 `raw_reports.provider`，是历史快照的来源标记。改了之后旧文档指向一个不存在的适配器 |

## ⛔ 必须读源码的部分

- **解析走 `asyncio.to_thread`，而且不会挪进 Celery**（`services/imports.py`）。
  CSV 解析是 CPU 密集的，在事件循环里做会把**整个进程**的所有请求一起卡住 ——
  症状是「全局变慢」而不是「导入变慢」，排查起来非常贵。**别删这层包装**。
  D6 原计划把这一步也挪进 worker，实际没挪：文件内容得跟着消息走一遍 broker，
  而且解析失败恰恰是要**当场说**的那类错误（层级填错、日期列认不出来）。挪走的
  是归一化。

- **排队失败不会让导入失败**（`services/task.py` 的 `enqueue_normalize`）。快照
  那时已经落进 Mongo 了，报 500 会让人以为导入没成功、再导一遍，于是白白多一条
  快照。看签名推不出来：返回 `str | None` 而不是抛异常，正是这个取舍的形状。

- **Mongo 侧没有事务**（`api/deps.py` 的 `get_mongo`）。一次导入请求里 PostgreSQL
  那边回滚了，Mongo 的快照**仍然留着**。这是刻意的取舍：快照多一条不伤害任何人，
  丢一条就再也拿不回「当时那个数」。看签名推不出来，因为它和 `SessionDep` 长得
  一模一样。

- **协议为什么是 `parse(content)` 而不是设计文档里的 `fetch(account, day)`**
  （`providers/base.py`）。拉取型和推送型的输入根本不同，硬塞进一个签名会让两边
  各带一半用不上的参数。接 API 适配器时的正确做法写在那个 docstring 里：加一个
  `fetch` 方法，**统一产物不统一入口**。

## 已知残余

- **`raw_reports` 没有索引**。按 `(account_id, stat_date)` 查现在是全集合扫描。
  数据量还小，加索引要先解决「Mongo 没有迁移工具、索引该在哪一步建」这个问题
  —— 放在启动时建会让进程启动依赖 Mongo 可用，那与就绪探针的分工冲突。
- **只支持 CSV，不支持 Excel（.xlsx）**。设计文档写的是「CSV/Excel」，Excel 要引
  `openpyxl`，等真的收到 .xlsx 再加 —— 加的时候是新增一个 provider，不改现有的。
- **同步解析**，见上，这是决定不是欠账。真要处理大文件，正确做法是先落对象存储、
  消息里只带一个键，而不是放宽 10 MiB 那个上限。
- **文件导入永远是手动的**，这不是欠账 —— 它的输入就是一份人刚导出来的文件。
  「每天自动拉数」是另一条路（[自动拉取](credentials.md)，D19 起），走 API 而不是
  文件；两条路的产物完全一样。
- **没有导入历史的查询接口**。要看导过什么只能直接查 Mongo。等真的需要「这份数据
  是哪次导入进来的」时再加。
