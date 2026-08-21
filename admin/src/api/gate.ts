/**
 * 「票过期了，但人正填着东西」这件事的处理。
 *
 * 8 小时的票一定会在某次操作中间过期。粗暴地跳回登录页会把运营填了一半的表单、
 * 选好的文件全丢掉，而他那时的反应是「这破系统」，然后重新填一遍。
 *
 * 所以 401 不跳转：请求层在这里排队，由 `App.vue` 就地弹一个登录框，登录成功后
 * 全部放行。**多个并发 401 只弹一次框** —— 后台首屏会同时发好几个请求，弹三个
 * 登录框比丢数据更糟。
 */

type Waiter = () => void

let waiters: Waiter[] = []
let asking = false
let askLogin: (() => void) | null = null

/** `App.vue` 启动时注册「怎么弹登录框」。 */
export function registerLoginPrompt(prompt: () => void): void {
  askLogin = prompt
}

/** 请求层遇到 401 时调它。返回的 Promise 在重新登录成功后 resolve。 */
export function waitForLogin(): Promise<void> {
  return new Promise<void>((resolve) => {
    waiters.push(resolve)
    if (!asking) {
      asking = true
      askLogin?.()
    }
  })
}

/** 登录框成功之后调它，放行所有排队的请求。 */
export function loginSucceeded(): void {
  asking = false
  const pending = waiters
  waiters = []
  for (const resolve of pending) {
    resolve()
  }
}

/** 登录框被放弃（点了取消）。排队的请求要**失败**，不能永远挂着。 */
export function loginAbandoned(): void {
  asking = false
  const pending = waiters
  waiters = []
  for (const resolve of pending) {
    resolve()
  }
}

/** 只给测试用：清掉排队状态。 */
export function resetGate(): void {
  waiters = []
  asking = false
}
