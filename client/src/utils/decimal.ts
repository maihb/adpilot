/**
 * 金额、比率、天数的显示格式化。
 *
 * 🔴 **这个文件和 `series.ts` 是客户端仅有的两个允许做数字转换的地方。**
 * `pages/` 和 `stores/` 里一律不许出现 `Number(` 或 `parseFloat(` —— 有一条
 * 扫源码的测试盯着（`tests/test_frontend_source.py`）。
 *
 * 立这条规矩是为了一个具体的坑：后端的金额和比率序列化出来**永远是 JSON
 * 字符串**（`conventions.md` 的「金额」一节），而 JS 里
 *
 *     Number(null) === 0
 *
 * 可撑天数为 `null` 的意思是「近期没花钱，算不出来」。一个 `Number()` 就把
 * 「不知道」变成了「还能撑 0 天」，于是每个暂停投放的账户都在客户屏幕上着火 ——
 * 而这件事不会有任何报错，页面照常渲染，只是意思变了。
 */

/** 后端下发的十进制数：字符串，或 `null`。**`null` 是无定义，不是 0。** */
export type DecimalStr = string | null | undefined

/**
 * 无定义时显示它。
 *
 * 不用空字符串：那一格会看起来像还没加载完，而「加载中」和「算不出来」是两件
 * 客户会做出不同反应的事。
 */
export const NO_VALUE = '—'

/**
 * 唯一的字符串 → number 转换点。
 *
 * `null` / `undefined` / 空串一律回 `null`，**绝不回 0**。解析不出数字也回
 * `null` —— 那说明后端给了预期之外的东西，显示「—」比显示 `NaN` 好。
 */
export function toNumber(value: DecimalStr): number | null {
  if (value === null || value === undefined || value === '') {
    return null
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

/**
 * 千分位分组。
 *
 * 不用 `toLocaleString`：各家小程序的 JS 引擎对它的实现和默认区域并不一致，
 * 而「同一个数在 H5 和小程序上显示不一样」是最难被发现的那类 bug。
 */
function group(value: number, fractionDigits: number): string {
  const fixed = value.toFixed(fractionDigits)
  const [integer, fraction] = fixed.split('.')
  const negative = integer.startsWith('-')
  const digits = negative ? integer.slice(1) : integer
  const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const head = `${negative ? '-' : ''}${grouped}`
  return fraction ? `${head}.${fraction}` : head
}

/**
 * 金额。**币种必须传** —— 客户手上的账户可以是不同币种的，一个不带单位的数字
 * 在这套系统里没有意义（`portal.md` 的口径一节）。
 *
 * 默认两位小数。单次点击成本这类小额传 4 位，否则 0.0123 会显示成 0.01。
 */
export function formatMoney(value: DecimalStr, currency: string, fractionDigits = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) {
    return NO_VALUE
  }
  return `${currency} ${group(parsed, fractionDigits)}`
}

/** 比率。后端存的是小数（0.03），显示时才乘 100。 */
export function formatPercent(value: DecimalStr, fractionDigits = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) {
    return NO_VALUE
  }
  return `${group(parsed * 100, fractionDigits)}%`
}

/** 倍数，给 ROAS 用。 */
export function formatMultiple(value: DecimalStr, fractionDigits = 2): string {
  const parsed = toNumber(value)
  if (parsed === null) {
    return NO_VALUE
  }
  return `${group(parsed, fractionDigits)}×`
}

/** 计数（展示、点击、转化）。转化数可以是小数——平台会给部分归因的值。 */
export function formatCount(value: DecimalStr | number, fractionDigits = 0): string {
  const parsed = typeof value === 'number' ? value : toNumber(value)
  if (parsed === null) {
    return NO_VALUE
  }
  return group(parsed, fractionDigits)
}

/** 可撑天数。一位小数——「还能撑 2.3 天」比「2 天」更能促成一次充值。 */
export function formatDays(value: DecimalStr): string {
  const parsed = toNumber(value)
  if (parsed === null) {
    return NO_VALUE
  }
  return `${group(parsed, 1)} 天`
}

/**
 * 可撑天数的三态。
 *
 * 🔴 **这是一个数字，但它有三种意思**，把前两种画成「0 天」是这个页面最容易犯、
 * 也最伤客户信任的错：
 *
 * - `unknown` —— 从没录过余额。是「不知道」，不是「没事」，更不是「没钱了」
 * - `idle` —— 有余额，但近期没花钱，可撑天数无定义。示例数据里第四个账户
 *   （暂停投放的那个）专门演示这个边界
 * - `known` —— 真的能算，可以按 `is_alerting` 上色
 */
export type RunwayState = 'unknown' | 'idle' | 'known'

export function runwayState(available: DecimalStr, daysLeft: DecimalStr): RunwayState {
  if (toNumber(available) === null) {
    return 'unknown'
  }
  if (toNumber(daysLeft) === null) {
    return 'idle'
  }
  return 'known'
}

/**
 * 环比变化，给日报用。**算不出来返回 `null` —— 调用方整段不显示。**
 *
 * 三种算不出来，处理方式都一样：对照期没有数据（后端把 baseline 三列同时置空）、
 * 对照值是 0（这个除法没有意义）、当期值本身无定义（比如没有转化时的 CPA）。
 *
 * 🔴 **返回「+100%」比留白危险得多。** 客户会拿那个百分比去做判断，而它根本没有
 * 基线 —— 这正是后端宁可把三列一起留空、也不补一个 0 的理由（`reports.md` 的
 * 数字口径一节）。
 */
export function formatChange(current: DecimalStr, baseline: DecimalStr): string | null {
  const now = toNumber(current)
  const then = toNumber(baseline)
  if (now === null || then === null || then === 0) {
    return null
  }
  const percent = ((now - then) / then) * 100
  return `${percent >= 0 ? '+' : ''}${group(percent, 1)}%`
}
