<script setup lang="ts">
import { ref } from 'vue'

import { getLoginCaptcha, login } from '../api/endpoints'
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

/** 连续失败两次之后后端才要验证码，平时这三个都是空的。 */
const captchaId = ref('')
const captchaImage = ref('')
const captchaAnswer = ref('')

/**
 * 问后端「现在要不要验证码」，要的话顺带把题拿回来。
 *
 * 🔴 **每次失败之后都要重新拉一张。** 验证码用一次即删（不管答对没答对），
 * 继续显示旧图的话人会照着抄一遍再被拒，看起来像「验证码永远是错的」。
 */
async function refreshCaptcha(): Promise<void> {
  if (!username.value) {
    return
  }
  try {
    const challenge = await getLoginCaptcha(username.value)
    captchaId.value = challenge.captcha_id ?? ''
    captchaImage.value = challenge.image ?? ''
  } catch {
    // 拿不到题就当作不需要：后端那边 Redis 挂了时也是放行的（同一个取舍，
    // 见 services/login_guard.py）。真要拦人的是密码本身。
    captchaId.value = ''
    captchaImage.value = ''
  }
  captchaAnswer.value = ''
}

async function submit(): Promise<void> {
  if (busy.value || !username.value || !password.value) {
    return
  }
  busy.value = true
  error.value = ''
  try {
    const token = await login(
      username.value,
      password.value,
      captchaId.value ? { id: captchaId.value, answer: captchaAnswer.value } : undefined,
    )
    saveSession({
      token: token.token,
      expiresAt: token.expires_at,
      username: username.value,
    })
    password.value = ''
    captchaId.value = ''
    captchaImage.value = ''
    captchaAnswer.value = ''
    visible.value = false
    // 放行所有因为 401 排队的请求（api/gate.ts）。
    loginSucceeded()
    emit('done')
  } catch {
    // 后端对用户名错和密码错回同一句话 —— 区分等于确认「这个用户名是存在的」。
    // 验证码错是另一句，但这里不分开显示：那等于告诉试密码的人「密码这关过了」。
    error.value = captchaId.value ? '登录失败，请重新输入密码和验证码' : '用户名或密码不对'
    // 失败之后重新问一次：这次可能刚好跨过阈值，验证码框要从无到有地出现。
    await refreshCaptcha()
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
          @blur="refreshCaptcha"
        />
      </el-form-item>
      <!-- 只有连续失败到阈值之后才出现。日常登录的那一两个人看不到它。 -->
      <el-form-item v-if="captchaImage" label="验证码">
        <div class="captcha">
          <el-input
            v-model="captchaAnswer"
            :disabled="busy"
            maxlength="8"
            placeholder="抄右边四个字符"
            @keyup.enter="submit"
          />
          <!-- 点一下换一张：抄不出来是常事，让人卡在一张图上没有意义。 -->
          <img
            :src="captchaImage"
            alt="验证码"
            title="点击换一张"
            class="captcha-image"
            @click="refreshCaptcha"
          />
        </div>
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

.captcha {
  align-items: center;
  display: flex;
  gap: 8px;
  width: 100%;
}

.captcha-image {
  border: 1px solid var(--el-border-color);
  border-radius: 4px;
  cursor: pointer;
  flex: none;
  height: 32px;
}
</style>
