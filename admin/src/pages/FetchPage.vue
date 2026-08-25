<script setup lang="ts">
/**
 * 数据源：平台授权 + 哪些账户在自动拉、拉得怎么样。
 *
 * 这一屏回答的问题是「**看板上的数字是新的吗**」。它比「拉取」本身更重要：
 * 拉取一停，花费就变成 0，而 0 花费和「昨天没投放」在别的每一屏上都长得一模
 * 一样（docs/design/2026-08-25-ads-api-fetch.md 第三节）。
 */
import { computed, onMounted, ref } from 'vue'

import {
  attachAccountCredential,
  createAuthorizeUrl,
  deactivateCredential,
  fetchAccountData,
  getAccountFetchState,
  listAdAccounts,
  listCredentials,
  type AdAccount,
  type Credential,
  type FetchState,
} from '../api/endpoints'
import { reason } from '../api/request'
import { formatInstant } from '../utils/format'

const credentials = ref<Credential[]>([])
const accounts = ref<AdAccount[]>([])
/** 账户 ID → 上次拉取的结局。**查不到就是「从来没拉过」**，不是出错了。 */
const states = ref<Record<number, FetchState>>({})

const label = ref('')
const busy = ref(false)
const loading = ref(false)
const errorText = ref('')
const noticeText = ref('')

/** 挂账户的下拉里只给在用的凭据 —— 停用的挂上去等于挂了个不会跑的。 */
const activeCredentials = computed(() => credentials.value.filter((item) => item.is_active))

onMounted(load)

async function load(): Promise<void> {
  loading.value = true
  errorText.value = ''
  try {
    const [creds, page] = await Promise.all([listCredentials(), listAdAccounts()])
    credentials.value = creds
    accounts.value = page.items
    await loadStates()
  } catch (error) {
    errorText.value = reason(error, '加载失败')
  } finally {
    loading.value = false
  }
}

async function loadStates(): Promise<void> {
  const linked = accounts.value.filter((account) => account.credential_id !== null)
  const results = await Promise.allSettled(
    linked.map((account) => getAccountFetchState(account.id)),
  )

  const next: Record<number, FetchState> = {}
  results.forEach((result, index) => {
    // 被拒的那些是 404「还没拉过」—— 那是正常状态，不是错误，所以这里不进
    // errorText，只是让那一行显示「还没拉过」。
    if (result.status === 'fulfilled') {
      next[linked[index].id] = result.value
    }
  })
  states.value = next
}

async function authorize(): Promise<void> {
  if (!label.value.trim() || busy.value) {
    return
  }
  busy.value = true
  errorText.value = ''
  try {
    const { url } = await createAuthorizeUrl(label.value.trim())
    // 整页跳转，**不用弹窗也不用 iframe**：平台的授权页会拒绝被嵌套，而那个
    // 失败只在控制台里表现为一条 X-Frame-Options 报错，看起来和授权毫无关系。
    window.location.href = url
  } catch (error) {
    errorText.value = reason(error, '拿授权地址失败')
  } finally {
    busy.value = false
  }
}

async function stop(id: number): Promise<void> {
  busy.value = true
  errorText.value = ''
  try {
    await deactivateCredential(id)
    await load()
  } catch (error) {
    errorText.value = reason(error, '停用失败')
  } finally {
    busy.value = false
  }
}

async function attach(account: AdAccount, credentialId: number | null): Promise<void> {
  busy.value = true
  errorText.value = ''
  noticeText.value = ''
  try {
    await attachAccountCredential(account.id, credentialId)
    noticeText.value = credentialId
      ? `${account.name} 已开启自动拉取`
      : `${account.name} 已关闭自动拉取`
    await load()
  } catch (error) {
    errorText.value = reason(error, '挂载失败')
  } finally {
    busy.value = false
  }
}

async function pullNow(account: AdAccount): Promise<void> {
  busy.value = true
  errorText.value = ''
  noticeText.value = ''
  try {
    const result = await fetchAccountData(account.id)
    noticeText.value =
      `${account.name}：拉到 ${result.rows} 行（${result.since} 至 ${result.until}），` +
      `余额${result.balance_captured ? '已更新' : '未取到'}。归一化任务 ` +
      `${result.task_id ?? '没排上队，稍后手动重跑'}`
    await loadStates()
  } catch (error) {
    errorText.value = reason(error, '拉取失败')
  } finally {
    busy.value = false
  }
}

function credentialName(id: number | null): string {
  if (id === null) {
    return '手工导入'
  }
  return credentials.value.find((item) => item.id === id)?.label ?? `#${id}`
}
</script>

<template>
  <div class="page" v-loading="loading">
    <el-alert v-if="errorText" type="error" :title="errorText" show-icon :closable="false" />
    <el-alert v-if="noticeText" type="success" :title="noticeText" show-icon :closable="false" />

    <el-card shadow="never">
      <template #header>平台授权</template>
      <p class="hint">
        授权是一次性的动作：点下去会跳到平台，选好广告账户点同意，平台再把你送回来。
        token 加密存在数据库里，这一屏永远不会显示它。
      </p>
      <div class="row">
        <el-input
          v-model="label"
          placeholder="给这次授权起个名字，比如「某客户的 BC」"
          maxlength="128"
          class="grow"
        />
        <el-button type="primary" :loading="busy" @click="authorize">发起 TikTok 授权</el-button>
      </div>

      <el-table :data="credentials" empty-text="还没有任何授权" class="table">
        <el-table-column prop="label" label="名字" min-width="160" />
        <el-table-column prop="platform" label="平台" width="90" />
        <el-table-column prop="provider" label="适配器" width="120" />
        <el-table-column label="覆盖账户" width="100">
          <template #default="{ row }">{{ row.external_account_ids.length }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'" size="small">
              {{ row.is_active ? '在用' : '已停用' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="授权时间" width="170">
          <template #default="{ row }">{{ formatInstant(row.created_at) }}</template>
        </el-table-column>
        <el-table-column width="90">
          <template #default="{ row }">
            <el-button v-if="row.is_active" text size="small" :loading="busy" @click="stop(row.id)">
              停用
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never">
      <template #header>账户与拉取状态</template>
      <p class="hint">
        挂上凭据就等于打开自动拉取，取消挂载就是关掉 —— 没有第二个开关。
        「上次成功」是判断看板数字新不新的唯一依据。
      </p>

      <el-table :data="accounts" empty-text="还没有广告账户" class="table">
        <el-table-column prop="name" label="账户" min-width="160" />
        <el-table-column prop="platform" label="平台" width="90" />
        <el-table-column label="数据来源" min-width="200">
          <template #default="{ row }">
            <el-select
              :model-value="row.credential_id"
              placeholder="手工导入"
              clearable
              size="small"
              :disabled="busy"
              @change="(value: number | null) => attach(row as AdAccount, value ?? null)"
            >
              <el-option
                v-for="item in activeCredentials"
                :key="item.id"
                :label="item.label"
                :value="item.id"
              />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column label="上次成功" width="180">
          <template #default="{ row }">
            <template v-if="row.credential_id === null">—</template>
            <template v-else-if="states[row.id]?.last_success_at">
              {{ formatInstant(states[row.id].last_success_at) }}
            </template>
            <el-tag v-else type="warning" size="small">还没拉过</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="连续失败" width="110">
          <template #default="{ row }">
            <el-tag
              v-if="states[row.id] && states[row.id].consecutive_failures > 0"
              type="danger"
              size="small"
            >
              {{ states[row.id].consecutive_failures }} 次
            </el-tag>
            <template v-else>—</template>
          </template>
        </el-table-column>
        <el-table-column label="最近的错误" min-width="220">
          <template #default="{ row }">{{ states[row.id]?.last_error ?? '—' }}</template>
        </el-table-column>
        <el-table-column width="110">
          <template #default="{ row }">
            <el-button
              v-if="row.credential_id !== null"
              text
              size="small"
              :loading="busy"
              @click="pullNow(row as AdAccount)"
            >
              立即拉取
            </el-button>
            <span v-else class="muted">{{ credentialName(row.credential_id) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.row {
  display: flex;
  gap: 12px;
  align-items: center;
}
.grow {
  flex: 1;
}
.table {
  margin-top: 16px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
  margin: 0 0 12px;
}
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
</style>
