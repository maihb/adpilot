/**
 * 票据的本地存取。**除了这张票，客户端什么都不存**（设计文档第十一节：不做
 * 离线缓存 —— 数据每天才更新一次，缓存过期制造的困惑远多于它省下的等待）。
 */

export interface Session {
  token: string
  /** ISO 时刻。到期后只能重新扫码。 */
  expiresAt: string
  /** 首屏标题要用它，省得一进来先转圈等 `/portal/me`。 */
  clientName: string
}

const KEY = 'adpilot.session'

export function loadSession(): Session | null {
  try {
    const raw = uni.getStorageSync(KEY)
    return raw ? (JSON.parse(raw as string) as Session) : null
  } catch {
    // 存储被清、或存进去的是上一个版本的形状。当作没登录处理，重扫一次就好 ——
    // 在这里抛出去会让整个应用在启动阶段白屏。
    return null
  }
}

export function saveSession(session: Session): void {
  uni.setStorageSync(KEY, JSON.stringify(session))
}

/**
 * 退出登录。
 *
 * ⚠️ **服务端没有对应动作。** 票是自签的、自包含的，撤销不了（见
 * `docs/business/auth.md`）—— 这里删掉的只是本机这一份，那张票在到期前仍然
 * 有效。这句话在「关于」页里要对客户说明白。
 */
export function clearSession(): void {
  uni.removeStorageSync(KEY)
}
