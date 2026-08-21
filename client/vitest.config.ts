import { defineConfig } from 'vitest/config'

/**
 * 单测**不复用 `vite.config.ts`**：那份配置带着 uni 插件，它要去读 `pages.json`
 * 和 `manifest.json`、还要按平台分支编译。而这里测的三个文件是纯函数，一个 uni
 * API 都不碰 —— 让它们依赖整条小程序构建链，只会换来一堆与被测逻辑无关的失败。
 *
 * 只测「算错了不会报错」的那几样，页面渲染不测（E2E 已明确不做，见
 * `docs/design/2026-08-21-client-app.md` 第九节）。
 */
export default defineConfig({
  test: {
    include: ['src/**/*.test.ts'],
    environment: 'node',
  },
})
