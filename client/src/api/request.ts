/**
 * 客户端**唯一**的请求出口。
 *
 * 认证头、并发上限、401 续期只在这里出现一次。散到页面里去的话，漏掉一个的
 * 症状是「某个页面偶尔要重新扫码」—— 那种 bug 没人能稳定复现。
 */

import { clearSession, loadSession, saveSession } from '../utils/storage'
import { isExpired } from '../utils/token'

/**
 * 后端地址，用 `VITE_API_BASE` 覆盖。
 *
 * 默认值两端不同，这是 uni-app「一套代码」里绕不过的一处：
 *
 * - **H5 走相对路径**。开发时由 vite 的 dev proxy 转发到 8000（见
 *   `vite.config.ts`），部署时前端与后端同源。这样本地开发不需要为跨域去后端
 *   开 CORS —— 一旦开了那个口子，它的默认值很容易被人一路带到生产。
 * - **小程序没有「相对路径」这回事**，`uni.request` 必须给完整 URL，所以那边
 *   默认指向本地 compose，真正部署时必须设 `VITE_API_BASE`（还要在小程序后台
 *   把域名加进白名单）。
 */
// ⚠️ 一次声明 + 条件赋值，不要在两个条件分支里各声明一次。条件编译是**构建期
// 的注释处理**，`vue-tsc` 看不见它 —— 两个 `const` 在类型检查那里就是重复声明，
// 而那道门禁跑在 CI 里。
let defaultBase = '/api'
// #ifdef MP-WEIXIN
defaultBase = 'http://localhost:8000/api'
// #endif

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? defaultBase

/**
 * 🔴 并发上限。
 *
 * 首页要按账户数并发取可撑天数，而微信小程序的同时请求数上限是 10 —— 撞上去
 * 之后被挤掉的请求会静默失败，症状是「偶尔有个卡片空着」，且只在账户多的客户
 * 身上出现。留出余量，取 5。
 */
const MAX_CONCURRENCY = 5

let active = 0
const waiting: Array<() => void> = []

async function acquire(): Promise<void> {
  if (active < MAX_CONCURRENCY) {
    active += 1
    return
  }
  await new Promise<void>((resolve) => waiting.push(resolve))
  active += 1
}

function release(): void {
  active -= 1
  const next = waiting.shift()
  if (next) {
    next()
  }
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/** 没登录、或票已经不能用了。页面收到它只需要跳扫码页。 */
export class NotAuthenticated extends Error {
  constructor(message = '登录已过期，请重新扫码') {
    super(message)
    this.name = 'NotAuthenticated'
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST'
  query?: Record<string, string | number | boolean | undefined>
  body?: unknown
  /** 默认带票。只有换票那两个接口是 false。 */
  auth?: boolean
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = `${BASE}${path}`
  if (!query) {
    return url
  }
  const parts: string[] = []
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) {
      parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    }
  }
  return parts.length ? `${url}?${parts.join('&')}` : url
}

interface RawResponse {
  statusCode: number
  data: unknown
}

function send(url: string, options: RequestOptions, token: string | null): Promise<RawResponse> {
  const header: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) {
    header.Authorization = `Bearer ${token}`
  }
  return new Promise((resolve, reject) => {
    uni.request({
      url,
      method: options.method ?? 'GET',
      data: options.body as Record<string, unknown> | undefined,
      header,
      success: (res) => resolve({ statusCode: res.statusCode, data: res.data }),
      fail: (err) => reject(new ApiError(0, err.errMsg || '网络请求失败')),
    })
  })
}

function detailOf(data: unknown, fallback: string): string {
  if (data && typeof data === 'object' && 'detail' in data) {
    const detail = (data as { detail: unknown }).detail
    if (typeof detail === 'string') {
      return detail
    }
  }
  return fallback
}

/**
 * 🔴 一次启动只准续一次票。
 *
 * 续期本身也会 401（到了 90 天绝对上限、或者客户已停止合作）。那时若还走
 * 「401 → 续期」这条通路就是死循环，而在小程序里它表现为**白屏转圈**，不是
 * 报错 —— 查起来毫无线索。
 */
let renewedThisLaunch = false

async function renew(): Promise<string | null> {
  if (renewedThisLaunch) {
    return null
  }
  renewedThisLaunch = true

  const session = loadSession()
  if (!session) {
    return null
  }
  try {
    const raw = await send(buildUrl('/auth/client/refresh'), { method: 'POST' }, session.token)
    if (raw.statusCode !== 200) {
      return null
    }
    const body = raw.data as { token: string; expires_at: string }
    saveSession({ ...session, token: body.token, expiresAt: body.expires_at })
    return body.token
  } catch {
    return null
  }
}

/** 票没了：清掉本地那份，回扫码页。**页面不需要各自处理这件事。** */
function bounceToRedeem(): never {
  clearSession()
  uni.reLaunch({ url: '/pages/redeem/redeem' })
  throw new NotAuthenticated()
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const needAuth = options.auth !== false
  let token: string | null = null

  if (needAuth) {
    const session = loadSession()
    if (!session || isExpired(session.expiresAt, Date.now())) {
      // 本地就能判定过期时不必先撞一次 401 —— 那一趟纯属浪费，而且在弱网下
      // 会让「登录过期」这件事晚好几秒才告诉客户。
      bounceToRedeem()
    }
    token = session.token
  }

  await acquire()
  try {
    let raw = await send(buildUrl(path, options.query), options, token)

    if (raw.statusCode === 401 && needAuth) {
      const fresh = await renew()
      if (!fresh) {
        bounceToRedeem()
      }
      raw = await send(buildUrl(path, options.query), options, fresh)
      if (raw.statusCode === 401) {
        bounceToRedeem()
      }
    }

    if (raw.statusCode >= 400) {
      throw new ApiError(raw.statusCode, detailOf(raw.data, `请求失败（${raw.statusCode}）`))
    }
    return raw.data as T
  } finally {
    release()
  }
}

/** 换到新票之后调它，否则同一次启动里第二次过期就不会再尝试续期了。 */
export function resetRenewGuard(): void {
  renewedThisLaunch = false
}
