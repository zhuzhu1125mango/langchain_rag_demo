<template>
  <aside
    :class="[
      'bg-white dark:bg-dark-800 border-l border-gray-200 dark:border-dark-600 transition-all duration-300 overflow-hidden',
      show ? 'w-[480px]' : 'w-0'
    ]"
  >
    <div v-if="show" class="h-full flex flex-col">
      <div class="px-4 py-3 border-b border-gray-200 dark:border-dark-600 flex items-center justify-between">
        <div class="flex items-center gap-2">
          <GitCompare class="w-4 h-4 text-primary-500" />
          <span class="text-sm font-medium text-gray-700 dark:text-gray-300">答案对比</span>
        </div>
        <button
          @click="emit('close')"
          class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
        >
          <X class="w-4 h-4" />
        </button>
      </div>
      <div class="flex-1 overflow-y-auto px-4 py-4">
        <div v-if="isComparing" class="flex flex-col items-center justify-center py-12">
          <div class="flex gap-1 mb-3">
            <span class="w-2 h-2 bg-primary-500 rounded-full animate-bounce" style="animation-delay: 0ms"></span>
            <span class="w-2 h-2 bg-primary-500 rounded-full animate-bounce" style="animation-delay: 150ms"></span>
            <span class="w-2 h-2 bg-primary-500 rounded-full animate-bounce" style="animation-delay: 300ms"></span>
          </div>
          <p class="text-sm text-gray-500 dark:text-gray-400">正在对比答案...</p>
        </div>
        <div v-else-if="compareResult" class="space-y-4">
          <!-- 问题显示 -->
          <div class="p-3 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <p class="text-xs text-gray-500 dark:text-gray-400 mb-1">对比问题</p>
            <p class="text-sm font-medium text-gray-700 dark:text-gray-200">{{ compareResult.question }}</p>
          </div>

          <!-- 差异分析总结 -->
          <div v-if="compareResult.comparison?._analysis" class="p-3 bg-gradient-to-r from-blue-50 to-purple-50 dark:from-blue-900/20 dark:to-purple-900/20 border border-blue-200 dark:border-blue-800 rounded-lg">
            <div class="flex items-center gap-2 mb-2">
              <Sparkles class="w-4 h-4 text-blue-500" />
              <span class="text-sm font-medium text-blue-600 dark:text-blue-400">差异分析</span>
              <span :class="[
                'px-2 py-0.5 text-xs rounded-full font-medium',
                compareResult.comparison._analysis.consistency === 'high' ? 'bg-green-100 dark:bg-green-900/30 text-green-600 dark:text-green-400' :
                compareResult.comparison._analysis.consistency === 'medium' ? 'bg-yellow-100 dark:bg-yellow-900/30 text-yellow-600 dark:text-yellow-400' :
                'bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400'
              ]">
                {{ compareResult.comparison._analysis.consistency === 'high' ? '高度一致' :
                   compareResult.comparison._analysis.consistency === 'medium' ? '存在差异' : '差异较大' }}
              </span>
            </div>
            <p class="text-sm text-gray-600 dark:text-gray-300 mb-2">
              {{ compareResult.comparison._analysis.summary }}
            </p>
            <div class="flex items-center gap-2">
              <span class="text-xs text-gray-500 dark:text-gray-400">相似度:</span>
              <div class="flex-1 bg-gray-200 dark:bg-dark-600 rounded-full h-2 overflow-hidden">
                <div
                  class="h-full rounded-full transition-all"
                  :class="[
                    compareResult.comparison._analysis.similarity_score >= 0.8 ? 'bg-green-500' :
                    compareResult.comparison._analysis.similarity_score >= 0.5 ? 'bg-yellow-500' : 'bg-red-500'
                  ]"
                  :style="{ width: (compareResult.comparison._analysis.similarity_score * 100) + '%' }"
                ></div>
              </div>
              <span class="text-xs font-medium text-gray-600 dark:text-gray-300">
                {{ (compareResult.comparison._analysis.similarity_score * 100).toFixed(0) }}%
              </span>
            </div>
          </div>

          <!-- 关键差异点 -->
          <div v-if="!!compareResult.comparison?._analysis?.differences?.length" class="space-y-2">
            <h4 class="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-2">
              <AlertCircle class="w-4 h-4 text-orange-500" />
              主要差异点
            </h4>
            <div class="space-y-2">
              <div
                v-for="(diff, idx) in compareResult.comparison._analysis!.differences"
                :key="idx"
                class="p-3 bg-orange-50 dark:bg-orange-900/20 border border-orange-200 dark:border-orange-800 rounded-lg"
              >
                <p class="text-sm text-gray-700 dark:text-gray-300">{{ diff }}</p>
              </div>
            </div>
          </div>

          <!-- 各知识库答案对比 -->
          <div class="space-y-3">
            <h4 class="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-2">
              <Database class="w-4 h-4 text-primary-500" />
              各知识库答案
            </h4>
            <div
              v-for="item in comparisonKBIds"
              :key="item.kbId"
              class="p-4 bg-gray-50 dark:bg-dark-700 rounded-lg border border-gray-200 dark:border-dark-600"
            >
              <div class="flex items-center gap-2 mb-3">
                <Database class="w-4 h-4 text-primary-500" />
                <span class="text-sm font-medium text-gray-700 dark:text-gray-200">{{ getKbName(item.kbId) }}</span>
                <span v-if="item.data.has_answer" class="px-2 py-0.5 text-xs bg-green-100 dark:bg-green-900/30 text-green-600 dark:text-green-400 rounded-full">
                  有答案
                </span>
                <span v-else class="px-2 py-0.5 text-xs bg-gray-200 dark:bg-gray-600 text-gray-500 dark:text-gray-400 rounded-full">
                  无答案
                </span>
                <span v-if="item.data.doc_count" class="text-xs text-gray-400 dark:text-gray-500">
                  ({{ item.data.doc_count }}个相关文档)
                </span>
              </div>
              <p class="text-sm text-gray-600 dark:text-gray-300 whitespace-pre-wrap leading-relaxed">{{ item.data.answer }}</p>
              <div v-if="!!item.data.source_metadata?.length" class="mt-3 pt-3 border-t border-gray-200 dark:border-dark-600">
                <p class="text-xs text-gray-500 dark:text-gray-400 mb-2 flex items-center gap-1">
                  <FileText class="w-3 h-3" />
                  文档来源
                </p>
                <div class="flex flex-wrap gap-2">
                  <div
                    v-for="(source, idx) in item.data.source_metadata"
                    :key="idx"
                    class="px-2 py-1 bg-white dark:bg-dark-600 rounded text-xs text-gray-600 dark:text-gray-300"
                  >
                    {{ source.filename }} - 第{{ source.chunk_index + 1 }}段
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- 知识库对比详情 -->
          <div v-if="!!compareResult.comparison?._analysis?.comparisons?.length" class="space-y-2">
            <h4 class="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-2">
              <GitCompareArrows class="w-4 h-4 text-purple-500" />
              知识库对比详情
            </h4>
            <div class="space-y-2">
              <div
                v-for="(comp, idx) in compareResult.comparison._analysis!.comparisons"
                :key="idx"
                class="p-3 bg-purple-50 dark:bg-purple-900/20 border border-purple-200 dark:border-purple-800 rounded-lg"
              >
                <div class="flex items-center justify-between mb-2">
                  <span class="text-xs text-gray-500 dark:text-gray-400">对比</span>
                  <span class="text-sm font-medium text-purple-600 dark:text-purple-400">
                    相似度: {{ (comp.similarity * 100).toFixed(1) }}%
                  </span>
                </div>
                <div class="flex items-center gap-2 text-xs text-gray-600 dark:text-gray-300">
                  <span class="font-medium">{{ comp.kb1_name }}</span>
                  <span class="text-gray-400">vs</span>
                  <span class="font-medium">{{ comp.kb2_name }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </aside>
</template>

<script setup lang="ts">
/**
 * 答案对比侧边栏组件
 * @description 在主对话区右侧展开，展示同一问题在多个知识库下的回答差异：
 * 一致性等级、相似度进度条、关键差异点、各知识库答案正文、知识库两两对比详情等。
 *
 * @props show - 是否展开侧边栏
 * @props isComparing - 是否正在请求对比接口
 * @props compareResult - 对比结果（含 comparison 各知识库条目与 _analysis 元数据）
 * @props getKbName - 知识库 ID → 名称映射函数
 *
 * @emits close - 关闭侧边栏
 */
import { computed } from 'vue'
import { GitCompare, X, Sparkles, AlertCircle, Database, FileText, GitCompareArrows } from '@lucide/vue'
import type { CompareResponse, KBComparisonItem } from '@/queries/chat'

const props = defineProps<{
  show: boolean
  isComparing: boolean
  compareResult: CompareResponse | null
  getKbName: (kbId: string) => string
}>()

const emit = defineEmits<{
  (e: 'close'): void
}>()

// 从对比结果中提取各知识库条目（排除 _analysis 元数据键）
const comparisonKBIds = computed(() => {
  if (!props.compareResult?.comparison) {
    return []
  }
  return Object.entries(props.compareResult.comparison)
    .filter(([kbId]) => kbId !== '_analysis')
    .map(([kbId, data]) => ({ kbId, data: data as KBComparisonItem }))
})
</script>
