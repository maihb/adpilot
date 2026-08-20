# 客户与账户

**代码**：`models/client.py` `models/ad_account.py`（表）· `schemas/client.py`
`schemas/ad_account.py`（出入参）· `services/client.py` `services/ad_account.py`（业务）·
`api/client.py` `api/ad_account.py`（路由）· `tests/test_clients_api.py`
`tests/test_ad_accounts_api.py`（测试）

**范围出处**：[设计文档 §6](../design/2026-08-19-mvp-design.md) · 里程碑 D3

**OpenAPI tag**：`clients`（两个实体共用一个 tag —— 账户脱离客户没有意义，规则也
写在同一篇里）

## 一句话职责

维护「**给谁投**」和「**用哪个账户投**」这两张底表，以及账户上那三个决定了后面所有
指标怎么解释的字段（`timezone` / `currency` / `external_id`）。

**不负责**：指标数据本身（那是归一化的事）、账户与平台的认证凭据（走 web 页面配置、
存生产库，不进这两张表）、任何形式的租户隔离（本项目单实例单使用者，多客户是业务
概念不是租户）。

## 接口

| operationId | 路径 | 说明 |
|---|---|---|
| `createClient` | `POST /api/clients` | 建客户，重名 409 |
| `listClients` | `GET /api/clients` | 分页列出，可按 `is_active` 筛 |
| `getClient` | `GET /api/clients/{client_id}` | 取单个 |
| `updateClient` | `PATCH /api/clients/{client_id}` | 只动传上来的字段 |
| `createAdAccount` | `POST /api/ad-accounts` | 建账户，客户不存在 422、账户重复 409 |
| `listAdAccounts` | `GET /api/ad-accounts` | 可按 `client_id` / `platform` / `is_active` 筛 |
| `getAdAccount` | `GET /api/ad-accounts/{account_id}` | 取单个 |
| `updateAdAccount` | `PATCH /api/ad-accounts/{account_id}` | 身份字段改不动，见下 |
| `createInvite` | `POST /api/clients/{client_id}/invites` | 生成邀请码，规则见 [auth](auth.md) |
| `listInvites` | `GET /api/clients/{client_id}/invites` | 列出某个客户的码，不含明文 |
| `revokeInvite` | `POST /api/clients/{client_id}/invites/{invite_id}/revoke` | 作废 |

邀请码挂在客户下面，但它的规则属于认证链路，写在
[认证与作用域](auth.md)那一篇里，这里不复述。

## 能读文档就够的部分

| 规则 | 一句话 |
|---|---|
| 客户名唯一 | 重名返回 409。导入那条链路按名字幂等找回客户（CSV 里通常只有客户名没有 ID），两行同名会让同一个客户的数据分裂 |
| 账户唯一键 | `(platform, external_id)`，重复返回 409。这是幂等重导的依据 —— 建重了，同一天的数据会分裂到两行，两边都对、加起来才是全部 |
| 跨平台同号是合法的 | 唯一键是**组合**，Meta 和 TikTok 各有一个同号账户很正常 |
| 没有删除 | 停止合作置 `is_active=false`。历史日报和结算都挂在客户下面，删了就成孤儿。两张表都没有 DELETE 接口 |
| 停用是**有副作用**的 | 置 `is_active=false` 之后，这个客户手上的 token **立刻**失效、邀请码也换不出新的。那是自包含 token 唯一的「踢人」手段，见 [auth](auth.md) |
| 停用不影响查询 | 列表默认连停用的一起返回，要筛用 `is_active` 参数 |
| PATCH 是局部更新 | 只动请求体里出现的字段；**显式传 `null` 会把 `note` 清空**，不传则原样不动 |
| 账户身份字段改不动 | `platform` 和 `external_id` 不在 PATCH 请求体里，传了会被静默忽略。要换就建新账户、停用旧的 |
| 时区必须是 IANA 名 | `America/Anchorage` 这种。写错在入口就 422 —— 它是 `stat_date` 的口径依据，错了所有日期的日切点都错，而数据看起来完全正常 |
| 不收 UTC 偏移量 | `+08:00` 一律拒绝：夏令时切换那天它是错的，而广告数据恰恰按自然日切 |
| 币种必须大写 | ISO 4217 三字母，小写**拒绝而不是转换**。库里同时存过 `usd` 和 `USD` 的话，按币种分组会分裂成两组 |
| 客户不存在是 422 不是 404 | URL 指的集合存在，不合法的是请求体。404 的意思是「换个 URL」，422 是「改请求体」 |
| 分页 | `page` 从 1 起，`page_size` 默认 20、**上限 100**，超限 422 而不是截断 |
| `total` 的含义 | 过滤后的总数，不是本页条数 —— 前端靠它算总页数 |
| 列表排序 | 固定按 `id` 倒序（新建的在前）。offset 分页必须有确定排序，否则翻页会漏行 |

## ⛔ 必须读源码的部分

- **改 `currency` / `timezone` 不会重算已有的 `daily_metrics`**
  （`services/ad_account.py` 的 `update()`）。那些行记的是**当时**的口径，改了账户
  设置，历史数据仍是旧口径下的数字。看签名完全推不出来 —— 它返回的是更新后的账户，
  没有任何迹象表明历史数据被留在了原地。要换口径得拿 Mongo 的原始快照重跑归一化。

- **账户可以改 `client_id`（转移给另一个客户），但历史指标的归属不跟着走**
  —— `daily_metrics` 挂的是 `account_id`，按客户汇总时走的是当前归属。也就是说
  转移之后，旧客户的历史报表会少掉这部分数据。目前没有拦这个操作，因为真实场景里
  它确实会发生（代运营换签约主体），但用之前要知道这个后果。

- **`AdAccount.client` 声明了 `lazy="raise"`**（`models/ad_account.py`）。列表和
  详情都**没有** eager load 它，出参里也只有 `client_id`。想在响应里带客户名，必须
  去 `services/ad_account.py` 显式加 `selectinload` —— 直接访问 `account.client`
  不会偷偷发查询，而是当场抛错。这是故意的：async 下的隐式懒加载会炸成
  `MissingGreenlet`，报错和真正的原因之间毫无线索。

- **唯一冲突的错误信息依赖一个前提**（`services/ad_account.py` 的
  `_flush_or_conflict`）：外键那条已经被 `_ensure_client_exists` 提前挡掉，所以走到
  那里的 `IntegrityError` 只可能是账户重复。**加第三条约束时这个前提就不成立了**，
  错误信息会指向错的字段。

## 已知残余

- **列表不支持按名字搜索**，只能分页翻。客户数量到几十个以上时要加，加的时候按
  [`api.md` 分页那一节](../code-rules/api.md#分页)的约定走：不改既有接口的形状。
- **没有批量建账户**。文件导入落地后（D3 后半）会需要「按 `(platform, external_id)`
  找不到就建」的 upsert 入口，那时加在 `services/ad_account.py`，不新增接口。
- **`note` 之外没有任何客户属性**（联系人、结算周期、合同价）。结算相关的字段等
  D6 之后连同结算表一起设计 —— 现在加会是拍脑袋，而且那几个字段一旦落库就
  [不能随便改名](../design/2026-08-19-schema-migration.md)。
