<template>
  <div class="overflow-x-auto">
    <table class="w-full">
      <thead>
        <tr class="bg-gray-50 dark:bg-dark-700">
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider w-12">
            <input
              type="checkbox"
              @change="$emit('toggle-select-all', $event)"
              :checked="kbStore.selectedCount === documents.length && documents.length > 0"
              class="rounded border-gray-300 dark:border-dark-500 bg-gray-100 dark:bg-dark-700 text-primary-600 focus:ring-primary-500"
            />
          </th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">文件名</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">类型</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">分类</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">文档类型</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">领域</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">标签</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">质量评分</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">处理状态</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">状态</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">上传时间</th>
          <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wider">操作</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-200 dark:divide-dark-600">
        <tr
          v-for="doc in documents"
          :key="doc.id"
          :class="[
            'hover:bg-gray-50 dark:hover:bg-dark-700 transition-colors',
            kbStore.isSelected(doc) ? 'bg-primary-50 dark:bg-primary-900/20' : ''
          ]"
        >
          <td class="px-4 py-4">
            <input
              type="checkbox"
              @change="kbStore.selectDocument(doc)"
              :checked="kbStore.isSelected(doc)"
              class="rounded border-gray-300 dark:border-dark-500 bg-gray-100 dark:bg-dark-700 text-primary-600 focus:ring-primary-500"
            />
          </td>
          <td class="px-4 py-4">
            <div class="flex items-center gap-3">
              <FileText class="w-5 h-5 text-gray-400" />
              <span class="text-sm text-gray-800 dark:text-white">{{ doc.filename }}</span>
            </div>
          </td>
          <td class="px-4 py-4">
            <span class="text-sm text-gray-600 dark:text-gray-300">{{ doc.file_type }}</span>
          </td>
          <td class="px-4 py-4">
            <span class="text-sm text-gray-600 dark:text-gray-300">{{ doc.category_name || '未分类' }}</span>
          </td>
          <td class="px-4 py-4">
            <span class="px-2 py-0.5 text-xs bg-blue-100 dark:bg-blue-900/30 text-blue-600 dark:text-blue-400 rounded">
              {{ doc.document_type_label || '未识别' }}
            </span>
          </td>
          <td class="px-4 py-4">
            <span class="text-sm text-gray-600 dark:text-gray-300">{{ doc.domain_label || '未分类' }}</span>
          </td>
          <td class="px-4 py-4">
            <div class="flex flex-wrap gap-1">
              <span
                v-for="tag in doc.tags"
                :key="tag"
                class="px-2 py-0.5 text-xs bg-gray-100 dark:bg-dark-600 text-gray-600 dark:text-gray-300 rounded"
              >
                {{ tag }}
              </span>
            </div>
          </td>
          <td class="px-4 py-4">
            <div class="flex items-center gap-2">
              <div class="flex items-center">
                <div class="w-16 h-2 bg-gray-200 dark:bg-dark-600 rounded-full overflow-hidden">
                  <div
                    class="h-full rounded-full transition-all"
                    :class="getQualityScoreClass(doc.quality_score)"
                    :style="{ width: (doc.quality_score || 0) + '%' }"
                  ></div>
                </div>
                <span class="ml-1 text-xs font-medium" :class="getQualityTextClass(doc.quality_score)">
                  {{ doc.quality_score || '-' }}
                </span>
              </div>
              <span v-if="doc.quality_grade" class="px-1.5 py-0.5 text-xs rounded" :class="getQualityGradeClass(doc.quality_grade)">
                {{ doc.quality_grade }}
              </span>
            </div>
          </td>
          <td class="px-4 py-4">
            <div class="flex flex-col gap-1">
              <div class="flex items-center gap-2">
                <span
                  :class="[
                    'px-2 py-0.5 text-xs rounded-full',
                    getProcessingStatusClass(doc.processing_status)
                  ]"
                >
                  {{ getProcessingStatusText(doc.processing_status) }}
                </span>
                <span v-if="doc.processing_progress !== undefined && doc.processing_progress < 100" class="text-xs text-gray-500">{{ doc.processing_progress }}%</span>
              </div>
              <div v-if="doc.processing_message" class="text-xs text-gray-500 truncate max-w-48" :title="doc.processing_message">
                {{ doc.processing_message }}
              </div>
            </div>
          </td>
          <td class="px-4 py-4">
            <span
              :class="[
                'px-2 py-1 text-xs rounded-full',
                doc.status === 'active'
                  ? 'bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400'
                  : 'bg-gray-100 text-gray-600 dark:bg-dark-600 dark:text-gray-400'
              ]"
            >
              {{ doc.status === 'active' ? '已上架' : '已下架' }}
            </span>
          </td>
          <td class="px-4 py-4">
            <span class="text-sm text-gray-500 dark:text-gray-400">{{ formatDate(doc.created_at) }}</span>
          </td>
          <td class="px-4 py-4">
            <div class="flex items-center gap-2">
              <button
                @click="$emit('preview', doc)"
                class="p-1.5 text-gray-500 hover:text-primary-600 dark:hover:text-primary-400 hover:bg-gray-100 dark:hover:bg-dark-700 rounded transition-colors"
                title="预览"
              >
                <Eye class="w-4 h-4" />
              </button>
              <button
                @click="$emit('classify', doc)"
                class="p-1.5 text-gray-500 hover:text-blue-600 dark:hover:text-blue-400 hover:bg-gray-100 dark:hover:bg-dark-700 rounded transition-colors"
                title="分类"
              >
                <Tags class="w-4 h-4" />
              </button>
              <button
                @click="$emit('quality', doc)"
                class="p-1.5 text-gray-500 hover:text-purple-600 dark:hover:text-purple-400 hover:bg-gray-100 dark:hover:bg-dark-700 rounded transition-colors"
                title="质量评估"
              >
                <Award class="w-4 h-4" />
              </button>
              <button
                @click="$emit('duplicate-detect', doc)"
                class="p-1.5 text-gray-500 hover:text-orange-600 dark:hover:text-orange-400 hover:bg-gray-100 dark:hover:bg-dark-700 rounded transition-colors"
                title="检测重复"
              >
                <Copy class="w-4 h-4" />
              </button>
              <button
                @click="$emit('toggle-status', doc)"
                class="p-1.5 text-gray-500 hover:text-primary-600 dark:hover:text-primary-400 hover:bg-gray-100 dark:hover:bg-dark-700 rounded transition-colors"
                :title="doc.status === 'active' ? '下架' : '上架'"
              >
                <ToggleLeft v-if="doc.status === 'active'" class="w-4 h-4" />
                <ToggleRight v-else class="w-4 h-4" />
              </button>
              <button
                v-if="doc.processing_status === 'failed'"
                @click="$emit('reprocess', doc)"
                class="p-1.5 text-gray-500 hover:text-green-600 dark:hover:text-green-400 hover:bg-gray-100 dark:hover:bg-dark-700 rounded transition-colors"
                title="重新处理"
              >
                <RotateCcw class="w-4 h-4" />
              </button>
              <button
                @click="$emit('delete', doc)"
                class="p-1.5 text-gray-500 hover:text-red-600 dark:hover:text-red-400 hover:bg-gray-100 dark:hover:bg-dark-700 rounded transition-colors"
                title="删除"
              >
                <Trash2 class="w-4 h-4" />
              </button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
/**
 * 文档表格组件
 * @description 展示当前知识库下的文档列表，支持全选/单选、质量评分/处理状态展示，
 * 以及预览、分类、质量评估、重复检测、上下架、重新处理、删除等行内操作。
 * 文档选择状态保留在 kb store 中，表格直接读写以避免 prop 透传。
 *
 * @props documents - 文档列表
 *
 * @emits toggle-select-all - 全选/取消全选
 * @emits preview - 预览文档
 * @emits classify - 文档分类
 * @emits quality - 质量评估
 * @emits duplicate-detect - 重复检测
 * @emits toggle-status - 上架/下架切换
 * @emits reprocess - 重新处理（仅失败状态可用）
 * @emits delete - 删除文档
 */
import { FileText, Eye, Tags, Award, Copy, ToggleLeft, ToggleRight, RotateCcw, Trash2 } from '@lucide/vue'
import { useKBStore } from '@/stores/kb'
import type { Document } from '@/queries/kb'
import { formatDate } from '@/utils/format'
import {
  getProcessingStatusText,
  getProcessingStatusClass,
  getQualityScoreClass,
  getQualityTextClass,
  getQualityGradeClass
} from './utils'

defineProps<{
  documents: Document[]
}>()

defineEmits<{
  (e: 'toggle-select-all', event: Event): void
  (e: 'preview', doc: Document): void
  (e: 'classify', doc: Document): void
  (e: 'quality', doc: Document): void
  (e: 'duplicate-detect', doc: Document): void
  (e: 'toggle-status', doc: Document): void
  (e: 'reprocess', doc: Document): void
  (e: 'delete', doc: Document): void
}>()

// 文档选择状态保留在 kb store 中，表格直接读写以避免 prop 透传
const kbStore = useKBStore()
</script>
