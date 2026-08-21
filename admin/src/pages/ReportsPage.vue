<script setup lang="ts">
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref } from 'vue'

import {
  generateReport,
  getReport,
  listAdAccounts,
  listReports,
  publishReport,
  reviseReport,
  type AdAccount,
  type Report,
} from '../api/endpoints'
import { reason } from '../api/request'
import { formatCount, formatInstant, formatMoney, fromLines, toLines } from '../utils/format'

/**
 * 日报状态的中文名。
 *
 * ⚠️ 这是后端 `ReportStatus` 在前端的**第二份拷贝**（同 `AlertsPage` 那份告警类型
 * 映射）。认不出的回落到原值 —— 那让「对不上」这件事不会报错，只会安静地把英文
 * 标识显示出来。有一条测试双向盯着它和 `models/report.py`：
 * `tests/test_frontend_source.py`。
 */
const REPORT_STATUS_NAMES: Record<string, string> = {
  draft: '草稿',
  pending_review: '待发布',
  published: '已发布',
}

const accounts = ref<AdAccount[]>([])
const accountId = ref<number | null>(null)
const statDate = ref(yesterday())
const items = ref<Report[]>([])
const loading = ref(false)
const generating = ref(false)

/** 抽屉里正在编辑的那一份。`null` 表示抽屉关着。 */
const editing = ref<Report | null>(null)
const draftSummary = ref('')
const draftHighlights = ref('')
const draftNextSteps = ref('')
const reviewer = ref('')
const saving = ref(false)

onMounted(async () => {
  const page = await listAdAccounts()
  accounts.value = page.items
  accountId.value = page.items[0]?.id ?? null
  if (accountId.value !== null) {
    await load()
  }
})

/**
 * 默认选昨天。
 *
 * ⚠️ 这是**运行这个浏览器的机器的**昨天，而 `stat_date` 的口径是账户时区下的自然
 * 日 —— 两者在日切点附近可能差一天。它只是个默认值，日期选择器就摆在旁边，运营
 * 看得见也改得动；界面上那句口径提示写明了按哪个时区算。
 */
function yesterday(): string {
  const day = new Date()
  day.setDate(day.getDate() - 1)
  const month = `${day.getMonth() + 1}`.padStart(2, '0')
  const date = `${day.getDate()}`.padStart(2, '0')
  return `${day.getFullYear()}-${month}-${date}`
}

const currentAccount = computed(() =>
  accounts.value.find((row) => row.id === accountId.value),
)

async function load(): Promise<void> {
  if (accountId.value === null) {
    return
  }
  loading.value = true
  try {
    const page = await listReports(accountId.value)
    items.value = page.items
  } finally {
    loading.value = false
  }
}

/**
 * 生成（或重新生成）。**可逆**，所以不做二次确认。
 *
 * 已发布的那份会被后端拒掉（409）—— 客户手上那份不会自己更新，库里这份也就不该
 * 变。未发布的重新生成会覆盖数字和模型原文，**并清掉人工修订**：数字变了，基于
 * 旧数字写的那段话未必还成立。这句话写在按钮旁边，不藏在弹窗里。
 */
async function generate(): Promise<void> {
  if (accountId.value === null) {
    return
  }
  generating.value = true
  try {
    const report = await generateReport(accountId.value, statDate.value)
    ElMessage.success(
      report.llm_narrative
        ? '生成好了，模型已经写了初稿 —— 改完才能发布'
        : '生成好了。模型没有产出（挂了或没配 LLM），那段话要你自己写',
    )
    await load()
    open(report)
  } catch (error) {
    ElMessage.error(reason(error, '生成失败'))
  } finally {
    generating.value = false
  }
}

function open(report: Report): void {
  editing.value = report
  // 预填人工版；还没改过的话，用模型初稿打底 —— 那正是「修订」这个动作的起点。
  const base = report.narrative ?? report.llm_narrative
  draftSummary.value = base?.summary ?? ''
  draftHighlights.value = fromLines(base?.highlights)
  draftNextSteps.value = fromLines(base?.next_steps)
  reviewer.value = report.reviewer ?? ''
}

async function save(): Promise<void> {
  if (!editing.value) {
    return
  }
  saving.value = true
  try {
    const updated = await reviseReport(editing.value.id, {
      narrative: {
        summary: draftSummary.value.trim(),
        highlights: toLines(draftHighlights.value),
        next_steps: toLines(draftNextSteps.value),
      },
      reviewer: reviewer.value.trim() || null,
    })
    editing.value = updated
    ElMessage.success('已存下你改的这版。模型原文没有被覆盖 —— 两版都留着')
    await load()
  } catch (error) {
    ElMessage.error(reason(error, '保存失败'))
  } finally {
    saving.value = false
  }
}

/**
 * 发布。🔴 **不可逆，且立刻影响外部人** —— 三级里的最高级，所以二次确认，且确认
 * 文案写出后果（`admin.md` 的写操作分级）。
 *
 * 两条硬校验在服务端：必须经人工修订、操作记录不能为空。任一条不满足返回 409，
 * 这里把后端那句话原样显示出来 —— 它说清了缺的是哪一件。
 */
async function publish(report: Report): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `发布之后「${report.stat_date}」这份日报客户立刻就能看到，而且从此不能再改、也不能重新生成。数字后来修正了只能在新一期里说明 —— 客户手上那份截图不会自己更新。`,
      '发布这份日报？',
      { type: 'warning', confirmButtonText: '发布', cancelButtonText: '再看看' },
    )
  } catch {
    return
  }
  try {
    const published = await publishReport(report.id)
    if (editing.value?.id === published.id) {
      editing.value = published
    }
    ElMessage.success('已发布，客户端现在能看到了')
    await load()
  } catch (error) {
    ElMessage.error(reason(error, '发布失败'))
  }
}

function statusName(status: string): string {
  return REPORT_STATUS_NAMES[status] ?? status
}

function statusTag(status: string): 'info' | 'warning' | 'success' {
  if (status === 'published') {
    return 'success'
  }
  return status === 'pending_review' ? 'warning' : 'info'
}
</script>

<template>
  <div class="page">
    <div class="head">
      <h2>日报</h2>
      <div class="actions">
        <el-select
          v-model="accountId"
          placeholder="选一个账户"
          style="width: 260px"
          @change="load"
        >
          <el-option
            v-for="account in accounts"
            :key="account.id"
            :label="account.name"
            :value="account.id"
          />
        </el-select>
        <el-date-picker
          v-model="statDate"
          type="date"
          value-format="YYYY-MM-DD"
          placeholder="报告哪一天"
        />
        <el-button type="primary" :loading="generating" @click="generate">生成日报</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" class="note">
      <template #default>
        日期是<b>账户时区</b>下的自然日（{{ currentAccount?.timezone ?? '按账户各自的时区' }}），
        不是你所在时区的那一天。生成时数字就固定下来了，之后平台补数据也不会再变。
        同一天重复生成会覆盖数字和模型初稿，并清掉已经改好的那版 —— 已发布的则改不动。
      </template>
    </el-alert>

    <el-table v-loading="loading" :data="items" empty-text="这个账户还没有日报" style="width: 100%">
      <el-table-column prop="stat_date" label="日期" width="120" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="statusTag(row.status)" size="small">{{ statusName(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="花费" width="140">
        <template #default="{ row }">{{ formatMoney(row.spend, row.currency) }}</template>
      </el-table-column>
      <el-table-column label="转化" width="90">
        <template #default="{ row }">{{ formatCount(row.conversions, 2) }}</template>
      </el-table-column>
      <el-table-column label="做了几件事" width="110">
        <template #default="{ row }">
          <!-- 空的话发不出去（服务端硬校验）。在列表上就标出来，免得点了发布才知道。 -->
          <span :class="{ blocked: !row.actions_snapshot.length }">
            {{ row.actions_snapshot.length }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="模型初稿" width="100">
        <template #default="{ row }">{{ row.llm_narrative ? '有' : '未生成' }}</template>
      </el-table-column>
      <el-table-column label="人工改过" width="150">
        <template #default="{ row }">
          {{ row.reviewed_at ? formatInstant(row.reviewed_at) : '还没有' }}
        </template>
      </el-table-column>
      <el-table-column label="操作" min-width="160">
        <template #default="{ row }">
          <!-- el-table 的插槽 row 是无类型的 DefaultRow（除非给表格上泛型），
               所以这里断言一次 —— 数据本来就是 listReports 返回的 Report[]。 -->
          <el-button link type="primary" @click="open(row as Report)">
            {{ row.status === 'published' ? '查看' : '修订' }}
          </el-button>
          <el-button
            v-if="row.status !== 'published'"
            link
            type="danger"
            @click="publish(row as Report)"
          >
            发布
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-drawer
      :model-value="editing !== null"
      :title="editing ? `${editing.stat_date} 的日报` : ''"
      size="52%"
      @close="editing = null"
    >
      <div v-if="editing" class="drawer">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="花费">
            {{ formatMoney(editing.spend, editing.currency) }}
          </el-descriptions-item>
          <el-descriptions-item label="CPA">
            {{ formatMoney(editing.cpa, editing.currency) }}
          </el-descriptions-item>
          <el-descriptions-item label="转化">
            {{ formatCount(editing.conversions, 2) }}
          </el-descriptions-item>
          <el-descriptions-item label="对照期">
            {{ editing.baseline_date ?? '没有可比数据' }}
          </el-descriptions-item>
        </el-descriptions>

        <div class="block">
          <div class="block-title">模型初稿（原文，永不修改）</div>
          <div v-if="editing.llm_narrative" class="draft">{{ editing.llm_narrative.summary }}</div>
          <el-alert
            v-else
            type="info"
            :closable="false"
            title="模型没有产出这一份（挂了，或者没配 LLM）。下面那段话要你自己写。"
          />
        </div>

        <div class="block">
          <div class="block-title">本期做了什么（客户会看到这一段）</div>
          <el-alert v-if="!editing.actions_snapshot.length" type="warning" :closable="false">
            <template #title>
              <!-- 🔴 这句话从 D14 起就在指路，而它指向的那一屏到 D17 才有 ——
                   在那之前登记操作只能 curl。所以这里给的是一个能点的链接，不是
                   一句「去某处」：运营已经在这个抽屉里了，让他自己去找那个账户,
                   等于把断链换成了一段路。
                   ⚠️ 登记完必须重新生成：日报的数字和操作快照都是生成那一刻固定
                   下来的（glossary 的「日报快照」），新登记的那条不会自己长进来。 -->
              这一期没有任何操作记录，发不出去。先去
              <router-link :to="`/accounts/${editing.account_id}`" target="_blank">
                这个账户的明细
              </router-link>
              登记当天做过的调整，再回来<b>重新生成</b>这一期。
            </template>
          </el-alert>
          <div v-for="(action, i) in editing.actions_snapshot" :key="i" class="action">
            <div class="action-summary">{{ action.summary }}</div>
            <div class="action-reason">{{ action.reason }}</div>
          </div>
        </div>

        <div class="block">
          <div class="block-title">你的版本（客户看到的是这一段）</div>
          <el-input
            v-model="draftSummary"
            type="textarea"
            :rows="5"
            placeholder="今天整体怎么样、变化可能来自什么"
          />
          <div class="sub-title">值得注意（一行一条）</div>
          <el-input v-model="draftHighlights" type="textarea" :rows="3" />
          <div class="sub-title">接下来（一行一条）</div>
          <el-input v-model="draftNextSteps" type="textarea" :rows="3" />
          <div class="sub-title">谁改的（可留空）</div>
          <el-input v-model="reviewer" placeholder="你的名字" style="max-width: 240px" />
        </div>

        <el-alert type="warning" :closable="false" class="gate">
          <template #default>
            改完才发得出去，这不是走过场：模型可能在句子里写出一个和上面对不上的百分比，
            而没有任何机器判定拦得住 —— 你是这件事唯一的防线。
          </template>
        </el-alert>

        <div class="drawer-actions">
          <el-button
            v-if="editing.status !== 'published'"
            type="primary"
            :loading="saving"
            @click="save"
          >
            保存修订
          </el-button>
          <el-button
            v-if="editing.status !== 'published'"
            type="danger"
            :disabled="!editing.reviewed_at || !editing.actions_snapshot.length"
            @click="publish(editing)"
          >
            发布
          </el-button>
          <span v-if="editing.status === 'published'" class="published">
            已于 {{ formatInstant(editing.published_at) }} 发布，客户已经看到 —— 不能再改了。
          </span>
        </div>
      </div>
    </el-drawer>
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
  align-items: center;
  gap: 12px;
}
.note {
  margin-bottom: 12px;
}
.blocked {
  color: #c45656;
  font-weight: 600;
}
.drawer {
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.block-title {
  font-weight: 600;
  margin-bottom: 8px;
}
.sub-title {
  color: #6b7280;
  font-size: 13px;
  margin: 12px 0 6px;
}
.draft {
  background: #f6f7f9;
  border-radius: 6px;
  padding: 12px;
  line-height: 1.7;
  color: #4b5563;
}
.action {
  border-top: 1px solid #f0f1f3;
  padding-top: 10px;
  margin-top: 10px;
}
.action:first-of-type {
  border-top: none;
  margin-top: 0;
  padding-top: 0;
}
.action-summary {
  font-weight: 600;
}
.action-reason {
  color: #6b7280;
  line-height: 1.6;
  margin-top: 4px;
}
.gate {
  line-height: 1.7;
}
.drawer-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.published {
  color: #6b7280;
}
</style>
