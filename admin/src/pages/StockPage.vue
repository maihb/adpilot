<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  importStock,
  listClients,
  listStockRunway,
  type Client,
  type StockImportResult,
  type StockRunway,
} from '../api/endpoints'
import { reason } from '../api/request'
import { formatCount, formatInstant } from '../utils/format'

/** 后端上限，超了回 413。**在选文件时就拦**，同 ImportPage。 */
const MAX_BYTES = 2 * 1024 * 1024

/**
 * 日均销量的三种来源 → 给人看的一句话。
 *
 * 🔴 **这一列不是装饰。** 推算出来的日均建立在「两次导入之间没补过货」这个假设
 * 上，而运营看到「还能撑 3 天」时第一个该问的就是这个数可信不可信。藏起来的话，
 * 一个刚补过货的款会显示成即将断货，而没有任何线索指向原因。
 */
const SALES_SOURCE_NAMES: Record<string, string> = {
  file: '来自导出文件',
  inferred: '按库存变化推算',
  none: '算不出来',
}

const clients = ref<Client[]>([])
const clientId = ref<number>()
const file = ref<File | null>(null)
const capturedAt = ref('')
const note = ref('')

const busy = ref(false)
const loading = ref(false)
const errorText = ref('')
const result = ref<StockImportResult | null>(null)
const items = ref<StockRunway[]>([])

onMounted(async () => {
  const page = await listClients()
  // 停止合作的客户不在这一屏里出现：它们的库存多少都不重要，而混在下拉框里
  // 只会让人选错。
  clients.value = page.items.filter((item) => item.is_active)
})

/** 只有一条快照的商品数 —— 那些款**必然**算不出日均，提示要说清「再导一次」。 */
const needSecondImport = computed(
  () => items.value.filter((item) => item.snapshot_count < 2 && item.sales_source === 'none').length,
)

function pickFile(event: Event): void {
  const input = event.target as HTMLInputElement
  const picked = input.files?.[0] ?? null
  if (picked && picked.size > MAX_BYTES) {
    errorText.value = `文件 ${(picked.size / 1024 / 1024).toFixed(1)} MB，超过 2 MB 上限。`
    input.value = ''
    file.value = null
    return
  }
  errorText.value = ''
  file.value = picked
}

async function load(): Promise<void> {
  if (!clientId.value) {
    items.value = []
    return
  }
  loading.value = true
  try {
    const page = await listStockRunway(clientId.value)
    items.value = page.items
  } finally {
    loading.value = false
  }
}

async function submit(): Promise<void> {
  if (!file.value || !clientId.value || busy.value) {
    return
  }
  busy.value = true
  errorText.value = ''
  result.value = null

  const form = new FormData()
  form.set('file', file.value)
  if (capturedAt.value) {
    // datetime-local 给的是没有时区的字符串，后端会 422 —— 它必须带时区，因为
    // 推算日均按**时刻差**算。补上浏览器本地时区，那正是填这个框的人心里想的
    // 那个时刻。
    form.set('captured_at', new Date(capturedAt.value).toISOString())
  }
  if (note.value.trim()) {
    form.set('note', note.value.trim())
  }

  try {
    result.value = await importStock(clientId.value, form)
    await load()
  } catch (error) {
    // 原样显示后端说了什么：认不出列名时它会把表头列出来，那正是运营要拿去改
    // 导出设置的信息（同 ImportPage）。
    errorText.value = reason(error, '导入失败')
  } finally {
    busy.value = false
  }
}

function sourceName(source: string): string {
  return SALES_SOURCE_NAMES[source] ?? source
}
</script>

<template>
  <div class="page">
    <h2>库存</h2>
    <p class="lead">
      店铺后台导出的库存表。列名会自动认（商品编码 / 库存 / 可选的日均销量），认不出
      会把表头列出来。
      <b>文件里没有的 SKU 不会被动</b>
      —— 只导主推款是正常做法。
    </p>

    <el-form label-width="90px" class="form">
      <el-form-item label="客户">
        <el-select v-model="clientId" placeholder="选一个客户" filterable style="width: 300px" @change="load">
          <el-option v-for="item in clients" :key="item.id" :label="item.name" :value="item.id" />
        </el-select>
      </el-form-item>

      <el-form-item label="库存时刻">
        <el-input v-model="capturedAt" type="datetime-local" style="width: 240px" />
        <div class="tip">
          这份库存<b>属于哪一刻</b>，不是上传时间。留空取现在；补传前几天导的表就填当时。
        </div>
      </el-form-item>

      <el-form-item label="备注">
        <el-input v-model="note" placeholder="从哪导的、什么口径" style="width: 360px" />
      </el-form-item>

      <el-form-item label="文件">
        <input type="file" accept=".csv,text/csv" @change="pickFile" />
        <div class="tip">上限 2 MB。{{ file ? `已选：${file.name}` : '' }}</div>
      </el-form-item>

      <el-form-item>
        <el-button type="primary" :loading="busy" :disabled="!file || !clientId" @click="submit">
          导入
        </el-button>
        <span class="tip inline">重传同一个时刻是覆盖，导错了再导一次就行。</span>
      </el-form-item>
    </el-form>

    <el-alert v-if="errorText" type="error" :closable="false" show-icon class="block">
      <template #title>这份文件没导进去</template>
      <pre class="detail">{{ errorText }}</pre>
    </el-alert>

    <el-alert v-if="result" type="success" :closable="false" show-icon class="block">
      <template #title>导入完成</template>
      <div class="detail">
        新建商品 {{ result.products_created }} 个，写入快照 {{ result.snapshots }} 条。
        <template v-if="result.skipped_rows">
          跳过 {{ result.skipped_rows }} 行 —— 通常是导出文件末尾的合计行。
        </template>
        <template v-if="!result.with_sales_column">
          这份文件<b>没有日均销量那一列</b>，日均要靠两次导入之间的库存变化推算 ——
          再导一次（隔一天以上）才算得出可撑天数。
        </template>
      </div>
    </el-alert>

    <template v-if="clientId">
      <h3>还能撑几天</h3>
      <p v-if="needSecondImport" class="tip">
        有 {{ needSecondImport }} 个款只有一条快照，日均还算不出来。隔一天再导一次即可。
      </p>
      <el-table
        v-loading="loading"
        :data="items"
        empty-text="这个客户还没有库存数据"
        style="width: 100%"
      >
        <el-table-column prop="sku" label="编码" width="150" />
        <el-table-column label="商品" min-width="200">
          <!-- 名字可空：有些导出只有编码和库存两列。退回显示编码，不显示空白。 -->
          <template #default="{ row }">{{ row.name || row.sku }}</template>
        </el-table-column>
        <el-table-column label="库存" width="110">
          <template #default="{ row }">{{ formatCount(row.stock_qty) }}</template>
        </el-table-column>
        <el-table-column label="日均销量" width="110">
          <!--
            算不出来时显示「—」而不是 0：那是「不知道」，不是「一件没卖」。
            formatCount 对 null 返回的正是这个破折号（utils 里有单测钉着）。
          -->
          <template #default="{ row }">{{ formatCount(row.avg_daily_sales) }}</template>
        </el-table-column>
        <el-table-column label="日均从哪来" width="150">
          <template #default="{ row }">
            <span class="muted">{{ sourceName(row.sales_source) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="还能撑" width="130">
          <template #default="{ row }">
            <el-tag v-if="row.is_alerting" type="danger" size="small">
              {{ formatCount(row.days_left, 1) }} 天
            </el-tag>
            <span v-else>{{ formatCount(row.days_left, 1) }} 天</span>
          </template>
        </el-table-column>
        <el-table-column label="库存时刻" width="160">
          <template #default="{ row }">{{ formatInstant(row.captured_at) }}</template>
        </el-table-column>
      </el-table>
    </template>
  </div>
</template>

<style scoped>
.page {
  max-width: 1080px;
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
.tip.inline {
  margin-left: 12px;
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
.muted {
  color: var(--el-text-color-secondary);
}
</style>
