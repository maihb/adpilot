import { describe, expect, it } from 'vitest'

import { barPercent, divide, peakSpend, sumSeries } from './aggregate'

const row = (spend: string | null, conversions = '1') => ({
  spend,
  conversions,
  revenue: '0',
  impressions: 100,
  clicks: 10,
})

describe('sumSeries', () => {
  it('空区间给一组 0，days 也是 0', () => {
    expect(sumSeries([])).toEqual({
      spend: 0,
      conversions: 0,
      revenue: 0,
      impressions: 0,
      clicks: 0,
      days: 0,
    })
  })

  it('字符串金额能加起来', () => {
    const totals = sumSeries([row('100.5'), row('200.25')])
    expect(totals.spend).toBeCloseTo(300.75, 6)
    expect(totals.days).toBe(2)
  })

  it('null 的那一项不当 0 加，但仍然计入 days', () => {
    // days 是「这个合计可不可信」的依据，所以它数的是行数不是有值数。
    const totals = sumSeries([row('100'), row(null)])
    expect(totals.spend).toBe(100)
    expect(totals.days).toBe(2)
  })
})

describe('peakSpend', () => {
  it('全是 null 时回 0，不炸', () => {
    expect(peakSpend([row(null), row(null)])).toBe(0)
  })

  it('取最大值', () => {
    expect(peakSpend([row('10'), row('99.9'), row('50')])).toBe(99.9)
  })
})

describe('divide', () => {
  it('🔴 分母为 0 回 null，不回 0', () => {
    // 和后端 _divide 同一个约定：「没有转化」不是「CPA 等于 0」。
    expect(divide(100, 0)).toBeNull()
  })

  it('正常相除回字符串，形状和后端下发的一致', () => {
    expect(divide(100, 4)).toBe('25')
  })
})

describe('barPercent', () => {
  it('没有数据的那天宽度是 0', () => {
    expect(barPercent(null, 100)).toBe('0%')
  })

  it('花了 0 的那天宽度也是 0，但它和「没数据」在别处区分', () => {
    expect(barPercent('0', 100)).toBe('0%')
  })

  it('很小的值仍然留一点宽度', () => {
    expect(barPercent('0.01', 1000)).toBe('2%')
  })

  it('峰值那天占满', () => {
    expect(barPercent('100', 100)).toBe('100%')
  })
})
