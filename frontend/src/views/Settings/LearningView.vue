<template>
  <div class="flex-1 overflow-auto bg-gray-50 dark:bg-dark-900 p-6">
    <div class="max-w-4xl mx-auto space-y-6">
      <div>
        <h1 class="text-xl font-semibold text-gray-800 dark:text-white">学习引擎</h1>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">管理策略学习和优化引擎的配置</p>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-5">
          <div class="flex items-center gap-3 mb-3">
            <div class="w-10 h-10 rounded-lg bg-blue-100 dark:bg-blue-900/30 flex items-center justify-center">
              <Cpu class="w-5 h-5 text-blue-600 dark:text-blue-400" />
            </div>
            <div>
              <p class="text-sm text-gray-500 dark:text-gray-400">总执行次数</p>
              <p class="text-xl font-semibold text-gray-800 dark:text-white">{{ stats?.total_executions || 0 }}</p>
            </div>
          </div>
        </div>

        <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-5">
          <div class="flex items-center gap-3 mb-3">
            <div class="w-10 h-10 rounded-lg bg-green-100 dark:bg-green-900/30 flex items-center justify-center">
              <CheckCircle class="w-5 h-5 text-green-600 dark:text-green-400" />
            </div>
            <div>
              <p class="text-sm text-gray-500 dark:text-gray-400">成功学习次数</p>
              <p class="text-xl font-semibold text-gray-800 dark:text-white">{{ stats?.successful_learnings || 0 }}</p>
            </div>
          </div>
        </div>

        <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-5">
          <div class="flex items-center gap-3 mb-3">
            <div class="w-10 h-10 rounded-lg bg-yellow-100 dark:bg-yellow-900/30 flex items-center justify-center">
              <AlertCircle class="w-5 h-5 text-yellow-600 dark:text-yellow-400" />
            </div>
            <div>
              <p class="text-sm text-gray-500 dark:text-gray-400">误分类案例</p>
              <p class="text-xl font-semibold text-gray-800 dark:text-white">{{ stats?.misclassification_count || 0 }}</p>
            </div>
          </div>
        </div>

        <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-5">
          <div class="flex items-center gap-3 mb-3">
            <div class="w-10 h-10 rounded-lg bg-purple-100 dark:bg-purple-900/30 flex items-center justify-center">
              <Target class="w-5 h-5 text-purple-600 dark:text-purple-400" />
            </div>
            <div>
              <p class="text-sm text-gray-500 dark:text-gray-400">平均置信度</p>
              <p class="text-xl font-semibold text-gray-800 dark:text-white">{{ (stats?.avg_confidence || 0).toFixed(2) }}</p>
            </div>
          </div>
        </div>
      </div>

      <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-6">
        <div class="flex items-center justify-between mb-6">
          <h2 class="font-medium text-gray-800 dark:text-white">引擎状态</h2>
          <div class="flex items-center gap-2">
            <span
              :class="[
                'px-3 py-1 rounded-full text-sm font-medium',
                config?.enabled
                  ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                  : 'bg-gray-100 text-gray-600 dark:bg-dark-600 dark:text-gray-400'
              ]"
            >
              {{ config?.enabled ? '运行中' : '已停止' }}
            </span>
          </div>
        </div>

        <div class="space-y-6">
          <div class="flex items-center justify-between p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <div>
              <p class="font-medium text-gray-800 dark:text-white">启用学习引擎</p>
              <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">开启自动策略学习和优化功能</p>
            </div>
            <button
              @click="toggleLearning"
              :disabled="updateConfigMutation.isPending.value"
              class="relative w-14 h-8 rounded-full transition-colors"
              :class="config?.enabled ? 'bg-primary-500' : 'bg-gray-300 dark:bg-dark-500'"
            >
              <span
                class="absolute top-1 w-6 h-6 bg-white rounded-full shadow transition-transform"
                :class="config?.enabled ? 'left-7' : 'left-1'"
              ></span>
            </button>
          </div>

          <div class="p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">学习间隔（小时）</label>
            <div v-if="isConfigLoading || learningInterval === null" class="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
              <RefreshCw class="w-4 h-4 animate-spin" />
              配置加载中...
            </div>
            <div v-else class="flex items-center gap-4">
              <input
                v-model.number="learningInterval"
                type="range"
                min="1"
                max="24"
                step="1"
                class="flex-1 h-2 bg-gray-200 dark:bg-dark-600 rounded-lg appearance-none cursor-pointer"
              />
              <span class="text-sm font-medium text-gray-800 dark:text-white w-16 text-right">{{ learningInterval }} 小时</span>
            </div>
          </div>

          <div class="p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">上次学习时间</label>
            <p class="text-gray-800 dark:text-white">
              {{ config?.last_learning_time ? formatDate(config.last_learning_time) : '从未执行' }}
            </p>
          </div>
        </div>

        <div class="flex items-center gap-3 mt-6">
          <button
            @click="saveConfig"
            :disabled="updateConfigMutation.isPending.value || isConfigLoading || learningInterval === null"
            class="px-4 py-2 bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <Save class="w-4 h-4 inline mr-2" />
            保存配置
          </button>
          <button
            @click="triggerLearning"
            :disabled="triggerMutation.isPending.value || !config?.enabled"
            class="px-4 py-2 bg-gray-100 dark:bg-dark-600 text-gray-700 dark:text-gray-300 rounded-lg hover:bg-gray-200 dark:hover:bg-dark-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
          >
            <RefreshCw class="w-4 h-4 inline mr-2" :class="{ 'animate-spin': triggerMutation.isPending }" />
            手动触发学习
          </button>
        </div>
      </div>

      <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-6">
        <h2 class="font-medium text-gray-800 dark:text-white mb-4">误分类案例分析</h2>
        <div class="overflow-x-auto">
          <table class="w-full">
            <thead>
              <tr class="border-b border-gray-200 dark:border-dark-600">
                <th class="text-left py-3 px-4 text-sm font-medium text-gray-500 dark:text-gray-400">问题</th>
                <th class="text-left py-3 px-4 text-sm font-medium text-gray-500 dark:text-gray-400">实际决策</th>
                <th class="text-left py-3 px-4 text-sm font-medium text-gray-500 dark:text-gray-400">正确决策</th>
                <th class="text-left py-3 px-4 text-sm font-medium text-gray-500 dark:text-gray-400">置信度</th>
                <th class="text-left py-3 px-4 text-sm font-medium text-gray-500 dark:text-gray-400">时间</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="caseItem in misclassificationCasesList"
                :key="caseItem.id"
                class="border-b border-gray-100 dark:border-dark-700 hover:bg-gray-50 dark:hover:bg-dark-700/50"
              >
                <td class="py-3 px-4 text-sm text-gray-800 dark:text-white max-w-xs truncate" :title="caseItem.question">
                  {{ caseItem.question }}
                </td>
                <td class="py-3 px-4">
                  <span
                    :class="[
                      'px-2 py-1 rounded text-xs font-medium',
                      caseItem.actual_decision
                        ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                        : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                    ]"
                  >
                    {{ caseItem.actual_decision ? '使用知识库' : '不使用知识库' }}
                  </span>
                </td>
                <td class="py-3 px-4">
                  <span
                    :class="[
                      'px-2 py-1 rounded text-xs font-medium',
                      caseItem.correct_decision
                        ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                        : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                    ]"
                  >
                    {{ caseItem.correct_decision ? '使用知识库' : '不使用知识库' }}
                  </span>
                </td>
                <td class="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                  {{ (caseItem.confidence * 100).toFixed(1) }}%
                </td>
                <td class="py-3 px-4 text-sm text-gray-500 dark:text-gray-400">
                  {{ formatDate(caseItem.created_at) }}
                </td>
              </tr>
              <tr v-if="misclassificationCasesList.length === 0">
                <td colspan="5" class="py-8 text-center text-gray-500 dark:text-gray-400">
                  <BarChart3 class="w-10 h-10 mx-auto mb-2 opacity-50" />
                  <p>暂无误分类案例</p>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 学习引擎设置页。
 *
 * 展示学习统计与误分类案例，支持启停学习引擎、调整学习间隔、
 * 保存配置与手动触发学习。
 */
import { ref, watch, computed } from 'vue'
import { Cpu, CheckCircle, AlertCircle, Target, Save, RefreshCw, BarChart3 } from '@lucide/vue'
import {
  useLearningStats,
  useLearningConfig,
  useMisclassificationAnalysis,
  useUpdateLearningConfig,
  useTriggerLearning,
} from '@/queries/kb'
import { useToast } from '@/composables/useToast'
import { formatDate } from '@/utils/format'

const toast = useToast()

const { data: stats } = useLearningStats()
const { data: config, refetch, isLoading: isConfigLoading } = useLearningConfig()
const { data: misclassificationCases } = useMisclassificationAnalysis(20)

const misclassificationCasesList = computed(() => misclassificationCases.value || [])

const updateConfigMutation = useUpdateLearningConfig()
const triggerMutation = useTriggerLearning()

const learningInterval = ref<number | null>(null)

watch(config, (newConfig) => {
  if (newConfig) {
    learningInterval.value = newConfig.learning_interval_hours
  }
}, { immediate: true })

/** 切换学习引擎启用状态并刷新配置。 */
async function toggleLearning(): Promise<void> {
  const newValue = !(config.value?.enabled ?? false)
  try {
    await updateConfigMutation.mutateAsync({ enabled: newValue })
    await refetch()
    toast.success(newValue ? '学习引擎已启用' : '学习引擎已禁用')
  } catch {
    toast.error('操作失败，请重试')
  }
}

/** 保存学习间隔配置并刷新。 */
async function saveConfig(): Promise<void> {
  if (learningInterval.value === null) return
  try {
    await updateConfigMutation.mutateAsync({ learning_interval_hours: learningInterval.value })
    await refetch()
    toast.success('配置保存成功')
  } catch {
    toast.error('保存失败，请重试')
  }
}

/** 手动触发一次学习任务并刷新配置。 */
async function triggerLearning(): Promise<void> {
  try {
    await triggerMutation.mutateAsync()
    await refetch()
    toast.success('学习任务已触发')
  } catch {
    toast.error('触发失败，请重试')
  }
}
</script>
