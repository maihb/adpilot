<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { registerLoginPrompt } from './api/gate'
import { clearSession, loadSession } from './utils/session'
import { formatInstant, hoursUntil } from './utils/format'
import LoginDialog from './components/LoginDialog.vue'

const route = useRoute()
const router = useRouter()

const loginVisible = ref(false)
/** 冷启动没票时不可关闭；操作中途过期时可以取消。 */
const loginClosable = ref(false)
const session = ref(loadSession())

const remainingHours = computed(() => (session.value ? hoursUntil(session.value.expiresAt, Date.now()) : 0))

onMounted(() => {
  // 请求层遇到 401 时会调它 —— 就地弹框，**不跳转、不清空页面**：粗暴地跳回登录页
  // 会把运营填了一半的表单和选好的文件一起丢掉。
  registerLoginPrompt(() => {
    loginClosable.value = true
    loginVisible.value = true
  })

  if (!session.value) {
    loginClosable.value = false
    loginVisible.value = true
  }
})

function afterLogin(): void {
  session.value = loadSession()
}

function signOut(): void {
  clearSession()
  session.value = null
  loginClosable.value = false
  loginVisible.value = true
}

const active = computed(() => {
  if (route.path.startsWith('/clients') || route.path.startsWith('/accounts')) {
    return '/clients'
  }
  return route.path
})

function go(path: string): void {
  void router.push(path)
}
</script>

<template>
  <el-container class="shell">
    <el-aside width="180px" class="side">
      <div class="brand">adpilot</div>
      <el-menu :default-active="active" :router="false" @select="go">
        <el-menu-item index="/">导入</el-menu-item>
        <el-menu-item index="/stock">库存</el-menu-item>
        <el-menu-item index="/alerts">告警</el-menu-item>
        <el-menu-item index="/reports">日报</el-menu-item>
        <el-menu-item index="/clients">客户</el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="top">
        <span class="who">
          <template v-if="session">
            {{ session.username }} · 票到 {{ formatInstant(session.expiresAt) }}
            <el-tag v-if="remainingHours < 1" type="warning" size="small">不到 1 小时</el-tag>
          </template>
          <template v-else>未登录</template>
        </span>
        <el-button v-if="session" text size="small" @click="signOut">退出</el-button>
      </el-header>

      <el-main>
        <!-- 票过期时这里**不会被卸载** —— 登录框是覆盖上去的，页面状态原样留着。 -->
        <router-view v-if="session" />
        <el-empty v-else description="登录之后才能看到数据" />
      </el-main>
    </el-container>

    <LoginDialog v-model:visible="loginVisible" :closable="loginClosable" @done="afterLogin" />
  </el-container>
</template>

<style scoped>
.shell {
  height: 100vh;
}
.side {
  border-right: 1px solid var(--el-border-color-light);
}
.brand {
  font-size: 18px;
  font-weight: 600;
  padding: 18px 20px;
}
.top {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  border-bottom: 1px solid var(--el-border-color-light);
}
.who {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
