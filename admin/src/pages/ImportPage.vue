<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  getTask,
  importReport,
  listAdAccounts,
  normalizeAccount,
  type AdAccount,
  type ImportResult,
  type TaskStatus,
} from '../api/endpoints'
import { NeedsRedo } from '../api/request'

/** 后端上限，超了回 413。**在选文件时就拦** —— 传完十兆再说不行，那十兆是白传的。 */
const MAX_BYTES = 10 * 1024 * 1024

/** 轮询上限。无限轮询的页面在标签页里留一夜，会把日志刷满。 */
const MAX_POLLS = 30
const POLL_INTERVAL_MS = 1000

const LEVELS = [
  { value: 'account', label: '账户' },
  { value: 'campaign', label: '广告系列' },
  { value: 'adgroup', label: '广告组' },
  { value: 'ad', label: '广告' },
]

const accounts = ref<AdAccount[]>([])
const accountId = ref<number>()
const level = ref('account')
const dateColumn = ref('')
const file = ref<File | null>(null)

const busy = ref(false)
const parseError = ref('')
const result = ref<ImportResult | null>(null)
const task = ref<TaskStatus | null>(null)
const taskNote = ref('')

onMounted(async () => {
  const page = await listAdAccounts()
  accounts.value = page.items
})

const selected = computed(() => accounts.value.find((item) => item.id === accountId.value))

function pickFile(event: Event): void {
  const input = event.target as HTMLInputElement
  const picked = input.files?.[0] ?? null
  if (picked && picked.size > MAX_BYTES) {
    parseError.value = `文件 ${(picked.size / 1024 / 1024).toFixed(1)} MB，超过 10 MB 上限。大文件请先在导出时按日期拆分。`
    input.value = ''
    file.value = null
    return
  }
  parseError.value = ''
  file.value = picked
}

async function submit(): Promise<void> {
  if (!file.value || !accountId.value || busy.value) {
    return
  }
  busy.value = true
  parseError.value = ''
  result.value = null
  task.value = null
  taskNote.value = ''

  const form = new FormData()
  form.set('account_id', String(accountId.value))
  form.set('file', file.value)
  form.set('level', level.value)
  form.set('provider', 'file_csv')
  if (dateColumn.value.trim()) {
    form.set('date_column', dateColumn.value.trim())
  }

  try {
    result.value = await importReport(form)
    await followTask(result.value.task_id)
  } catch (error) {
    if (error instanceof NeedsRedo) {
      parseError.value = error.message
    } else {
      // 🔴 **原样显示后端说了什么。** 认不出日期列时它会把表头列出来，那正是运营
      // 要拿去改导出设置的信息 —— 换成一句「导入失败」等于让人去猜。
      parseError.value = error instanceof Error ? error.message : '导入失败'
    }
  } finally {
    busy.value = false
  }
}

/**
 * 归一化是**第二段**，异步的。
 *
 * 落快照成功不等于数字能查了 —— 只显示一个「导入成功」，运营会以为已经完事。
 */
async function followTask(taskId: string | null): Promise<void> {
  if (!taskId) {
    // 队列连不上时快照照样落好，只是没人接着跑。给一个手动入口，而不是一个永远
    // 转圈的进度条。
    taskNote.value = '快照已存下，但归一化没排上队（队列连不上）。可以手动跑一次。'
    return
  }
  for (let attempt = 0; attempt < MAX_POLLS; attempt += 1) {
    task.value = await getTask(taskId)
    if (task.value.ready) {
      return
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS))
  }
  taskNote.value = '归一化还没跑完。它会继续在后台跑，稍后刷新这个页面看结果。'
}

async function normalizeNow(): Promise<void> {
  if (!accountId.value) {
    return
  }
  busy.value = true
  try {
    const summary = await normalizeAccount(accountId.value)
    taskNote.value = `手动归一化完成：写入 ${summary.rows} 行，覆盖 ${summary.days.length} 天。`
  } catch (error) {
    taskNote.value = error instanceof Error ? error.message : '归一化失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="page">
    <h2>导入报表</h2>
    <p class="lead">
      平台后台导出的 CSV。解析在这一步<b>当场</b>完成，归一化随后异步跑 —— 下面会分开
      告诉你两段各自的结果。
    </p>

    <el-form label-width="90px" class="form">
      <el-form-item label="广告账户">
        <el-select v-model="accountId" placeholder="选一个账户" filterable style="width: 360px">
          <el-option
            v-for="account in accounts"
            :key="account.id"
            :label="`${account.name}（${account.platform} · ${account.currency}）`"
            :value="account.id"
          />
        </el-select>
      </el-form-item>

      <el-form-item label="投放层级">
        <el-radio-group v-model="level">
          <el-radio v-for="item in LEVELS" :key="item.value" :value="item.value">
            {{ item.label }}
          </el-radio>
        </el-radio-group>
        <div class="tip">导出时选的是什么就填什么。填错会让同一天出现两个层级的数字。</div>
      </el-form-item>

      <el-form-item label="日期列名">
        <el-input v-model="dateColumn" placeholder="留空自动探测" style="width: 240px" />
        <div class="tip">探测不到时下面会把表头列出来，照着填一个。</div>
      </el-form-item>

      <el-form-item label="文件">
        <input type="file" accept=".csv,text/csv" @change="pickFile" />
        <div class="tip">上限 10 MB。{{ file ? `已选：${file.name}` : '' }}</div>
      </el-form-item>

      <el-form-item>
        <el-button
          type="primary"
          :loading="busy"
          :disabled="!file || !accountId"
          @click="submit"
        >
          导入
        </el-button>
      </el-form-item>
    </el-form>

    <el-alert v-if="parseError" type="error" :closable="false" show-icon class="block">
      <template #title>第一段（解析）没过</template>
      <pre class="detail">{{ parseError }}</pre>
    </el-alert>

    <template v-if="result">
      <el-alert type="success" :closable="false" show-icon class="block">
        <template #title>第一段（解析 + 落快照）完成</template>
        <div class="detail">
          写入 {{ result.rows }} 行，覆盖 {{ result.days.length }} 天（{{ result.days.join('、') }}）。
          <template v-if="result.skipped_rows">
            跳过 {{ result.skipped_rows }} 行 —— 通常是导出文件里残留的小计行。
          </template>
        </div>
      </el-alert>

      <el-alert
        :type="task?.state === 'SUCCESS' ? 'success' : task?.error ? 'error' : 'info'"
        :closable="false"
        show-icon
        class="block"
      >
        <template #title>第二段（归一化）{{ task?.ready ? '完成' : '进行中' }}</template>
        <div class="detail">
          <template v-if="task">任务 {{ task.task_id }} · 状态 {{ task.state }}</template>
          <template v-if="task?.error"> · {{ task.error }}</template>
          <div v-if="taskNote">{{ taskNote }}</div>
          <el-button
            v-if="!task || taskNote"
            size="small"
            :loading="busy"
            class="redo"
            @click="normalizeNow"
          >
            手动归一化这个账户
          </el-button>
        </div>
      </el-alert>

      <p class="after">
        数字要核对的话去
        <router-link v-if="selected" :to="`/accounts/${selected.id}`">
          {{ selected.name }} 的明细
        </router-link>
        。
      </p>
    </template>
  </div>
</template>

<style scoped>
.page {
  max-width: 860px;
}
.lead {
  color: var(--el-text-color-secondary);
  line-height: 1.8;
}
.form {
  margin-top: 8px;
}
.tip {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.6;
}
.block {
  margin-bottom: 12px;
}
.detail {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 13px;
  line-height: 1.7;
  margin: 0;
}
.redo {
  margin-top: 8px;
}
.after {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
</style>
