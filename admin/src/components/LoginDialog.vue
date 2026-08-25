<script setup lang="ts">
import { ref, watch } from 'vue'

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
const captchaBusy = ref(false)

/**
 * 问后端「现在要不要验证码」，要的话顺带把题拿回来。
 *
 * 🔴 **每次失败之后都要重新拉一张。** 验证码用一次即删（不管答对没答对），继续
 * 显示旧图的话人会照着抄一遍再被拒，看起来像「验证码永远是错的」。
 *
 * 🔴🔴 **反过来，不该拉的时候更不能拉。** 这个函数每调一次就换一张图并清空已填的
 * 答案，所以**绝不能挂在密码框的 blur 上** —— 填完密码去点验证码输入框那一下正好
 * 触发它，人就永远在照着一张刚作废的图抄，表现成「验证码怎么填都不对」。
 * 挂在用户名的 blur 上是对的：那时人还没开始看图。（2026-08-25 实测踩到过）
 */
async function refreshCaptcha(): Promise<void> {
  if (!username.value || captchaBusy.value) {
    return
  }
  captchaBusy.value = true
  try {
    const challenge = await getLoginCaptcha(username.value)
    captchaId.value = challenge.captcha_id ?? ''
    captchaImage.value = challenge.image ?? ''
  } catch {
    // 拿不到题就当作不需要：后端在 Redis 挂了时也是放行的（同一个取舍，见
    // services/login_guard.py）。真正拦人的是密码本身。
    captchaId.value = ''
    captchaImage.value = ''
  } finally {
    captchaBusy.value = false
  }
  captchaAnswer.value = ''
}

// 🔴 打开时就问一次。失败计数在**服务端**、活 15 分钟，所以「上一次没登进去就
// 把页面关了」之后再打开，验证码该是已经要着的 —— 只在提交失败后才去问的话，
// 人会先白挨一次拒绝。前提是用户名已经填着（记住的那种），空着就等 blur。
watch(visible, (open) => {
  if (open) {
    error.value = ''
    void refreshCaptcha()
  }
})

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
    // 验证码错是另一句，但这里**不分开显示**：那等于告诉试密码的人「密码这关过了」。
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
    class="login-dialog"
    width="420px"
    align-center
    :close-on-click-modal="false"
    :close-on-press-escape="props.closable"
    :show-close="false"
    @close="cancel"
  >
    <!-- 自绘头部：Element 默认那条标题栏留不出品牌区，索性整块替掉。 -->
    <template #header>
      <div class="head">
        <div class="head__mark">ad<span>pilot</span></div>
        <p class="head__tag">投放数据中台</p>
        <button v-if="props.closable" class="head__close" type="button" title="取消" @click="cancel">
          ×
        </button>
      </div>
    </template>

    <div class="body">
      <div class="body__heading">
        <span>WELCOME BACK</span>
        <h2>登录后台</h2>
        <p>账号来自 <code>.env</code> 的 <code>OPERATOR_USERNAME</code>，票有效期 8 小时</p>
      </div>

      <el-form label-position="top" size="large" @submit.prevent="submit">
        <el-form-item label="账号">
          <el-input
            v-model="username"
            placeholder="运营账号"
            autocomplete="username"
            :disabled="busy"
            @blur="refreshCaptcha"
          >
            <template #prefix>
              <svg class="ico" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm0 2c-4 0-7 2-7 4.5V20h14v-1.5c0-2.5-3-4.5-7-4.5Z" />
              </svg>
            </template>
          </el-input>
        </el-form-item>

        <el-form-item label="密码">
          <el-input
            v-model="password"
            type="password"
            placeholder="生成 OPERATOR_PASSWORD_HASH 时设的那个"
            autocomplete="current-password"
            :disabled="busy"
            show-password
            @keyup.enter="submit"
          >
            <template #prefix>
              <svg class="ico" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M17 9h-1V7a4 4 0 1 0-8 0v2H7a1 1 0 0 0-1 1v9a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1v-9a1 1 0 0 0-1-1Zm-7-2a2 2 0 1 1 4 0v2h-4V7Z" />
              </svg>
            </template>
          </el-input>
        </el-form-item>

        <!-- 只有连续失败到阈值之后才出现。日常登录的那一两个人看不到它。 -->
        <el-form-item v-if="captchaImage" label="验证码">
          <div class="cap">
            <div class="cap__row">
              <el-input
                v-model="captchaAnswer"
                placeholder="输入右侧 4 位"
                maxlength="8"
                autocomplete="off"
                :disabled="busy"
                @keyup.enter="submit"
              />
              <button
                class="cap__img"
                type="button"
                title="看不清？点击换一张"
                aria-label="刷新验证码"
                :disabled="captchaBusy || busy"
                @click="refreshCaptcha"
              >
                <img :src="captchaImage" alt="验证码，点击可刷新" />
              </button>
            </div>
            <button class="cap__again" type="button" :disabled="captchaBusy || busy" @click="refreshCaptcha">
              看不清？点击换一张
            </button>
          </div>
        </el-form-item>

        <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="err" />

        <button class="submit" type="button" :disabled="busy" @click="submit">
          {{ busy ? '登录中…' : '进入后台' }}
        </button>
      </el-form>

      <div class="foot"><span />票到期后会就地续，不会丢掉页面上的东西<span /></div>
    </div>
  </el-dialog>
</template>

<style scoped>
/* Element 的 dialog 自带内边距，会把渐变头部框在中间 —— 整块清掉自己排。 */
.login-dialog :deep(.el-dialog) {
  overflow: hidden;
  padding: 0;
  border-radius: 16px;
  box-shadow: 0 26px 70px rgba(0, 12, 33, 0.38);
}
.login-dialog :deep(.el-dialog__header),
.login-dialog :deep(.el-dialog__body) {
  padding: 0;
  margin: 0;
}

.head {
  position: relative;
  padding: 26px 32px 22px;
  background: linear-gradient(105deg, #062a5e, #0b6cff 78%);
  color: #fff;
}
.head__mark {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.head__mark span {
  color: #7fd0ff;
}
.head__tag {
  margin: 4px 0 0;
  color: #b9d6f5;
  font-size: 12px;
  letter-spacing: 0.14em;
}
.head__close {
  position: absolute;
  top: 16px;
  right: 18px;
  padding: 0 6px;
  color: #cfe4fb;
  font-size: 22px;
  line-height: 1;
  background: transparent;
  border: 0;
  cursor: pointer;
}
.head__close:hover {
  color: #fff;
}

.body {
  padding: 24px 32px 26px;
}
.body__heading {
  margin-bottom: 18px;
}
.body__heading span {
  color: #0b6cff;
  font-size: 11px;
  font-weight: 750;
  letter-spacing: 0.16em;
}
.body__heading h2 {
  margin: 6px 0 5px;
  color: #122944;
  font-size: 22px;
}
.body__heading p {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.7;
}

.ico {
  width: 15px;
  height: 15px;
  fill: #90a4bd;
}
.login-dialog :deep(.el-form-item__label) {
  color: #344b68;
  font-weight: 650;
}
.login-dialog :deep(.el-input__wrapper) {
  min-height: 44px;
  background: #f8fbff;
  box-shadow: 0 0 0 1px #dce7f4 inset;
}

.cap {
  width: 100%;
}
.cap__row {
  display: flex;
  align-items: stretch;
  gap: 10px;
  width: 100%;
}
.cap__row :deep(.el-input) {
  flex: 1 1 auto;
  min-width: 0;
}
.cap__row :deep(.el-input__inner) {
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.cap__img {
  flex: 0 0 132px;
  width: 132px;
  height: 44px;
  padding: 2px;
  overflow: hidden;
  background: #f4f8ff;
  border: 1px solid #b9cee8;
  border-radius: 8px;
  cursor: pointer;
}
.cap__img:hover {
  border-color: #6ea8ed;
}
.cap__img:disabled {
  cursor: wait;
  opacity: 0.7;
}
.cap__img img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.cap__again {
  display: block;
  margin: 5px 1px 0 auto;
  padding: 0;
  color: #3978c9;
  font-size: 11px;
  background: transparent;
  border: 0;
  cursor: pointer;
}
.cap__again:hover {
  text-decoration: underline;
}

.err {
  margin-bottom: 14px;
}

.submit {
  width: 100%;
  height: 46px;
  margin-top: 2px;
  color: #fff;
  font-size: 15px;
  font-weight: 680;
  background: linear-gradient(90deg, #075bd8, #0b83ef);
  border: 0;
  border-radius: 8px;
  box-shadow: 0 10px 22px rgba(10, 104, 225, 0.24);
  cursor: pointer;
}
.submit:hover:not(:disabled) {
  background: linear-gradient(90deg, #0651c2, #0a76d8);
}
.submit:disabled {
  cursor: wait;
  opacity: 0.72;
}

.foot {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 20px;
  color: #95a2b2;
  font-size: 11px;
}
.foot span {
  flex: 1;
  height: 1px;
  background: #e3eaf2;
}
</style>
