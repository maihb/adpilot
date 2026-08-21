import { defineConfig } from 'vite'
import uni from '@dcloudio/vite-plugin-uni'

/**
 * H5 开发时把 `/api` 代理到后端，**而不是给后端开 CORS**。
 *
 * 跨域这件事只在「前端 dev server 和后端不同端口」时才出现，它是个开发期问题；
 * 用代理解决，生产上前端与后端同源，两边都不需要 CORS 配置。反过来做的话，
 * 仓库里就会留下一个默认允许某个 origin 的配置项，而那种默认值最容易被一路
 * 带到公网上。
 *
 * 后端不在 8000 的话设 `VITE_API_PROXY`。
 */
export default defineConfig({
  plugins: [uni()],
  server: {
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
