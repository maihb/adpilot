<script setup lang="ts">
import { onLoad } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'

import { getReport, listAccounts, type PortalReport } from '../../api/portal'
import {
  formatChange,
  formatCount,
  formatMoney,
  formatMultiple,
  formatPercent,
  toNumber,
} from '../../utils/decimal'
import { formatInstant } from '../../utils/time'

const report = ref<PortalReport | null>(null)
const accountName = ref('')
const loading = ref(true)
const error = ref('')

onLoad((query) => {
  // 页面里不许出现 Number( —— 转换一律走 utils（tests/test_frontend_source.py 盯着）。
  void load(toNumber(query?.id) ?? 0)
})

async function load(reportId: number): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    // 账户名不在日报出参里（它是标签不是口径，见 reports.vue 那段注释），
    // 所以顺带取一次账户列表。
    const [detail, accounts] = await Promise.all([getReport(reportId), listAccounts()])
    report.value = detail
    accountName.value = accounts.items.find((row) => row.id === detail.account_id)?.name ?? ''
  } catch {
    error.value = '这份日报没能打开。它可能已经不在了，或者不属于你。'
  } finally {
    loading.value = false
  }
}

/**
 * 那段人话的三部分。
 *
 * ⚠️ **后端给了默认空数组，但 OpenAPI 把它们标成非必需**（Pydantic 的
 * `default_factory` 不进 required），于是生成出来的类型是 `string[] | undefined`。
 * 在这里补齐一次，模板里就不用到处写 `?.` —— 而漏写一个 `?.` 在小程序端是运行时
 * 报错，不是编译期。
 */
const narrative = computed(() => {
  const written = report.value?.narrative
  return {
    summary: written?.summary ?? '',
    highlights: written?.highlights ?? [],
    next_steps: written?.next_steps ?? [],
  }
})

/**
 * 数字块。**环比算不出来时那一格整段不显示**，不显示「+0%」。
 *
 * 对照期（上周同日）没有数据时，后端把 baseline 三项一起置空 —— 补一个 0 会算出
 * 「上升了 100%」这种凭空的百分比（client-app.md 口径第一条的同一个道理）。
 */
const figures = computed(() => {
  const row = report.value
  if (!row) {
    return []
  }
  return [
    {
      label: '花费',
      value: formatMoney(row.spend, row.currency),
      change: formatChange(row.spend, row.baseline_spend),
    },
    {
      label: '转化数',
      value: formatCount(row.conversions, 2),
      change: formatChange(row.conversions, row.baseline_conversions),
    },
    {
      label: 'CPA',
      value: formatMoney(row.cpa, row.currency),
      change: formatChange(row.cpa, row.baseline_cpa),
    },
    { label: 'ROAS', value: formatMultiple(row.roas), change: null },
    { label: '展示', value: formatCount(row.impressions), change: null },
    { label: '点击', value: formatCount(row.clicks), change: null },
    { label: '点击率', value: formatPercent(row.ctr), change: null },
    { label: '千次展示成本', value: formatMoney(row.cpm, row.currency), change: null },
  ]
})

/**
 * 一格环比都算不出来时，得说一句为什么，否则客户会以为哪里没加载出来。
 *
 * 判据是「有没有算出环比」而不是「有没有对照期」：对照期存在、但那天花费是 0 时
 * 除法同样没有意义（暂停投放的账户就是这样），此时同样一格都不显示。
 */
const noComparison = computed(() => figures.value.every((item) => item.change === null))
</script>

<template>
  <view class="wrap">
    <view v-if="loading" class="state">加载中…</view>
    <view v-else-if="error" class="state">{{ error }}</view>

    <template v-else-if="report">
      <view class="head">
        <text class="day">{{ report.stat_date }}</text>
        <text class="account">{{ accountName }}</text>
      </view>
      <!-- 口径必须挨着数字：日期是账户时区下的自然日，不是客户所在时区的那一天。
           不注明的话，客户拿自己后台的数字来对永远差一截。 -->
      <view class="caliber">
        口径：{{ report.timezone }} 时区的自然日 · 金额单位 {{ report.currency }}
      </view>

      <!-- 这段话由运营确认后才发布。模型只写初稿，数字不由它产生。 -->
      <view class="card narrative">
        <view class="summary">{{ narrative.summary }}</view>

        <view v-if="narrative.highlights.length" class="block">
          <view class="block-title">值得注意</view>
          <view v-for="(line, i) in narrative.highlights" :key="i" class="bullet">
            · {{ line }}
          </view>
        </view>

        <view v-if="narrative.next_steps.length" class="block">
          <view class="block-title">接下来</view>
          <view v-for="(line, i) in narrative.next_steps" :key="i" class="bullet">
            · {{ line }}
          </view>
        </view>
      </view>

      <view class="grid">
        <view v-for="item in figures" :key="item.label" class="cell">
          <view class="cell-label">{{ item.label }}</view>
          <view class="cell-value">{{ item.value }}</view>
          <view v-if="item.change" class="cell-change">较上周同日 {{ item.change }}</view>
        </view>
      </view>
      <view v-if="noComparison" class="footnote">
        上周同日没有可比的数据，所以这份日报里不做环比。
      </view>

      <view class="card">
        <view class="block-title">本期做了什么</view>
        <view v-if="!report.actions.length" class="empty">这一天没有调整记录。</view>
        <view v-for="(action, i) in report.actions" :key="i" class="action">
          <!-- 🔴 **不显示操作时刻。** performed_at 是真实时刻，而 formatInstant 按
               手机本地时区渲染 —— 账户时区 08-19 中午的一次调整，在 UTC+8 的手机上
               会显示成「08-20 03:00」，于是一份 08-19 的日报里出现了 08-20 的操作，
               客户会以为记错了日子。日报本来就已经限定了是哪一天，几点做的对解释
               效果没有帮助，不值得为它引入一次跨时区渲染。 -->
          <view class="action-summary">{{ action.summary }}</view>
          <!-- 「为什么这么调」是日报里最值钱的一段，平台的变更日志给不出它。 -->
          <view class="action-reason">{{ action.reason }}</view>
        </view>
      </view>

      <view v-if="report.alerts.length" class="card">
        <view class="block-title">当时要注意的事</view>
        <view v-for="(line, i) in report.alerts" :key="i" class="bullet">· {{ line }}</view>
      </view>

      <view class="footnote">
        数字是这份日报生成时定下来的，之后即使平台补了数据也不会再变 —— 你今天看到的
        和当初收到的是同一份。发布于 {{ formatInstant(report.published_at) }}。
      </view>
    </template>
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
.head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  padding: 0 8rpx;
}
.day {
  font-size: 44rpx;
  font-weight: 600;
}
.account {
  color: #6b7280;
  font-size: 26rpx;
}
.caliber {
  color: #9ca3af;
  font-size: 22rpx;
  padding: 8rpx 8rpx 24rpx;
}
.card {
  background: #ffffff;
  border-radius: 16rpx;
  padding: 28rpx;
  margin-bottom: 20rpx;
}
.narrative .summary {
  color: #1f2329;
  font-size: 30rpx;
  line-height: 1.8;
}
.block {
  margin-top: 24rpx;
}
.block-title {
  color: #6b7280;
  font-size: 24rpx;
  font-weight: 600;
  margin-bottom: 12rpx;
}
.bullet {
  color: #1f2329;
  line-height: 1.7;
  font-size: 27rpx;
}
.grid {
  display: flex;
  flex-wrap: wrap;
  background: #ffffff;
  border-radius: 16rpx;
  padding: 12rpx 0;
  margin-bottom: 20rpx;
}
.cell {
  width: 50%;
  padding: 20rpx 28rpx;
  box-sizing: border-box;
}
.cell-label {
  color: #9ca3af;
  font-size: 22rpx;
}
.cell-value {
  font-size: 34rpx;
  font-weight: 600;
  margin-top: 6rpx;
}
.cell-change {
  color: #6b7280;
  font-size: 20rpx;
  margin-top: 4rpx;
}
.empty {
  color: #9ca3af;
  font-size: 26rpx;
}
.action {
  border-top: 1rpx solid #f0f1f3;
  padding-top: 20rpx;
  margin-top: 20rpx;
}
.action:first-of-type {
  border-top: none;
  padding-top: 0;
}
.action-summary {
  font-size: 28rpx;
  font-weight: 600;
  line-height: 1.6;
}
.action-reason {
  color: #4b5563;
  line-height: 1.7;
  font-size: 26rpx;
  margin-top: 8rpx;
}
.footnote {
  color: #9ca3af;
  font-size: 22rpx;
  line-height: 1.6;
  padding: 16rpx 8rpx;
}
</style>
