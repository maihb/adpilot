<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

import {
  createInvite,
  getClient,
  listAdAccounts,
  listInvites,
  revokeInvite,
  updateClient,
  type AdAccount,
  type Client,
  type Invite,
  type InviteCreated,
} from '../api/endpoints'
import { NeedsRedo } from '../api/request'
import { formatInstant, toNumber } from '../utils/format'

const route = useRoute()
// 页面里不许出现裸的数字转换（tests/test_frontend_source.py 盯着），走 utils。
const clientId = toNumber(String(route.params.id)) ?? 0

const client = ref<Client | null>(null)
const accounts = ref<AdAccount[]>([])
const invites = ref<Invite[]>([])
const loading = ref(false)

const ttlDays = ref(30)
/** 🔴 刚发出来的码。**明文只在这里出现这一次**，关掉就再也拿不到。 */
const fresh = ref<InviteCreated | null>(null)
const copied = ref(false)
const busy = ref(false)

onMounted(load)

async function load(): Promise<void> {
  loading.value = true
  try {
    const [detail, accountPage, invitePage] = await Promise.all([
      getClient(clientId),
      listAdAccounts(clientId),
      listInvites(clientId),
    ])
    client.value = detail
    accounts.value = accountPage.items
    invites.value = invitePage.items
  } finally {
    loading.value = false
  }
}

async function issue(): Promise<void> {
  busy.value = true
  try {
    fresh.value = await createInvite(clientId, ttlDays.value)
    copied.value = false
    await load()
  } catch (error) {
    ElMessage.error(error instanceof NeedsRedo ? error.message : '发码失败')
  } finally {
    busy.value = false
  }
}

async function copyCode(): Promise<void> {
  if (!fresh.value) {
    return
  }
  try {
    await navigator.clipboard.writeText(fresh.value.code)
    copied.value = true
  } catch {
    // 非 HTTPS 下 clipboard API 不可用（后台常常就跑在 http 局域网里）。
    // 不报错——码就明晃晃地显示在上面，手选复制即可。
    ElMessage.warning('这个浏览器不让自动复制，请手动选中上面那串码')
  }
}

/**
 * 作废邀请码：**不可逆**，所以二次确认。
 *
 * 确认文案要说清楚它管不到什么：已经换出去的 token 不受影响（自签、自包含，
 * 撤销不了），最多再活 7 天。
 */
async function revoke(invite: Invite): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '作废之后这个码再也换不到票，且不可撤销。注意：已经用它换出去的 token 不受影响，最多再活 7 天 —— 要立刻断掉那个客户的访问，得停用客户。',
      '作废这个邀请码？',
      { type: 'warning', confirmButtonText: '作废', cancelButtonText: '算了' },
    )
  } catch {
    return
  }
  try {
    await revokeInvite(clientId, invite.id)
    await load()
  } catch (error) {
    ElMessage.error(error instanceof NeedsRedo ? error.message : '作废失败')
  }
}

/**
 * 停用 / 恢复客户。
 *
 * 🔴 **这是全系统唯一一个「点下去立刻影响外部人」的操作。**
 * `require_client_scope` 每次请求都查一遍 `is_active`，所以那个客户手上还没过期
 * 的票**当场失效** —— 确认文案里必须写出这句话，不能只写「确定停用吗」。
 */
async function toggleActive(): Promise<void> {
  if (!client.value) {
    return
  }
  const disabling = client.value.is_active
  if (disabling) {
    try {
      await ElMessageBox.confirm(
        '这个客户会立刻看不到任何数据 —— 他手机上那张还没过期的票当场失效，不用等 7 天。数据本身不会被删，随时可以恢复合作。',
        `停用「${client.value.name}」？`,
        { type: 'warning', confirmButtonText: '停用', cancelButtonText: '算了' },
      )
    } catch {
      return
    }
  }
  try {
    client.value = await updateClient(clientId, { is_active: !disabling })
    ElMessage.success(disabling ? '已停用，该客户的访问已经断掉' : '已恢复合作')
  } catch (error) {
    ElMessage.error(error instanceof NeedsRedo ? error.message : '改状态失败')
  }
}

function inviteState(invite: Invite): { label: string; type: 'success' | 'info' | 'danger' } {
  if (invite.revoked_at) {
    return { label: '已作废', type: 'danger' }
  }
  if (Date.parse(invite.expires_at) < Date.now()) {
    return { label: '已过期', type: 'info' }
  }
  return { label: '可用', type: 'success' }
}

const title = computed(() => client.value?.name ?? '客户')
</script>

<template>
  <div v-loading="loading" class="page">
    <div class="head">
      <h2>{{ title }}</h2>
      <el-button v-if="client" :type="client.is_active ? 'danger' : 'success'" @click="toggleActive">
        {{ client.is_active ? '停用客户' : '恢复合作' }}
      </el-button>
    </div>

    <el-descriptions v-if="client" :column="3" border class="block">
      <el-descriptions-item label="ID">{{ client.id }}</el-descriptions-item>
      <el-descriptions-item label="状态">
        <el-tag :type="client.is_active ? 'success' : 'info'" size="small">
          {{ client.is_active ? '合作中' : '已停用' }}
        </el-tag>
      </el-descriptions-item>
      <el-descriptions-item label="建于">{{ formatInstant(client.created_at) }}</el-descriptions-item>
      <el-descriptions-item label="内部备注" :span="3">
        {{ client.note || '—' }}
      </el-descriptions-item>
    </el-descriptions>

    <h3>广告账户</h3>
    <el-table :data="accounts" empty-text="这个客户还没有广告账户" style="width: 100%">
      <el-table-column prop="name" label="名称" min-width="220" />
      <el-table-column prop="platform" label="平台" width="90" />
      <el-table-column prop="external_id" label="平台侧 ID" width="180" />
      <el-table-column prop="currency" label="币种" width="80" />
      <el-table-column prop="timezone" label="时区" width="180" />
      <el-table-column label="" width="90">
        <template #default="{ row }">
          <router-link :to="`/accounts/${row.id}`">明细</router-link>
        </template>
      </el-table-column>
    </el-table>

    <h3>邀请码</h3>
    <div class="issue">
      <span>有效期</span>
      <el-input-number v-model="ttlDays" :min="1" :max="365" size="small" />
      <span>天</span>
      <el-button type="primary" size="small" :loading="busy" @click="issue">发一个新码</el-button>
      <span class="muted">有效期内可以反复使用，一个客户往往有两个人要看。</span>
    </div>

    <el-table :data="invites" empty-text="还没发过码" style="width: 100%">
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="inviteState(row as Invite).type" size="small">{{ inviteState(row as Invite).label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="发于" width="160">
        <template #default="{ row }">{{ formatInstant(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="过期" width="160">
        <template #default="{ row }">{{ formatInstant(row.expires_at) }}</template>
      </el-table-column>
      <el-table-column prop="use_count" label="用过" width="80" />
      <el-table-column label="最近一次" width="160">
        <template #default="{ row }">{{ formatInstant(row.last_used_at) }}</template>
      </el-table-column>
      <el-table-column label="" width="90">
        <template #default="{ row }">
          <el-button
            v-if="!row.revoked_at"
            text
            type="danger"
            size="small"
            @click="revoke(row as Invite)"
          >
            作废
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <p class="muted small">
      列表里看不到码本身 —— 库里存的是哈希，还原不回来。这不是页面坏了：码只在生成
      那一刻显示一次。
    </p>

    <!-- 🔴 明文码唯一一次出现的地方。不是 toast，不会自己消失。 -->
    <el-dialog
      :model-value="fresh !== null"
      title="这个码只显示这一次"
      width="520px"
      :close-on-click-modal="false"
      @close="fresh = null"
    >
      <p class="warn">
        关掉这个框之后就再也看不到它了 —— 库里存的是哈希。现在就把它发给客户，或者
        先复制下来。
      </p>
      <el-input :model-value="fresh?.code" readonly class="code" />
      <p class="muted small">
        有效期到 {{ formatInstant(fresh?.expires_at) }}。客户扫码或粘贴进客户端即可，
        期间可以反复使用。
      </p>
      <template #footer>
        <el-button type="primary" @click="copyCode">
          {{ copied ? '已复制' : '复制' }}
        </el-button>
        <el-button @click="fresh = null">我已经发出去了</el-button>
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
.block {
  margin-bottom: 8px;
}
.issue {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.muted {
  color: var(--el-text-color-secondary);
}
.small {
  font-size: 12px;
  line-height: 1.7;
}
.warn {
  color: var(--el-color-danger);
  line-height: 1.7;
  margin-top: 0;
}
.code :deep(input) {
  font-family: ui-monospace, monospace;
  font-size: 15px;
}
</style>
