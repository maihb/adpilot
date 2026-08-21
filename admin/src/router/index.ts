import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'

/**
 * 路由表。
 *
 * **引 vue-router 但不引 Pinia**：多页面加带参详情页，手写路由会很快长成一个劣质
 * 的 vue-router；而后台的状态只有「当前这张票」和「当前页的数据」，`ref` 就够，
 * 一个状态管理库在这里只是多一层间接。
 *
 * 用 hash 模式：后台可能被挂在任意子路径下（`/admin/`），hash 模式不需要服务端
 * 配合改写 URL —— 而这个项目还没有部署脚本，少一个部署前提就少一个坑。
 */
const routes: RouteRecordRaw[] = [
  // 首页就是导入。它是这个系统要替掉的那件手工活本身，也是运营每天用得最多的
  // 一屏 —— 藏在两层菜单后面，人就会退回去用 Excel。
  { path: '/', name: 'import', component: () => import('../pages/ImportPage.vue') },
  { path: '/alerts', name: 'alerts', component: () => import('../pages/AlertsPage.vue') },
  // 日报排在告警之后：运营的一天是「导入 → 看告警 → 出日报」。
  { path: '/reports', name: 'reports', component: () => import('../pages/ReportsPage.vue') },
  { path: '/clients', name: 'clients', component: () => import('../pages/ClientsPage.vue') },
  {
    path: '/clients/:id',
    name: 'client-detail',
    component: () => import('../pages/ClientDetailPage.vue'),
  },
  {
    path: '/accounts/:id',
    name: 'account-detail',
    component: () => import('../pages/AccountDetailPage.vue'),
  },
]

export const router = createRouter({
  history: createWebHashHistory(),
  routes,
})
