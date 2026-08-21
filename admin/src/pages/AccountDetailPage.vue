<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  getAdAccount,
  listActions,
  listBalances,
  listDailyMetrics,
  normalizeAccount,
  recordAction,
  recordBalance,
  type Action,
  type AdAccount,
  type Balance,
  type DailyMetric,
} from '../api/endpoints'
import { ACTION_KIND_NAMES, METRIC_LEVEL_NAMES, nameOf, options } from '../api/enums'
import { reason } from '../api/request'
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

const actions = ref<Action[]>([])

/**
 * 登记一次投放调整。
 *
 * 🔴 **这一屏不是可有可无的台账。** 日报发布硬校验「当期操作记录非空」
 * （`services/report.py`），所以在这里登记之前，那一天的日报**发不出去** ——
 * 在 D17 之前这件事只能靠 curl，而日报屏上那句「先去账户明细登记」指向的正是
 * 这里，指了好几轮却没有地方可去。
 */
const logging = ref(false)
const actionKind = ref('budget')
const actionLevel = ref('account')
const actionObjectId = ref('')
const actionSummary = ref('')
const actionReason = ref('')
const actionPerformedAt = ref('')
const actionOperator = ref('')

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
    const [detail, metricPage, balancePage, actionPage] = await Promise.all([
      getAdAccount(accountId),
      listDailyMetrics(accountId, start, end),
      listBalances(accountId),
      listActions(accountId),
    ])
    account.value = detail
    metrics.value = metricPage.items
    balances.value = balancePage.items
    actions.value = actionPage.items
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
    note.value = reason(error, '归一化失败')
  } finally {
    busy.value = false
  }
}

async function submitAction(): Promise<void> {
  if (!actionSummary.value.trim() || !actionReason.value.trim() || !actionPerformedAt.value) {
    return
  }

  // 🔴 **未来的时刻在这里就拦下来**，不等后端 422。
  //
  // 后端拦它是因为落在未来的记录永远不会进任何一期日报（却看起来像是记过了）。
  // 前端也拦一次的理由不同：`el-date-picker` 的默认值是**今天此刻**，而运营常常
  // 是下班前补登记白天做的事 —— 手一滑把分钟调大几格就成了未来，而那时表单已经
  // 填满了一屏，一个 422 弹窗读起来像是「哪里格式不对」。
  const performedAt = new Date(actionPerformedAt.value)
  if (performedAt.getTime() > Date.now()) {
    ElMessage.error('操作时刻在未来 —— 填的是这次调整「实际发生」的时刻，不是登记时刻')
    return
  }

  busy.value = true
  try {
    await recordAction(accountId, {
      kind: actionKind.value as Action['kind'],
      level: actionLevel.value as Action['level'],
      // 账户级操作不带 object_id：把账户自己的 external_id 填进来不增加信息。
      object_id: actionLevel.value === 'account' ? null : actionObjectId.value.trim() || null,
      summary: actionSummary.value.trim(),
      reason: actionReason.value.trim(),
      // datetime-local / el-date-picker 给的都是没有时区的本地时刻，而后端**拒绝
      // naive datetime** —— 补上本机时区偏移再发（同录余额那条）。
      performed_at: performedAt.toISOString(),
      operator: actionOperator.value.trim() || null,
    })
    logging.value = false
    actionSummary.value = ''
    actionReason.value = ''
    actionObjectId.value = ''
    await load()
    ElMessage.success('已登记 —— 这一天的日报现在发得出去了')
  } catch (error) {
    ElMessage.error(reason(error, '登记失败'))
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
    ElMessage.error(reason(error, '录余额失败'))
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
        <el-button size="small" type="primary" @click="logging = true">登记一次调整</el-button>
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

    <h3>最近做了什么</h3>
    <p class="muted small">
      日报里「本期做了什么」那一段只有这一个来源。
      <b>这一天没有记录，那一天的日报就发不出去</b>
      —— 那是服务端的硬校验，不是提示。
    </p>
    <el-table :data="actions.slice(0, 8)" empty-text="还没登记过调整" style="width: 100%">
      <el-table-column label="做了什么" min-width="240">
        <template #default="{ row }">{{ row.summary }}</template>
      </el-table-column>
      <el-table-column label="为什么这么做" min-width="280">
        <!--
          🔴 这一列比上一列宽，是刻意的：它是这张表和平台变更日志的唯一区别。
          平台记得住「预算 500 → 800」，记不住「周末 CPM 普涨，先扛量到周一」，
          而后者才是日报里值钱的那句。
        -->
        <template #default="{ row }">{{ row.reason }}</template>
      </el-table-column>
      <el-table-column label="类型" width="90">
        <template #default="{ row }">{{ nameOf(ACTION_KIND_NAMES, row.kind) }}</template>
      </el-table-column>
      <el-table-column label="层级" width="100">
        <template #default="{ row }">
          {{ nameOf(METRIC_LEVEL_NAMES, row.level) }}
          <span v-if="row.object_id" class="muted">· {{ row.object_id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="什么时候做的" width="160">
        <!-- performed_at 是操作实际发生的时刻，不是登记时刻（created_at）。 -->
        <template #default="{ row }">{{ formatInstant(row.performed_at) }}</template>
      </el-table-column>
      <el-table-column label="谁" width="90">
        <template #default="{ row }">{{ row.operator || '—' }}</template>
      </el-table-column>
    </el-table>

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

    <el-dialog v-model="logging" title="登记一次投放调整" width="620px">
      <el-form label-width="110px" @submit.prevent="submitAction">
        <el-form-item label="做了什么">
          <el-input v-model="actionSummary" placeholder="一行人话，会原样进日报（A 系列日预算 500 → 800）" />
        </el-form-item>
        <el-form-item label="为什么这么做">
          <el-input
            v-model="actionReason"
            type="textarea"
            :rows="3"
            placeholder="周末 CPM 普涨，先扛量到周一再看"
          />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="actionKind" style="width: 160px">
            <el-option
              v-for="item in options(ACTION_KIND_NAMES)"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="动在哪一级">
          <el-select v-model="actionLevel" style="width: 160px">
            <el-option
              v-for="item in options(METRIC_LEVEL_NAMES)"
              :key="item.value"
              :label="item.label"
              :value="item.value"
            />
          </el-select>
          <el-input
            v-if="actionLevel !== 'account'"
            v-model="actionObjectId"
            placeholder="平台侧的对象 ID"
            style="width: 240px; margin-left: 12px"
          />
        </el-form-item>
        <el-form-item label="什么时候做的">
          <el-date-picker
            v-model="actionPerformedAt"
            type="datetime"
            placeholder="实际发生的时刻，不是现在"
          />
        </el-form-item>
        <el-form-item label="谁做的">
          <el-input v-model="actionOperator" placeholder="可留空" style="width: 240px" />
        </el-form-item>
      </el-form>
      <p class="muted small">
        「为什么这么做」是必填的，也是这张表存在的全部理由 —— 平台的变更日志补得上
        「改了什么」，补不上「为什么」，而后者是日报里唯一值钱的那段。
        登记之后<b>没有修改和删除</b>：填错了再登记一条说明，那本身也是投放过程的一部分。
      </p>
      <template #footer>
        <el-button @click="logging = false">取消</el-button>
        <el-button
          type="primary"
          :loading="busy"
          :disabled="!actionSummary.trim() || !actionReason.trim() || !actionPerformedAt"
          @click="submitAction"
        >
          登记
        </el-button>
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
