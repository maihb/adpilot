/**
 * 时刻的显示。
 *
 * 🔴 **和 `stat_date` 正好相反，别混。**
 *
 * - `stat_date`（`series.ts`）是**账户时区下的自然日**，一个标签。它不能被
 *   `new Date()` 解释，那会按手机时区偏移一天。
 * - 这里处理的是 `opened_at` / `captured_at` 这类**真实时刻**（带时区的
 *   ISO 串）。它们按客户手机的本地时区显示才是对的 —— 「这条告警是什么时候
 *   开始的」问的是真实时间，不是某个账户的日历。
 *
 * 两者在出参里长得像（都是字符串），处理方式却相反，所以分开两个文件。
 */

/** 无效或缺失的时刻显示成它。和金额那边用同一个符号，视觉上一致。 */
export const NO_TIME = '—'

function pad(value: number): string {
  return value < 10 ? `0${value}` : String(value)
}

/**
 * 显示成 `MM-DD HH:mm`。
 *
 * 不显示年份：客户端只看得到近期的告警，年份是纯噪音；真要追溯得去内部后台。
 */
export function formatInstant(iso: string | null | undefined): string {
  if (!iso) {
    return NO_TIME
  }
  const millis = Date.parse(iso)
  if (!Number.isFinite(millis)) {
    return NO_TIME
  }
  const at = new Date(millis)
  return `${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(at.getHours())}:${pad(at.getMinutes())}`
}
