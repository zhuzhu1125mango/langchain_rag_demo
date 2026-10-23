<template>
  <div class="flex-1 overflow-auto bg-page p-4 sm:p-6">
    <div class="max-w-2xl mx-auto space-y-6">
      <div class="flex items-start justify-between">
        <div>
          <h1 class="text-xl font-semibold text-content">API 密钥</h1>
          <p class="text-sm text-content-muted mt-1">
            以你的身份访问 API（MCP 客户端、脚本、CI 集成）。
          </p>
        </div>
        <el-button
          v-if="enabled"
          type="primary"
          :disabled="maxReached"
          :data-testid="'api-key-new-btn'"
          @click="dialogVisible = true"
        >
          <Plus class="w-4 h-4 mr-1" />
          新建密钥
        </el-button>
      </div>

      <div
        v-if="enabled"
        class="p-3 rounded-lg bg-blue-50 dark:bg-blue-900/20 border-l-4 border-blue-400 dark:border-blue-600"
      >
        <p class="text-sm text-blue-700 dark:text-blue-400">
          密钥明文仅在创建时显示一次；如泄露请立即吊销，吊销后下一请求即失效。
        </p>
      </div>

      <!-- 功能未启用空态（USER_API_KEYS_ENABLED=false） -->
      <AppCard
        v-if="!enabled"
        padding="none"
        class="p-10 text-center"
        data-testid="api-keys-disabled"
      >
        <KeyRound class="w-10 h-10 mx-auto text-content-disabled" />
        <p class="mt-3 text-sm text-content-muted">
          该功能未启用，请检查后端配置 USER_API_KEYS_ENABLED
        </p>
      </AppCard>

      <!-- 列表 -->
      <AppCard
        v-else
        padding="none"
        data-testid="api-keys-table"
      >
        <el-skeleton v-if="isLoading" class="p-6" :rows="3" animated />
        <template v-else>
          <div
            v-if="!keys.length"
            class="p-10 text-center"
            data-testid="api-keys-empty"
          >
            <KeyRound class="w-10 h-10 mx-auto text-content-disabled" />
            <p class="mt-3 text-sm text-content-muted">还没有 API 密钥</p>
            <el-button type="primary" plain class="mt-4" @click="dialogVisible = true">创建第一把密钥</el-button>
          </div>
          <el-table v-else :data="keys" style="width: 100%">
            <el-table-column prop="name" label="名称" min-width="130" show-overflow-tooltip />
            <el-table-column label="密钥" min-width="150">
              <template #default="{ row }">
                <code class="font-mono text-xs" :class="rowMeta(row).dimmed ? 'text-content-subtle' : ''">
                  {{ row.key_prefix }}…
                </code>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="rowMeta(row).tagType" size="small">{{ rowMeta(row).label }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最近使用" width="110">
              <template #default="{ row }">
                <span class="text-xs text-content-muted">{{ formatRelativeTime(row.last_used_at) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90" align="right">
              <template #default="{ row }">
                <el-button
                  v-if="rowMeta(row).status === 'active'"
                  type="danger"
                  plain
                  size="small"
                  :data-testid="'api-key-revoke-btn'"
                  @click="handleRevoke(row)"
                >
                  吊销
                </el-button>
                <span v-else class="text-content-disabled">—</span>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </AppCard>
    </div>

    <CreateApiKeyDialog
      v-model:visible="dialogVisible"
      :active-count="activeCount"
      :max-per-user="maxPerUser"
    />
  </div>
</template>

<script setup lang="ts">
/**
 * 设置页 - API 密钥管理（E.6）。
 *
 * 列表（名称/前缀/三态/最近使用/吊销）+ 创建入口；功能未启用与空列表
 * 各有专属空态。状态推导与相对时间展示来自 queries/apiKeys.ts。
 */
import { computed, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { KeyRound, Plus } from '@lucide/vue'
import CreateApiKeyDialog from '@/components/settings/CreateApiKeyDialog.vue'
import { AppCard } from '@/components/ui'
import {
  deriveKeyStatus,
  formatRelativeTime,
  useApiKeys,
  useApiKeysConfigInfo,
  useRevokeApiKey,
  type ApiKeyStatus,
  type UserApiKey
} from '@/queries/apiKeys'
import { handleMutationError, useToast } from '@/composables/useToast'

const dialogVisible = ref(false)
const toast = useToast()

const configQuery = useApiKeysConfigInfo()
const enabled = computed(() => configQuery.data.value?.user_api_keys_enabled === true)
const maxPerUser = computed(() => configQuery.data.value?.user_api_keys_max_per_user ?? 5)

const keysQuery = useApiKeys()
const keys = computed<UserApiKey[]>(() => keysQuery.data.value ?? [])
const isLoading = computed(() => keysQuery.isLoading.value)

const activeCount = computed(() => keys.value.filter((k) => deriveKeyStatus(k) === 'active').length)
const maxReached = computed(() => activeCount.value >= maxPerUser.value)

const revokeMutation = useRevokeApiKey()

/** 行元数据：状态推导 + badge 类型 + 是否置灰。 */
function rowMeta(row: UserApiKey): { status: ApiKeyStatus; label: string; tagType: 'success' | 'warning' | 'info'; dimmed: boolean } {
  const status = deriveKeyStatus(row)
  if (status === 'revoked') return { status, label: '已吊销', tagType: 'info', dimmed: true }
  if (status === 'expired') return { status, label: '已过期', tagType: 'warning', dimmed: true }
  return { status, label: '活跃', tagType: 'success', dimmed: false }
}

async function handleRevoke(row: UserApiKey): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认吊销 ${row.key_prefix}…（${row.name}）？使用该密钥的客户端将立即失去访问权限。`,
      '吊销 API 密钥',
      { confirmButtonText: '吊销', cancelButtonText: '取消', type: 'warning', confirmButtonClass: 'el-button--danger' }
    )
  } catch {
    return // 用户取消
  }
  try {
    await revokeMutation.mutateAsync(row.id)
    toast.success('已吊销', '该密钥的下一请求即失效')
  } catch (error) {
    handleMutationError(error, '吊销失败', '吊销失败，请稍后重试')
  }
}
</script>
