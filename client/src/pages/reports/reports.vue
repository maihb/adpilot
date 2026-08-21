<script setup lang="ts">
import { onShow } from '@dcloudio/uni-app'
import { ref } from 'vue'

import { listAccounts, listReports, type PortalReport } from '../../api/portal'
import { useAuthStore } from '../../stores/auth'
import { formatMoney } from '../../utils/decimal'

const auth = useAuthStore()
const items = ref<PortalReport[]>([])
const accountNames = ref<Record<number, string>>({})
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
    // 顺带取一次账户列表，只为把 account_id 显示成名字。
    //
    // 账户名**刻意不进日报出参**：日报快照存的是币种和时区这类「解释数字所必需」
    // 的口径，而名字只是个标签 —— 客户改了账户名之后，旧日报也该显示新名字。
    // 账户是个位数，这次请求很小。
    const [page, accounts] = await Promise.all([listReports(), listAccounts()])
    items.value = page.items
    accountNames.value = Object.fromEntries(accounts.items.map((row) => [row.id, row.name]))
  } catch {
    error.value = '日报没能加载出来。'
  } finally {
    loading.value = false
  }
}

function accountName(accountId: number): string {
  return accountNames.value[accountId] ?? `账户 #${accountId}`
}

function open(report: PortalReport): void {
  uni.navigateTo({ url: `/pages/report/report?id=${report.id}` })
}
</script>

<template>
  <view class="wrap">
    <view v-if="loading" class="state">加载中…</view>
    <view v-else-if="error" class="state">{{ error }}</view>
    <view v-else-if="!items.length" class="state">
      还没有日报。日报由运营确认后发布，发布之后就会出现在这里。
    </view>

    <view v-for="report in items" :key="report.id" class="card" @tap="open(report)">
      <view class="card-head">
        <!-- stat_date 是**账户时区下的自然日**，是个标签不是时刻 —— 不做本地
             时区转换，原样显示（client-app.md 的口径第四条）。 -->
        <text class="day">{{ report.stat_date }}</text>
        <text class="account">{{ accountName(report.account_id) }}</text>
      </view>

      <view class="spend">{{ formatMoney(report.spend, report.currency) }}</view>

      <!-- 这段话经过人工确认才发得出来。列表里只给第一段，全文点进去看。 -->
      <view class="summary">{{ report.narrative.summary }}</view>

      <view class="meta">
        <text v-if="report.actions.length">本期做了 {{ report.actions.length }} 件事</text>
        <text v-else>本期没有调整记录</text>
        <text class="more">查看全文 ›</text>
      </view>
    </view>

    <view v-if="items.length" class="footnote">
      日报里的数字是生成那一刻定下来的，之后不会再变 —— 同一天的日报今天看和明天看是
      一样的。口径（时区、币种）写在每份日报里。
    </view>
  </view>
</template>

<style scoped>
.wrap {
  padding: 32rpx 24rpx 64rpx;
}
.state {
  color: #6b7280;
  padding: 64rpx 8rpx;
  text-align: center;
  line-height: 1.7;
}
.card {
  background: #ffffff;
  border-radius: 16rpx;
  padding: 28rpx;
  margin-bottom: 20rpx;
}
.card-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
}
.day {
  font-size: 30rpx;
  font-weight: 600;
}
.account {
  color: #9ca3af;
  font-size: 24rpx;
}
.spend {
  font-size: 44rpx;
  font-weight: 600;
  margin-top: 12rpx;
}
.summary {
  color: #1f2329;
  line-height: 1.7;
  margin-top: 16rpx;
  font-size: 28rpx;
}
.meta {
  display: flex;
  justify-content: space-between;
  color: #9ca3af;
  font-size: 22rpx;
  margin-top: 20rpx;
}
.more {
  color: #1f6feb;
}
.footnote {
  color: #9ca3af;
  font-size: 22rpx;
  line-height: 1.6;
  padding: 16rpx 8rpx;
}
</style>
