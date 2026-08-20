# 日指标：归一化与查询

**代码**：`services/field_maps.py`（列名映射的**唯一收口点**）·
`services/normalize.py`（快照 → 日指标）· `services/daily_metric.py`（查询）·
`schemas/daily_metric.py`（出参与派生指标）· `api/daily_metric.py`（路由）·
`models/daily_metric.py`（表）· `tests/test_normalize.py` `tests/test_metrics_api.py`

**范围出处**：[设计文档 §6](../design/2026-08-19-mvp-design.md) · 里程碑 D3–D5

**OpenAPI tag**：`metrics`

## 一句话职责

把 Mongo 里的原始快照变成 `daily_metrics`，并把它读出来。

**不负责**：抓数据（[数据接入](imports.md)的事）、告警判定（`rules/`，D6）、把数字
写成人话（LLM，D12）。**也不存派生指标** —— CPM/CPC/CTR/CPA/ROAS 全部现算。

## 接口

| operationId | 路径 | 说明 |
|---|---|---|
| `normalizeAccount` | `POST /api/ad-accounts/{account_id}/normalize` | 把该账户的快照归一化进日指标，可反复跑 |
| `listDailyMetrics` | `GET /api/ad-accounts/{account_id}/daily-metrics` | 按天查，附现算的派生指标 |

## 能读文档就够的部分

### 归一化

| 规则 | 一句话 |
|---|---|
| **取最新的那条快照** | 同一个 (账户, 层级, 日期) 有多条快照时只用 `fetched_at` 最大的。重导是常态，最新那条才是当前最准确的说法 |
| **按唯一键 upsert** | `(account_id, level, object_id, stat_date)` 重复就更新。**没有它，同一天导两次就是双倍花费** |
| **可以反复跑** | 重跑是覆盖不是追加。映射规则改了、发现取错字段了，重跑一次修正历史 |
| `level` 来自快照 | 导入时显式记下的，不从内容推断 —— 它是唯一键的一部分，猜错会新增一份而不是覆盖 |
| **币种取账户的** | 不从 `Amount spent (USD)` 那个后缀解析。账户改过币种时该信账户 |
| 缺对象 ID 的行跳过 | 通常是导出文件里残留的小计行。跳过数在响应里报出来 |
| 缺指标列按 0 | 平台不给某个字段是正常的。**唯独 `reach` 用 `null` 不用 0** —— 0 和「没这个数」在报表里不是一回事 |
| 没有快照 → 0 行 | 不报错。那通常意味着还没导入 |
| **clicks 固定取链接点击** | 「全部点击」把点赞、展开、看主页都算进去，两者能差好几倍，用它算的 CPC 会好看得离谱 |
| ⚠️ **conversions 取哪个事件待定** | 现在取平台给的 Results / 转化数，即导出时在后台选定的那个事件。假设收口在 `field_maps.CONVERSIONS_COLUMNS` |

### 查询

| 规则 | 一句话 |
|---|---|
| 日期区间**必填、闭区间** | 这是唯一一张按天线性增长的表，默认全量迟早出事 |
| `stat_date` 是账户时区的自然日 | 跨账户汇总时不要把不同时区的同一个 `stat_date` 当成同一天直接相加。能加，但要知道加的是什么，并在日报里注明 |
| **派生指标现算不存** | 存下来就等于把公式复制进数据库，口径一改两份立刻对不上 |
| **分母为 0 返回 `null`** | 不是 0，也不是无穷。写 0 会让「今天没有转化」和「今天 CPA 是 0 元」变成同一个显示值 |
| CTR 存小数 | 不存百分数，展示时再乘 100 |
| 🔴 **reach 跨天不可加** | 同一个人两天都被触达，相加会把他算两次。周期汇总的 reach 必须向平台单独请求那个周期的值。`frequency` 由它算出来，同理，所以一列都不存 |
| 排序 | (日期倒序, 对象 ID)。带上对象 ID 是为了同一天内行序稳定，否则 offset 分页会漏行 |
| 账户不存在 → 404 | 而不是返回空列表。否则前端分不出该提示「查无此账户」还是「换个日期试试」 |

## ⛔ 必须读源码的部分

- **`_upsert()` 的 `set_` 里必须显式带 `updated_at`**（`services/normalize.py`）。
  `ON CONFLICT DO UPDATE` 走 Core 语句、**绕过 ORM**，`TimestampMixin` 上那个
  `onupdate=now()` 根本不会触发。漏了的症状是「数据明明重导过、`updated_at` 还停
  在上次」—— 而排查回填问题时看的正是这个列。加新列到这张表时，这里也要跟着加。

- **`canonical()` 保留括号里的内容**（`services/field_maps.py`）。看起来该把
  `Amount spent (USD)` 的括号一起去掉，但 `Clicks (link)` 和 `Clicks (all)` 会因此
  撞成同一个键 —— 而这两个正是必须区分的。代价是币种后缀进了键，所以匹配用**前缀
  而非全等**，候选顺序即优先级。改这个函数前先读那段注释。

- **`_latest_snapshots()` 用聚合而不是查回来再挑**（`services/normalize.py`）。
  重导多次的账户，快照数是天数的好几倍。这不是性能洁癖：那是一张会线性增长的
  集合，全拉回进程只为扔掉大部分，内存会随使用时间一起涨。

## 已知残余

- ⚠️ **conversions 取哪个转化事件没有配置项**，见上。定下来的当天改
  `field_maps.CONVERSIONS_COLUMNS` 一处，并回 [glossary](glossary.md) 填值。
- **没有聚合接口**：按天汇总、环比、跨账户合计都还没有。日报（D12）需要它们，
  那时加在 `services/daily_metric.py`，**派生指标仍然现算** —— 汇总之后再算，
  不是把每天的比率平均（那是错的）。
- **归一化是同步的**。账户天数多时一次请求会跑很久，D6 起应由 Celery 消费。
- **没有「哪些天还没归一化」的查询**。现在只能全跑一遍（幂等，所以安全，但费）。
- **`reach` 的周期汇总没做**，日报里的频次只写「当日频次」。
