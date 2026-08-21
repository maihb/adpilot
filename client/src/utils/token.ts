/**
 * 票的剩余有效期判定。
 *
 * 单独成文件是因为它满足和 `decimal.ts` 同一个条件：**算错了不会报错**，只会
 * 表现成「客户偶尔要重新扫码」或者「票过期了还在用」，两种都很难复现。
 */

/** 剩余有效期不足它就静默续一次。两天足够覆盖一个周末不打开的客户。 */
export const RENEW_BEFORE_MS = 2 * 24 * 60 * 60 * 1000

/** 距离到期还有多少毫秒。解析不出来的一律当作已过期——宁可多换一次票。 */
export function millisUntil(expiresAt: string | null | undefined, now: number): number {
  if (!expiresAt) {
    return 0
  }
  const deadline = Date.parse(expiresAt)
  return Number.isFinite(deadline) ? deadline - now : 0
}

export function isExpired(expiresAt: string | null | undefined, now: number): boolean {
  return millisUntil(expiresAt, now) <= 0
}

/** 该不该在冷启动时静默续期。已经过期的**不续**——过期的票换不出新票，只能重扫。 */
export function shouldRenew(expiresAt: string | null | undefined, now: number): boolean {
  const remaining = millisUntil(expiresAt, now)
  return remaining > 0 && remaining < RENEW_BEFORE_MS
}
