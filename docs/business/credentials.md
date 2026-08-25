# 自动拉取与平台凭据

**代码**：`providers/tiktok.py`（TikTok 适配器）· `providers/fake_api.py`（脱机用的
假 provider）· `auth/crypto.py`（token 加解密）· `models/fetch.py`（两张表）·
`services/credential.py`（授权与解密）· `services/fetch.py`（编排）·
`rules/fetch.py`（停更判定）· `tasks/fetch.py`（排期）· `api/credential.py`
`api/oauth.py`（路由）

**范围出处**：[自动拉取平台数据](../design/2026-08-25-ads-api-fetch.md) · 里程碑 D19

**OpenAPI tag**：`credentials`

## 一句话职责

**让「昨天花了多少」不依赖任何人的记性。**

**不负责**：字段映射（归一化那一步的事，见[数据接入](imports.md)）、去重（归一化
按唯一键 upsert）、把告警推出去（[告警与巡检](alerts.md)）、Meta（协议留好了，
凭据到位当天加一个文件）。

## 接口

| operationId | 路径 | 说明 |
|---|---|---|
| `createAuthorizeUrl` | `POST /api/credentials/authorize-url` | 拿一个「去平台点同意」的地址 |
| `completeTikTokAuthorization` | `GET /api/oauth/tiktok/callback` | 平台跳回来的落点，**免认证** |
| `listCredentials` | `GET /api/credentials` | 已有的授权，新的在前 |
| `deactivateCredential` | `POST /api/credentials/{id}/deactivate` | 停用一个凭据 |
| `attachAccountCredential` | `PUT /api/ad-accounts/{id}/credential` | 把账户挂上凭据（= 开自动拉取） |
| `fetchAccountData` | `POST /api/ad-accounts/{id}/fetch` | 立刻拉一次 |
| `getAccountFetchState` | `GET /api/ad-accounts/{id}/fetch-state` | 这个账户上次拉取的结局 |

## 🔴 先读这一条：拉不到数比拉错数更危险

手工导入有个隐含的好处：**导的人知道自己导了没有。** 自动化把这个「知道」拿走了，
于是失败的默认呈现方式变成了「昨天花费 0，曝光 0，点击 0」—— 而那**和「昨天没
投放」在每一屏上都长得一模一样**：日报照常生成，余额告警会因为没有新消耗而安静
下来，整套系统会非常自信地给出一个基于空气的结论。

所以这套东西里**优先级最高的不是拉取，是拉取失败的可见性**：

| 机制 | 在哪 |
|---|---|
| 每次拉取的结局落库（不是只写日志） | `fetch_states` 表，一账户一行 |
| 连着失败或太久没成功 → 开告警 | `rules/fetch.py` 判定，告警巡检对账 |
| 告警文案带上 `last_error` | 推到群里的只有那一行，而人要做什么取决于错在哪 |

## 能读文档就够的部分

| 规则 | 一句话 |
|---|---|
| **挂凭据 = 开自动拉取** | 没有第二个开关。`ad_accounts.credential_id` 为空就是不拉（CSV 导入的账户永远是这个状态） |
| 一个凭据管一批账户 | 平台一次授权返回一批 `advertiser_ids`。做成账户级的话，撤销和轮换必然漏掉一份 |
| **token 加密落库** | 密钥是 env 的 `CREDENTIALS_SECRET`。**丢了所有凭据全部作废**，只能重走授权 —— 它和 `AUTH_SECRET` 不是一个量级的损失 |
| 出参里没有 token | 密文也不给。要换只有一条路：重新授权 |
| 每次拉最近 3 天 | 不是只拉昨天。平台会回填改数，而滚动窗口在快照 append-only + upsert 这套结构上是**免费**的 |
| **默认不拉「今天」** | 当天数据还没走完，落进去会让异动规则读出「花费暴跌」。要看当天实时消耗那是余额那条线 |
| 默认拉账户级 + 系列级 | 广告级行数是几十倍，而现在没有一屏用得上 |
| 每天大约拉一次 | 判据是「这个日切之后成功拉过没有」，不是「几点了」。排期每小时跑，绝大多数轮次什么都不做 |
| 排期在每小时**第 40 分** | 前面留 20 分钟跑完，再到整点巡检、第 20 分日报。三个数字互不相等有测试盯着 |
| 一个账户失败别的照拉 | 和定时日报**相反**的取舍：拉取不花钱，一个账户的 token 失效不该让别人停更 |
| 余额失败不算整次失败 | 有些账户类型平台不给余额，那不是故障。日指标照样是好的 |
| 余额的 `captured_at` 是拉取时刻 | 平台不告诉我们余额是什么时候的。所以 `note` 记「自动拉取」，人核对时才知道按哪个时刻理解 |
| 手动触发和排期同一个函数 | 两条路径各写一份实现的话，将来腐化的必然是没人天天看的那条 |
| **回调接口免认证** | 浏览器跳转带不了 Authorization 头。防护是 `state`（签名 + 过期 + 用途三重校验） |
| `auth_code` 是一次性的 | 回调失败别刷新页面重试，重新从后台点「发起授权」 |
| 假 provider 只在非生产 | `fake_api` 在 `ENVIRONMENT=prod` 下造不出来，对象名一律 `demo-` 前缀 |

## ⛔ 必须读源码的部分

- **失败记录必须写在另一个事务里**（`tasks/fetch.py` 的模块 docstring）。拉取失败
  会让那个账户的事务回滚，**连同刚写进 `fetch_states` 的失败记录一起** —— 于是
  「这个账户拉不到数」永远不会被巡检看见。这个坑没有任何报错，而它恰恰发生在
  最需要那条记录的时刻。

- **`FetchError.retryable` 拿不准时填 `False`**（`providers/base.py`）。误判成
  「不可重试」的代价是一条多余的告警；反过来是数据静默停更好几天 —— 因为重试一个
  已经失效的 token，五次退避之后任务安静地失败，而任务结果一天后就过期了。

- **HTTP 200 不代表成功**（`providers/tiktok.py` 的 `_request`）。TikTok 把业务
  错误放在响应体的 `code` 里，状态码照样 200。只判 `is_success` 的话，一个
  「token 已失效」会被当成一次拿到零行的成功拉取。

- **请求的 metrics 清单为什么可配置**（`providers/tiktok.py` 的模块 docstring）。
  请求一个不存在的 metric 是**整个请求 400**，而收入类指标（GMV）正是最容易随
  平台功能改名的那一批。核心集合写死，它们走 `TIKTOK_EXTRA_METRICS`。

- **加密防的不是什么**（`auth/crypto.py`）。它不防「同时拿到数据库和 env」——
  那时两样都在对方手上。它防的是这两者**分头泄露**，而那恰恰是最常见的形态：
  dump 传出去了、env 没有。

## 已知残余

- **只有 TikTok。** Meta 那边拿凭据只要几十分钟（BM 里建 System User，访问自有
  账户不需要 App Review），但**没有真实数据可拉** —— 在跑量的客户账户在 TikTok。
  协议、凭据存储、排期、失败可见性全部按「会有第二个平台」设计，加 Meta 是加一个
  文件的事。
- **投放状态还没拉**（`platform_objects` 那张表还不存在）。设计文档第七节留着，
  它要支撑的是「在投却零花费 = 故障」这条判据 —— 在那之前，账户真的停投时拉回
  零行仍然会被当成正常。
- **token 刷新没做。** TikTok 的长期 token 没有刷新流程，表上 `expires_at` 可空
  是给 Meta 留的（60 天大限）。现在写等于写一段永远不执行的代码。
- **没有令牌桶限流。** 账户数是个位数，串行 + 指数退避够用。上令牌桶要先有「被
  限流了」的实测。
- **回调返回的是 JSON，不是跳回后台页面。** 授权成功后人得自己切回凭据页刷新。
- **历史回补要人手动发起。** 接上 API 之前的日子只能靠 CSV 导入或手动触发拉一段
  区间，没有「自动把过去 90 天补齐」的按钮。
