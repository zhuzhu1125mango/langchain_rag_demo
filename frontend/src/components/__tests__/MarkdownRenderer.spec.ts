import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import MarkdownRenderer from '../MarkdownRenderer.vue'
import type { MessageSource } from '@/queries/chat'

describe('MarkdownRenderer', () => {
  it('渲染标题与加粗', async () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '# 标题\n\nHello **world**' }
    })
    await wrapper.vm.$nextTick()
    const html = wrapper.html()
    expect(html).toContain('标题')
    expect(html).toContain('<h1>')
    expect(html).toContain('<strong>world</strong>')
  })

  it('XSS 净化：script 标签不输出', async () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '<script>alert(1)</script>正常文本' }
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.html()).not.toContain('<script')
    expect(wrapper.html()).toContain('正常文本')
  })

  it('代码块渲染 pre/code 并带高亮 class', async () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '```js\nconst a = 1\n```' }
    })
    await wrapper.vm.$nextTick()
    const html = wrapper.html()
    expect(html).toContain('<pre')
    expect(html).toContain('<code')
    expect(html).toContain('hljs')
  })

  it('kb 来源命中段落时注入内联引用上标并触发 cite-click', async () => {
    const sources: MessageSource[] = [
      {
        source: 'doc1',
        score: 0.9,
        source_type: 'kb',
        index: 1,
        content: 'RAG 是检索增强生成技术，用于知识库问答'
      }
    ]
    const wrapper = mount(MarkdownRenderer, {
      props: {
        content: 'RAG 是检索增强生成技术，用于知识库问答系统。',
        sources
      }
    })
    await wrapper.vm.$nextTick()
    const sup = wrapper.find('sup.cite-ref')
    expect(sup.exists()).toBe(true)
    expect(sup.attributes('data-cite-index')).toBe('1')

    await sup.trigger('click')
    expect(wrapper.emitted('cite-click')?.[0]).toEqual([1])
  })

  it('纯文本无来源时不注入引用', async () => {
    const wrapper = mount(MarkdownRenderer, {
      props: { content: '普通回答' }
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.find('sup.cite-ref').exists()).toBe(false)
  })
})
