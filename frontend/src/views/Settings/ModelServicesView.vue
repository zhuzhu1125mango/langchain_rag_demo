<template>
  <div class="flex-1 overflow-auto bg-page p-4 sm:p-6">
    <div class="max-w-5xl mx-auto space-y-6">
      <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <h1 class="text-xl font-semibold text-content">模型服务</h1>
          <p class="text-sm text-content-muted mt-1">配置本地 Ollama 与外部 OpenAI-Compatible Provider，并按任务路由。</p>
        </div>
        <div class="flex items-center gap-2">
          <AppButton variant="secondary" size="sm" :loading="isLoading" @click="refetch()">
            <RefreshCw class="w-4 h-4" />
            刷新
          </AppButton>
          <AppButton variant="primary" size="sm" :disabled="!snapshot" @click="applyRegistry">
            <CheckCircle class="w-4 h-4" />
            校验并应用
          </AppButton>
        </div>
      </div>

      <AppCard v-if="snapshotError || (!snapshot && !getAdminKey())" padding="lg" class="border-amber-200 dark:border-amber-800">
        <div class="flex items-start gap-3">
          <KeyRound class="w-5 h-5 text-amber-500 shrink-0 mt-0.5" />
          <div class="flex-1 min-w-0">
            <p class="font-medium text-content">需要管理员密钥</p>
            <p class="text-sm text-content-muted mt-1">
              生产环境模型配置需要 <code class="text-xs">X-Admin-Key</code>；开发环境未配置 ADMIN_KEY 时可直接使用。
            </p>
            <div class="flex flex-col sm:flex-row gap-2 mt-3">
              <AppInput v-model="adminKeyInput" type="password" placeholder="输入 ADMIN_KEY" size="sm" class="flex-1" />
              <AppButton variant="primary" size="sm" @click="saveAdminKey">保存密钥</AppButton>
            </div>
            <p v-if="snapshotError" class="text-xs text-red-500 mt-2">{{ snapshotError }}</p>
          </div>
        </div>
      </AppCard>

      <AppStateView v-if="isLoading" state="loading" padding="lg" />
      <AppStateView
        v-else-if="snapshotError && !snapshot"
        state="error"
        title="加载模型配置失败"
        :description="snapshotError"
        padding="lg"
        @retry="refetch()"
      />
      <template v-else-if="snapshot">
        <AppCard padding="lg">
          <div class="flex items-center justify-between mb-4">
            <div>
              <h2 class="font-medium text-content">Provider</h2>
              <p class="text-sm text-content-muted mt-1">本地 Ollama 或 OpenAI-Compatible 服务连接。</p>
            </div>
            <AppButton variant="primary" size="sm" @click="openProvider()">
              <Plus class="w-4 h-4" />
              新增 Provider
            </AppButton>
          </div>
          <div v-if="snapshot.providers.length" class="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <div v-for="p in snapshot.providers" :key="p.id" class="rounded-xl border border-line bg-surface-subtle p-4">
              <div class="flex items-start justify-between gap-3">
                <div class="min-w-0">
                  <div class="flex flex-wrap items-center gap-2">
                    <span class="font-medium text-content">{{ p.name }}</span>
                    <AppBadge :variant="p.enabled ? 'success' : 'neutral'">{{ p.enabled ? '启用' : '停用' }}</AppBadge>
                    <AppBadge :variant="p.external ? 'warning' : 'info'">{{ p.external ? '外部' : '本地' }}</AppBadge>
                  </div>
                  <p class="text-xs text-content-muted mt-1 break-all">{{ p.provider_type }} · {{ p.base_url || '默认地址' }}</p>
                  <p class="text-xs text-content-subtle mt-1">
                    Key：{{ p.has_api_key ? (p.api_key_hint || '已保存') : (p.api_key_env ? `环境变量 ${p.api_key_env}` : '未配置') }}
                  </p>
                </div>
                <div class="flex items-center gap-1 shrink-0">
                  <AppButton variant="ghost" size="icon" title="测试连接" @click="testSaved(p.id)">
                    <PlugZap class="w-4 h-4" />
                  </AppButton>
                  <AppButton variant="ghost" size="icon" title="编辑" @click="openProvider(p)">
                    <Pencil class="w-4 h-4" />
                  </AppButton>
                  <AppButton variant="danger-ghost" size="icon" title="删除" @click="removeProvider(p)">
                    <Trash2 class="w-4 h-4" />
                  </AppButton>
                </div>
              </div>
            </div>
          </div>
          <AppStateView v-else state="empty" title="暂无 Provider" description="新增 Ollama 或 OpenAI-Compatible 服务后开始配置模型" padding="sm" />
        </AppCard>

        <AppCard padding="lg">
          <div class="flex items-center justify-between mb-4">
            <div>
              <h2 class="font-medium text-content">模型</h2>
              <p class="text-sm text-content-muted mt-1">声明能力后，路由只会分配到满足要求的模型。</p>
            </div>
            <AppButton variant="primary" size="sm" :disabled="!snapshot.providers.length" @click="openModel()">
              <Plus class="w-4 h-4" />
              新增模型
            </AppButton>
          </div>
          <div v-if="snapshot.models.length" class="space-y-2">
            <div v-for="m in snapshot.models" :key="m.id" class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 rounded-xl border border-line bg-surface-subtle p-3">
              <div class="min-w-0">
                <div class="flex flex-wrap items-center gap-2">
                  <span class="font-medium text-content">{{ m.display_name || m.model_name }}</span>
                  <span class="text-xs text-content-subtle">{{ providerName(m.provider_id) }}</span>
                  <AppBadge :variant="m.enabled ? 'success' : 'neutral'">{{ m.enabled ? '启用' : '停用' }}</AppBadge>
                </div>
                <div class="flex flex-wrap gap-1 mt-1">
                  <AppBadge v-for="cap in m.capabilities" :key="cap" variant="info">{{ cap }}</AppBadge>
                  <span class="text-xs text-content-subtle">ctx {{ m.context_window || '-' }}</span>
                  <span class="text-xs text-content-subtle">{{ priceSummary(m) }}</span>
                </div>
              </div>
              <div class="flex items-center gap-1 shrink-0">
                <AppButton variant="ghost" size="icon" title="编辑" @click="openModel(m)">
                  <Pencil class="w-4 h-4" />
                </AppButton>
                <AppButton variant="danger-ghost" size="icon" title="删除" @click="removeModel(m)">
                  <Trash2 class="w-4 h-4" />
                </AppButton>
              </div>
            </div>
          </div>
          <AppStateView v-else state="empty" title="暂无模型" description="先新增 Provider，再添加模型" padding="sm" />
        </AppCard>

        <AppCard padding="lg">
          <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
            <div>
              <h2 class="font-medium text-content">向量索引 / Embedding</h2>
              <p class="text-sm text-content-muted mt-1">
                切换 Embedding 模型或维度后必须先重建索引；重建完成才会应用新配置，旧索引保留用于回滚。
              </p>
            </div>
            <div class="flex items-center gap-2">
              <AppButton variant="secondary" size="sm" :loading="indexLoading" @click="refetchIndex()">
                <RefreshCw class="w-4 h-4" />
                刷新
              </AppButton>
              <AppButton
                variant="secondary"
                size="sm"
                :disabled="!indexStatus?.can_rollback"
                :loading="rollbackIndexMutation.isPending.value"
                @click="rollbackEmbeddingIndex"
              >
                回滚
              </AppButton>
              <AppButton
                variant="primary"
                size="sm"
                :loading="rebuildIndexMutation.isPending.value || indexStatus?.status === 'rebuilding'"
                @click="rebuildEmbeddingIndex"
              >
                重建索引
              </AppButton>
            </div>
          </div>
          <AppStateView v-if="indexLoading && !indexStatus" state="loading" padding="sm" />
          <AppStateView
            v-else-if="indexError && !indexStatus"
            state="error"
            title="加载索引状态失败"
            :description="indexError"
            padding="sm"
            @retry="refetchIndex()"
          />
          <template v-else-if="indexStatus">
            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
              <div class="rounded-xl border border-line bg-surface-subtle p-4">
                <p class="text-xs text-content-muted">当前索引</p>
                <p class="font-medium text-content mt-1">
                  {{ indexStatus.indexed?.display_name || indexStatus.indexed?.model_name || '未知模型' }}
                </p>
                <p class="text-xs text-content-subtle mt-1">
                  维度 {{ indexStatus.indexed?.dimension || '-' }} · {{ indexStatus.chunk_count }} 切片
                </p>
              </div>
              <div class="rounded-xl border border-line bg-surface-subtle p-4">
                <p class="text-xs text-content-muted">待应用配置</p>
                <p class="font-medium text-content mt-1">
                  {{ indexStatus.target.display_name || indexStatus.target.model_name }}
                </p>
                <p class="text-xs text-content-subtle mt-1">维度 {{ indexStatus.target.dimension }}</p>
              </div>
            </div>
            <div class="flex flex-wrap items-center gap-2 mt-3">
              <AppBadge :variant="indexStatus.in_sync ? 'success' : 'warning'">
                {{ indexStatus.in_sync ? '已同步' : '需重建' }}
              </AppBadge>
              <AppBadge v-if="indexStatus.status === 'rebuilding'" variant="warning">重建中</AppBadge>
              <AppBadge v-else-if="indexStatus.status === 'failed'" variant="danger">最近一次重建失败</AppBadge>
              <AppBadge v-else-if="indexStatus.status === 'rolled_back'" variant="info">已回滚</AppBadge>
              <span v-if="indexStatus.can_rollback" class="text-xs text-content-subtle">存在可回滚备份</span>
            </div>
            <p v-if="indexStatus.last_error" class="text-xs text-red-500 mt-2 break-all">{{ indexStatus.last_error }}</p>
            <p v-if="indexStatus.needs_rebuild" class="text-xs text-amber-600 dark:text-amber-400 mt-2">
              保存 Embedding 路由后点击「重建索引」；重建会重新向量化全部切片并原子切换。
            </p>
          </template>
        </AppCard>

        <AppCard padding="lg">
          <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
            <div>
              <h2 class="font-medium text-content">用量与成本</h2>
              <p class="text-sm text-content-muted mt-1">
                最近 {{ usageDays }} 天的持久化聚合 + 当前进程增量；成本按模型配置的每 1K tokens 单价估算。
              </p>
            </div>
            <div class="flex items-center gap-2">
              <AppSelect
                :model-value="usageDays"
                size="sm"
                class="w-32"
                @update:model-value="usageDays = Number($event)"
              >
                <option :value="7">最近 7 天</option>
                <option :value="30">最近 30 天</option>
                <option :value="90">最近 90 天</option>
              </AppSelect>
              <AppButton variant="secondary" size="sm" :loading="usageLoading" @click="refetchUsage()">
                <RefreshCw class="w-4 h-4" />
                刷新
              </AppButton>
            </div>
          </div>
          <p v-if="!hasPricingConfigured" class="text-xs text-amber-600 dark:text-amber-400 mb-3">
            当前没有模型配置单价，调用会计入「未定价」。可在模型编辑中填写每 1K tokens 的输入/输出价格。
          </p>
          <AppStateView v-if="usageLoading" state="loading" padding="sm" />
          <AppStateView
            v-else-if="usageError && !usageRows.length"
            state="error"
            title="加载用量失败"
            :description="usageError"
            padding="sm"
            @retry="refetchUsage()"
          />
          <div v-else-if="usageRows.length" class="overflow-x-auto">
            <table class="w-full">
              <thead>
                <tr class="border-b border-line">
                  <th class="text-left py-3 px-4 text-sm font-medium text-content-muted">模型</th>
                  <th class="text-left py-3 px-4 text-sm font-medium text-content-muted">任务</th>
                  <th class="text-left py-3 px-4 text-sm font-medium text-content-muted">调用 / 失败</th>
                  <th class="text-left py-3 px-4 text-sm font-medium text-content-muted">Tokens（入 / 出）</th>
                  <th class="text-left py-3 px-4 text-sm font-medium text-content-muted">平均延迟</th>
                  <th class="text-left py-3 px-4 text-sm font-medium text-content-muted">估算成本</th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="row in usageRows"
                  :key="usageKey(row)"
                  class="border-b border-line-subtle hover:bg-gray-50 dark:hover:bg-dark-700/50"
                >
                  <td class="py-3 px-4">
                    <div class="text-sm text-content">{{ modelLabel(row) }}</div>
                    <div class="text-xs text-content-subtle">{{ providerName(row.provider_id) }}</div>
                  </td>
                  <td class="py-3 px-4">
                    <code class="text-xs text-content-muted">{{ row.task }}</code>
                  </td>
                  <td class="py-3 px-4 text-sm text-content whitespace-nowrap">
                    {{ row.calls }}
                    <span v-if="row.errors" class="text-red-500"> / 失败 {{ row.errors }}</span>
                  </td>
                  <td class="py-3 px-4 text-sm text-content-muted whitespace-nowrap">
                    {{ row.input_tokens }} / {{ row.output_tokens }}
                  </td>
                  <td class="py-3 px-4 text-sm text-content-muted whitespace-nowrap">{{ formatLatency(row.avg_latency_ms) }}</td>
                  <td class="py-3 px-4 text-sm text-content whitespace-nowrap">
                    {{ formatCost(row) }}
                    <span v-if="row.unpriced_calls" class="block text-xs text-amber-600 dark:text-amber-400">
                      {{ row.unpriced_calls }} 次未定价
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <AppStateView v-else state="empty" title="暂无用量" description="产生模型调用后，这里会显示调用数、Token、延迟与成本" padding="sm" />
        </AppCard>

        <AppCard padding="lg">
          <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-4">
            <div>
              <h2 class="font-medium text-content">任务路由</h2>
              <p class="text-sm text-content-muted mt-1">answer / fast / embedding 为必需路由；保存后点击“校验并应用”生效。</p>
            </div>
            <AppButton variant="secondary" size="sm" :disabled="!snapshot.models.length" @click="saveRoutes">
              <Save class="w-4 h-4" />
              保存路由
            </AppButton>
          </div>
          <div v-if="snapshot.models.length" class="space-y-3">
            <div v-for="draft in routeDrafts" :key="draft.task" class="rounded-xl border border-line bg-surface-subtle p-3">
              <div class="flex flex-wrap items-center gap-2 mb-2">
                <code class="text-xs font-semibold text-content">{{ draft.task }}</code>
                <AppBadge v-if="requiredTasks.includes(draft.task)" variant="warning">必需</AppBadge>
              </div>
              <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
                <label class="block">
                  <span class="block text-xs text-content-muted mb-1">主模型</span>
                  <AppSelect v-model="draft.primary_model_id" size="sm">
                    <option value="">选择主模型</option>
                    <option v-for="m in snapshot.models" :key="m.id" :value="m.id">{{ m.display_name || m.model_name }}</option>
                  </AppSelect>
                </label>
                <label class="block">
                  <span class="block text-xs text-content-muted mb-1">备用模型（可多选）</span>
                  <select
                    v-model="draft.fallback_model_ids"
                    multiple
                    class="w-full px-3 py-1.5 text-sm border border-line rounded-control bg-surface-subtle text-content focus:outline-none focus:ring-2 focus:ring-primary-500"
                  >
                    <option v-for="m in snapshot.models" :key="m.id" :value="m.id">{{ m.display_name || m.model_name }}</option>
                  </select>
                </label>
              </div>
              <label v-if="draft.primary_model_id" class="inline-flex items-center gap-2 mt-2 text-xs text-content-muted">
                <input v-model="draft.allow_external_fallback" type="checkbox" class="rounded border-line-strong text-primary-600" />
                允许本地主模型失败后降级到外部 Provider
              </label>
            </div>
          </div>
          <AppStateView v-else state="empty" title="暂无可配置模型" description="先添加模型，再配置任务路由" padding="sm" />
        </AppCard>
      </template>
    </div>

    <AppDialog v-model="providerDialog" :title="providerEditingId ? '编辑 Provider' : '新增 Provider'" width="560px">
      <div class="space-y-3">
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block text-sm text-content-secondary mb-1">名称 *</label>
            <AppInput v-model="providerForm.name" placeholder="如 openai / local-ollama" />
          </div>
          <div>
            <label class="block text-sm text-content-secondary mb-1">类型</label>
            <AppSelect v-model="providerForm.provider_type">
              <option value="ollama">Ollama</option>
              <option value="openai_compatible">OpenAI-Compatible</option>
            </AppSelect>
          </div>
        </div>
        <div>
          <label class="block text-sm text-content-secondary mb-1">Base URL</label>
          <AppInput v-model="providerForm.base_url" placeholder="http://host.docker.internal:11434 或 https://api.example.com/v1" />
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block text-sm text-content-secondary mb-1">API Key（只写）</label>
            <AppInput v-model="providerForm.api_key" type="password" placeholder="留空表示不修改" />
          </div>
          <div>
            <label class="block text-sm text-content-secondary mb-1">API Key 环境变量</label>
            <AppInput v-model="providerForm.api_key_env" placeholder="如 OPENAI_API_KEY" />
          </div>
        </div>
        <div class="flex flex-wrap gap-4 text-sm text-content-secondary">
          <label class="inline-flex items-center gap-2">
            <input v-model="providerForm.external" type="checkbox" class="rounded border-line-strong text-primary-600" />
            外部服务
          </label>
          <label class="inline-flex items-center gap-2">
            <input v-model="providerForm.api_key_required" type="checkbox" class="rounded border-line-strong text-primary-600" />
            必须配置 Key
          </label>
          <label class="inline-flex items-center gap-2">
            <input v-model="providerForm.enabled" type="checkbox" class="rounded border-line-strong text-primary-600" />
            启用
          </label>
        </div>
        <p v-if="testResult" :class="testResult.ok ? 'text-xs text-green-600' : 'text-xs text-red-500'">
          {{ testResult.ok ? `连接成功，发现 ${testResult.models.length} 个模型` : `连接失败：${testResult.error || '未知错误'}` }}
        </p>
      </div>
      <template #footer>
        <AppButton variant="ghost" size="sm" :loading="testProviderMutation.isPending.value" @click="testDraft">
          测试连接
        </AppButton>
        <AppButton variant="secondary" size="sm" @click="providerDialog = false">取消</AppButton>
        <AppButton variant="primary" size="sm" :loading="saveProviderPending" @click="saveProvider">保存</AppButton>
      </template>
    </AppDialog>

    <AppDialog v-model="modelDialog" :title="modelEditingId ? '编辑模型' : '新增模型'" width="560px">
      <div class="space-y-3">
        <div>
          <label class="block text-sm text-content-secondary mb-1">Provider *</label>
          <AppSelect v-model="modelForm.provider_id">
            <option value="">选择 Provider</option>
            <option v-for="p in snapshot?.providers || []" :key="p.id" :value="p.id">{{ p.name }}</option>
          </AppSelect>
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label class="block text-sm text-content-secondary mb-1">模型名 *</label>
            <AppInput v-model="modelForm.model_name" placeholder="如 gpt-4.1-mini" />
          </div>
          <div>
            <label class="block text-sm text-content-secondary mb-1">显示名</label>
            <AppInput v-model="modelForm.display_name" placeholder="如 外部推理模型" />
          </div>
        </div>
        <div>
          <label class="block text-sm text-content-secondary mb-2">能力</label>
          <div class="flex flex-wrap gap-3 text-sm text-content-secondary">
            <label v-for="cap in capabilityOptions" :key="cap" class="inline-flex items-center gap-2">
              <input v-model="modelForm.capabilities" type="checkbox" :value="cap" class="rounded border-line-strong text-primary-600" />
              {{ cap }}
            </label>
          </div>
        </div>
        <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <div>
            <label class="block text-sm text-content-secondary mb-1">上下文</label>
            <AppInput v-model.number="modelForm.context_window" type="number" />
          </div>
          <div>
            <label class="block text-sm text-content-secondary mb-1">最大输出</label>
            <AppInput v-model.number="modelForm.max_output_tokens" type="number" />
          </div>
          <div>
            <label class="block text-sm text-content-secondary mb-1">Embedding 维度</label>
            <AppInput v-model.number="modelForm.embedding_dimension" type="number" />
          </div>
        </div>
        <div>
          <label class="block text-sm text-content-secondary mb-2">成本单价（每 1K tokens，留空表示未配置）</label>
          <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <div>
              <label class="block text-xs text-content-muted mb-1">输入</label>
              <AppInput
                :model-value="modelForm.input_cost_per_1k ?? ''"
                type="number"
                min="0"
                step="0.000001"
                placeholder="如 0.001"
                @update:model-value="value => (modelForm.input_cost_per_1k = value === '' ? null : Number(value))"
              />
            </div>
            <div>
              <label class="block text-xs text-content-muted mb-1">输出</label>
              <AppInput
                :model-value="modelForm.output_cost_per_1k ?? ''"
                type="number"
                min="0"
                step="0.000001"
                placeholder="如 0.002"
                @update:model-value="value => (modelForm.output_cost_per_1k = value === '' ? null : Number(value))"
              />
            </div>
            <div>
              <label class="block text-xs text-content-muted mb-1">币种</label>
              <AppSelect v-model="modelForm.price_currency">
                <option value="USD">USD</option>
                <option value="CNY">CNY</option>
                <option value="EUR">EUR</option>
              </AppSelect>
            </div>
          </div>
        </div>
      </div>
      <template #footer>
        <AppButton variant="secondary" size="sm" @click="modelDialog = false">取消</AppButton>
        <AppButton variant="primary" size="sm" :loading="saveModelPending" @click="saveModel">保存</AppButton>
      </template>
    </AppDialog>
  </div>
</template>

<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import {
  CheckCircle,
  KeyRound,
  Pencil,
  PlugZap,
  Plus,
  RefreshCw,
  Save,
  Trash2
} from '@lucide/vue'
import { AppBadge, AppButton, AppCard, AppDialog, AppInput, AppSelect, AppStateView } from '@/components/ui'
import { handleMutationError, useToast } from '@/composables/useToast'
import {
  getAdminKey,
  setAdminKey as persistAdminKey,
  useAdminModelSnapshot,
  useApplyRegistry,
  useCreateModel,
  useCreateProvider,
  useDeleteModel,
  useDeleteProvider,
  useIndexStatus,
  useModelUsage,
  useRebuildIndex,
  useRollbackIndex,
  useSaveRoutes,
  useTestProvider,
  useTestSavedProvider,
  useUpdateModel,
  useUpdateProvider,
  type AdminModel,
  type AdminProvider,
  type AdminRoute,
  type ConnectionTestResult,
  type ModelPayload,
  type ModelUsageRow,
  type ProviderPayload,
  type RoutePayload
} from '@/queries/adminModels'

const toast = useToast()
const adminKeyInput = ref(getAdminKey())
const { data: snapshot, isLoading, error: snapshotQueryError, refetch } = useAdminModelSnapshot()
const snapshotError = computed(() => {
  const err = snapshotQueryError.value as { response?: { status?: number; data?: { detail?: string } } } | null
  if (!err) return ''
  return err.response?.data?.detail || (err.response?.status ? `请求失败 (${err.response.status})` : '加载失败')
})

const usageDays = ref(7)
const {
  data: usageData,
  isLoading: usageLoading,
  error: usageQueryError,
  refetch: refetchUsage
} = useModelUsage(usageDays)
const usageRows = computed(() => usageData.value?.usage || [])
const usageError = computed(() => {
  const err = usageQueryError.value as { response?: { status?: number; data?: { detail?: string } } } | null
  if (!err) return ''
  return err.response?.data?.detail || (err.response?.status ? `请求失败 (${err.response.status})` : '加载失败')
})
const hasPricingConfigured = computed(
  () => snapshot.value?.models.some(m => m.input_cost_per_1k != null || m.output_cost_per_1k != null) ?? false
)

const {
  data: indexStatus,
  isLoading: indexLoading,
  error: indexQueryError,
  refetch: refetchIndex
} = useIndexStatus()
const indexError = computed(() => {
  const err = indexQueryError.value as { response?: { status?: number; data?: { detail?: string } } } | null
  if (!err) return ''
  return err.response?.data?.detail || (err.response?.status ? `请求失败 (${err.response.status})` : '加载失败')
})
const rebuildIndexMutation = useRebuildIndex()
const rollbackIndexMutation = useRollbackIndex()

const createProviderMutation = useCreateProvider()
const updateProviderMutation = useUpdateProvider()
const deleteProviderMutation = useDeleteProvider()
const testProviderMutation = useTestProvider()
const testSavedProviderMutation = useTestSavedProvider()
const createModelMutation = useCreateModel()
const updateModelMutation = useUpdateModel()
const deleteModelMutation = useDeleteModel()
const saveRoutesMutation = useSaveRoutes()
const applyRegistryMutation = useApplyRegistry()

function saveAdminKey(): void {
  persistAdminKey(adminKeyInput.value.trim())
  refetch()
  refetchUsage()
  refetchIndex()
}

async function rebuildEmbeddingIndex(): Promise<void> {
  const force = Boolean(indexStatus.value?.in_sync)
  try {
    await ElMessageBox.confirm(
      force
        ? '当前索引已与配置同步，将强制重新向量化全部切片并重建索引？'
        : '将重新向量化全部切片并切换到新索引，重建期间旧索引继续服务。继续？',
      '重建向量索引',
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await rebuildIndexMutation.mutateAsync(force)
    toast.success('重建任务已提交', '完成后会自动应用新的 Embedding 配置')
    refetchIndex()
  } catch (err) {
    handleMutationError(err, '提交索引重建失败')
  }
}

async function rollbackEmbeddingIndex(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      '将把向量索引与 Embedding 路由回滚到上一次重建前的版本？',
      '回滚索引',
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await rollbackIndexMutation.mutateAsync()
    toast.success('索引已回滚')
    refetch()
    refetchIndex()
  } catch (err) {
    handleMutationError(err, '回滚索引失败')
  }
}

const providerDialog = ref(false)
const providerEditingId = ref<string | null>(null)
const testResult = ref<ConnectionTestResult | null>(null)
const providerForm = reactive<ProviderPayload>({
  name: '',
  provider_type: 'openai_compatible',
  base_url: '',
  api_key: '',
  api_key_env: '',
  external: true,
  api_key_required: true,
  enabled: true
})

function openProvider(provider?: AdminProvider): void {
  providerEditingId.value = provider?.id || null
  testResult.value = null
  Object.assign(providerForm, {
    name: provider?.name || '',
    provider_type: provider?.provider_type || 'openai_compatible',
    base_url: provider?.base_url || '',
    api_key: '',
    api_key_env: provider?.api_key_env || '',
    external: provider?.external ?? true,
    api_key_required: provider?.api_key_required ?? true,
    enabled: provider?.enabled ?? true
  })
  providerDialog.value = true
}

const saveProviderPending = computed(() =>
  createProviderMutation.isPending.value || updateProviderMutation.isPending.value
)

async function saveProvider(): Promise<void> {
  try {
    const payload: ProviderPayload = { ...providerForm }
    // 编辑时留空表示保留原 Key；只有显式输入才更新，避免误清空。
    if (!payload.api_key) delete payload.api_key
    if (providerEditingId.value) {
      await updateProviderMutation.mutateAsync({ id: providerEditingId.value, payload })
    } else {
      await createProviderMutation.mutateAsync(payload)
    }
    toast.success('Provider 已保存')
    providerDialog.value = false
    refetch()
  } catch (err) {
    handleMutationError(err, '保存 Provider 失败')
  }
}

async function testDraft(): Promise<void> {
  try {
    testResult.value = await testProviderMutation.mutateAsync({ ...providerForm })
  } catch (err) {
    handleMutationError(err, '连接测试失败')
  }
}

async function testSaved(id: string): Promise<void> {
  try {
    const result = await testSavedProviderMutation.mutateAsync(id)
    if (result.ok) toast.success('连接成功', `发现 ${result.models.length} 个模型`)
    else toast.error('连接失败', result.error || '未知错误')
  } catch (err) {
    handleMutationError(err, '连接测试失败')
  }
}

async function removeProvider(provider: AdminProvider): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除 Provider「${provider.name}」？`, '确认删除', { type: 'warning' })
    await deleteProviderMutation.mutateAsync(provider.id)
    toast.success('Provider 已删除')
    refetch()
  } catch (err) {
    if (typeof err === 'string' && (err === 'cancel' || err === 'close')) return
    handleMutationError(err, '删除 Provider 失败')
  }
}

const modelDialog = ref(false)
const modelEditingId = ref<string | null>(null)
const modelForm = reactive<ModelPayload>({
  provider_id: '',
  model_name: '',
  display_name: '',
  capabilities: ['chat', 'stream'],
  context_window: 0,
  max_output_tokens: 0,
  embedding_dimension: 0,
  input_cost_per_1k: null,
  output_cost_per_1k: null,
  price_currency: 'USD',
  enabled: true
})
const capabilityOptions = ['chat', 'stream', 'tools', 'reasoning', 'vision', 'embedding', 'rerank']
const saveModelPending = computed(() =>
  createModelMutation.isPending.value || updateModelMutation.isPending.value
)

function openModel(model?: AdminModel): void {
  modelEditingId.value = model?.id || null
  Object.assign(modelForm, {
    provider_id: model?.provider_id || snapshot.value?.providers[0]?.id || '',
    model_name: model?.model_name || '',
    display_name: model?.display_name || '',
    capabilities: model?.capabilities || ['chat', 'stream'],
    context_window: model?.context_window || 0,
    max_output_tokens: model?.max_output_tokens || 0,
    embedding_dimension: model?.embedding_dimension || 0,
    input_cost_per_1k: model?.input_cost_per_1k ?? null,
    output_cost_per_1k: model?.output_cost_per_1k ?? null,
    price_currency: model?.price_currency || 'USD',
    enabled: model?.enabled ?? true
  })
  modelDialog.value = true
}

async function saveModel(): Promise<void> {
  try {
    if (modelEditingId.value) {
      await updateModelMutation.mutateAsync({ id: modelEditingId.value, payload: { ...modelForm } })
    } else {
      await createModelMutation.mutateAsync({ ...modelForm })
    }
    toast.success('模型已保存')
    modelDialog.value = false
    refetch()
  } catch (err) {
    handleMutationError(err, '保存模型失败')
  }
}

async function removeModel(model: AdminModel): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除模型「${model.display_name || model.model_name}」？`, '确认删除', { type: 'warning' })
    await deleteModelMutation.mutateAsync(model.id)
    toast.success('模型已删除')
    refetch()
  } catch (err) {
    if (typeof err === 'string' && (err === 'cancel' || err === 'close')) return
    handleMutationError(err, '删除模型失败')
  }
}

const requiredTasks = ['answer', 'fast', 'embedding']
const allTasks = [
  'answer',
  'answer_direct',
  'fast',
  'intent_router',
  'title_generation',
  'query_rewrite',
  'verifier',
  'agent_decision',
  'wiki_compile',
  'doc_analyze',
  'embedding',
  'rerank'
]
type RouteDraft = AdminRoute
const routeDrafts = ref<RouteDraft[]>([])

function buildRouteDrafts(routes: AdminRoute[]): RouteDraft[] {
  const byTask = new Map(routes.map(route => [route.task, route]))
  return allTasks.map(task => {
    const route = byTask.get(task)
    return {
      task,
      primary_model_id: route?.primary_model_id || '',
      fallback_model_ids: route?.fallback_model_ids || [],
      reasoning: route?.reasoning || 'auto',
      temperature: route?.temperature ?? null,
      max_output_tokens: route?.max_output_tokens ?? null,
      timeout_s: route?.timeout_s ?? null,
      allow_external_fallback: route?.allow_external_fallback ?? false
    }
  })
}

watch(snapshot, value => {
  if (value) routeDrafts.value = buildRouteDrafts(value.routes)
}, { immediate: true })

function providerName(id: string): string {
  return snapshot.value?.providers.find(p => p.id === id)?.name || id
}

const CURRENCY_SYMBOLS: Record<string, string> = { USD: '$', CNY: '¥', EUR: '€', JPY: '¥' }

function currencySymbol(currency?: string): string {
  const code = (currency || 'USD').toUpperCase()
  return CURRENCY_SYMBOLS[code] || `${code} `
}

function priceSummary(model: AdminModel): string {
  const symbol = currencySymbol(model.price_currency)
  const parts: string[] = []
  if (model.input_cost_per_1k != null) parts.push(`入 ${symbol}${model.input_cost_per_1k}`)
  if (model.output_cost_per_1k != null) parts.push(`出 ${symbol}${model.output_cost_per_1k}`)
  return parts.length ? `价格 ${parts.join(' / ')} 每 1K` : '未定价'
}

function usageKey(row: ModelUsageRow): string {
  return `${row.provider_id}:${row.model_id}:${row.task}:${row.currency}`
}

function modelLabel(row: ModelUsageRow): string {
  const model = snapshot.value?.models.find(m => m.id === row.model_id)
  return model?.display_name || model?.model_name || row.model_name
}

function formatLatency(ms: number): string {
  return `${Math.round(ms || 0).toLocaleString()} ms`
}

function formatCost(row: ModelUsageRow): string {
  if (!row.priced_calls) return '—'
  const amount = row.estimated_cost || 0
  const digits = amount > 0 && amount < 0.01 ? 6 : 4
  return `${currencySymbol(row.currency)}${amount.toFixed(digits)}`
}

async function saveRoutes(): Promise<void> {
  const payload: RoutePayload[] = routeDrafts.value
    .filter(item => item.primary_model_id)
    .map(item => ({
      task: item.task,
      primary_model_id: item.primary_model_id,
      fallback_model_ids: item.fallback_model_ids,
      reasoning: item.reasoning,
      temperature: item.temperature,
      max_output_tokens: item.max_output_tokens,
      timeout_s: item.timeout_s,
      allow_external_fallback: item.allow_external_fallback
    }))
  try {
    await saveRoutesMutation.mutateAsync(payload)
    toast.success('路由已保存')
    refetch()
  } catch (err) {
    handleMutationError(err, '保存路由失败')
  }
}

async function applyRegistry(): Promise<void> {
  try {
    const result = await applyRegistryMutation.mutateAsync()
    toast.success('模型配置已生效', `${result.providers} Providers / ${result.models} 模型 / ${result.routes} 路由`)
    refetch()
  } catch (err) {
    handleMutationError(err, '应用模型配置失败')
  }
}
</script>
