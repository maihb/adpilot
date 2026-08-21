/**
 * 后台**唯一**的请求出口。认证头、401 之后的重新登录、错误消息的提取都在这里。
 */

import { loadSession } from '../utils/session'
import { waitForLogin } from './gate'

/** 默认相对路径，由 vite 的 dev proxy 转发；部署时前后端同源。 */
const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? '/api'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/**
 * 🔴 票过期时这个请求**没有被重放**，人要自己再点一次。
 *
 * 只重放 GET 是刻意的：导入不是幂等的（`raw_reports` append-only，重放会多一份
 * 快照），建客户、发邀请码同理。让人重点一次的成本是一次点击，而一次悄悄的重复
 * 导入没有任何人会发现。
 */
export class NeedsRedo extends Error {
  constructor(message = '刚才登录过期了，已经重新登录 —— 请再操作一次') {
    super(message)
    this.name = 'NeedsRedo'
  }
}

/**
 * 🔴 **把后端说的那句话原样显示出来**，只有它真的没说话时才用兜底文案。
 *
 * 两类错误都要显示 message，但原因不同：`NeedsRedo` 说的是「票过期了、已经重新
 * 登录、请再点一次」，`ApiError` 的 message 是后端 4xx 里那句 `detail` —— 而这个
 * 项目的后端**刻意把 detail 写成能指导操作的**（认不出日期列时列出表头、日报发不
 * 出去时说清缺的是哪一件）。换成一句「操作失败」等于让人去猜，或者来问人。
 *
 * 做成共用函数而不是每个页面写一遍 `instanceof`：写漏一个不会报错，只会安静地把
 * 后端那句话吞掉 —— 而错误分支恰恰是最少被走到、最晚被发现的地方。
 */
export function reason(error: unknown, fallback: string): string {
  // `ApiError` 和 `NeedsRedo` 都继承 `Error`，所以这一行就够了 —— 再显式判一次那
  // 两个类型是冗余的（写过一版，被单测「改了却不红」逼出来的）。`fallback` 只在
  // 抛出来的东西**根本不是 Error** 时用得上。
  return error instanceof Error ? error.message : fallback
}

type Method = 'GET' | 'POST' | 'PATCH'

export interface RequestOptions {
  method?: Method
  body?: unknown
  /** multipart 上传用它。**给了 form 就不要给 body。** */
  form?: FormData
  query?: Record<string, string | number | boolean | undefined>
  /** 默认带票；只有登录接口是 false。 */
  auth?: boolean
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = `${BASE}${path}`
  if (!query) {
    return url
  }
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) {
      params.set(key, String(value))
    }
  }
  const encoded = params.toString()
  return encoded ? `${url}?${encoded}` : url
}

async function detailOf(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    if (typeof body.detail === 'string') {
      return body.detail
    }
    // 422 的 detail 是一个数组（FastAPI 的校验错误）。原样 JSON 出来比吞掉强：
    // 后端在认不出日期列时会把表头列在里面，那正是运营要拿去改导出设置的信息。
    if (body.detail !== undefined) {
      return JSON.stringify(body.detail)
    }
  } catch {
    // 响应不是 JSON（502、网关 HTML 页），走下面的兜底
  }
  return `请求失败（${response.status}）`
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = {}
  const session = loadSession()
  if (options.auth !== false && session) {
    headers.Authorization = `Bearer ${session.token}`
  }

  let body: BodyInit | undefined
  if (options.form) {
    // ⚠️ 不要自己设 Content-Type：multipart 的 boundary 由浏览器生成，手写一个
    // Content-Type 会让 boundary 对不上，后端解析出一个空表单。
    body = options.form
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  return fetch(buildUrl(path, options.query), {
    method: options.method ?? 'GET',
    headers,
    body,
  })
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  let response = await send(path, options)

  if (response.status === 401 && options.auth !== false) {
    // 不跳转、不清空页面：就地等一个登录框（api/gate.ts）。
    await waitForLogin()
    if (!loadSession()) {
      throw new ApiError(401, '需要登录')
    }
    if ((options.method ?? 'GET') === 'GET') {
      response = await send(path, options)
    } else {
      throw new NeedsRedo()
    }
  }

  if (!response.ok) {
    throw new ApiError(response.status, await detailOf(response))
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}
