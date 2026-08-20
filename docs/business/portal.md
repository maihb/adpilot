# 客户自助端

**代码**：`api/portal.py`（路由）· `schemas/portal.py`（出参）·
`services/*.py` 里那几个 `*_for_client` / `series_for_client` ·
`tests/test_portal_api.py`

**范围出处**：[前端接入与认证方案](../design/2026-08-21-client-auth.md) · 里程碑 D9
（接口）、D10–D11（uni-app 前端）

## 一句话职责

客户自己看自己的投放数据。**全只读，作用域锁死在一个客户上。**

**不负责**：任何写操作。客户能改的东西一旦存在，「谁改的」就成了新问题，而这套
系统连操作记录都还没做（设计文档第八节）。

## 接口

全部在 `/api/portal/` 下，全部要客户端 token（`ClientBearer`）。

| operationId | 路径 | 说明 |
|---|---|---|
| `getPortalProfile` | GET `/api/portal/me` | 我是谁。**不含内部备注** |
| `listPortalAccounts` | GET `/api/portal/accounts` | 我的广告账户，停投的也列 |
| `listPortalMetrics` | GET `/api/portal/accounts/{account_id}/daily-metrics` | 每日时间线，一天一行 |
| `getPortalRunway` | GET `/api/portal/accounts/{account_id}/balance-runway` | 余额还能撑几天 |
| `listPortalAlerts` | GET `/api/portal/alerts` | 我这边有什么要注意的 |

日报接口等 D12–D13（日报服务本身还没有）。

## 能读文档就够的部分

| 规则 | 一句话 |
|---|---|
| 客户是谁 | 从 token 里的 `client_id` 来，**没有任何接口接受它当入参** |
| 不属于自己的 `account_id` | 404，不是 403 —— 403 等于承认那个账户存在 |
| 时间线的口径 | `stat_date` 是**账户时区**下的自然日，出参里带着 `timezone` 和 `currency` |
| 时间线的层级 | 逐天取「有数据的最高层级」，**客户端不暴露层级选项**（同一天两个层级不能相加） |
| 没有数据的那天 | **不出现在结果里**，不补零 —— 「花了 0」和「没导入」是两件事 |
| 一次能查多久 | 最长 92 天，超了 422 |
| 账户列表 | 不分页，一次给完；**不含 `external_id`**（平台侧账户 ID 是运营的工作面） |
| 余额没录过时 | 各字段 `null` 而不是 0 —— 那是「不知道」，不是「没事」 |
| 告警默认范围 | 只给未解决的，`only_open=false` 才给历史 |
| 告警出参 | 比内部那套**少一个 `notified_at`**（推送成功没有是运维信息） |
| 停止合作的客户 | 立刻什么都看不到（每次请求都查一次 `is_active`） |

## ⛔ 必须读源码的部分

- **作用域靠两层机器保证，不靠自觉。** 漏一次的后果是把 A 客户的花费给 B 客户
  看见，而这种漏**不会有任何报错**，也不会有人来投诉。第一层是
  `tests/test_auth_guard.py`：遍历 openapi.json，`/api/portal/` 下每个接口都必须
  要 `ClientBearer`。第二层是 `services/` 里那几个函数把 `client_id` 做成**必填
  关键字参数** —— 「查全部客户的指标」在这条路径上根本写不出来。加接口之前先读
  `api/deps.py` 的 `require_client_scope` 和 `services/ad_account.py` 的
  `get_for_client`。

- **`api/portal.py` 里的每个 handler 都要写 `ClientScopeDep`**，哪怕它用不上
  那个 `client_id`（目前没有这种情况）—— 那个依赖同时在做「客户还在合作吗」的
  检查，少了它，一张已发出的 token 能一直用到 90 天上限。

## 已知残余

- **日报接口没有**，D12–D13 随日报服务一起。
- **没有推送**。客户端要看到告警只能自己打开小程序 —— 主动推送要么走微信模板
  消息（需要企业资质，见设计文档第八节「不做微信授权登录」同源的理由），要么
  走短信，两条都超出 MVP。
- **一次只能看一个账户的时间线**。多账户合并需要先决定要不要换汇，而汇率是个
  全新的数据域（同「库存断货」，见 [BUSINESS.md](BUSINESS.md) 的状态列）。
