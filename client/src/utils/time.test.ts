import { describe, expect, it } from 'vitest'

import { NO_TIME, formatInstant } from './time'

describe('formatInstant', () => {
  it('缺失或无效一律显示破折号，不显示 1970', () => {
    // Date.parse 解析失败会给 NaN，直接 new Date(NaN) 再格式化会出 "NaN-NaN"，
    // 而 new Date(null) 会给 1970-01-01 —— 两种都比一个破折号糟。
    expect(formatInstant(null)).toBe(NO_TIME)
    expect(formatInstant(undefined)).toBe(NO_TIME)
    expect(formatInstant('不是时间')).toBe(NO_TIME)
  })

  it('补零到两位', () => {
    const shown = formatInstant('2026-08-05T09:07:00Z')
    expect(shown).toMatch(/^\d{2}-\d{2} \d{2}:\d{2}$/)
  })

  it('带时区的时刻按本地时区显示（这一条和 stat_date 正好相反）', () => {
    // 同一个时刻用两种时区写法表达，显示结果必须一致 —— 说明它确实按时刻解析，
    // 而不是把字符串前 10 位当日历用。
    expect(formatInstant('2026-08-20T00:00:00Z')).toBe(formatInstant('2026-08-20T08:00:00+08:00'))
  })
})
