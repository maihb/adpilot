/**
 * 时间线的区间展开。
 *
 * 🔴 **后端不补零。** 没有数据的那天**不在返回的数组里** —— 「那天花了 0」和
 * 「那天没导入」是两件事（`portal.md` 的口径一节）。前端只要按数组顺序连线，
 * 就等于宣称中间那几天花了钱、而且是线性变化的。
 *
 * 这个文件把区间补全成连续的一天一格，缺的那天是 `null` **而不是 0**。画折线
 * 时那一格要断开，列表里那一行要显示「未导入」。
 */

/** 任何带 `stat_date` 的一行。日期是 `YYYY-MM-DD`。 */
export interface Dated {
  stat_date: string
}

/** 区间里的一天。`item` 为 `null` 表示**那天没有数据**，不是那天花了 0。 */
export interface Slot<T extends Dated> {
  stat_date: string
  item: T | null
}

/**
 * 日期算术全程走 UTC。
 *
 * 🔴 `stat_date` 是**账户时区下的自然日**，它是一个标签，不是一个时刻
 * （`glossary.md` 的时区一节）。`new Date('2026-08-20')` 会按客户手机所在时区
 * 去解释它，在西半球直接差一天 —— 而客户的手机时区和广告账户时区**本来就常常
 * 不是一回事**，这正是这套系统要说清的口径。
 */
export function addDays(isoDate: string, delta: number): string {
  const [year, month, day] = isoDate.split('-').map((part) => Number(part))
  const shifted = new Date(Date.UTC(year, month - 1, day + delta))
  return shifted.toISOString().slice(0, 10)
}

/** 闭区间天数。`start` 晚于 `end` 时回 0。 */
export function daysBetween(start: string, end: string): number {
  const from = Date.parse(`${start}T00:00:00Z`)
  const to = Date.parse(`${end}T00:00:00Z`)
  if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) {
    return 0
  }
  return Math.round((to - from) / 86_400_000) + 1
}

/**
 * 把稀疏的时间线展开成 `start`–`end` 的连续区间（闭区间，升序）。
 *
 * 区间外的行会被丢掉；同一天出现多行时保留最后一行（后端不会这样给，但前端
 * 不该因为这个崩掉）。
 */
export function expandRange<T extends Dated>(items: T[], start: string, end: string): Slot<T>[] {
  const byDate = new Map<string, T>()
  for (const item of items) {
    byDate.set(item.stat_date, item)
  }

  const total = daysBetween(start, end)
  const slots: Slot<T>[] = []
  let cursor = start
  for (let index = 0; index < total; index += 1) {
    slots.push({ stat_date: cursor, item: byDate.get(cursor) ?? null })
    cursor = addDays(cursor, 1)
  }
  return slots
}

/** 今天（UTC 日历）。默认区间的右端，够用——差一天不影响「近 14 天」这种粒度。 */
export function todayIso(): string {
  return new Date().toISOString().slice(0, 10)
}
