<script setup lang="ts">
import { onLoad } from '@dcloudio/uni-app'
import { computed, ref } from 'vue'

import { listMetrics, type PortalMetricDay, type PortalMetrics } from '../../api/portal'
import { barPercent, divide, peakSpend, sumSeries } from '../../utils/aggregate'
import { formatCount, formatMoney, formatMultiple, formatPercent, toNumber } from '../../utils/decimal'
import { addDays, expandRange, todayIso, type Slot } from '../../utils/series'

/** 默认区间。14 天覆盖「上周同一天」那个异动基线，客户看到告警时两天都在眼前。 */
const WINDOW_DAYS = 14

const accountId = ref(0)
const series = ref<PortalMetrics | null>(null)
const loading = ref(true)
const error = ref('')

const end = todayIso()
const start = addDays(end, -(WINDOW_DAYS - 1))

onLoad((query) => {
  // 页面里不许出现 Number(——转换一律走 utils（tests/test_frontend_source.py 盯着）。
  accountId.value = toNumber(query?.id) ?? 0
  uni.setNavigationBarTitle({ title: readName(query?.name) || '账户明细' })
  void load()
})

/**
 * 从 URL 参数里取账户名。
 *
 * ⚠️ **两端的解码次数不一样。** 首页传过来时 encodeURIComponent 过一次（名字里
 * 可能有 & 和 #），H5 上 uni-app 自己又解一遍，于是地址栏里看到的是双重编码；
 * 小程序端不保证同样的行为。这里多解一次是**幂等安全的** —— 已经是明文的字符串
 * 里没有 %，解码不会改变它。
 *
 * 名字里有个裸的 % 时 decodeURIComponent 会抛（"50%off" 这种真实存在），所以
 * 兜住它回原值：标题显示得不完美，总好过整个页面白屏。
 */
function readName(raw: unknown): string {
  const value = (raw ?? '').toString()
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    series.value = await listMetrics(accountId.value, start, end)
  } catch {
    error.value = '这个账户的数据没能加载出来。'
  } finally {
    loading.value = false
  }
}

const currency = computed(() => series.value?.currency ?? '')
const rows = computed(() => series.value?.items ?? [])

/**
 * 🔴 展开成连续区间，缺的那天是 `null`。
 *
 * 后端不补零 —— 没有数据的那天根本不在 `items` 里。直接按数组顺序渲染，就等于
 * 宣称中间那几天花了钱。展开之后那几天显示「未导入」，和「花了 0」分得开。
 *
 * 倒序：客户最关心昨天。
 */
const slots = computed<Slot<PortalMetricDay>[]>(() =>
  series.value ? expandRange(rows.value, start, end).reverse() : [],
)

/** 花费最大的一天，给下面那根横条定标尺。 */
const peak = computed(() => peakSpend(rows.value))

function barWidth(row: PortalMetricDay | null): string {
  return row ? barPercent(row.spend, peak.value) : '0%'
}

/** 区间合计。显示用，不是对账口径 —— 理由写在 utils/aggregate.ts 的模块注释里。 */
const totals = computed(() => sumSeries(rows.value))
const totalCpa = computed(() => divide(totals.value.spend, totals.value.conversions))
const totalRoas = computed(() => divide(totals.value.revenue, totals.value.spend))
</script>

<template>
  <view class="wrap">
    <view v-if="loading" class="state">加载中…</view>
    <view v-else-if="error" class="state">{{ error }}</view>

    <template v-else-if="series">
      <view class="summary">
        <view class="range">{{ start }} 至 {{ end }}</view>
        <view class="cells">
          <view class="cell">
            <text class="label">花费</text>
            <text class="value">{{ formatMoney(String(totals.spend), currency) }}</text>
          </view>
          <view class="cell">
            <text class="label">转化</text>
            <text class="value">{{ formatCount(totals.conversions) }}</text>
          </view>
          <view class="cell">
            <text class="label">CPA</text>
            <text class="value">{{ formatMoney(totalCpa, currency) }}</text>
          </view>
          <view class="cell">
            <text class="label">ROAS</text>
            <text class="value">{{ formatMultiple(totalRoas) }}</text>
          </view>
        </view>
        <view class="caption">
          金额是 {{ series.currency }}，日期是 {{ series.timezone }} 下的自然日。
          区间内有数据的天数：{{ totals.days }} / {{ slots.length }}。
        </view>
      </view>

      <view v-for="slot in slots" :key="slot.stat_date" class="row">
        <view class="row-head">
          <text class="date">{{ slot.stat_date }}</text>
          <text v-if="slot.item" class="spend">
            {{ formatMoney(slot.item.spend, currency) }}
          </text>
          <!-- 🔴 缺数据的那天：显示「未导入」，不显示 0。花了 0 和没导入是两件事。 -->
          <text v-else class="missing">未导入</text>
        </view>

        <view class="bar-track">
          <view class="bar" :style="{ width: barWidth(slot.item) }" />
        </view>

        <view v-if="slot.item" class="metrics">
          <text>展示 {{ formatCount(slot.item.impressions) }}</text>
          <text>点击 {{ formatCount(slot.item.clicks) }}</text>
          <text>CTR {{ formatPercent(slot.item.ctr) }}</text>
          <text>转化 {{ formatCount(slot.item.conversions) }}</text>
          <text>CPA {{ formatMoney(slot.item.cpa, currency) }}</text>
          <text>ROAS {{ formatMultiple(slot.item.roas) }}</text>
        </view>
      </view>
    </template>
  </view>
</template>

<style scoped>
.wrap {
  padding: 24rpx 24rpx 64rpx;
}
.state {
  color: #6b7280;
  padding: 64rpx 0;
  text-align: center;
}
.summary {
  background: #ffffff;
  border-radius: 16rpx;
  padding: 28rpx;
  margin-bottom: 20rpx;
}
.range {
  color: #6b7280;
  font-size: 24rpx;
}
.cells {
  display: flex;
  margin-top: 20rpx;
}
.cell {
  flex: 1;
  display: flex;
  flex-direction: column;
}
.label {
  color: #9ca3af;
  font-size: 22rpx;
}
.value {
  font-size: 28rpx;
  font-weight: 600;
  margin-top: 6rpx;
}
.caption {
  color: #9ca3af;
  font-size: 22rpx;
  line-height: 1.6;
  margin-top: 20rpx;
}
.row {
  background: #ffffff;
  border-radius: 12rpx;
  padding: 20rpx 24rpx;
  margin-bottom: 12rpx;
}
.row-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}
.date {
  color: #4b5563;
  font-size: 26rpx;
}
.spend {
  font-size: 30rpx;
  font-weight: 600;
}
.missing {
  color: #c0c4cc;
  font-size: 24rpx;
}
.bar-track {
  background: #f3f4f6;
  border-radius: 4rpx;
  height: 8rpx;
  margin: 12rpx 0;
  overflow: hidden;
}
.bar {
  background: #1f6feb;
  height: 8rpx;
}
.metrics {
  display: flex;
  flex-wrap: wrap;
  color: #6b7280;
  font-size: 22rpx;
}
.metrics text {
  margin-right: 20rpx;
}
</style>
