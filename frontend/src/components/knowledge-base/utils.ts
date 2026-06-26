/**
 * 知识库模块共享的展示辅助函数。
 *
 * 这些函数原属 KnowledgeBaseView.vue 内部逻辑，拆分后被表格与质量评估弹窗共同使用，
 * 故抽取至此避免重复。日期格式化统一使用 @/utils/format 中的 formatDate。
 */

/** 文档处理状态文本映射。 */
export function getProcessingStatusText(status?: string): string {
  const statusMap: Record<string, string> = {
    pending: '待处理',
    uploading: '上传中',
    processing: '处理中',
    completed: '已完成',
    failed: '失败'
  }
  return statusMap[status || ''] || '未知'
}

/** 文档处理状态样式映射。 */
export function getProcessingStatusClass(status?: string): string {
  const classMap: Record<string, string> = {
    pending: 'bg-gray-100 text-gray-600 dark:bg-dark-600 dark:text-gray-400',
    uploading: 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400',
    processing: 'bg-yellow-100 text-yellow-600 dark:bg-yellow-900/30 dark:text-yellow-400',
    completed: 'bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400',
    failed: 'bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400'
  }
  return classMap[status || ''] || 'bg-gray-100 text-gray-600 dark:bg-dark-600 dark:text-gray-400'
}

/** 质量评分进度条颜色。 */
export function getQualityScoreClass(score?: number): string {
  if (!score) return 'bg-gray-300 dark:bg-dark-500'
  if (score >= 90) return 'bg-green-500'
  if (score >= 80) return 'bg-green-400'
  if (score >= 70) return 'bg-yellow-500'
  if (score >= 60) return 'bg-orange-500'
  return 'bg-red-500'
}

/** 质量评分文本颜色。 */
export function getQualityTextClass(score?: number): string {
  if (!score) return 'text-gray-400'
  if (score >= 90) return 'text-green-600 dark:text-green-400'
  if (score >= 80) return 'text-green-600 dark:text-green-400'
  if (score >= 70) return 'text-yellow-600 dark:text-yellow-400'
  if (score >= 60) return 'text-orange-600 dark:text-orange-400'
  return 'text-red-600 dark:text-red-400'
}

/** 质量等级徽章样式。 */
export function getQualityGradeClass(grade?: string): string {
  const classMap: Record<string, string> = {
    '优秀': 'bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400',
    '良好': 'bg-green-100 text-green-600 dark:bg-green-900/30 dark:text-green-400',
    '中等': 'bg-yellow-100 text-yellow-600 dark:bg-yellow-900/30 dark:text-yellow-400',
    '及格': 'bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400',
    '需改进': 'bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400'
  }
  return classMap[grade || ''] || 'bg-gray-100 text-gray-600 dark:bg-dark-600 dark:text-gray-400'
}
