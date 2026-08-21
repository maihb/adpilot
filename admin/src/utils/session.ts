/**
 * 运营票据的本地存取。
 *
 * 键名带 `admin.` 前缀：两个前端理论上可能被部署到同一个域名下（后台挂
 * `/admin/`、客户端挂 `/`），那时 localStorage 是共享的 —— 同名键会让两张票
 * 互相覆盖，而症状是「登录之后马上又要登录」。
 */

export interface Session {
  token: string
  /** ISO 时刻。**8 小时，不滑动续期** —— 后台的权限比客户端大得多。 */
  expiresAt: string
  username: string
}

const KEY = 'adpilot.admin.session'

export function loadSession(): Session | null {
  try {
    const raw = localStorage.getItem(KEY)
    return raw ? (JSON.parse(raw) as Session) : null
  } catch {
    return null
  }
}

export function saveSession(session: Session): void {
  localStorage.setItem(KEY, JSON.stringify(session))
}

export function clearSession(): void {
  localStorage.removeItem(KEY)
}
