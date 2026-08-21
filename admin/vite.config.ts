import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

/**
 * 内部后台的构建配置。
 *
 * **Element Plus 按需引入**：全量是 2 MB 量级的 JS + CSS。后台是内网工具，慢一点
 * 不致命，但「陌生人 clone 下来跑一遍」是这个项目的主目标，而一个首屏要等几秒的
 * 演示会直接影响判断。代价是两个构建期插件，收益是产物只带用到的那十来个组件。
 *
 * **`/api` 走 dev proxy 而不是给后端开 CORS**，理由和客户端那边一字不差
 * （client/vite.config.ts）：跨域只是开发期问题，用代理解决，生产上前后端同源。
 */
export default defineConfig({
  plugins: [
    vue(),
    // auto-import 管的是 ElMessage / ElMessageBox 这类**函数式**组件，它们不是
    // 模板里的标签，Components 那个插件收不到。少了它，写 ElMessage 会在运行时
    // 才报 undefined —— 而那通常发生在某个错误分支里，正好是最少被走到的地方。
    AutoImport({ resolvers: [ElementPlusResolver()] }),
    Components({ resolvers: [ElementPlusResolver()] }),
  ],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    // 🔴 5173 被客户端占了。两个前端**必须能同时跑** —— D12 的验收标准里有一条是
    // 「后台停用客户 → 客户端当场看不到数据」，那得两个窗口都开着才验得了。
    port: 5174,
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
