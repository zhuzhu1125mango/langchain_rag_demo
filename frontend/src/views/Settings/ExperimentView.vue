<template>
  <div class="flex-1 overflow-auto bg-gray-50 dark:bg-dark-900 p-6">
    <div class="max-w-4xl mx-auto space-y-6">
      <div>
        <h1 class="text-xl font-semibold text-gray-800 dark:text-white">A/B测试实验管理</h1>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">管理和分析A/B测试实验，优化问答策略</p>
      </div>

      <div class="flex items-center justify-between">
        <div class="flex items-center gap-2">
          <button
            v-for="filter in filters"
            :key="filter.value"
            @click="activeFilter = filter.value"
            :class="[
              'px-3 py-1.5 rounded-full text-sm font-medium transition-colors',
              activeFilter === filter.value
                ? 'bg-primary-500 text-white'
                : 'bg-white dark:bg-dark-800 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700'
            ]"
          >
            {{ filter.label }}
          </button>
        </div>
        <button
          @click="openCreateModal"
          class="flex items-center gap-2 px-4 py-2 bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors"
        >
          <Plus class="w-4 h-4" />
          创建实验
        </button>
      </div>

      <div
        v-if="selectedExperimentIds.length > 0"
        class="flex items-center justify-between p-3 bg-white dark:bg-dark-800 rounded-lg border border-primary-200 dark:border-primary-800 shadow-sm"
      >
        <div class="flex items-center gap-3">
          <span class="text-sm text-gray-700 dark:text-gray-300">
            已选择 <span class="font-semibold text-primary-600 dark:text-primary-400">{{ selectedExperimentIds.length }}</span> 个实验
          </span>
          <button
            @click="toggleSelectAll"
            class="text-sm text-primary-600 dark:text-primary-400 hover:text-primary-700 dark:hover:text-primary-300"
          >
            {{ isAllSelected ? '取消全选' : '全选' }}
          </button>
        </div>
        <div class="flex items-center gap-2">
          <button
            @click="clearSelection"
            class="px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
          >
            取消
          </button>
          <button
            @click="batchDelete"
            :disabled="deleteMutation.isPending.value"
            class="flex items-center gap-1.5 px-3 py-1.5 bg-red-500 text-white text-sm rounded-lg hover:bg-red-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Trash2 class="w-4 h-4" />
            批量删除
          </button>
        </div>
      </div>

      <div class="space-y-4">
        <div
          v-for="experiment in filteredExperiments"
          :key="experiment.id"
          class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-5"
        >
          <div class="flex items-start justify-between mb-4">
            <div class="flex items-start gap-3">
              <input
                type="checkbox"
                :checked="selectedExperimentIds.includes(experiment.id)"
                @change="toggleSelect(experiment.id)"
                class="mt-1 w-4 h-4 text-primary-600 border-gray-300 rounded focus:ring-primary-500 cursor-pointer"
              />
              <div>
                <div class="flex items-center gap-2">
                  <h3 class="font-medium text-gray-800 dark:text-white">{{ experiment.name }}</h3>
                  <span
                    :class="[
                      'px-2 py-0.5 rounded-full text-xs font-medium',
                      getStatusClass(experiment.status)
                    ]"
                  >
                    {{ getStatusLabel(experiment.status) }}
                  </span>
                </div>
                <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">{{ experiment.description || '-' }}</p>
              </div>
            </div>
            <div class="flex items-center gap-2">
              <button
                v-if="experiment.status === 'created'"
                @click="startExperiment(experiment.id)"
                :disabled="startMutation.isPending.value"
                class="px-3 py-1.5 bg-green-500 text-white text-sm rounded-lg hover:bg-green-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Play class="w-4 h-4 inline mr-1" />
                启动
              </button>
              <button
                v-if="experiment.status === 'running'"
                @click="stopExperiment(experiment.id)"
                :disabled="stopMutation.isPending.value"
                class="px-3 py-1.5 bg-red-500 text-white text-sm rounded-lg hover:bg-red-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <Square class="w-4 h-4 inline mr-1" />
                停止
              </button>
              <button
                v-if="experiment.status === 'stopped'"
                @click="analyzeExperiment(experiment.id)"
                :disabled="analyzeMutation.isPending.value"
                class="px-3 py-1.5 bg-blue-500 text-white text-sm rounded-lg hover:bg-blue-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <BarChart3 class="w-4 h-4 inline mr-1" />
                分析
              </button>
            </div>
          </div>

          <div class="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div class="p-3 bg-gray-50 dark:bg-dark-700 rounded-lg">
              <p class="text-xs text-gray-500 dark:text-gray-400">变体数量</p>
              <p class="font-semibold text-gray-800 dark:text-white">{{ experiment.variants?.length || 0 }}</p>
            </div>
            <div class="p-3 bg-gray-50 dark:bg-dark-700 rounded-lg">
              <p class="text-xs text-gray-500 dark:text-gray-400">监控指标</p>
              <p class="font-semibold text-gray-800 dark:text-white">{{ experiment.metrics?.length || 0 }}</p>
            </div>
            <div class="p-3 bg-gray-50 dark:bg-dark-700 rounded-lg">
              <p class="text-xs text-gray-500 dark:text-gray-400">创建时间</p>
              <p class="font-semibold text-gray-800 dark:text-white">{{ formatDate(experiment.created_at) }}</p>
            </div>
            <div class="p-3 bg-gray-50 dark:bg-dark-700 rounded-lg">
              <p class="text-xs text-gray-500 dark:text-gray-400">启动时间</p>
              <p class="font-semibold text-gray-800 dark:text-white">{{ experiment.started_at ? formatDate(experiment.started_at) : '未启动' }}</p>
            </div>
          </div>

          <div v-if="experiment.variants?.length" class="mt-4 pt-4 border-t border-gray-100 dark:border-dark-700">
            <p class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">实验变体</p>
            <div class="flex flex-wrap gap-2">
              <span
                v-for="variant in experiment.variants"
                :key="variant.id"
                class="px-3 py-1 bg-gray-100 dark:bg-dark-600 text-gray-700 dark:text-gray-300 text-sm rounded-full"
              >
                {{ variant.name }} ({{ (variant.weight * 100).toFixed(0) }}%)
              </span>
            </div>
          </div>

          <div
            v-if="selectedExperiment === experiment.id && experimentResult"
            class="mt-4 pt-4 border-t border-gray-100 dark:border-dark-700"
          >
            <h4 class="font-medium text-gray-800 dark:text-white mb-3">实验结果分析</h4>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
              <div
                v-for="result in experimentResult.variant_results"
                :key="result.variant_id"
                class="p-4 bg-gray-50 dark:bg-dark-700 rounded-lg"
              >
                <div class="flex items-center justify-between mb-2">
                  <span class="font-medium text-gray-800 dark:text-white">{{ result.variant_name }}</span>
                  <span class="text-sm text-gray-500 dark:text-gray-400">样本量: {{ result.sample_size }}</span>
                </div>
                <div class="space-y-2">
                  <div
                    v-for="(metric, metricName) in result.metrics"
                    :key="metricName"
                    class="flex items-center justify-between text-sm"
                  >
                    <span class="text-gray-600 dark:text-gray-400">{{ metricName }}</span>
                    <span class="font-medium text-gray-800 dark:text-white">{{ (metric.mean as number).toFixed(2) }}</span>
                  </div>
                </div>
              </div>
            </div>
            <div v-if="experimentResult.winner_variant_id" class="mt-4 p-3 bg-green-50 dark:bg-green-900/30 rounded-lg">
              <div class="flex items-center gap-2">
                <Trophy class="w-5 h-5 text-green-600 dark:text-green-400" />
                <span class="font-medium text-green-700 dark:text-green-400">
                  获胜变体: {{ getVariantName(experimentResult.winner_variant_id, experiment.variants) }}
                </span>
              </div>
              <p class="text-sm text-green-600 dark:text-green-400 mt-1">
                置信度: {{ (experimentResult.confidence_level as number * 100).toFixed(1) }}%
              </p>
            </div>
          </div>
        </div>

        <div v-if="filteredExperiments.length === 0" class="py-16 text-center text-gray-500 dark:text-gray-400">
          <FlaskConical class="w-16 h-16 mx-auto mb-4 opacity-50" />
          <p class="text-lg font-medium">暂无实验</p>
          <p class="text-sm mt-1">点击上方按钮创建第一个A/B测试实验</p>
        </div>
      </div>
    </div>

    <el-dialog
      v-model="modalVisible"
      title="创建实验"
      width="560px"
    >
      <form @submit.prevent="createExperiment" class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">实验名称 *</label>
          <input
            v-model="formData.name"
            type="text"
            required
            placeholder="请输入实验名称"
            class="w-full px-3 py-2 border border-gray-300 dark:border-dark-500 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">描述</label>
          <textarea
            v-model="formData.description"
            rows="2"
            placeholder="请输入实验描述（可选）"
            class="w-full px-3 py-2 border border-gray-300 dark:border-dark-500 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none"
          ></textarea>
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">实验变体</label>
          <div class="space-y-2">
            <div
              v-for="(variant, index) in formData.variants"
              :key="index"
              class="flex items-center gap-2"
            >
              <input
                v-model="variant.name"
                type="text"
                :placeholder="`变体 ${index + 1} 名称`"
                class="flex-1 px-3 py-2 border border-gray-300 dark:border-dark-500 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
              />
              <input
                v-model.number="variant.weight"
                type="number"
                min="0"
                max="1"
                step="0.1"
                placeholder="权重"
                class="w-24 px-3 py-2 border border-gray-300 dark:border-dark-500 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
              />
              <button
                v-if="formData.variants && formData.variants.length > 2"
                type="button"
                @click="removeVariant(index)"
                class="p-2 text-gray-400 hover:text-red-500 hover:bg-gray-100 dark:hover:bg-dark-600 rounded-lg transition-colors"
              >
                <X class="w-4 h-4" />
              </button>
            </div>
          </div>
          <button
            v-if="formData.variants && formData.variants.length < 5"
            type="button"
            @click="addVariant"
            class="mt-2 flex items-center gap-1 text-sm text-primary-500 hover:text-primary-600"
          >
            <Plus class="w-4 h-4" />
            添加变体
          </button>
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">监控指标</label>
          <div class="flex flex-wrap gap-2">
            <button
              v-for="metric in availableMetrics"
              :key="metric"
              type="button"
              @click="toggleMetric(metric)"
              :class="[
                'px-3 py-1 rounded-full text-sm font-medium transition-colors',
                formData.metrics?.includes(metric)
                  ? 'bg-primary-500 text-white'
                  : 'bg-gray-100 dark:bg-dark-600 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-dark-500'
              ]"
            >
              {{ metric }}
            </button>
          </div>
        </div>
      </form>
      <template #footer>
        <button
          @click="modalVisible = false"
          class="px-4 py-2 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-600 rounded-lg transition-colors"
        >
          取消
        </button>
        <button
          @click="createExperiment"
          :disabled="!formData.name || createMutation.isPending.value"
          class="px-4 py-2 bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          创建实验
        </button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
/**
 * A/B 实验设置页。
 *
 * 管理实验的创建、启动、停止、分析与批量删除，支持按状态筛选、
 * 配置变体与监控指标，并展示实验结果与获胜变体。
 */
import { ref, computed } from 'vue'
import { Plus, Play, Square, BarChart3, Trophy, FlaskConical, X, Trash2 } from '@lucide/vue'
import { ElMessageBox } from 'element-plus'
import {
  useExperiments,
  useCreateExperiment,
  useStartExperiment,
  useStopExperiment,
  useAnalyzeExperiment,
  useExperimentResult,
  useDeleteExperiments,
  type ExperimentCreateRequest,
  type ExperimentResult,
  type BatchDeleteExperimentsResult
} from '@/queries/kb'
import { useToast } from '@/composables/useToast'
import { formatDate } from '@/utils/format'

const toast = useToast()

const { data: experiments, refetch } = useExperiments()

const createMutation = useCreateExperiment()
const startMutation = useStartExperiment()
const stopMutation = useStopExperiment()
const analyzeMutation = useAnalyzeExperiment()
const deleteMutation = useDeleteExperiments()

const activeFilter = ref<string>('all')
const modalVisible = ref(false)
const selectedExperiment = ref<string | null>(null)
const experimentResult = ref<ExperimentResult | null>(null)
const selectedExperimentIds = ref<string[]>([])

const filters = [
  { label: '全部', value: 'all' },
  { label: '待启动', value: 'created' },
  { label: '运行中', value: 'running' },
  { label: '已停止', value: 'stopped' }
]

const availableMetrics = ['准确率', '响应时间', '用户满意度', '转化率']

const formData = ref<ExperimentCreateRequest>({
  name: '',
  description: '',
  variants: [
    { id: 'a', name: '变体 A', weight: 0.5 },
    { id: 'b', name: '变体 B', weight: 0.5 }
  ],
  metrics: ['准确率']
})

const filteredExperiments = computed(() => {
  if (!experiments.value) return []
  if (activeFilter.value === 'all') return experiments.value
  return experiments.value.filter(e => e.status === activeFilter.value)
})

const isAllSelected = computed(() => {
  if (filteredExperiments.value.length === 0) return false
  return filteredExperiments.value.every(e => selectedExperimentIds.value.includes(e.id))
})

/** 根据实验状态返回对应的样式类，未知状态回退为待启动样式。 */
function getStatusClass(status?: string): string {
  const classes: Record<string, string> = {
    created: 'bg-gray-100 text-gray-600 dark:bg-dark-600 dark:text-gray-400',
    running: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
    stopped: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
  }
  if (status && status in classes) {
    return classes[status]!
  }
  return classes['created']!
}

/** 将状态码转换为中文标签。 */
function getStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    created: '待启动',
    running: '运行中',
    stopped: '已停止'
  }
  return labels[status] || status
}

/** 新增一个变体，ID 按字母序递增，权重取均分值。 */
function addVariant(): void {
  const id = String.fromCharCode(97 + formData.value.variants!.length)
  formData.value.variants!.push({
    id,
    name: `变体 ${id.toUpperCase()}`,
    weight: 1 / (formData.value.variants!.length + 1)
  })
}

/** 按索引移除表单中的变体。 */
function removeVariant(index: number): void {
  formData.value.variants!.splice(index, 1)
}

/** 切换监控指标的选中状态，已选则移除、未选则追加。 */
function toggleMetric(metric: string): void {
  if (!formData.value.metrics) {
    formData.value.metrics = []
  }
  const index = formData.value.metrics.indexOf(metric)
  if (index > -1) {
    formData.value.metrics.splice(index, 1)
  } else {
    formData.value.metrics.push(metric)
  }
}

/** 根据变体 ID 查找名称，找不到时回退为 ID 本身。 */
function getVariantName(variantId: string, variants?: Array<{ id: string; name: string; weight: number }>): string {
  return variants?.find(v => v.id === variantId)?.name || variantId
}

/** 打开创建实验弹窗，重置表单为默认变体与指标。 */
function openCreateModal(): void {
  formData.value = {
    name: '',
    description: '',
    variants: [
      { id: 'a', name: '变体 A', weight: 0.5 },
      { id: 'b', name: '变体 B', weight: 0.5 }
    ],
    metrics: ['准确率']
  }
  modalVisible.value = true
}

/** 提交表单创建实验，成功后关闭弹窗。 */
async function createExperiment(): Promise<void> {
  if (!formData.value.name) return

  try {
    await createMutation.mutateAsync(formData.value)
    toast.success('实验创建成功')
    modalVisible.value = false
  } catch {
    toast.error('创建失败，请重试')
  }
}

/** 启动指定实验并刷新列表。 */
async function startExperiment(experimentId: string): Promise<void> {
  try {
    await startMutation.mutateAsync(experimentId)
    await refetch()
    toast.success('实验已启动')
  } catch {
    toast.error('启动失败，请重试')
  }
}

/** 停止指定实验并刷新列表。 */
async function stopExperiment(experimentId: string): Promise<void> {
  try {
    await stopMutation.mutateAsync(experimentId)
    await refetch()
    toast.success('实验已停止')
  } catch {
    toast.error('停止失败，请重试')
  }
}

/** 触发实验分析并加载结果，展示在对应实验卡片下方。 */
async function analyzeExperiment(experimentId: string): Promise<void> {
  try {
    await analyzeMutation.mutateAsync(experimentId)
    const resultQuery = useExperimentResult(experimentId)
    await resultQuery.refetch()
    experimentResult.value = resultQuery.data.value || null
    selectedExperiment.value = experimentId
    toast.success('分析完成')
  } catch {
    toast.error('分析失败，请重试')
  }
}

/** 切换单个实验的选中状态（用于批量操作）。 */
function toggleSelect(experimentId: string): void {
  const index = selectedExperimentIds.value.indexOf(experimentId)
  if (index > -1) {
    selectedExperimentIds.value.splice(index, 1)
  } else {
    selectedExperimentIds.value.push(experimentId)
  }
}

/** 全选/取消全选当前筛选结果中的实验，保留筛选范围外的已选项。 */
function toggleSelectAll(): void {
  if (isAllSelected.value) {
    selectedExperimentIds.value = selectedExperimentIds.value.filter(
      id => !filteredExperiments.value.some(e => e.id === id)
    )
  } else {
    const currentIds = new Set(selectedExperimentIds.value)
    filteredExperiments.value.forEach(e => currentIds.add(e.id))
    selectedExperimentIds.value = Array.from(currentIds)
  }
}

/** 清空所有已选实验。 */
function clearSelection(): void {
  selectedExperimentIds.value = []
}

/** 二次确认后批量删除选中实验，按成功/失败数提示并刷新列表。 */
async function batchDelete(): Promise<void> {
  if (selectedExperimentIds.value.length === 0) return

  try {
    await ElMessageBox.confirm(
      `确定要删除选中的 ${selectedExperimentIds.value.length} 个实验吗？相关变体、指标、结果和分流记录将一并清理，此操作不可恢复。`,
      '批量删除确认',
      {
        confirmButtonText: '确认删除',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
  } catch {
    return
  }

  try {
    const result: BatchDeleteExperimentsResult = await deleteMutation.mutateAsync(
      selectedExperimentIds.value
    )
    selectedExperimentIds.value = []

    if (result.failed_count > 0) {
      toast.warning(
        '部分删除成功',
        `成功 ${result.success_count} 个，失败 ${result.failed_count} 个`
      )
    } else {
      toast.success(result.message)
    }
    await refetch()
  } catch {
    toast.error('批量删除失败，请重试')
  }
}
</script>