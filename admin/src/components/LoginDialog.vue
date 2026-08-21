<script setup lang="ts">
import { ref } from 'vue'

import { login } from '../api/endpoints'
import { loginAbandoned, loginSucceeded } from '../api/gate'
import { saveSession } from '../utils/session'

const props = defineProps<{
  /** 冷启动没有票时不可关闭；操作中途过期时可以取消。 */
  closable: boolean
}>()

const emit = defineEmits<{ (event: 'done'): void }>()

const visible = defineModel<boolean>('visible', { required: true })

const username = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')

async function submit(): Promise<void> {
  if (busy.value || !username.value || !password.value) {
    return
  }
  busy.value = true
  error.value = ''
  try {
    const token = await login(username.value, password.value)
    saveSession({
      token: token.token,
      expiresAt: token.expires_at,
      username: username.value,
    })
    password.value = ''
    visible.value = false
    // 放行所有因为 401 排队的请求（api/gate.ts）。
    loginSucceeded()
    emit('done')
  } catch {
    // 后端对用户名错和密码错回同一句话 —— 区分等于确认「这个用户名是存在的」。
    error.value = '用户名或密码不对'
  } finally {
    busy.value = false
  }
}

function cancel(): void {
  if (!props.closable) {
    return
  }
  visible.value = false
  // 🔴 排队的请求必须被放走，否则它们永远挂着，页面看起来像卡死了。
  loginAbandoned()
}
</script>

<template>
  <el-dialog
    v-model="visible"
    title="运营登录"
    width="420px"
    :close-on-click-modal="false"
    :close-on-press-escape="props.closable"
    :show-close="props.closable"
    @close="cancel"
  >
    <p class="hint">
      账号来自 <code>.env</code> 的 <code>OPERATOR_USERNAME</code>，密码是生成
      <code>OPERATOR_PASSWORD_HASH</code> 时设的那个。票有效期 8 小时。
    </p>

    <el-form label-width="60px" @submit.prevent="submit">
      <el-form-item label="账号">
        <el-input v-model="username" autocomplete="username" :disabled="busy" />
      </el-form-item>
      <el-form-item label="密码">
        <el-input
          v-model="password"
          type="password"
          autocomplete="current-password"
          :disabled="busy"
          show-password
          @keyup.enter="submit"
        />
      </el-form-item>
    </el-form>

    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />

    <template #footer>
      <el-button v-if="props.closable" @click="cancel">取消</el-button>
      <el-button type="primary" :loading="busy" @click="submit">登录</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  line-height: 1.7;
  margin: 0 0 16px;
}
</style>
