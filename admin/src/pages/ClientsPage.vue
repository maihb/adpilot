<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { createClient, listClients, type Client } from '../api/endpoints'
import { reason } from '../api/request'
import { formatInstant } from '../utils/format'

const items = ref<Client[]>([])
const loading = ref(false)

const creating = ref(false)
const name = ref('')
const note = ref('')
const busy = ref(false)
const error = ref('')

onMounted(load)

async function load(): Promise<void> {
  loading.value = true
  try {
    const page = await listClients()
    items.value = page.items
  } finally {
    loading.value = false
  }
}

async function submit(): Promise<void> {
  if (!name.value.trim() || busy.value) {
    return
  }
  busy.value = true
  error.value = ''
  try {
    await createClient(name.value.trim(), note.value.trim())
    // 建客户是**可逆**操作（改错了改回来就行），所以不弹确认框。
    creating.value = false
    name.value = ''
    note.value = ''
    await load()
  } catch (err) {
    error.value = reason(err, '建客户失败')
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="page">
    <div class="head">
      <h2>客户</h2>
      <el-button type="primary" @click="creating = true">新建客户</el-button>
    </div>

    <el-table v-loading="loading" :data="items" empty-text="还没有客户" style="width: 100%">
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="name" label="名称" min-width="200" />
      <el-table-column prop="note" label="内部备注" min-width="240">
        <template #default="{ row }">
          <!-- 内部备注：客户端那套出参里没有它，只在这边看得到。 -->
          <span class="muted">{{ row.note || '—' }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
            {{ row.is_active ? '合作中' : '已停用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="建于" width="150">
        <template #default="{ row }">{{ formatInstant(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="" width="90">
        <template #default="{ row }">
          <router-link :to="`/clients/${row.id}`">详情</router-link>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="creating" title="新建客户" width="440px">
      <el-form label-width="80px" @submit.prevent="submit">
        <el-form-item label="名称">
          <el-input v-model="name" placeholder="客户名" />
        </el-form-item>
        <el-form-item label="内部备注">
          <el-input v-model="note" type="textarea" :rows="2" placeholder="只有运营看得到" />
        </el-form-item>
      </el-form>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <template #footer>
        <el-button @click="creating = false">取消</el-button>
        <el-button type="primary" :loading="busy" :disabled="!name.trim()" @click="submit">
          建立
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
.muted {
  color: var(--el-text-color-secondary);
}
</style>
