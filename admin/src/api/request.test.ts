import { describe, expect, it } from 'vitest'

import { ApiError, NeedsRedo, reason } from './request'

/**
 * 🔴 这一组守的是一个跑出来才发现的 bug：页面清一色写着
 * `error instanceof NeedsRedo ? error.message : '操作失败'`，而业务错误抛的是
 * `ApiError` —— 于是后端 409 里那句「这一期没有任何操作记录，发不出去。先登记当天
 * 做过的调整，再重新生成日报」被吞成了一句「发布失败」，运营只能来问人。
 *
 * 这个项目的后端是**刻意把 detail 写成能指导操作的**，所以「原样显示」不是礼貌，
 * 是那些消息唯一的用处。
 */
describe('reason', () => {
  it('业务错误显示后端那句话，不是兜底文案', () => {
    const backend = '这一期没有任何操作记录，发不出去。先登记当天做过的调整，再重新生成日报'

    expect(reason(new ApiError(409, backend), '发布失败')).toBe(backend)
  })

  it('票过期那条也显示它自己的消息', () => {
    // 它说的是「已经重新登录了，请再点一次」—— 换成「操作失败」会让人以为白干了
    expect(reason(new NeedsRedo(), '发布失败')).toContain('再操作一次')
  })

  it('普通 Error 也优先用它的消息', () => {
    // 这一条顺带钉住实现：ApiError / NeedsRedo 都继承 Error，所以上面两条其实
    // 走的是同一行。别再为它们加显式分支 —— 那是冗余，不是保险。
    expect(reason(new Error('网络断了'), '发布失败')).toBe('网络断了')
  })

  it('只有真的不是 Error 时才用兜底文案', () => {
    expect(reason('字符串不是 Error', '发布失败')).toBe('发布失败')
    expect(reason(undefined, '发布失败')).toBe('发布失败')
  })
})
