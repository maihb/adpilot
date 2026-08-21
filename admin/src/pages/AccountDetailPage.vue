<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  getAdAccount,
  listBalances,
  listDailyMetrics,
  normalizeAccount,
  recordBalance,
  type AdAccount,
  type Balance,
  type DailyMetric,
} from '../api/endpoints'
import { NeedsRedo } from '../api/request'
import { formatCount, formatInstant, formatMoney, formatMultiple, formatPercent, toNumber } from '../utils/format'

const route = useRoute()
const accountId = toNumber(String(route.params.id)) ?? 0

/** 明细默认看 28 天：比客户端那边宽，运营要核对的往往是「上个月那一段」。 */
const WINDOW_DAYS = 28

const account = ref<AdAccount | null>(null)
const metrics = ref<DailyMetric[]>([])
const balances = ref<Balance[]>([])
const loading = ref(false)
const busy = ref(false)
const note = ref('')

const recording = ref(false)
const available = ref('')
const capturedAt = ref('')
const balanceNote = ref('')

/** 日期算术全程 UTC —— `stat_date` 是账户时区下的标签，不是时刻。 */
function isoDaysAgo(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10)
}

const start = isoDaysAgo(WINDOW_DAYS - 1)
const end = isoDaysAgo(0)

onMounted(load)

async function load(): Promise<void> {
  loading.value = true
  try {
    const [detail, metricPage, balancePage] = await Promise.all([
      getAdAccount(accountId),
      listDailyMetrics(accountId, start, end),
      listBalances(accountId),
    ])
    account.value = detail
    metrics.value = metricPage.items
    balances.value = balancePage.items
  } finally {
    loading.value = false
  }
}

const currency = computed(() => account.value?.currency ?? '')

async function normalize(): Promise<void> {
  busy.value = true
  note.value = ''
  try {
    const summary = await normalizeAccount(accountId)
    note.value = `归一化完成：写入 ${summary.rows} 行，覆盖 ${summary.days.length} 天，用到 ${summary.snapshots} 条快照。`
    await load()
  } catch (error) {
    note.value = error instanceof NeedsRedo ? error.message : '归一化失败'
  } finally {
    busy.value = false
  }
}

async function submitBalance(): Promise<void> {
  if (!available.value.trim() || !capturedAt.value) {
    return
  }
  busy.value = true
  try {
    await recordBalance(accountId, {
      // 金额原样把字符串交出去 —— 转成 number 会在这里丢精度，而后端收的是 Decimal。
      available: available.value.trim(),
      // datetime-local 给的是没有时区的本地时刻，后端**拒绝 naive datetime**，
      // 所以补上本机时区偏移再发。
      captured_at: new Date(capturedAt.value).toISOString(),
      note: balanceNote.value.trim() || null,
    })
    recording.value = false
    available.value = ''
    balanceNote.value = ''
    await load()
    ElMessage.success('余额已录入')
  } catch (error) {
    ElMessage.error(error instanceof NeedsRedo ? error.message : '录余额失败')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div v-loading="loading" class="page">
    <div class="head">
      <h2>{{ account?.name ?? '账户' }}</h2>
      <div class="actions">
        <el-button size="small" @click="recording = true">录一笔余额</el-button>
        <el-button size="small" :loading="busy" @click="normalize">重跑归一化</el-button>
      </div>
    </div>

    <el-descriptions v-if="account" :column="4" border class="block">
      <el-descriptions-item label="平台">{{ account.platform }}</el-descriptions-item>
      <el-descriptions-item label="币种">{{ account.currency }}</el-descriptions-item>
      <el-descriptions-item label="时区">{{ account.timezone }}</el-descriptions-item>
      <el-descriptions-item label="平台侧 ID">{{ account.external_id }}</el-descriptions-item>
    </el-descriptions>

    <el-alert v-if="note" :title="note" type="info" :closable="false" class="block" />

    <h3>最近余额</h3>
    <el-table :data="balances.slice(0, 5)" empty-text="还没录过余额" style="width: 100%">
      <el-table-column label="可用余额" width="160">
        <template #default="{ row }">{{ formatMoney(row.available, row.currency) }}</template>
      </el-table-column>
      <el-table-column label="是什么时候的" width="180">
        <template #default="{ row }">{{ formatInstant(row.captured_at) }}</template>
      </el-table-column>
      <el-table-column prop="note" label="备注" min-width="200" />
    </el-table>

    <h3>日指标（{{ start }} 至 {{ end }}）</h3>
    <p class="muted small">
      日期是 {{ account?.timezone }} 下的自然日，金额是 {{ currency }}。
      没有数据的那天不在表里 —— 那和「花了 0」不是一回事。
    </p>
    <el-table :data="metrics" empty-text="这段时间没有数据" style="width: 100%" max-height="520">
      <el-table-column prop="stat_date" label="日期" width="110" />
      <el-table-column prop="level" label="层级" width="90" />
      <el-table-column prop="object_name" label="对象" min-width="180">
        <template #default="{ row }">{{ row.object_name || row.object_id }}</template>
      </el-table-column>
      <el-table-column label="花费" width="130">
        <template #default="{ row }">{{ formatMoney(row.spend, currency) }}</template>
      </el-table-column>
      <el-table-column label="展示" width="100">
        <template #default="{ row }">{{ formatCount(row.impressions) }}</template>
      </el-table-column>
      <el-table-column label="点击" width="90">
        <template #default="{ row }">{{ formatCount(row.clicks) }}</template>
      </el-table-column>
      <el-table-column label="CTR" width="90">
        <template #default="{ row }">{{ formatPercent(row.ctr) }}</template>
      </el-table-column>
      <el-table-column label="转化" width="90">
        <template #default="{ row }">{{ formatCount(row.conversions) }}</template>
      </el-table-column>
      <el-table-column label="CPA" width="120">
        <template #default="{ row }">{{ formatMoney(row.cpa, currency) }}</template>
      </el-table-column>
      <el-table-column label="ROAS" width="90">
        <template #default="{ row }">{{ formatMultiple(row.roas) }}</template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="recording" title="录一笔余额" width="440px">
      <el-form label-width="110px" @submit.prevent="submitBalance">
        <el-form-item label="可用余额">
          <el-input v-model="available" :placeholder="`${currency}，如 1234.56`" />
        </el-form-item>
        <el-form-item label="是什么时候的">
          <el-date-picker v-model="capturedAt" type="datetime" placeholder="不是录入时间" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="balanceNote" placeholder="从哪看来的、是不是刚充完值" />
        </el-form-item>
      </el-form>
      <p class="muted small">
        币种取账户的（{{ currency }}）—— 让人手填早晚会出现「账户是 USD、余额录成
        CNY」的一条快照，而算出来的可撑天数看起来完全正常。
      </p>
      <template #footer>
        <el-button @click="recording = false">取消</el-button>
        <el-button type="primary" :loading="busy" @click="submitBalance">录入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.actions {
  display: flex;
  gap: 8px;
}
.block {
  margin-bottom: 12px;
}
.muted {
  color: var(--el-text-color-secondary);
}
.small {
  font-size: 12px;
  line-height: 1.7;
}
</style>
