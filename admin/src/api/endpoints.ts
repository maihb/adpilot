/**
 * 内部接口的薄封装。**类型全部从 `generated/schema.ts` 取**，一个都不手写 ——
 * 后端改了出参形状这里就编译不过，那是这个后台唯一的契约门禁。
 */

import type { components } from './generated/schema'
import { request } from './request'

type Schemas = components['schemas']

export type Client = Schemas['ClientResponse']
export type AdAccount = Schemas['AdAccountResponse']
export type Invite = Schemas['InviteResponse']
export type InviteCreated = Schemas['InviteCreatedResponse']
export type DailyMetric = Schemas['DailyMetricItem']
export type Alert = Schemas['AlertItem']
export type Balance = Schemas['BalanceItem']
export type ImportResult = Schemas['ImportResponse']
export type TaskStatus = Schemas['TaskStatusResponse']
export type Report = Schemas['ReportItem']
export type Action = Schemas['ActionItem']
export type Product = Schemas['ProductItem']
export type StockRunway = Schemas['StockRunwayResponse']
export type StockImportResult = Schemas['StockImportResponse']
export type ReportNarrative = Schemas['ReportNarrative']
export type Credential = Schemas['CredentialRead']
export type FetchResult = Schemas['FetchResponse']
export type FetchState = Schemas['FetchStateRead']

// —— 认证 ————————————————————————————————————————————————

export function login(
  username: string,
  password: string,
  captcha?: { id: string; answer: string },
): Promise<Schemas['TokenResponse']> {
  return request<Schemas['TokenResponse']>('/auth/login', {
    method: 'POST',
    body: {
      username,
      password,
      captcha_id: captcha?.id,
      captcha_answer: captcha?.answer,
    },
    auth: false,
  })
}

/** 这个账号现在要不要验证码；要的话响应里直接带着题
 *  （docs/design/2026-08-25-login-captcha.md）。 */
export function getLoginCaptcha(username: string): Promise<Schemas['CaptchaResponse']> {
  return request<Schemas['CaptchaResponse']>('/auth/captcha', {
    query: { username },
    auth: false,
  })
}

// —— 客户与账户 ————————————————————————————————————————————

export function listClients(page = 1, pageSize = 50): Promise<Schemas['ClientListResponse']> {
  return request<Schemas['ClientListResponse']>('/clients', {
    query: { page, page_size: pageSize },
  })
}

export function getClient(clientId: number): Promise<Client> {
  return request<Client>(`/clients/${clientId}`)
}

export function createClient(name: string, note?: string): Promise<Client> {
  return request<Client>('/clients', { method: 'POST', body: { name, note: note || null } })
}

/**
 * 改客户。
 *
 * 🔴 `is_active: false` 是**立刻影响外部人**的操作：`require_client_scope` 每次
 * 请求都查一遍它，所以那个客户手上还没过期的票**当场失效**。调用它的地方必须先
 * 二次确认，且确认文案要写出这个后果。
 */
export function updateClient(
  clientId: number,
  patch: Schemas['ClientUpdateRequest'],
): Promise<Client> {
  return request<Client>(`/clients/${clientId}`, { method: 'PATCH', body: patch })
}

export function listAdAccounts(clientId?: number): Promise<Schemas['AdAccountListResponse']> {
  return request<Schemas['AdAccountListResponse']>('/ad-accounts', {
    query: { client_id: clientId, page: 1, page_size: 100 },
  })
}

export function getAdAccount(accountId: number): Promise<AdAccount> {
  return request<AdAccount>(`/ad-accounts/${accountId}`)
}

// —— 邀请码 ——————————————————————————————————————————————

export function listInvites(clientId: number): Promise<Schemas['InviteListResponse']> {
  return request<Schemas['InviteListResponse']>(`/clients/${clientId}/invites`)
}

/**
 * 发一个邀请码。
 *
 * 🔴 **返回值里的 `code` 是明文码唯一一次出现的地方** —— 库里存的是哈希，列表
 * 接口只有状态。调用方必须把它留在界面上直到人主动关掉，不能做成一闪而过的
 * toast。
 */
export function createInvite(clientId: number, ttlDays: number): Promise<InviteCreated> {
  return request<InviteCreated>(`/clients/${clientId}/invites`, {
    method: 'POST',
    body: { ttl_days: ttlDays },
  })
}

export function revokeInvite(clientId: number, inviteId: number): Promise<Invite> {
  return request<Invite>(`/clients/${clientId}/invites/${inviteId}/revoke`, { method: 'POST' })
}

// —— 导入与归一化 ————————————————————————————————————————

export function importReport(form: FormData): Promise<ImportResult> {
  return request<ImportResult>('/imports', { method: 'POST', form })
}

export function getTask(taskId: string): Promise<TaskStatus> {
  return request<TaskStatus>(`/tasks/${taskId}`)
}

export function normalizeAccount(accountId: number): Promise<Schemas['NormalizeResponse']> {
  return request<Schemas['NormalizeResponse']>(`/ad-accounts/${accountId}/normalize`, {
    method: 'POST',
  })
}

// —— 指标、余额、告警 ————————————————————————————————————

export function listDailyMetrics(
  accountId: number,
  start: string,
  end: string,
): Promise<Schemas['DailyMetricListResponse']> {
  return request<Schemas['DailyMetricListResponse']>(`/ad-accounts/${accountId}/daily-metrics`, {
    query: { start, end, page: 1, page_size: 100 },
  })
}

export function listBalances(accountId: number): Promise<Schemas['BalanceListResponse']> {
  return request<Schemas['BalanceListResponse']>(`/ad-accounts/${accountId}/balances`)
}

export function recordBalance(
  accountId: number,
  body: Schemas['BalanceCreateRequest'],
): Promise<Balance> {
  return request<Balance>(`/ad-accounts/${accountId}/balances`, { method: 'POST', body })
}

// —— 投放操作记录 ————————————————————————————————————
//
// 🔴 日报发布**硬校验这张表当期非空**（services/report.py）。也就是说：不登记，
// 日报就发不出去 —— 这两个接口不是可有可无的台账，它们在运营那条动线上。

export function listActions(accountId: number): Promise<Schemas['ActionListResponse']> {
  return request<Schemas['ActionListResponse']>(`/ad-accounts/${accountId}/actions`, {
    query: { page: 1, page_size: 50 },
  })
}

/**
 * 登记一次投放调整。
 *
 * **可逆那一级**（admin.md 的三级分类）：这张表没有修改和删除，但填错了再登记
 * 一条说明即可 —— 那本身也是投放过程的一部分。所以不需要二次确认。
 *
 * `source` 不在入参里，后端也不接受：手工登记的一律 `manual`。放它出去，人会
 * 随手标成「平台抓的」，于是「这条的 reason 可不可信」再也答不上来。
 */
export function recordAction(
  accountId: number,
  body: Schemas['ActionCreateRequest'],
): Promise<Action> {
  return request<Action>(`/ad-accounts/${accountId}/actions`, { method: 'POST', body })
}

export function listAlerts(onlyOpen: boolean): Promise<Schemas['AlertListResponse']> {
  return request<Schemas['AlertListResponse']>('/alerts', {
    query: { only_open: onlyOpen, page: 1, page_size: 50 },
  })
}

export function sweepAlerts(): Promise<Schemas['SweepResponse']> {
  return request<Schemas['SweepResponse']>('/alerts/sweep', { method: 'POST' })
}

// —— 日报 ——————————————————————————————————————————————
//
// 三个写操作按「能不能收回来」分级（admin.md）：生成和修订都可逆（重新生成、
// 再改一次），**发布收不回来** —— 客户手上那份不会自己更新，所以它是这三个里
// 唯一需要二次确认的。

export function listReports(accountId: number): Promise<Schemas['ReportListResponse']> {
  return request<Schemas['ReportListResponse']>(`/ad-accounts/${accountId}/reports`, {
    query: { page: 1, page_size: 50 },
  })
}

/**
 * 生成（或重新生成）某一天的日报。
 *
 * 数字在这一刻固定下来，此后不随平台回填变化。**已发布的那份会被拒**（409）——
 * 客户手上那份不会自己更新，库里这份也就不该变。
 */
export function generateReport(accountId: number, statDate: string): Promise<Report> {
  return request<Report>(`/ad-accounts/${accountId}/reports`, {
    method: 'POST',
    body: { stat_date: statDate },
  })
}

export function getReport(reportId: number): Promise<Report> {
  return request<Report>(`/reports/${reportId}`)
}

/** 存下人工修订后的那一版。**`llm_narrative` 不会被动** —— 模型原文永不修改。 */
export function reviseReport(
  reportId: number,
  body: Schemas['ReportReviseRequest'],
): Promise<Report> {
  return request<Report>(`/reports/${reportId}`, { method: 'PATCH', body })
}

/** 发布。两条硬校验在服务端（人工修订过、操作记录非空），不满足返回 409。 */
export function publishReport(reportId: number): Promise<Report> {
  return request<Report>(`/reports/${reportId}/publish`, { method: 'POST' })
}

// —— 商品与库存 ————————————————————————————————————————
//
// 只有批量导入，没有单条录入 —— 和余额刚好相反：余额一个账户一个数，库存一个
// 客户几十上百个 SKU（docs/business/stock.md）。

export function listProducts(clientId: number): Promise<Schemas['ProductListResponse']> {
  return request<Schemas['ProductListResponse']>(`/clients/${clientId}/products`, {
    query: { page: 1, page_size: 200 },
  })
}

/**
 * 导一份库存表。
 *
 * **可逆的写操作**（admin.md 那三级里的第一级）：重传同一个时刻就是覆盖，导错了
 * 再导一次即可 —— 所以它不需要二次确认。
 */
export function importStock(clientId: number, form: FormData): Promise<StockImportResult> {
  return request<StockImportResult>(`/clients/${clientId}/stock-imports`, {
    method: 'POST',
    form,
  })
}

export function listStockRunway(clientId: number): Promise<Schemas['StockRunwayListResponse']> {
  return request<Schemas['StockRunwayListResponse']>(`/clients/${clientId}/stock-runway`)
}


// —— 自动拉取与平台凭据 ————————————————————————————————————

/** 拿一个「去平台点同意」的地址。**这一步不写任何东西**，所以重复点是安全的。 */
export function createAuthorizeUrl(label: string): Promise<Schemas['AuthorizeUrlResponse']> {
  return request<Schemas['AuthorizeUrlResponse']>('/credentials/authorize-url', {
    method: 'POST',
    body: { label },
  })
}

export function listCredentials(): Promise<Credential[]> {
  return request<Credential[]>('/credentials')
}

export function deactivateCredential(id: number): Promise<Credential> {
  return request<Credential>(`/credentials/${id}/deactivate`, { method: 'POST' })
}

/** 挂上凭据 = 开自动拉取，传 `null` = 关掉。没有第二个开关。 */
export function attachAccountCredential(
  accountId: number,
  credentialId: number | null,
): Promise<AdAccount> {
  return request<AdAccount>(`/ad-accounts/${accountId}/credential`, {
    method: 'PUT',
    body: { credential_id: credentialId },
  })
}

/** 立刻拉一次。不传日期就是滚动窗口（后端配置，默认最近 3 天）。 */
export function fetchAccountData(
  accountId: number,
  range?: { since?: string; until?: string },
): Promise<FetchResult> {
  return request<FetchResult>(`/ad-accounts/${accountId}/fetch`, {
    method: 'POST',
    body: { since: range?.since || null, until: range?.until || null },
  })
}

/** 上次拉取的结局。**从来没拉过是 404**，调用方要把它当成「没接自动拉取」而不是错误。 */
export function getAccountFetchState(accountId: number): Promise<FetchState> {
  return request<FetchState>(`/ad-accounts/${accountId}/fetch-state`)
}
