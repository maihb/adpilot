/**
 * 区间聚合：把一段时间线加成几个总数。
 *
 * ⚠️ **这里用 JS 的 number 求和，不是 Decimal。** 浏览器和小程序里没有 Decimal，
 * 而这几个数是**显示用的合计**，不是对账口径 —— 双精度的误差在 1e-10 量级，四舍
 * 五入到两位小数之后看不出来。真要拿去对账的数字必须来自后端，那边用的是
 * PostgreSQL 的 `numeric`（`conventions.md` 的「金额」一节）。
 *
 * 之所以还是把它集中到这里：跨天求和最容易出的错**不是精度，是把 `null` 当 0**，
 * 而那个错在页面里散着写就没人测得到。
 */

import { toNumber, type DecimalStr } from './decimal'

/** 时间线里参与求和的那几个字段。 */
export interface Summable {
  spend: DecimalStr
  conversions: DecimalStr
  revenue: DecimalStr
  impressions: number
  clicks: number
}

export interface Totals {
  spend: number
  conversions: number
  revenue: number
  impressions: number
  clicks: number
  /** 真正有数据的天数。和区间长度不一样时，说明中间有几天没导入。 */
  days: number
}

/**
 * 求和。
 *
 * 🔴 `null` 的那一项**跳过**，不当 0 加进去 —— 两者在总数上恰好等价，但在
 * `days` 上不等价，而 `days` 正是「这个合计可不可信」的依据。
 */
export function sumSeries(rows: readonly Summable[]): Totals {
  const totals: Totals = {
    spend: 0,
    conversions: 0,
    revenue: 0,
    impressions: 0,
    clicks: 0,
    days: 0,
  }
  for (const row of rows) {
    totals.spend += toNumber(row.spend) ?? 0
    totals.conversions += toNumber(row.conversions) ?? 0
    totals.revenue += toNumber(row.revenue) ?? 0
    totals.impressions += row.impressions
    totals.clicks += row.clicks
    totals.days += 1
  }
  return totals
}

/** 一段时间线里最大的花费，用来给条形图定标尺。全空时回 0。 */
export function peakSpend(rows: readonly Summable[]): number {
  let peak = 0
  for (const row of rows) {
    const value = toNumber(row.spend)
    if (value !== null && value > peak) {
      peak = value
    }
  }
  return peak
}

/**
 * 派生指标的除法，**回字符串**，好让它和后端下发的形状一致（都能直接喂给
 * `formatMoney` 这些函数）。
 *
 * 🔴 分母为 0 回 `null`（无定义），不回 0 —— 和后端 `_divide` 同一个约定：
 * 「今天没有转化」和「今天 CPA 是 0 元」在日报里天差地别。
 */
export function divide(numerator: number, denominator: number): string | null {
  if (denominator === 0 || !Number.isFinite(denominator) || !Number.isFinite(numerator)) {
    return null
  }
  return String(numerator / denominator)
}

/** 条形图的宽度百分比。有值但很小的那天也留 2% ——「几乎没花钱」和「没数据」要看得出差别。 */
export function barPercent(value: DecimalStr, peak: number): string {
  const parsed = toNumber(value)
  if (parsed === null || peak <= 0 || parsed <= 0) {
    return '0%'
  }
  return `${Math.max(2, (parsed / peak) * 100)}%`
}
