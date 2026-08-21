<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { listAlerts, sweepAlerts, type Alert } from '../api/endpoints'
import { NeedsRedo } from '../api/request'
import { formatInstant } from '../utils/format'

/**
 * 告警类型的中文名。
 *
 * ⚠️ 这是后端 `AlertKind` 在前端的**第二份拷贝**（客户端还有第三份）。认不出的
 * 回落到原值 —— 那让「对不上」这件事不会报错，只会安静地把英文标识显示出来。
 * 有一条测试双向盯着它和 `models/alert.py`：`tests/test_frontend_source.py`。
 */
const KIND_NAMES: Record<string, string> = {
  balance_low: '余额',
  metric_anomaly: '指标异动',
}

const items = ref<Alert[]>([])
const onlyOpen = ref(true)
const loading = ref(false)
const sweeping = ref(false)
const note = ref('')

onMounted(load)

async function load(): Promise<void> {
  loading.value = true
  try {
    const page = await listAlerts(onlyOpen.value)
    items.value = page.items
  } finally {
    loading.value = false
  }
}

/**
 * 立刻巡检一遍，不等下一个整点。
 *
 * 巡检是**幂等**的（对账，不是每轮写一条），所以这个按钮可以随便点 —— 那也正是
 * 它敢挂在定时任务上的原因。
 */
async function sweep(): Promise<void> {
  sweeping.value = true
  note.value = ''
  try {
    const summary = await sweepAlerts()
    note.value = `巡检了 ${summary.accounts} 个账户：新开 ${summary.opened}，仍成立 ${summary.still_open}，自动收掉 ${summary.resolved}，推送 ${summary.notified}。`
    await load()
  } catch (error) {
    note.value = error instanceof NeedsRedo ? error.message : '巡检失败'
  } finally {
    sweeping.value = false
  }
}

function kindName(kind: string): string {
  return KIND_NAMES[kind] ?? kind
}
</script>

<template>
  <div class="page">
    <div class="head">
      <h2>告警</h2>
      <div class="actions">
        <el-switch
          v-model="onlyOpen"
          active-text="只看未解决"
          inactive-text="全部历史"
          @change="load"
        />
        <el-button :loading="sweeping" @click="sweep">立刻巡检</el-button>
      </div>
    </div>

    <el-alert v-if="note" :title="note" type="info" :closable="false" class="note" />

    <el-table v-loading="loading" :data="items" empty-text="没有告警" style="width: 100%">
      <el-table-column label="类型" width="110">
        <template #default="{ row }">{{ kindName(row.kind) }}</template>
      </el-table-column>
      <el-table-column prop="subject" label="对象" width="140" />
      <el-table-column prop="message" label="说了什么" min-width="360" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.status === 'open' ? 'danger' : 'success'" size="small">
            {{ row.status === 'open' ? '未解决' : '已恢复' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="发生于" width="150">
        <template #default="{ row }">{{ formatInstant(row.opened_at) }}</template>
      </el-table-column>
      <el-table-column label="推送" width="150">
        <template #default="{ row }">
          <!-- 客户端那套刻意不给这个字段：推送成功没有是运维信息，跟客户无关。 -->
          {{ row.notified_at ? formatInstant(row.notified_at) : '未推送' }}
        </template>
      </el-table-column>
      <el-table-column label="账户" width="90">
        <template #default="{ row }">
          <router-link :to="`/accounts/${row.account_id}`">明细</router-link>
        </template>
      </el-table-column>
    </el-table>
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
  gap: 16px;
}
.note {
  margin-bottom: 12px;
}
</style>
