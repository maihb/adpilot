<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'

import { listAlerts, type PortalAlert } from '../../api/portal'
import { useAuthStore } from '../../stores/auth'
import { formatInstant } from '../../utils/time'

const auth = useAuthStore()
const items = ref<PortalAlert[]>([])
const onlyOpen = ref(true)
const loading = ref(true)
const error = ref('')

onShow(() => {
  void load()
})

async function load(): Promise<void> {
  loading.value = true
  error.value = ''

  const ok = await auth.bootstrap()
  if (!ok) {
    uni.reLaunch({ url: '/pages/redeem/redeem' })
    return
  }

  try {
    const page = await listAlerts(onlyOpen.value)
    items.value = page.items
  } catch {
    error.value = '告警没能加载出来。'
  } finally {
    loading.value = false
  }
}

function toggle(): void {
  onlyOpen.value = !onlyOpen.value
  void load()
}

/**
 * 告警类型的中文名。
 *
 * 后端的 `kind` 是稳定的机器标识，认不出的一律回落到原值 —— 后端加了新类型时，
 * 客户看到一个英文标识总好过看到「未知」或者整条不显示。
 *
 * ⚠️ 这份映射是**后端枚举在前端的第二份拷贝**，而回落逻辑让「对不上」这件事
 * 不会报错、只会安静地把英文标识显示给客户（写这段时就把 `balance_low` 记成了
 * `balance_runway`，是跑起来才看见的）。所以有一条测试双向盯着它和
 * `models/alert.py` 的 `AlertKind`：`tests/test_frontend_source.py`。
 */
const KIND_NAMES: Record<string, string> = {
  balance_low: '余额',
  metric_anomaly: '指标异动',
}

function kindName(kind: string): string {
  return KIND_NAMES[kind] ?? kind
}
</script>

<template>
  <view class="wrap">
    <view class="header">
      <!-- 导航栏已经是「要注意的事」，这里写当前范围，按钮写点下去会发生什么。 -->
      <view class="title">{{ onlyOpen ? '未解决的' : '全部历史' }}</view>
      <view class="switch" @tap="toggle">
        {{ onlyOpen ? '看全部历史' : '只看未解决' }}
      </view>
    </view>

    <view v-if="loading" class="state">加载中…</view>
    <view v-else-if="error" class="state">{{ error }}</view>
    <view v-else-if="!items.length" class="state">
      {{ onlyOpen ? '暂时没有要处理的事。' : '还没有过任何告警。' }}
    </view>

    <view v-for="alert in items" :key="alert.id" class="card">
      <view class="card-head">
        <text class="kind">{{ kindName(alert.kind) }}</text>
        <text class="status" :class="alert.status">
          {{ alert.status === 'open' ? '未解决' : '已恢复' }}
        </text>
      </view>

      <!-- message 是**规则算出来的事实**，不是 LLM 写的解释，可以直接显示。 -->
      <view class="message">{{ alert.message }}</view>

      <view class="times">
        <text>发生于 {{ formatInstant(alert.opened_at) }}</text>
        <text v-if="alert.resolved_at">· 恢复于 {{ formatInstant(alert.resolved_at) }}</text>
        <text v-else>· 最近确认 {{ formatInstant(alert.last_seen_at) }}</text>
      </view>
    </view>

    <view v-if="items.length" class="footnote">
      告警每小时自动巡检一次。同一件事同时只会有一条 —— 问题还在就刷新时间，不再重复打扰。
    </view>
  </view>
</template>

<style scoped>
.wrap {
  padding: 32rpx 24rpx 64rpx;
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8rpx 8rpx 24rpx;
}
.title {
  font-size: 40rpx;
  font-weight: 600;
}
.switch {
  color: #1f6feb;
  font-size: 26rpx;
}
.state {
  color: #6b7280;
  padding: 64rpx 8rpx;
  text-align: center;
}
.card {
  background: #ffffff;
  border-radius: 16rpx;
  padding: 28rpx;
  margin-bottom: 20rpx;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.kind {
  font-size: 28rpx;
  font-weight: 600;
}
.status {
  font-size: 22rpx;
  border-radius: 8rpx;
  padding: 4rpx 12rpx;
}
.status.open {
  background: #fdeaea;
  color: #d1242f;
}
.status.resolved {
  background: #eaf5ec;
  color: #1a7f37;
}
.message {
  color: #1f2329;
  line-height: 1.7;
  margin-top: 16rpx;
}
.times {
  color: #9ca3af;
  font-size: 22rpx;
  margin-top: 16rpx;
}
.times text {
  margin-right: 8rpx;
}
.footnote {
  color: #9ca3af;
  font-size: 22rpx;
  line-height: 1.6;
  padding: 16rpx 8rpx;
}
</style>
