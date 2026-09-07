import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ReasoningPanel from '../chat/ReasoningPanel.vue'
import type { ReasoningStep } from '@/queries/chat'

function step(overrides: Partial<ReasoningStep>): ReasoningStep {
  return {
    id: overrides.id ?? Math.random().toString(36).slice(2),
    step: 'kb_retrieve',
    status: 'done',
    title: '检索知识库',
    content: '',
    ...overrides
  }
}

describe('ReasoningPanel', () => {
  it('无步骤时不渲染', () => {
    const wrapper = mount(ReasoningPanel, { props: { steps: [] } })
    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('折叠态显示摘要文本', () => {
    const wrapper = mount(ReasoningPanel, {
      props: {
        steps: [
          step({ id: '1', step: 'intent_routing', status: 'running', title: '意图路由', timestamp: 1 }),
          step({ id: '2', step: 'kb_retrieve', status: 'done', title: '检索知识库', timestamp: 2, metadata: { sources_count: 5 } })
        ]
      }
    })
    const summary = wrapper.text()
    expect(summary).toContain('正在意图路由')
    expect(summary).toContain('已检索知识库')
    expect(summary).toContain('命中 5 个片段')
  })

  it('历史消息同 step 重复条目去重渲染（不残留 running 转圈）', () => {
    const wrapper = mount(ReasoningPanel, {
      props: {
        steps: [
          step({ id: 'a', step: 'answer_generate', status: 'running', title: '生成回答', timestamp: 1 }),
          step({ id: 'b', step: 'answer_generate', status: 'done', title: '生成回答', timestamp: 2 })
        ]
      }
    })
    expect(wrapper.text()).toContain('已生成回答')
    expect(wrapper.text()).not.toContain('正在生成回答')
  })

  it('点击展开后显示时间线步骤与耗时', async () => {
    const wrapper = mount(ReasoningPanel, {
      props: {
        steps: [step({ id: '1', title: '检索知识库', content: '命中 3 段', duration_ms: 120, timestamp: 1 })]
      }
    })
    await wrapper.find('button').trigger('click')
    expect(wrapper.text()).toContain('思考过程')
    expect(wrapper.text()).toContain('检索知识库')
    expect(wrapper.text()).toContain('命中 3 段')
    expect(wrapper.text()).toContain('120ms')
  })

  it('failed 步骤正常展示', async () => {
    const wrapper = mount(ReasoningPanel, {
      props: {
        steps: [step({ id: '1', status: 'failed', title: '网页搜索', content: '网络超时', timestamp: 1 })]
      }
    })
    await wrapper.find('button').trigger('click')
    expect(wrapper.text()).toContain('网页搜索')
    expect(wrapper.text()).toContain('网络超时')
  })
})
