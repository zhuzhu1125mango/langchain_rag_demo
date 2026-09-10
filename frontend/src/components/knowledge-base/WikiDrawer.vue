<template>
  <el-drawer
    :model-value="visible"
    :title="`${kbName} · Wiki 页面`"
    size="560px"
    @update:model-value="(val: boolean) => emit('update:visible', val)"
  >
    <!-- 工具栏 -->
    <div class="flex items-center justify-between mb-4">
      <p class="text-xs text-gray-500 dark:text-gray-400">
        由文档离线编译生成的补充语料，参与混合检索
      </p>
      <div class="flex items-center gap-2">
        <button
          @click="onLint"
          :disabled="lintLoading || !kbId"
          :class="[
            'flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg transition-colors',
            lintLoading
              ? 'bg-gray-200 dark:bg-dark-700 text-gray-400 cursor-not-allowed'
              : 'text-primary-600 dark:text-primary-400 hover:bg-primary-50 dark:hover:bg-primary-900/20'
          ]"
        >
          <Stethoscope class="w-3.5 h-3.5" :class="{ 'animate-pulse': lintLoading }" />
          <span>{{ lintLoading ? '体检中...' : '体检' }}</span>
        </button>
        <button
          @click="onRebuild"
          :disabled="rebuilding || !kbId"
          :class="[
            'flex items-center gap-1.5 px-3 py-1.5 text-xs rounded-lg transition-colors',
            rebuilding
              ? 'bg-gray-200 dark:bg-dark-700 text-gray-400 cursor-not-allowed'
              : 'text-primary-600 dark:text-primary-400 hover:bg-primary-50 dark:hover:bg-primary-900/20'
          ]"
        >
          <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': rebuilding }" />
          <span>{{ rebuilding ? '重编译中...' : '全量重编译' }}</span>
        </button>
      </div>
    </div>

    <!-- 体检报告（内联展示，只报告不修复） -->
    <div
      v-if="lintReport"
      class="mb-4 border border-gray-200 dark:border-dark-600 rounded-lg p-3"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs font-medium text-gray-700 dark:text-gray-200">
          体检报告 · 共检查 {{ lintReport.checked_pages }} 页
        </p>
        <button
          @click="lintReport = null"
          class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
          aria-label="关闭体检报告"
        >
          <X class="w-3.5 h-3.5" />
        </button>
      </div>
      <p v-if="!lintReport.issues.length" class="text-xs text-emerald-600 dark:text-emerald-400 mt-2">
        未发现问题
      </p>
      <ul v-else class="mt-2 space-y-1.5">
        <li
          v-for="(issue, idx) in lintReport.issues"
          :key="idx"
          class="flex items-start gap-2 text-xs"
        >
          <span :class="['shrink-0 px-1.5 py-0.5 rounded', levelBadgeClass(issue.level)]">
            {{ levelLabel(issue.level) }}
          </span>
          <span class="text-gray-800 dark:text-white shrink-0">{{ issue.title }}</span>
          <span class="text-gray-500 dark:text-gray-400 min-w-0">
            {{ ruleLabel(issue.rule) }}：{{ issue.message }}
          </span>
        </li>
      </ul>
    </div>

    <!-- 重编译进度条 -->
    <div v-if="rebuilding" class="mb-4">
      <div class="h-1.5 bg-gray-200 dark:bg-dark-700 rounded-full overflow-hidden">
        <div
          class="h-full bg-primary-500 transition-all duration-500"
          :style="{ width: rebuildProgress + '%' }"
        ></div>
      </div>
      <p class="text-xs text-gray-500 dark:text-gray-400 mt-1.5">{{ rebuildMessage }}</p>
    </div>

    <!-- 页面列表 -->
    <div v-if="isLoading" class="flex items-center justify-center py-12">
      <Loader2 class="w-6 h-6 text-gray-400 animate-spin" />
    </div>

    <div v-else-if="!pages?.length" class="text-center py-12">
      <BookOpen class="w-10 h-10 text-gray-300 dark:text-gray-600 mx-auto mb-3" />
      <p class="text-sm text-gray-500 dark:text-gray-400">暂无 Wiki 页面</p>
      <p class="text-xs text-gray-400 dark:text-gray-500 mt-1">
        上传文档并开启 WIKI_COMPILE_ENABLED 后自动生成
      </p>
    </div>

    <div v-else class="space-y-2">
      <div
        v-for="page in pages"
        :key="page.id"
        class="border border-gray-200 dark:border-dark-600 rounded-lg overflow-hidden"
      >
        <button
          @click="togglePage(page)"
          class="w-full flex items-center gap-2 px-3 py-2.5 text-left hover:bg-gray-50 dark:hover:bg-dark-700/50 transition-colors"
        >
          <span
            :class="[
              'shrink-0 px-1.5 py-0.5 text-xs rounded',
              typeBadgeClass(page.page_type)
            ]"
          >
            {{ typeLabel(page.page_type) }}
          </span>
          <span class="flex-1 min-w-0 truncate text-sm text-gray-800 dark:text-white">
            {{ page.title }}
          </span>
          <ChevronDown
            class="w-4 h-4 shrink-0 text-gray-400 transition-transform"
            :class="{ 'rotate-180': expandedId === page.id }"
          />
        </button>

        <div v-if="expandedId === page.id" class="px-3 pb-3 border-t border-gray-100 dark:border-dark-700">
          <div class="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-xs text-gray-500 dark:text-gray-400">
            <span>来源文档 {{ page.source_doc_count }} 篇</span>
            <span>v{{ page.revision }}</span>
            <span v-if="page.updated_at">{{ formatDate(page.updated_at) }}</span>
          </div>

          <div v-if="contentLoading" class="flex items-center gap-2 py-4 text-xs text-gray-500">
            <Loader2 class="w-4 h-4 animate-spin" />
            <span>加载正文...</span>
          </div>
          <div
            v-else
            class="text-xs bg-gray-50 dark:bg-dark-900 rounded p-3 max-h-72 overflow-auto markdown-body"
          >
            <MarkdownRenderer :content="expandedContent" />
          </div>
        </div>
      </div>
    </div>
  </el-drawer>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useQueryClient } from '@tanstack/vue-query'
import { ElMessageBox } from 'element-plus'
import { RefreshCw, Loader2, BookOpen, ChevronDown, Stethoscope, X } from '@lucide/vue'
import { buildWsUrl } from '@/utils/ws'
import { useToast } from '@/composables/useToast'
import MarkdownRenderer from '@/components/MarkdownRenderer.vue'
import {
  useWikiPages,
  useRebuildWiki,
  fetchWikiPageContent,
  fetchWikiLint,
  type WikiPageSummary,
  type WikiLintReport
} from '@/queries/kb'

/**
 * Wiki 页面抽屉（LLM-Wiki 编译层 Phase 2）。
 *
 * 展示当前知识库的编译产物（实体页/主题页/索引页），支持单页正文预览与
 * 全量重编译。重编译为后台任务，通过复用上传进度 WebSocket 通道实时跟踪。
 */

const props = defineProps<{
  visible: boolean
  kbId?: string
  kbName?: string
}>()

const emit = defineEmits<{
  (e: 'update:visible', val: boolean): void
}>()

const toast = useToast()
const queryClient = useQueryClient()

const kbIdRef = computed(() => props.kbId)
const { data: pages, isLoading } = useWikiPages(kbIdRef)
const rebuildMutation = useRebuildWiki()

// 正文预览：单页展开 + 懒加载（切换页面时重新加载，内容缓存至本次会话）
const expandedId = ref('')
const expandedContent = ref('')
const contentLoading = ref(false)
const contentCache = ref<Map<string, string>>(new Map())

const rebuilding = ref(false)
const rebuildProgress = ref(0)
const rebuildMessage = ref('')

// 体检（P3）：规则级零 LLM，同步毫秒级
const lintLoading = ref(false)
const lintReport = ref<WikiLintReport | null>(null)

/** 拉取体检报告并内联展示。 */
async function onLint(): Promise<void> {
  if (!props.kbId || lintLoading.value) return
  lintLoading.value = true
  try {
    lintReport.value = await fetchWikiLint(props.kbId)
  } catch (error) {
    toast.error('体检失败', error instanceof Error ? error.message : '未知错误')
  } finally {
    lintLoading.value = false
  }
}

/** 体检级别中文标签。 */
function levelLabel(level: string): string {
  const labels: Record<string, string> = { error: '错误', warning: '警告', info: '提示' }
  return labels[level] || level
}

/** 体检级别徽标配色。 */
function levelBadgeClass(level: string): string {
  const classes: Record<string, string> = {
    error: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
    warning: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
    info: 'bg-gray-100 text-gray-600 dark:bg-dark-700 dark:text-gray-300'
  }
  return classes[level] || classes.info!
}

/** 体检规则中文标签。 */
function ruleLabel(rule: string): string {
  const labels: Record<string, string> = {
    broken_link: '断链',
    orphan_page: '孤立页',
    missing_in_index: '目录缺失',
    empty_source: '空源页',
    abnormal_size: '体量异常'
  }
  return labels[rule] || rule
}

/** 切换页面展开态并懒加载正文。 */
async function togglePage(page: WikiPageSummary): Promise<void> {
  if (expandedId.value === page.id) {
    expandedId.value = ''
    return
  }
  expandedId.value = page.id

  const cached = contentCache.value.get(page.id)
  if (cached !== undefined) {
    expandedContent.value = cached
    return
  }

  contentLoading.value = true
  expandedContent.value = ''
  try {
    const data = await fetchWikiPageContent(props.kbId!, page.id)
    contentCache.value.set(page.id, data.content)
    // 防止用户快速切换页面后旧响应覆盖新展开页
    if (expandedId.value === page.id) {
      expandedContent.value = data.content
    }
  } catch (error) {
    toast.error('正文加载失败', error instanceof Error ? error.message : '未知错误')
  } finally {
    contentLoading.value = false
  }
}

/** 二次确认后提交全量重编译并建立 WS 进度跟踪。 */
async function onRebuild(): Promise<void> {
  if (!props.kbId || rebuilding.value) return

  try {
    await ElMessageBox.confirm(
      '将清理既有 Wiki 页面并按全部文档重新编译，耗时较长，期间检索仍可用。确定继续？',
      '全量重编译确认',
      { confirmButtonText: '重编译', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }

  try {
    const result = await rebuildMutation.mutateAsync(props.kbId)
    rebuilding.value = true
    rebuildProgress.value = 0
    rebuildMessage.value = result.message
    trackRebuildProgress(result.upload_id)
  } catch (error) {
    toast.error('重编译提交失败', error instanceof Error ? error.message : '未知错误')
  }
}

/**
 * 建立进度 WebSocket 连接跟踪重编译任务。
 *
 * 复用上传进度通道（后端 create_upload_progress/update_upload_progress 同源），
 * 连接后服务端先推送当前快照，终态（completed/failed）后关闭并刷新列表。
 */
function trackRebuildProgress(uploadId: string): void {
  const apiKey = localStorage.getItem('api_key') || import.meta.env.VITE_API_KEY
  const token = localStorage.getItem('token')
  const ws = new WebSocket(buildWsUrl(`/api/documents/upload/progress/ws/${uploadId}`))

  let finished = false

  ws.onopen = () => {
    // 首帧鉴权：api_key 不走 URL query（避免进反向代理访问日志）
    ws.send(JSON.stringify({ type: 'auth', token: token || undefined, api_key: apiKey || '' }))
  }

  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data)
      if (data.type === 'auth_ok') return
      if (data.progress !== undefined) {
        rebuildProgress.value = Math.round(data.progress)
      }
      if (data.message) {
        rebuildMessage.value = data.message
      }
      if (data.status === 'completed' || data.status === 'failed') {
        finished = true
        rebuilding.value = false
        try { ws.close() } catch { /* 忽略关闭错误 */ }
        if (data.status === 'completed') {
          toast.success('Wiki 重编译完成', rebuildMessage.value)
          queryClient.invalidateQueries({ queryKey: ['wiki_pages'] })
        } else {
          toast.error('Wiki 重编译失败', rebuildMessage.value)
        }
      }
    } catch {
      // 忽略解析错误
    }
  }

  ws.onclose = () => {
    // 未达终态即断开（如终态记录 60s 清理导致连接被服务端关闭）：
    // 保守恢复交互并刷新一次列表
    if (!finished) {
      rebuilding.value = false
      queryClient.invalidateQueries({ queryKey: ['wiki_pages'] })
    }
  }
}

/** 页面类型中文标签。 */
function typeLabel(pageType: string): string {
  const labels: Record<string, string> = { entity: '实体', topic: '主题', index: '索引' }
  return labels[pageType] || pageType
}

/** 页面类型徽标配色。 */
function typeBadgeClass(pageType: string): string {
  const classes: Record<string, string> = {
    entity: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
    topic: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300',
    index: 'bg-gray-100 text-gray-600 dark:bg-dark-700 dark:text-gray-300'
  }
  return classes[pageType] || classes.index!
}

/** 格式化更新时间。 */
function formatDate(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

// 切换知识库或关闭抽屉时重置预览状态
watch(() => [props.kbId, props.visible], () => {
  expandedId.value = ''
  expandedContent.value = ''
  lintReport.value = null
})
</script>
