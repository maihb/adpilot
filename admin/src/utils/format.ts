/**
 * 金额、比率、时刻的显示格式化。
 *
 * 🔴 **和客户端那份是同一套规矩，代码却是各写各的**（`client/src/utils/`）。
 * 不抽共享包是刻意的（[内部后台设计 §2](../../../docs/design/2026-08-21-admin-console.md)）：
 * 两个前端的运行时不同，而真正相同的逻辑只有这几十行。
 *
 * 那条最要紧的规矩在这里一字不变 —— 后端的金额和比率下发的永远是**字符串**，而
 *
 *     Number(null) === 0
 *
 * 后台里同样有「无定义」：没有转化的那天 CPA 是 `null`，近期没花钱的账户可撑天数
 * 是 `null`。把它们显示成 0，运营会据此做出错误的判断，而这件事不会报错。
 */

/** 后端下发的十进制数：字符串，或 `null`（无定义 —— **不是 0**）。 */
export type DecimalStr = string | null | undefined

export const NO_VALUE = '—'

/** 唯一的字符串 → number 转换点。`null` 一律回 `null`，绝不回 0。 */
export function toNumber(value: DecimalStr): number | null {
  if (value === null || value === undefined || value === '') {
    return null
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function group(value: number, fractionDigits: number): string {
  const fixed = value.toFixed(fractionDigits)
  const [integer, fraction] = fixed.split('.')
  const negative = integer.startsWith('-')
  const digits = negative ? integer.slice(1) : integer
  const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const head = `${negative ? '-' : ''}${grouped}`
  return fraction ? `${head}.${fraction}` : head
}

/** 金额。**币种必须传** —— 后台会同时看到多个币种的账户，不带单位的数字没有意义。 */
export function formatMoney(value: DecimalStr, currency: string, fractionDigits = 2): string {
  const parsed = toNumber(value)
  return parsed === null ? NO_VALUE : `${currency} ${group(parsed, fractionDigits)}`
}

/** 比率。后端存小数，显示时乘 100。 */
export function formatPercent(value: DecimalStr, fractionDigits = 2): string {
  const parsed = toNumber(value)
  return parsed === null ? NO_VALUE : `${group(parsed * 100, fractionDigits)}%`
}

/** 倍数，给 ROAS 用。 */
export function formatMultiple(value: DecimalStr, fractionDigits = 2): string {
  const parsed = toNumber(value)
  return parsed === null ? NO_VALUE : `${group(parsed, fractionDigits)}×`
}

/** 计数。转化数可以是小数——平台会给部分归因的值。 */
export function formatCount(value: DecimalStr | number, fractionDigits = 0): string {
  const parsed = typeof value === 'number' ? value : toNumber(value)
  return parsed === null ? NO_VALUE : group(parsed, fractionDigits)
}

function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value)
}

/**
 * 真实时刻 → `YYYY-MM-DD HH:mm`，按本机时区。
 *
 * ⚠️ **只用于时刻**（`created_at` / `expires_at` / `opened_at`）。`stat_date` 是
 * 账户时区下的自然日，是个标签 —— 那种值直接当字符串显示，`new Date()` 一下会按
 * 本机时区偏移一天。后台会同时看到多个时区的账户，这个区分比客户端那边更容易踩。
 */
export function formatInstant(iso: string | null | undefined): string {
  if (!iso) {
    return NO_VALUE
  }
  const millis = Date.parse(iso)
  if (!Number.isFinite(millis)) {
    return NO_VALUE
  }
  const at = new Date(millis)
  const date = `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`
  return `${date} ${pad(at.getHours())}:${pad(at.getMinutes())}`
}

/** 距离某个时刻还有多久，用于「票还剩几小时」。已过期回 0。 */
export function hoursUntil(iso: string | null | undefined, now: number): number {
  if (!iso) {
    return 0
  }
  const deadline = Date.parse(iso)
  if (!Number.isFinite(deadline)) {
    return 0
  }
  return Math.max(0, (deadline - now) / 3_600_000)
}
