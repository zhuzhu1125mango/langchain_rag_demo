import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import MessageSources from '../chat/MessageSources.vue'
import type { MessageSource } from '@/queries/chat'

function source(overrides: Partial<MessageSource>): MessageSource {
  return {
    index: 1,
    source: 'doc-1',
    score: 0.8,
    document_name: '示例文档.pdf',
    chunk_index: 0,
    content: '命中片段内容',
    ...overrides
  }
}

describe('MessageSources 编译页徽标（P4 source_kind 透出）', () => {
  it('source_kind=wiki 时标题旁渲染「编译」徽标并带提示', () => {
    const wrapper = mount(MessageSources, {
      props: { sources: [source({ source_kind: 'wiki' })] }
    })
    const badge = wrapper.find('span[title*="编译的综合页"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('编译')
  })

  it('raw/无 source_kind 的 kb 来源不渲染徽标', () => {
    const wrapper = mount(MessageSources, {
      props: {
        sources: [source({ source_kind: 'raw' }), source({ source: 'doc-2', document_name: 'b.pdf' })]
      }
    })
    expect(wrapper.find('span[title*="编译的综合页"]').exists()).toBe(false)
  })

  it('web 来源不渲染徽标', () => {
    const wrapper = mount(MessageSources, {
      props: { sources: [source({ source_type: 'web', url: 'https://example.com/a', title: '网页' })] }
    })
    expect(wrapper.find('span[title*="编译的综合页"]').exists()).toBe(false)
  })
})
