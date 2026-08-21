<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'

import { getRunway, listAccounts, type PortalAccount, type PortalRunway } from '../../api/portal'
import { useAuthStore } from '../../stores/auth'
import { formatDays, formatMoney, runwayState } from '../../utils/decimal'

interface Card {
  account: PortalAccount
  runway: PortalRunway | null
}

const auth = useAuthStore()
const cards = ref<Card[]>([])
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
    const accounts = await listAccounts()
    // 并发取每个账户的可撑天数。请求层把并发压在 5 —— 微信小程序同时请求数
    // 上限是 10，撞上去被挤掉的请求会静默失败（`api/request.ts`）。
    cards.value = await Promise.all(
      accounts.items.map(async (account) => ({
        account,
        // 某个账户的余额取失败不该让整页空掉：那张卡显示「—」，其余照常。
        runway: await getRunway(account.id).catch(() => null),
      })),
    )
  } catch {
    error.value = '数据没能加载出来，下拉重试一次。'
  } finally {
    loading.value = false
  }
}

/**
 * 可撑天数的三态文案。
 *
 * 🔴 `unknown` 和 `idle` **都不能显示成「0 天」** —— 前者是「没录过余额」，
 * 后者是「近期没花钱，算不出来」，而 0 天的意思是「明天就停投」。
 */
function runwayText(runway: PortalRunway | null): string {
  if (!runway) {
    return '—'
  }
  const state = runwayState(runway.available, runway.days_left)
  if (state === 'unknown') {
    return '余额未录入'
  }
  if (state === 'idle') {
    return '近期无消耗'
  }
  return `还能撑 ${formatDays(runway.days_left)}`
}

function runwayClass(runway: PortalRunway | null): string {
  if (!runway || runwayState(runway.available, runway.days_left) !== 'known') {
    return 'muted'
  }
  return runway.is_alerting ? 'danger' : 'ok'
}

function openAccount(account: PortalAccount): void {
  // 账户名进 URL 要编码：示例数据里的名字带空格和短横，真实客户名还可能带 & 和 #。
  const name = encodeURIComponent(account.name)
  uni.navigateTo({ url: `/pages/account/account?id=${account.id}&name=${name}` })
}

/** 日均消耗的窗口里缺了几天，这个日均（以及可撑天数）就要打个问号。 */
function coverageNote(runway: PortalRunway | null): string {
  if (!runway || runway.days_with_data === null || runway.lookback_from === null) {
    return ''
  }
  const from = runway.lookback_from
  const to = runway.lookback_to ?? from
  return `按 ${from} 至 ${to} 内 ${runway.days_with_data} 天有数据的日均算`
}
</script>

<template>
  <view class="wrap">
    <view class="header">
      <view class="title">{{ auth.clientName || '我' }} 的投放看板</view>
      <view class="subtitle">余额归零就是直接停投，这一屏先看它。</view>
    </view>

    <view v-if="loading" class="state">加载中…</view>
    <view v-else-if="error" class="state">{{ error }}</view>
    <view v-else-if="!cards.length" class="state">还没有广告账户，等投放负责人配置。</view>

    <view
      v-for="card in cards"
      :key="card.account.id"
      class="card"
      hover-class="card-hover"
      @tap="openAccount(card.account)"
    >
      <view class="card-head">
        <text class="name">{{ card.account.name }}</text>
        <text v-if="!card.account.is_active" class="tag">已停投</text>
      </view>

      <view class="meta">
        {{ card.account.platform }} · {{ card.account.currency }} · {{ card.account.timezone }}
      </view>

      <view class="runway" :class="runwayClass(card.runway)">
        {{ runwayText(card.runway) }}
      </view>

      <view v-if="card.runway && card.runway.available !== null" class="balance">
        余额 {{ formatMoney(card.runway.available, card.runway.currency) }}
        <text v-if="card.runway.avg_daily_spend !== null">
          · 日均 {{ formatMoney(card.runway.avg_daily_spend, card.runway.currency) }}
        </text>
      </view>

      <view v-if="coverageNote(card.runway)" class="note">{{ coverageNote(card.runway) }}</view>
    </view>

    <view v-if="cards.length" class="footnote">
      日期是各账户自己时区下的自然日，金额是账户币种 —— 不同账户之间不能直接相加。
    </view>
  </view>
</template>

<style scoped>
.wrap {
  padding: 32rpx 24rpx 64rpx;
}
.header {
  padding: 8rpx 8rpx 24rpx;
}
.title {
  font-size: 40rpx;
  font-weight: 600;
}
.subtitle {
  color: #6b7280;
  margin-top: 8rpx;
}
.state {
  color: #6b7280;
  padding: 48rpx 8rpx;
  text-align: center;
}
.card {
  background: #ffffff;
  border-radius: 16rpx;
  padding: 28rpx;
  margin-bottom: 20rpx;
}
.card-hover {
  background: #f0f2f5;
}
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.name {
  font-size: 32rpx;
  font-weight: 600;
}
.tag {
  background: #f3f4f6;
  color: #6b7280;
  border-radius: 8rpx;
  padding: 4rpx 12rpx;
  font-size: 22rpx;
}
.meta {
  color: #9ca3af;
  font-size: 24rpx;
  margin-top: 8rpx;
}
.runway {
  font-size: 36rpx;
  font-weight: 600;
  margin-top: 20rpx;
}
.runway.ok {
  color: #1a7f37;
}
.runway.danger {
  color: #d1242f;
}
.runway.muted {
  color: #9ca3af;
}
.balance {
  color: #4b5563;
  margin-top: 8rpx;
}
.note {
  color: #9ca3af;
  font-size: 22rpx;
  margin-top: 8rpx;
}
.footnote {
  color: #9ca3af;
  font-size: 22rpx;
  line-height: 1.6;
  padding: 16rpx 8rpx;
}
</style>
