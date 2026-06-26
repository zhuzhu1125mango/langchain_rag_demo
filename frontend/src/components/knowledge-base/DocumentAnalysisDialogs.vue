<template>
  <!-- 文档分类弹窗 -->
  <el-dialog
    :model-value="visibleClassify"
    @update:model-value="$emit('update:visibleClassify', $event)"
    :title="'文档分类: ' + (doc?.filename || '')"
    width="500px"
  >
    <div v-if="isClassifying" class="flex items-center justify-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-500"></div>
    </div>
    <div v-else-if="classifyResult" class="space-y-4">
      <div class="bg-blue-50 dark:bg-blue-900/20 rounded-lg p-4">
        <div class="flex items-center gap-2 mb-2">
          <Tags class="w-5 h-5 text-blue-500" />
          <span class="font-medium text-gray-800 dark:text-white">文档类型</span>
        </div>
        <p class="text-lg font-semibold text-blue-600 dark:text-blue-400">{{ classifyResult.document_type_label }}</p>
      </div>
      <div class="bg-purple-50 dark:bg-purple-900/20 rounded-lg p-4">
        <div class="flex items-center gap-2 mb-2">
          <Database class="w-5 h-5 text-purple-500" />
          <span class="font-medium text-gray-800 dark:text-white">所属领域</span>
        </div>
        <p class="text-lg font-semibold text-purple-600 dark:text-purple-400">{{ classifyResult.domain_label }}</p>
      </div>
      <div>
        <p class="text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">主题标签</p>
        <div class="flex flex-wrap gap-2">
          <span
            v-for="topic in classifyResult.topics"
            :key="topic"
            class="px-3 py-1 text-sm bg-gray-100 dark:bg-dark-600 text-gray-700 dark:text-gray-300 rounded-full"
          >
            {{ topic }}
          </span>
        </div>
      </div>
      <div class="bg-gray-50 dark:bg-dark-700 rounded-lg p-4">
        <p class="text-sm text-gray-600 dark:text-gray-400">{{ classifyResult.summary }}</p>
      </div>
      <div class="flex items-center justify-between text-sm text-gray-500">
        <span>置信度</span>
        <span class="font-medium">{{ (classifyResult.confidence * 100).toFixed(0) }}%</span>
      </div>
    </div>
    <template #footer>
      <button
        @click="$emit('update:visibleClassify', false)"
        class="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
      >
        关闭
      </button>
    </template>
  </el-dialog>

  <!-- 质量评估弹窗 -->
  <el-dialog
    :model-value="visibleQuality"
    @update:model-value="$emit('update:visibleQuality', $event)"
    :title="'质量评估: ' + (doc?.filename || '')"
    width="600px"
  >
    <div v-if="isEvaluating" class="flex items-center justify-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-500"></div>
    </div>
    <div v-else-if="qualityResult" class="space-y-4">
      <div class="text-center py-4">
        <div class="inline-flex items-center justify-center w-20 h-20 rounded-full" :class="getQualityGradeClass(qualityResult.overall_grade)">
          <span class="text-3xl font-bold" :class="qualityResult.overall_score >= 70 ? 'text-white' : 'text-gray-800'">{{ qualityResult.overall_score }}</span>
        </div>
        <p class="mt-3 text-xl font-semibold" :class="getQualityTextClass(qualityResult.overall_score)">{{ qualityResult.overall_grade }}</p>
      </div>

      <div class="grid grid-cols-2 gap-4">
        <div class="bg-green-50 dark:bg-green-900/20 rounded-lg p-4">
          <div class="flex items-center justify-between mb-2">
            <span class="text-sm text-gray-600 dark:text-gray-400">完整性</span>
            <span class="text-sm font-medium text-green-600 dark:text-green-400">{{ qualityResult.completeness }}%</span>
          </div>
          <div class="w-full bg-gray-200 dark:bg-dark-600 rounded-full h-2">
            <div class="h-full bg-green-500 rounded-full" :style="{ width: qualityResult.completeness + '%' }"></div>
          </div>
          <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ qualityResult.completeness_comment }}</p>
        </div>
        <div class="bg-blue-50 dark:bg-blue-900/20 rounded-lg p-4">
          <div class="flex items-center justify-between mb-2">
            <span class="text-sm text-gray-600 dark:text-gray-400">可读性</span>
            <span class="text-sm font-medium text-blue-600 dark:text-blue-400">{{ qualityResult.readability }}%</span>
          </div>
          <div class="w-full bg-gray-200 dark:bg-dark-600 rounded-full h-2">
            <div class="h-full bg-blue-500 rounded-full" :style="{ width: qualityResult.readability + '%' }"></div>
          </div>
          <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ qualityResult.readability_comment }}</p>
        </div>
        <div class="bg-purple-50 dark:bg-purple-900/20 rounded-lg p-4">
          <div class="flex items-center justify-between mb-2">
            <span class="text-sm text-gray-600 dark:text-gray-400">结构</span>
            <span class="text-sm font-medium text-purple-600 dark:text-purple-400">{{ qualityResult.structure }}%</span>
          </div>
          <div class="w-full bg-gray-200 dark:bg-dark-600 rounded-full h-2">
            <div class="h-full bg-purple-500 rounded-full" :style="{ width: qualityResult.structure + '%' }"></div>
          </div>
          <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ qualityResult.structure_comment }}</p>
        </div>
        <div class="bg-orange-50 dark:bg-orange-900/20 rounded-lg p-4">
          <div class="flex items-center justify-between mb-2">
            <span class="text-sm text-gray-600 dark:text-gray-400">相关性</span>
            <span class="text-sm font-medium text-orange-600 dark:text-orange-400">{{ qualityResult.relevance }}%</span>
          </div>
          <div class="w-full bg-gray-200 dark:bg-dark-600 rounded-full h-2">
            <div class="h-full bg-orange-500 rounded-full" :style="{ width: qualityResult.relevance + '%' }"></div>
          </div>
          <p class="mt-2 text-xs text-gray-500 dark:text-gray-400">{{ qualityResult.relevance_comment }}</p>
        </div>
      </div>

      <div class="bg-yellow-50 dark:bg-yellow-900/20 rounded-lg p-4">
        <div class="flex items-center gap-2 mb-2">
          <Award class="w-4 h-4 text-yellow-600" />
          <span class="text-sm font-medium text-gray-700 dark:text-gray-300">改进建议</span>
        </div>
        <ul class="space-y-1">
          <li v-for="(suggestion, index) in qualityResult.suggestions" :key="index" class="text-sm text-gray-600 dark:text-gray-400 flex items-start gap-2">
            <span class="text-yellow-500">•</span>
            {{ suggestion }}
          </li>
        </ul>
      </div>
    </div>
    <template #footer>
      <button
        @click="$emit('update:visibleQuality', false)"
        class="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
      >
        关闭
      </button>
    </template>
  </el-dialog>

  <!-- 重复检测弹窗 -->
  <el-dialog
    :model-value="visibleDuplicate"
    @update:model-value="$emit('update:visibleDuplicate', $event)"
    :title="'重复检测: ' + (doc?.filename || '')"
    width="600px"
  >
    <div v-if="isDetectingDuplicates" class="flex items-center justify-center py-8">
      <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-500"></div>
    </div>
    <div v-else class="space-y-4">
      <div v-if="duplicateResults.length === 0" class="text-center py-8">
        <Copy class="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-3" />
        <p class="text-gray-500 dark:text-gray-400">未检测到相似文档</p>
      </div>
      <div v-else>
        <div class="flex items-center justify-between mb-4">
          <span class="text-sm text-gray-600 dark:text-gray-400">检测到 {{ duplicateResults.length }} 个相似文档</span>
          <span class="text-xs text-gray-400">相似度阈值: 70%</span>
        </div>
        <div class="space-y-3 max-h-80 overflow-y-auto">
          <div
            v-for="result in duplicateResults"
            :key="result.document_id"
            class="flex items-center justify-between p-3 bg-gray-50 dark:bg-dark-700 rounded-lg"
          >
            <div class="flex-1">
              <div class="flex items-center gap-2">
                <FileText class="w-4 h-4 text-gray-400" />
                <span class="text-sm font-medium text-gray-800 dark:text-white">{{ result.filename }}</span>
              </div>
              <p v-if="result.kb_name" class="text-xs text-gray-500 mt-1">{{ result.kb_name }}</p>
            </div>
            <div class="flex items-center gap-3">
              <span
                class="px-2 py-1 text-xs rounded-full"
                :class="result.similarity >= 0.9 ? 'bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400' :
                        result.similarity >= 0.8 ? 'bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400' :
                        'bg-yellow-100 text-yellow-600 dark:bg-yellow-900/30 dark:text-yellow-400'"
              >
                {{ (result.similarity * 100).toFixed(0) }}%
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
    <template #footer>
      <button
        @click="$emit('update:visibleDuplicate', false)"
        class="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
      >
        关闭
      </button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
/**
 * 文档分析对话框组件
 * @description 合并三种文档分析能力：
 *  1) 文档分类（document_type/domain/topics/confidence/summary）；
 *  2) 质量评估（overall_score/overall_grade 及完整性、可读性、结构、相关性四维评分与改进建议）；
 *  3) 重复检测（基于相似度阈值的近似文档列表）。
 * 三类弹窗独立显隐，打开时分别触发对应分析接口。
 *
 * @props visibleClassify - 分类弹窗显隐
 * @props visibleQuality - 质量评估弹窗显隐
 * @props visibleDuplicate - 重复检测弹窗显隐
 * @props doc - 当前操作的文档
 *
 * @emits update:visibleClassify - 分类弹窗显隐变化
 * @emits update:visibleQuality - 质量评估弹窗显隐变化
 * @emits update:visibleDuplicate - 重复检测弹窗显隐变化
 */
import { ref, watch } from 'vue'
import { Tags, Database, Award, Copy, FileText } from '@lucide/vue'
import { api } from '@/utils/axios'
import { useToast } from '@/composables/useToast'
import { getQualityGradeClass, getQualityTextClass } from './utils'
import type { Document } from '@/queries/kb'

const props = defineProps<{
  visibleClassify: boolean
  visibleQuality: boolean
  visibleDuplicate: boolean
  doc: Document | null
}>()

defineEmits<{
  (e: 'update:visibleClassify', value: boolean): void
  (e: 'update:visibleQuality', value: boolean): void
  (e: 'update:visibleDuplicate', value: boolean): void
}>()

const toast = useToast()

// 文档分类分析结果（类型/领域/主题标签/置信度/摘要）
const classifyResult = ref<{
  document_type: string
  document_type_label: string
  topics: string[]
  domain: string
  domain_label: string
  confidence: number
  summary: string
} | null>(null)

// 质量评估结果（总分/等级 + 完整性/可读性/结构/相关性四维评分与评语 + 改进建议）
const qualityResult = ref<{
  overall_score: number
  overall_grade: string
  completeness: number
  completeness_comment: string
  readability: number
  readability_comment: string
  structure: number
  structure_comment: string
  relevance: number
  relevance_comment: string
  suggestions: string[]
} | null>(null)

// 重复检测结果（相似文档 ID/文件名/相似度/所属知识库）
const duplicateResults = ref<{
  document_id: string
  filename: string
  similarity: number
  kb_id: string
  kb_name?: string
}[]>([])

const isClassifying = ref(false)
const isEvaluating = ref(false)
const isDetectingDuplicates = ref(false)

// 各弹窗打开时触发对应分析接口
watch(() => props.visibleClassify, (val) => {
  if (val && props.doc) classifyDocument(props.doc)
})
watch(() => props.visibleQuality, (val) => {
  if (val && props.doc) evaluateQuality(props.doc)
})
watch(() => props.visibleDuplicate, (val) => {
  if (val && props.doc) detectDuplicates(props.doc)
})

/** 对指定文档进行智能分类。 */
async function classifyDocument(doc: Document): Promise<void> {
  isClassifying.value = true
  classifyResult.value = null

  try {
    const response = await api.post<typeof classifyResult.value>(`/documents/${doc.id}/classify`)
    classifyResult.value = response
    toast.success('分类完成')
  } catch (error) {
    toast.error('分类失败', error instanceof Error ? error.message : '未知错误')
  } finally {
    isClassifying.value = false
  }
}

/** 对指定文档进行质量评估。 */
async function evaluateQuality(doc: Document): Promise<void> {
  isEvaluating.value = true
  qualityResult.value = null

  try {
    // 一次性分析操作，无需缓存，故直接调用 api 而未走 query/mutation hook
    const response = await api.post<typeof qualityResult.value>(`/documents/${doc.id}/quality`)
    qualityResult.value = response
    toast.success('质量评估完成')
  } catch (error) {
    toast.error('质量评估失败', error instanceof Error ? error.message : '未知错误')
  } finally {
    isEvaluating.value = false
  }
}

/** 检测与指定文档相似的其他文档。 */
async function detectDuplicates(doc: Document): Promise<void> {
  isDetectingDuplicates.value = true
  duplicateResults.value = []

  try {
    // 一次性分析操作，无需缓存，故直接调用 api 而未走 query/mutation hook
    const response = await api.post<typeof duplicateResults.value>('/documents/duplicate-detect', {
      doc_id: doc.id,
      kb_id: doc.kb_id,
      threshold: 0.7
    })
    duplicateResults.value = response
    if (response.length === 0) {
      toast.info('未检测到重复文档')
    } else {
      toast.success(`检测到 ${response.length} 个相似文档`)
    }
  } catch (error) {
    toast.error('重复检测失败', error instanceof Error ? error.message : '未知错误')
  } finally {
    isDetectingDuplicates.value = false
  }
}
</script>
