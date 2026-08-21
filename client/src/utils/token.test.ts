import { describe, expect, it } from 'vitest'

import { RENEW_BEFORE_MS, isExpired, millisUntil, shouldRenew } from './token'

const NOW = Date.parse('2026-08-21T00:00:00Z')
const hours = (count: number) => new Date(NOW + count * 3600_000).toISOString()

describe('票的有效期', () => {
  it('没有到期时间一律当作已过期', () => {
    // 宁可多换一次票：反过来（当作有效）的症状是每个请求都 401。
    expect(isExpired(null, NOW)).toBe(true)
    expect(millisUntil(undefined, NOW)).toBe(0)
  })

  it('解析不出来的时间戳也当作已过期', () => {
    expect(isExpired('不是时间', NOW)).toBe(true)
  })

  it('还剩很久就不续期', () => {
    expect(shouldRenew(hours(24 * 6), NOW)).toBe(false)
  })

  it('进入两天窗口才续期', () => {
    expect(shouldRenew(hours(24), NOW)).toBe(true)
    expect(RENEW_BEFORE_MS).toBe(2 * 24 * 3600_000)
  })

  it('🔴 已经过期的不续期', () => {
    // 过期的票换不出新票（后端 refresh 要一张未过期的票），只能重新扫码。
    // 这里若回 true，冷启动就会白白撞一次 401 再跳扫码页。
    expect(shouldRenew(hours(-1), NOW)).toBe(false)
    expect(isExpired(hours(-1), NOW)).toBe(true)
  })
})
