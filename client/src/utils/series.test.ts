import { describe, expect, it } from 'vitest'

import { addDays, daysBetween, expandRange } from './series'

describe('addDays', () => {
  it('跨月', () => {
    expect(addDays('2026-08-31', 1)).toBe('2026-09-01')
  })

  it('跨年往回', () => {
    expect(addDays('2026-01-01', -1)).toBe('2025-12-31')
  })

  it('不受本地时区影响', () => {
    // 全程走 UTC。用 new Date('2026-08-20') 的话，西半球会差一天，而 stat_date
    // 是账户时区下的自然日——它是标签不是时刻。
    expect(addDays('2026-08-20', 0)).toBe('2026-08-20')
  })
})

describe('daysBetween', () => {
  it('闭区间，含两端', () => {
    expect(daysBetween('2026-08-01', '2026-08-01')).toBe(1)
    expect(daysBetween('2026-08-01', '2026-08-14')).toBe(14)
  })

  it('顺序反了回 0，不回负数', () => {
    expect(daysBetween('2026-08-14', '2026-08-01')).toBe(0)
  })
})

describe('expandRange', () => {
  it('缺的那天是 null，不是补零的行', () => {
    // 🔴 后端不补零：「花了 0」和「没导入」是两件事。展开出来的缺口必须能被
    // 页面分辨出来，折线在那里断开而不是落到 0。
    const rows = [
      { stat_date: '2026-08-01', spend: '100' },
      { stat_date: '2026-08-03', spend: '300' },
    ]
    const slots = expandRange(rows, '2026-08-01', '2026-08-03')

    expect(slots.map((slot) => slot.stat_date)).toEqual(['2026-08-01', '2026-08-02', '2026-08-03'])
    expect(slots[1].item).toBeNull()
    expect(slots[0].item?.spend).toBe('100')
  })

  it('区间外的行丢掉', () => {
    const rows = [{ stat_date: '2026-07-31', spend: '1' }]
    const slots = expandRange(rows, '2026-08-01', '2026-08-02')
    expect(slots.every((slot) => slot.item === null)).toBe(true)
  })

  it('一天数据都没有时给出完整的空区间', () => {
    // 页面据此显示「这段时间还没有数据」，而不是一片空白。
    expect(expandRange([], '2026-08-01', '2026-08-03')).toHaveLength(3)
  })
})
