<template>
  <div class="markdown-body" v-html="renderedContent"></div>
</template>

<script setup lang="ts">
/**
 * Markdown 渲染组件
 * @description 基于 markdown-it 渲染 Markdown 文本为 HTML 并绑定到 v-html；
 * 集成 highlight.js 对代码块做语法高亮，并启用链接自动识别与排版优化。
 *
 * @props content - 待渲染的 Markdown 原始字符串
 */
import { computed, onMounted, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'

const props = defineProps<{
  content: string
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
})

const renderedContent = computed(() => {
  if (!md.value) return ''
  return md.value.render(props.content)
})
</script>