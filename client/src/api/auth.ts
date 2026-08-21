/**
 * 换票。**这是仅有的两个不带票的请求**（另一个是运营登录，客户端用不到）。
 */

import type { components } from './generated/schema'
import { request, resetRenewGuard } from './request'
import { loadSession, saveSession } from '../utils/storage'

type ClientToken = components['schemas']['ClientTokenResponse']

/**
 * 邀请码换票，成功即写入本地。
 *
 * 码无效、过期、被作废、客户已停止合作，后端一律回同一个 404 —— 分开报错等于
 * 告诉试码的人「这个码是真的，只是过期了」。所以前端这里也只能给一句话。
 */
export async function redeemInvite(code: string): Promise<ClientToken> {
  const body = await request<ClientToken>('/auth/redeem', {
    method: 'POST',
    body: { code: code.trim() },
    auth: false,
  })
  saveSession({
    token: body.token,
    expiresAt: body.expires_at,
    clientName: body.client_name,
  })
  // 换到新票，续期额度重新给一次；不重置的话，同一次启动里下一次过期就不会再
  // 尝试续期，而是直接把客户弹回扫码页。
  resetRenewGuard()
  return body
}

/** 冷启动时的静默续期。失败**不报错** —— 票还没过期，下次再说。 */
export async function refreshClientToken(): Promise<boolean> {
  const session = loadSession()
  if (!session) {
    return false
  }
  try {
    const body = await request<components['schemas']['TokenResponse']>('/auth/client/refresh', {
      method: 'POST',
    })
    saveSession({ ...session, token: body.token, expiresAt: body.expires_at })
    return true
  } catch {
    return false
  }
}
