import { describe, expect, it } from 'vitest'

import { NO_VALUE, formatInstant, formatMoney, formatPercent, hoursUntil, toNumber } from './format'

describe('toNumber', () => {
  it('🔴 null 回 null，绝不回 0', () => {
    // 后台里同样有「无定义」：没有转化的那天 CPA 是 null，近期没花钱的账户可撑
    // 天数是 null。显示成 0 会让运营据此做出错误判断，而这不会报错。
    expect(toNumber(null)).toBeNull()
    expect(toNumber(undefined)).toBeNull()
    expect(toNumber('')).toBeNull()
  })

  it('解析不出数字回 null，不回 NaN', () => {
    expect(toNumber('无')).toBeNull()
  })

  it('0 是 0，和「没有」分得开', () => {
    expect(toNumber('0')).toBe(0)
  })
})

describe('格式化', () => {
  it('无定义一律破折号', () => {
    expect(formatMoney(null, 'USD')).toBe(NO_VALUE)
    expect(formatPercent(null)).toBe(NO_VALUE)
    expect(formatInstant(null)).toBe(NO_VALUE)
  })

  it('金额带币种和千分位', () => {
    expect(formatMoney('1234567.891', 'USD')).toBe('USD 1,234,567.89')
  })

  it('负数不吃掉负号', () => {
    expect(formatMoney('-1234.5', 'EUR')).toBe('EUR -1,234.50')
  })

  it('比率乘 100', () => {
    expect(formatPercent('0.0325')).toBe('3.25%')
  })

  it('时刻带年份 —— 后台要翻历史，客户端不用', () => {
    expect(formatInstant('2026-08-05T09:07:00Z')).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/)
  })

  it('解析不出的时刻不显示 1970', () => {
    expect(formatInstant('不是时间')).toBe(NO_VALUE)
  })
})

describe('hoursUntil', () => {
  const now = Date.parse('2026-08-21T00:00:00Z')

  it('已过期回 0，不回负数', () => {
    expect(hoursUntil('2026-08-20T00:00:00Z', now)).toBe(0)
  })

  it('缺失当作已过期', () => {
    expect(hoursUntil(null, now)).toBe(0)
  })

  it('还剩几小时', () => {
    expect(hoursUntil('2026-08-21T08:00:00Z', now)).toBeCloseTo(8, 6)
  })
})
