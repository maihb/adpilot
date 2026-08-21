/**
 * **客户端仅有的一个 store。**
 *
 * 不给指标和账户建 store 是刻意的：数据是每日粒度的，缓存收益低，而缓存过期
 * 是 bug 源（设计文档第八节）。页面各自请求就够快。
 */

import { defineStore } from 'pinia'

import { refreshClientToken } from '../api/auth'
import { clearSession, loadSession } from '../utils/storage'
import { isExpired, shouldRenew } from '../utils/token'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    clientName: '',
    signedIn: false,
  }),

  actions: {
    /**
     * 冷启动时调一次。
     *
     * 顺序要紧：先判本地票有没有过期，过期的**不去续**（后端要一张未过期的票
     * 才肯换新的），直接当作没登录。没过期但快到期了才静默续一次。
     */
    async bootstrap(): Promise<boolean> {
      const session = loadSession()
      if (!session || isExpired(session.expiresAt, Date.now())) {
        this.signOut()
        return false
      }

      this.clientName = session.clientName
      this.signedIn = true

      if (shouldRenew(session.expiresAt, Date.now())) {
        // 续不上也不影响这次使用 —— 票还没过期。真到期那天会走 401 那条路。
        await refreshClientToken()
      }
      return true
    },

    adopt(clientName: string): void {
      this.clientName = clientName
      this.signedIn = true
    },

    /** 只删本地那份票。**服务端没有对应动作**，自签票撤销不了。 */
    signOut(): void {
      clearSession()
      this.clientName = ''
      this.signedIn = false
    },
  },
})
