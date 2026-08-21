import { defineConfig } from 'vitest/config'

/**
 * 单测**不复用 `vite.config.ts`**：那份配置带着 Element Plus 的两个按需引入插件，
 * 而这里测的是纯函数，一个组件都不碰。让它们依赖整条组件解析链，只会换来一堆与
 * 被测逻辑无关的失败。
 *
 * 只测「算错了不会报错」的那几样，页面渲染不测（E2E 已明确不做）。
 */
export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node',
  },
})
