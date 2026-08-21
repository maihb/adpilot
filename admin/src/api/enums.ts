/**
 * 后端枚举在前端的中文名。
 *
 * 🔴 **这个文件里的每一份映射都是后端枚举的第二份拷贝。**
 *
 * 拷贝本身躲不掉（后端不下发中文名，下发了也会在两处各改一遍），真正的问题是
 * **对不上时不会报错** —— 页面对认不出的值一律回落到原始标识，于是一个英文
 * 字符串安静地显示给运营，而没有任何东西会红。
 *
 * 所以两件事：
 *
 * 1. **所有这类映射收口到这一个文件**，不散在页面里。散着的时候门禁只盯得住
 *    它认得出名字的那几份 —— `KIND_NAMES` 有测试盯着，而 `ImportPage` 里那份
 *    层级清单曾经一直没有；
 * 2. `tests/test_frontend_source.py` 扫这个文件，和后端的 `ActionKind` /
 *    `MetricLevel` **双向对齐** —— 后端加了新成员而这里没给中文名要红，这里
 *    留着一个后端已经没有的键同样要红（那通常意味着某个值被改名了，而改名的
 *    那半边正是会安静出错的地方）。
 *
 * 告警类型和日报状态**不在这里**：那两份长在页面里，各自有一条更老的门禁盯着
 * （抓的是 `const KIND_NAMES` / `const REPORT_STATUS_NAMES` 这两个名字）。把
 * 它们搬过来要同时改那条测试，而那是另一件事 —— 混在一次改动里，两边都不好 review。
 */

/** 投放调整的类型。分类是为了让「本周改了 3 次预算」这种句子写得出来。 */
export const ACTION_KIND_NAMES: Record<string, string> = {
  budget: '预算',
  bid: '出价',
  creative: '素材',
  targeting: '定向',
  status: '起停',
  other: '其它',
}

/**
 * 归一化后的投放层级。
 *
 * ⚠️ 平台叫法不统一（Meta 的 ad set、TikTok 的 ad group），后端归一化时一律映射
 * 到 `adgroup` —— 这里显示成「广告组」是跟着后端口径走的，不是跟着某个平台。
 */
export const METRIC_LEVEL_NAMES: Record<string, string> = {
  account: '账户',
  campaign: '广告系列',
  adgroup: '广告组',
  ad: '广告',
}

/**
 * 库存的日均销量是从哪来的。
 *
 * ⚠️ 对应的后端**不是枚举**，是 `services/product.py` 里那三个常量
 * （`SALES_FROM_FILE` / `SALES_INFERRED` / `SALES_UNKNOWN`）。门禁照样双向盯着 ——
 * 「不是枚举」不代表拷贝对不上时会有人发现。
 *
 * 🔴 这几句话不是装饰：推算出来的日均建立在「两次导入之间没补过货」这个假设上，
 * 而运营看到「还能撑 3 天」时第一个该问的就是这个数可信不可信。
 */
export const SALES_SOURCE_NAMES: Record<string, string> = {
  file: '来自导出文件',
  inferred: '按库存变化推算',
  none: '算不出来',
}

/** `Record` → `el-option` 要的那种数组。 */
export function options(names: Record<string, string>): { value: string; label: string }[] {
  return Object.entries(names).map(([value, label]) => ({ value, label }))
}

/** 认不出就回落到原始标识 —— 显示一个英文字符串，好过显示空白。 */
export function nameOf(names: Record<string, string>, value: string): string {
  return names[value] ?? value
}
