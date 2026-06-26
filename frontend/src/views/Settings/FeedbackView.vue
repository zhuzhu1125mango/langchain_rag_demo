<template>
  <div class="flex-1 overflow-auto bg-gray-50 dark:bg-dark-900 p-6">
    <div class="max-w-4xl mx-auto space-y-6">
      <div>
        <h1 class="text-xl font-semibold text-gray-800 dark:text-white">评价反馈统计</h1>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">查看用户对问答结果的评价汇总</p>
      </div>

      <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-5">
          <div class="flex items-center gap-3 mb-3">
            <div class="w-10 h-10 rounded-lg bg-blue-100 dark:bg-blue-900/30 flex items-center justify-center">
              <MessageSquare class="w-5 h-5 text-blue-600 dark:text-blue-400" />
            </div>
            <div>
              <p class="text-sm text-gray-500 dark:text-gray-400">总评价数</p>
              <p class="text-xl font-semibold text-gray-800 dark:text-white">{{ stats?.total_count || 0 }}</p>
            </div>
          </div>
        </div>

        <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-5">
          <div class="flex items-center gap-3 mb-3">
            <div class="w-10 h-10 rounded-lg bg-green-100 dark:bg-green-900/30 flex items-center justify-center">
              <ThumbsUp class="w-5 h-5 text-green-600 dark:text-green-400" />
            </div>
            <div>
              <p class="text-sm text-gray-500 dark:text-gray-400">好评数</p>
              <p class="text-xl font-semibold text-gray-800 dark:text-white">{{ stats?.positive_count || 0 }}</p>
            </div>
          </div>
        </div>

        <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-5">
          <div class="flex items-center gap-3 mb-3">
            <div class="w-10 h-10 rounded-lg bg-red-100 dark:bg-red-900/30 flex items-center justify-center">
              <ThumbsDown class="w-5 h-5 text-red-600 dark:text-red-400" />
            </div>
            <div>
              <p class="text-sm text-gray-500 dark:text-gray-400">差评数</p>
              <p class="text-xl font-semibold text-gray-800 dark:text-white">{{ stats?.negative_count || 0 }}</p>
            </div>
          </div>
        </div>

        <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-5">
          <div class="flex items-center gap-3 mb-3">
            <div class="w-10 h-10 rounded-lg bg-yellow-100 dark:bg-yellow-900/30 flex items-center justify-center">
              <Star class="w-5 h-5 text-yellow-600 dark:text-yellow-400" />
            </div>
            <div>
              <p class="text-sm text-gray-500 dark:text-gray-400">平均评分</p>
              <p class="text-xl font-semibold text-gray-800 dark:text-white">{{ (stats?.average_rating || 0).toFixed(2) }}</p>
            </div>
          </div>
        </div>
      </div>

      <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-6">
        <h2 class="font-medium text-gray-800 dark:text-white mb-6">评分分布</h2>
        <div class="flex items-end gap-4 justify-center h-48">
          <div
            v-for="(count, index) in ratingDistribution"
            :key="index"
            class="flex flex-col items-center gap-2 flex-1"
          >
            <div
              class="w-full bg-primary-500 rounded-t-lg transition-all duration-300"
              :style="{
                height: `${(count / Math.max(...ratingDistribution, 1)) * 150}px`,
                minHeight: count > 0 ? '8px' : '4px',
                backgroundColor: getRatingColor(index + 1)
              }"
            ></div>
            <span class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ index + 1 }}星</span>
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ count }}</span>
          </div>
        </div>
      </div>

      <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-6">
        <div class="flex items-center justify-between mb-4">
          <h2 class="font-medium text-gray-800 dark:text-white">评价列表</h2>
          <div class="flex items-center gap-2">
            <select
              v-model="filterRating"
              class="px-3 py-2 border border-gray-300 dark:border-dark-500 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white text-sm focus:outline-none focus:ring-2 focus:ring-primary-500"
            >
              <option value="all">全部评分</option>
              <option :value="1">1星</option>
              <option :value="2">2星</option>
              <option :value="3">3星</option>
              <option :value="4">4星</option>
              <option :value="5">5星</option>
            </select>
          </div>
        </div>
        <div class="space-y-4">
          <div
            v-for="feedback in filteredFeedbacks"
            :key="feedback.id"
            class="p-4 bg-gray-50 dark:bg-dark-700 rounded-lg"
          >
            <div class="flex items-start justify-between">
              <div class="flex-1">
                <div class="flex items-center gap-2 mb-2">
                  <div class="flex">
                    <Star
                      v-for="i in 5"
                      :key="i"
                      class="w-4 h-4"
                      :class="i <= feedback.rating ? 'text-yellow-400 fill-yellow-400' : 'text-gray-300'"
                    />
                  </div>
                  <span class="text-sm text-gray-500 dark:text-gray-400">{{ formatDate(feedback.created_at) }}</span>
                </div>
                <p class="text-sm text-gray-500 dark:text-gray-400">
                  {{ feedback.reason || '暂无评价内容' }}
                </p>
              </div>
              <button
                @click="confirmDelete(feedback)"
                class="p-2 text-gray-400 hover:text-red-500 hover:bg-gray-200 dark:hover:bg-dark-600 rounded-lg transition-colors"
                title="删除评价"
              >
                <Trash2 class="w-4 h-4" />
              </button>
            </div>
          </div>
          <div v-if="filteredFeedbacks.length === 0" class="py-12 text-center text-gray-500 dark:text-gray-400">
            <MessageSquareOff class="w-12 h-12 mx-auto mb-3 opacity-50" />
            <p>暂无评价数据</p>
          </div>
        </div>
      </div>
    </div>

    <el-dialog
      v-model="deleteConfirmVisible"
      title="确认删除"
      width="360px"
    >
      <p class="text-gray-700 dark:text-gray-300">
        确定要删除这条评价吗？
      </p>
      <p class="text-sm text-gray-500 dark:text-gray-400 mt-2">此操作无法撤销。</p>
      <template #footer>
        <button
          @click="deleteConfirmVisible = false"
          class="px-4 py-2 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-600 rounded-lg transition-colors"
        >
          取消
        </button>
        <button
          @click="deleteFeedback"
          :disabled="deleteMutation.isPending.value"
          class="px-4 py-2 bg-red-500 text-white rounded-lg hover:bg-red-600 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          删除
        </button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
/**
 * 反馈管理设置页。
 *
 * 展示评价统计（总数、好评/差评、平均评分）、评分分布与评价列表，
 * 支持按评分筛选与删除评价。
 */
import { ref, computed } from 'vue'
import { MessageSquare, ThumbsUp, ThumbsDown, Star, Trash2, MessageSquareOff } from '@lucide/vue'
import { useFeedbackStats, useFeedbackList, useDeleteFeedback, type FeedbackItem } from '@/queries/kb'
import { useDeleteConfirm } from '@/composables/useCrudModal'
import { formatDate } from '@/utils/format'
import { useToast } from '@/composables/useToast'

const toast = useToast()

const { data: stats } = useFeedbackStats()
const { data: feedbackList } = useFeedbackList()
const deleteMutation = useDeleteFeedback()

const filterRating = ref<string>('all')

const {
  deleteConfirmVisible,
  deletingItem: deletingFeedback,
  confirmDelete,
  closeDeleteConfirm
} = useDeleteConfirm<FeedbackItem>()

const ratingDistribution = computed(() => {
  const counts = [0, 0, 0, 0, 0]
  const list = feedbackList.value
  if (!list) return counts
  for (let i = 0; i < list.length; i++) {
    const fb = list[i]
    if (!fb) continue
    const rating = fb.rating
    if (rating === undefined || rating < 1 || rating > 5) continue
    const idx = rating - 1
    counts[idx]!++
  }
  return counts
})

const filteredFeedbacks = computed(() => {
  if (!feedbackList.value) return []
  if (filterRating.value === 'all') return feedbackList.value
  return feedbackList.value.filter(fb => fb.rating === Number(filterRating.value))
})

/** 根据评分（1-5）返回对应颜色，越低越偏红、越高越偏绿，越界返回灰色。 */
function getRatingColor(rating: number): string {
  const colors = [
    '#ef4444',
    '#f97316',
    '#eab308',
    '#22c55e',
    '#10b981'
  ]
  return colors[rating - 1] || '#6b7280'
}

/** 确认删除当前选中评价，成功后关闭确认框。 */
async function deleteFeedback(): Promise<void> {
  if (!deletingFeedback.value) return

  try {
    await deleteMutation.mutateAsync(deletingFeedback.value.id)
    toast.success('评价删除成功')
    closeDeleteConfirm()
  } catch {
    toast.error('删除失败，请重试')
  }
}
</script>
