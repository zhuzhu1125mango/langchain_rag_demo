<template>
  <div v-if="dedupedSources.length" class="space-y-1.5">
    <!-- 标题栏：图标 + 标题 + 来源数量徽章 -->
    <div class="flex items-center gap-1.5 mb-1">
      <BookOpen class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />
      <span class="text-xs font-medium text-gray-600 dark:text-gray-300">参考来源</span>
      <span class="text-xs px-1.5 py-0.5 rounded-full bg-gray-100 text-gray-500 dark:bg-dark-700 dark:text-gray-400">
        {{ dedupedSources.length }}
      </span>
    </div>

    <!-- 来源卡片列表 -->
    <div class="space-y-1.5">
      <div
        v-for="source in visibleSources"
        :key="getSourceKey(source)"
        class="flex items-start gap-2 p-2 border rounded-lg cursor-pointer transition-all duration-150 bg-gray-50 border-gray-200 hover:-translate-y-0.5 hover:shadow-md hover:border-primary-400 dark:bg-dark-800 dark:border-dark-600 dark:hover:border-primary-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
        role="button"
        tabindex="0"
        @click="emit('navigate', source)"
        @keydown.enter="emit('navigate', source)"
      >
        <!-- 编号徽章 -->
        <span class="flex-shrink-0 w-5 h-5 inline-flex items-center justify-center text-xs font-semibold rounded-full bg-primary-50 text-primary-600 dark:bg-primary-900/40 dark:text-primary-300 mt-0.5">
          {{ source.index }}
        </span>

        <!-- 来源类型图标 / favicon：web 优先显示网站 favicon，加载失败回退到 Globe 图标 -->
        <div class="flex-shrink-0 mt-0.5 flex items-center text-gray-500 dark:text-gray-400">
          <img
            v-if="source.source_type === 'web' && source.url && !failedFaviconUrls.includes(source.url)"
            :src="getFaviconUrl(source.url)"
            alt=""
            class="w-4 h-4 rounded-sm"
            @error="onFaviconError(source.url)"
          />
          <Globe v-else-if="source.source_type === 'web'" class="w-4 h-4" />
          <FileText v-else class="w-4 h-4" />
        </div>

        <!-- 主体内容：标题（+编译页徽标） + 副标题 + 命中片段预览 -->
        <div class="flex-1 min-w-0">
          <p class="text-xs font-medium text-gray-800 dark:text-gray-200 flex items-center gap-1">
            <span class="truncate">{{ getSourceTitle(source) }}</span>
            <span
              v-if="source.source_kind === 'wiki'"
              class="flex-shrink-0 px-1 py-px rounded bg-violet-100 text-violet-700 dark:bg-violet-900/50 dark:text-violet-300"
              title="该条来自 LLM 编译的综合页，非原文"
            >
              编译
            </span>
          </p>
          <p class="text-xs text-gray-500 dark:text-gray-400 truncate mt-0.5">
            {{ getSourceSubtitle(source) }}
          </p>
          <p
            v-if="getSourceSnippet(source)"
            class="text-xs text-gray-600 dark:text-gray-400 mt-1 line-clamp-2 leading-snug"
          >
            {{ getSourceSnippet(source) }}
          </p>
        </div>

        <!-- 相关性分数（仅 kb 来源且 score > 0 时显示，按分数高低分色） -->
        <span
          v-if="source.source_type !== 'web' && source.score > 0"
          class="flex-shrink-0 text-xs font-semibold px-1.5 py-0.5 rounded mt-0.5"
          :class="getScoreClass(source.score)"
        >
          {{ Math.round(source.score * 100) }}%
        </span>
      </div>
    </div>

    <!-- 折叠/展开按钮：来源数超过阈值时显示 -->
    <button
      v-if="dedupedSources.length > collapseThreshold"
      type="button"
      class="flex items-center justify-center gap-1 w-full mt-1.5 py-1.5 text-xs text-gray-500 dark:text-gray-400 rounded-md transition-colors hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-dark-700 dark:hover:text-gray-300"
      @click="expanded = !expanded"
    >
      <ChevronDown class="w-3 h-3 transition-transform" :class="expanded ? 'rotate-180' : ''" />
      <span>{{ expanded ? '收起' : `展开剩余 ${dedupedSources.length - collapseThreshold} 个来源` }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
/**
 * 消息来源展示组件
 * @description 参考主流模型（ChatGPT/Perplexity/Kimi）的来源展示样式，以浅色卡片形式
 * 展示在助手气泡外部。支持编号徽章、来源类型图标（web favicon / 文档图标）、
 * 命中片段预览、相关性分数可视化、来源去重、超过阈值折叠展开、深色模式与键盘可访问。
 *
 * @props sources - 当前消息的来源列表（来自 SSE end 事件或历史会话加载）
 * @emits navigate - 点击来源卡片时触发，携带来源元数据，由父组件决定跳转网页或打开文档弹窗
 */
import { ref, computed } from 'vue'
import { BookOpen, Globe, FileText, ChevronDown } from '@lucide/vue'
import type { MessageSource } from '@/queries/chat'

const props = defineProps<{
  sources: MessageSource[]
}>()

const emit = defineEmits<{
  (e: 'navigate', source: MessageSource): void
}>()

/** 折叠阈值：来源数超过此值时默认只显示前 N 个，其余折叠 */
const collapseThreshold = 3

/** 是否已展开全部来源 */
const expanded = ref(false)

/** favicon 加载失败的 URL 列表，用于回退到默认 Globe 图标 */
const failedFaviconUrls = ref<string[]>([])

/** favicon 加载失败时记录 URL，触发响应式更新回退到默认图标 */
function onFaviconError(url: string): void {
  if (!failedFaviconUrls.value.includes(url)) {
    failedFaviconUrls.value = [...failedFaviconUrls.value, url]
  }
}

/** 按规则去重：web 按 url，kb 按 document_id+chunk_index，回退按 source。保持原顺序。 */
const dedupedSources = computed<MessageSource[]>(() => {
  const seen = new Set<string>()
  const result: MessageSource[] = []
  for (const s of props.sources) {
    const key = getSourceKey(s)
    if (seen.has(key)) continue
    seen.add(key)
    result.push(s)
  }
  return result
})

/** 当前可见的来源列表：折叠时只显示前 threshold 个，展开时显示全部 */
const visibleSources = computed(() => {
  if (expanded.value) return dedupedSources.value
  return dedupedSources.value.slice(0, collapseThreshold)
})

/** 获取来源唯一 key，用于去重和 v-for 的 key */
function getSourceKey(source: MessageSource): string {
  if (source.source_type === 'web' && source.url) return `web:${source.url}`
  if (source.document_id) return `kb:${source.document_id}:${source.chunk_index ?? 0}`
  return `kb:${source.source}:${source.chunk_index ?? 0}`
}

/** 获取 favicon URL（Google S2 服务，在线环境有效，离线或加载失败时回退到默认图标） */
function getFaviconUrl(url: string): string {
  try {
    const domain = new URL(url).hostname
    return `https://www.google.com/s2/favicons?domain=${domain}&sz=32`
  } catch {
    return ''
  }
}

/** 获取来源标题：web 取 title，kb 取 document_name */
function getSourceTitle(source: MessageSource): string {
  if (source.source_type === 'web') {
    return source.title || source.document_name || '网页来源'
  }
  return source.document_name || source.title || '未知文档'
}

/** 获取来源副标题：web 显示域名，kb 显示 "第 N/M 段" */
function getSourceSubtitle(source: MessageSource): string {
  if (source.source_type === 'web' && source.url) {
    try {
      return new URL(source.url).hostname.replace(/^www\./, '')
    } catch {
      return source.url
    }
  }
  const chunkIdx = source.chunk_index ?? source.page
  if (chunkIdx !== undefined && chunkIdx !== null) {
    const total = source.total_chunks
    // chunk_index 通常是 0-based，展示时转为 1-based
    return total && total > 1 ? `第 ${chunkIdx + 1}/${total} 段` : `第 ${chunkIdx + 1} 段`
  }
  return ''
}

/** 获取命中片段预览：去除多余空白后截断 80 字，超过显示省略号 */
function getSourceSnippet(source: MessageSource): string {
  const content = source.content
  if (!content) return ''
  const trimmed = content.replace(/\s+/g, ' ').trim()
  if (trimmed.length <= 80) return trimmed
  return trimmed.slice(0, 80) + '...'
}

/** 相关性分数颜色分级：>=0.8 绿色（高相关），>=0.5 黄色（中相关），<0.5 灰色（低相关） */
function getScoreClass(score: number): string {
  if (score >= 0.8) {
    return 'bg-green-100 text-green-700 dark:bg-green-900/60 dark:text-green-300'
  }
  if (score >= 0.5) {
    return 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/60 dark:text-yellow-300'
  }
  return 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400'
}
</script>
