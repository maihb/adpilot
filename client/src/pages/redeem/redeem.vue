<script setup lang="ts">
import { onLoad } from '@dcloudio/uni-app'
import { ref } from 'vue'

import { redeemInvite } from '../../api/auth'
import { useAuthStore } from '../../stores/auth'

const auth = useAuthStore()

const code = ref('')
const busy = ref(false)
const error = ref('')

/**
 * H5 上是链接带码进来的（`.../#/pages/redeem/redeem?code=XXX`）——浏览器没有
 * 扫码 API，这是唯一的自动入口。小程序端走下面那个扫码按钮。
 */
onLoad((query) => {
  const incoming = (query?.code ?? '').toString().trim()
  if (incoming) {
    code.value = incoming
    void submit()
  }
})

async function submit(): Promise<void> {
  const value = code.value.trim()
  if (!value || busy.value) {
    return
  }
  busy.value = true
  error.value = ''
  try {
    const body = await redeemInvite(value)
    auth.adopt(body.client_name)

    // #ifdef H5
    // ⚠️ 邀请码进过地址栏，就会进浏览器历史、也可能被截图转发出去。换到票之后
    // 立刻把它从 URL 上抹掉；票已经在本地了，这个码不再需要出现在任何地方。
    window.history.replaceState(null, '', window.location.pathname)
    // #endif

    uni.reLaunch({ url: '/pages/index/index' })
  } catch {
    // 后端对「码不存在 / 过期 / 被作废 / 客户已停止合作」回的是同一个 404 ——
    // 分开报错等于告诉试码的人「这个码是真的，只是过期了」。前端照样只给一句话。
    error.value = '这个邀请码用不了。请找投放负责人要一个新的。'
  } finally {
    busy.value = false
  }
}

// #ifdef MP-WEIXIN
function scan(): void {
  uni.scanCode({
    onlyFromCamera: false,
    success: (res) => {
      // 二维码里通常是一整条链接，把 code 参数摘出来；直接是裸码的也认。
      const match = /[?&]code=([^&]+)/.exec(res.result)
      code.value = match ? decodeURIComponent(match[1]) : res.result.trim()
      void submit()
    },
  })
}
// #endif
</script>

<template>
  <view class="wrap">
    <view class="title">打开我的投放看板</view>
    <view class="hint">邀请码由你的投放负责人发给你，有效期内可以反复使用。</view>

    <!-- #ifdef MP-WEIXIN -->
    <button class="primary" :disabled="busy" @tap="scan">扫一扫</button>
    <view class="divider">或者手动输入</view>
    <!-- #endif -->

    <input
      v-model="code"
      class="field"
      placeholder="粘贴邀请码"
      :disabled="busy"
      confirm-type="go"
      @confirm="submit"
    />
    <button class="primary" :disabled="busy || !code.trim()" @tap="submit">
      {{ busy ? '正在打开…' : '进入看板' }}
    </button>

    <view v-if="error" class="error">{{ error }}</view>
  </view>
</template>

<style scoped>
.wrap {
  padding: 64rpx 48rpx;
}
.title {
  font-size: 44rpx;
  font-weight: 600;
  margin-bottom: 16rpx;
}
.hint {
  color: #6b7280;
  line-height: 1.6;
  margin-bottom: 48rpx;
}
.field {
  background: #ffffff;
  border: 2rpx solid #e5e7eb;
  border-radius: 12rpx;
  padding: 24rpx;
  margin-bottom: 24rpx;
}
.primary {
  background: #1f6feb;
  color: #ffffff;
  border-radius: 12rpx;
  margin-bottom: 24rpx;
}
.primary[disabled] {
  background: #a8c3ef;
}
.divider {
  text-align: center;
  color: #9ca3af;
  margin: 16rpx 0 24rpx;
}
.error {
  color: #d1242f;
  line-height: 1.6;
}
</style>
