import { describe, expect, it } from 'vitest'

import {
  NO_VALUE,
  formatChange,
  formatCount,
  formatDays,
  formatMoney,
  formatMultiple,
  formatPercent,
  runwayState,
  toNumber,
} from './decimal'

describe('toNumber', () => {
  it('null 回 null，绝不回 0', () => {
    // 🔴 这一条是整个客户端最要紧的断言。Number(null) === 0，而 days_left 为
    // null 的意思是「算不出来」——写成 0 会让每个暂停投放的账户看起来都在着火。
    expect(toNumber(null)).toBeNull()
    expect(toNumber(undefined)).toBeNull()
    expect(toNumber('')).toBeNull()
  })

  it('解析不出数字时回 null，不回 NaN', () => {
    expect(toNumber('无')).toBeNull()
  })

  it('字符串数字照常解析', () => {
    expect(toNumber('123.4567')).toBe(123.4567)
    expect(toNumber('0')).toBe(0)
  })

  it('区分「0」和「没有」', () => {
    expect(toNumber('0')).toBe(0)
    expect(toNumber(null)).toBeNull()
  })
})

describe('格式化', () => {
  it('无定义一律显示破折号，不显示 0', () => {
    expect(formatMoney(null, 'USD')).toBe(NO_VALUE)
    expect(formatPercent(null)).toBe(NO_VALUE)
    expect(formatMultiple(null)).toBe(NO_VALUE)
    expect(formatDays(null)).toBe(NO_VALUE)
    expect(formatCount(null)).toBe(NO_VALUE)
  })

  it('金额带币种，千分位分组', () => {
    expect(formatMoney('1234567.891', 'USD')).toBe('USD 1,234,567.89')
    expect(formatMoney('0', 'CNY')).toBe('CNY 0.00')
  })

  it('小额可以要更多小数位', () => {
    expect(formatMoney('0.0123', 'USD', 4)).toBe('USD 0.0123')
  })

  it('负数的分组不吃掉负号', () => {
    expect(formatMoney('-1234.5', 'USD')).toBe('USD -1,234.50')
  })

  it('比率存小数，显示时乘 100', () => {
    expect(formatPercent('0.0325')).toBe('3.25%')
  })

  it('ROAS 显示成倍数', () => {
    expect(formatMultiple('4.05')).toBe('4.05×')
  })
})

describe('runwayState', () => {
  it('从没录过余额是 unknown', () => {
    expect(runwayState(null, null)).toBe('unknown')
  })

  it('有余额但近期没花钱是 idle，不是 0 天', () => {
    // 示例数据里那个暂停投放的账户就长这样。
    expect(runwayState('5000', null)).toBe('idle')
  })

  it('能算出来才是 known', () => {
    expect(runwayState('5000', '2.3')).toBe('known')
  })

  it('余额真的是 0 仍然算 known', () => {
    // 「余额 0」是一个事实，和「没录过」不是一回事。
    expect(runwayState('0', '0')).toBe('known')
  })
})

describe('formatChange', () => {
  it('给出带符号的百分比', () => {
    expect(formatChange('124.1', '100')).toBe('+24.1%')
    expect(formatChange('76', '100')).toBe('-24.0%')
  })

  it('对照期缺数据时返回 null，让调用方整段不显示', () => {
    // 🔴 补一个 0 会算出「上升了 100%」这种凭空的百分比，而客户会拿它做判断
    expect(formatChange('120', null)).toBeNull()
    expect(formatChange(null, '100')).toBeNull()
  })

  it('对照值是 0 时返回 null —— 那个除法没有意义', () => {
    expect(formatChange('120', '0')).toBeNull()
  })
})
