<template>
  <div
    class="markdown-body"
    v-html="renderedContent"
    @click="onContentClick"
    @keydown="onContentKeydown"
  ></div>
</template>

<script setup lang="ts">
/**
 * Markdown 渲染组件
 * @description 基于 markdown-it 渲染 Markdown 文本为 HTML 并绑定到 v-html；
 * 集成 highlight.js 对代码块做语法高亮，并启用链接自动识别与排版优化。
 *
 * 当传入 sources（kb 知识库来源）时，会对答案正文按段落做来源指纹匹配，
 * 在命中段落的末尾插入上标引用标记 `[N]`，点击上标触发 cite-click 事件，
 * 由父组件复用来源卡片的点击逻辑（打开文档详情弹窗）。
 *
 * @props content - 待渲染的 Markdown 原始字符串
 * @props sources - 可选，当前消息的来源列表（仅 kb 来源参与内联引用匹配）
 * @emits cite-click - 点击内联引用上标时触发，携带来源序号（index，从 1 开始）
 */
import { computed, onMounted, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import DOMPurify from 'dompurify'
import type { MessageSource } from '@/queries/chat'

const props = defineProps<{
  content: string
  sources?: MessageSource[]
}>()

const emit = defineEmits<{
  (e: 'cite-click', sourceIndex: number): void
}>()

const md = ref<MarkdownIt | null>(null)

/**
 * 代码高亮处理：作为 markdown-it 的 highlight 选项传入。
 * 优先按指定语言高亮；语言未注册或高亮抛错时回退到自动检测语言高亮。
 */
const highlight = (str: string, lang: string): string => {
  if (lang && hljs.getLanguage(lang)) {
    try {
      return hljs.highlight(str, { language: lang }).value
    } catch {
    }
  }
  return hljs.highlightAuto(str).value
}

onMounted(() => {
  md.value = new MarkdownIt({
    highlight,
    html: false,
    linkify: true,
    typographer: true
  })

  // 为所有链接添加安全属性，并过滤非 http/https scheme
  DOMPurify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      const href = node.getAttribute('href') || ''
      const schemeMatch = href.match(/^(\w+):/)
      if (schemeMatch && !['http:', 'https:'].includes(schemeMatch[1]?.toLowerCase() ?? '')) {
        node.removeAttribute('href')
        node.setAttribute('role', 'link')
        node.setAttribute('aria-disabled', 'true')
      } else if (href) {
        node.setAttribute('target', '_blank')
        node.setAttribute('rel', 'noopener noreferrer')
      }
    }
  })
})

const renderedContent = computed(() => {
  if (!md.value) return ''
  let html = md.value.render(props.content)
  // 存在 kb 来源时，尝试在正文中注入内联引用上标
  if (props.sources?.length) {
    html = injectCitations(html, props.sources)
  }
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [
      'p', 'br', 'hr',
      'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
      'ul', 'ol', 'li',
      'strong', 'b', 'em', 'i', 'code', 'pre',
      'a', 'blockquote',
      // sup：内联引用上标（cite-ref）；span：highlight.js 代码高亮节点
      'sup', 'span',
      'table', 'thead', 'tbody', 'tr', 'th', 'td'
    ]
    // 属性白名单走 DOMPurify 默认集（ALLOWED_ATTR 仅支持数组形式的严格白名单，
    // 按标签配置的对象写法会被运行时静默忽略，等于默认行为，故不显式传入）
  })
})

/**
 * 在渲染后的 HTML 中注入内联引用上标。
 *
 * 匹配策略（简化版，宁可不做也不错标）：
 * 1. 仅处理 kb 来源，取其 content 前 20 字（去空白）作为指纹
 * 2. 按 <p> 段落顺序，依次尝试匹配待匹配的来源指纹
 * 3. 段落纯文本包含当前指纹时，在 </p> 前插入 <sup>[N]</sup>，并推进到下一个来源
 * 4. 不匹配则保留原段落，下一段继续尝试同一来源
 *
 * 这样保证每段最多一个引用、来源按序匹配，避免错标。
 */
function injectCitations(html: string, sources: MessageSource[]): string {
  const kbSources = sources.filter(s => s.source_type !== 'web' && s.content)
  if (kbSources.length === 0) return html

  // 构建指纹列表，过滤掉过短的内容（<10 字不参与匹配，避免误匹配）
  const fingerprints = kbSources
    .map(s => ({
      index: s.index || 1,
      fingerprint: (s.content || '').replace(/\s+/g, '').trim().slice(0, 20)
    }))
    .filter(f => f.fingerprint.length >= 10)

  if (fingerprints.length === 0) return html

  const parts: string[] = []
  let lastIdx = 0
  const pPattern = /<p>([\s\S]*?)<\/p>/g
  let match: RegExpExecArray | null
  let fpIdx = 0

  while ((match = pPattern.exec(html)) !== null) {
    // 保留 <p> 之前的非段落内容（如 <h1>、<ul> 等）
    parts.push(html.slice(lastIdx, match.index))

    const inner = match[1] ?? ''
    // 提取段落纯文本用于指纹匹配（去标签和空白）
    const text = inner.replace(/<[^>]+>/g, '').replace(/\s+/g, '')

    const fp = fpIdx < fingerprints.length ? fingerprints[fpIdx] : undefined
    if (fp && text.includes(fp.fingerprint)) {
      parts.push(`<p>${inner}<sup class="cite-ref" data-cite-index="${fp.index}" tabindex="0" role="button" aria-label="引用来源 ${fp.index}">[${fp.index}]</sup></p>`)
      fpIdx++
    } else {
      parts.push(match[0])
    }
    lastIdx = match.index + match[0].length
  }
  parts.push(html.slice(lastIdx))
  return parts.join('')
}

/**
 * 内容区域点击事件委托：点击内联引用上标时，提取 data-cite-index 并触发 cite-click。
 */
function onContentClick(e: MouseEvent): void {
  const target = (e.target as HTMLElement).closest('.cite-ref')
  if (!target) return
  const idx = Number(target.getAttribute('data-cite-index'))
  if (idx) {
    emit('cite-click', idx)
  }
}

/**
 * 内容区域键盘事件委托：按 Enter/Space 时触发内联引用上标的 cite-click。
 */
function onContentKeydown(e: KeyboardEvent): void {
  if (e.key !== 'Enter' && e.key !== ' ') return
  const target = (e.target as HTMLElement).closest('.cite-ref')
  if (!target) return
  e.preventDefault()
  const idx = Number(target.getAttribute('data-cite-index'))
  if (idx) {
    emit('cite-click', idx)
  }
}
</script>

<style scoped>
/* 内联引用上标样式：蓝色小号上标，hover 时加深 */
.cite-ref {
  display: inline-block;
  margin-left: 2px;
  padding: 0 4px;
  font-size: 0.7em;
  line-height: 1;
  color: #3b82f6;
  background-color: rgba(59, 130, 246, 0.1);
  border-radius: 3px;
  cursor: pointer;
  vertical-align: super;
  transition: background-color 0.15s ease;
}

.cite-ref:hover {
  background-color: rgba(59, 130, 246, 0.25);
}

/* 助手气泡（蓝底白字）内的引用上标使用浅色适配 */
.chat-bubble-assistant .cite-ref {
  color: #bfdbfe;
  background-color: rgba(255, 255, 255, 0.15);
}

.chat-bubble-assistant .cite-ref:hover {
  background-color: rgba(255, 255, 255, 0.3);
}
</style>
