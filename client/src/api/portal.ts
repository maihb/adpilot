/**
 * 五个客户端接口的薄封装。
 *
 * **类型一个都不手写**，全部从 `generated/schema.ts` 取 —— 那份文件由后端的
 * OpenAPI 生成，后端改了出参形状这里就编译不过。这是这个客户端在没有 E2E 的
 * 情况下，对「契约还对不对」唯一的机器保证（设计文档第一节）。
 */

import type { components } from './generated/schema'
import { request } from './request'

type Schemas = components['schemas']

export type PortalProfile = Schemas['PortalProfileResponse']
export type PortalAccount = Schemas['PortalAccountItem']
export type PortalMetrics = Schemas['PortalMetricsResponse']
export type PortalMetricDay = Schemas['PortalMetricDay']
export type PortalRunway = Schemas['PortalRunwayResponse']
export type PortalAlert = Schemas['PortalAlertItem']

export function getProfile(): Promise<PortalProfile> {
  return request<PortalProfile>('/portal/me')
}

export function listAccounts(): Promise<Schemas['PortalAccountListResponse']> {
  return request<Schemas['PortalAccountListResponse']>('/portal/accounts')
}

/**
 * 一个账户的每日时间线。
 *
 * ⚠️ 返回的 `items` 是**稀疏**的 —— 没有数据的那天不在里面。画之前一律先过
 * `utils/series.ts` 的 `expandRange`。
 */
export function listMetrics(accountId: number, start: string, end: string): Promise<PortalMetrics> {
  return request<PortalMetrics>(`/portal/accounts/${accountId}/daily-metrics`, {
    query: { start, end },
  })
}

export function getRunway(accountId: number): Promise<PortalRunway> {
  return request<PortalRunway>(`/portal/accounts/${accountId}/balance-runway`)
}

export function listAlerts(onlyOpen = true): Promise<Schemas['PortalAlertListResponse']> {
  return request<Schemas['PortalAlertListResponse']>('/portal/alerts', {
    query: { only_open: onlyOpen },
  })
}
