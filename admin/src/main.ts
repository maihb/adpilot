import { createApp } from 'vue'

// Element Plus 的组件按需引入（vite.config.ts 的两个插件），但**样式的基础变量
// 得整体引一次** —— 那不是组件，按需解析器收不到它。少了这一行，页面能渲染，
// 但所有间距和颜色变量都是空的。
import 'element-plus/theme-chalk/base.css'

import App from './App.vue'
import { router } from './router'
import './style.css'

createApp(App).use(router).mount('#app')
