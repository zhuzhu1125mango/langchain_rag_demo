import { describe, it, expect } from 'vitest'
import { mergeReasoningSteps } from '../reasoning'
import type { ReasoningStep } from '@/queries/chat'

function step(overrides: Partial<ReasoningStep>): ReasoningStep {
  return {
    id: overrides.id ?? Math.random().toString(36).slice(2),
    step: 'kb_retrieve',
    status: 'running',
    title: '检索知识库',
    content: '',
    ...overrides
  }
}

describe('mergeReasoningSteps', () => {
  it('新 step 追加到末尾', () => {
    const existing = [step({ step: 'intent_routing', title: '意图路由' })]
    const incoming = [step({ step: 'kb_retrieve', title: '检索知识库' })]
    const merged = mergeReasoningSteps(existing, incoming)
    expect(merged.map(s => s.step)).toEqual(['intent_routing', 'kb_retrieve'])
  })

  it('同一 step 的新事件覆盖旧条目（id→step 修复回归测试）', () => {
    const existing = [
      step({ id: 'old-id', step: 'answer_generate', status: 'running', title: '生成回答' })
    ]
    const incoming = [
      step({ id: 'new-id', step: 'answer_generate', status: 'done', title: '生成回答' })
    ]
    const merged = mergeReasoningSteps(existing, incoming)
    expect(merged).toHaveLength(1)
    expect(merged[0]?.id).toBe('new-id')
    expect(merged[0]?.status).toBe('done')
  })

  it('web_search 的搜索中→搜索完成正确覆盖且不产生重复', () => {
    const merged = mergeReasoningSteps(
      [step({ id: 'a', step: 'web_search', status: 'running', title: '搜索中' })],
      [step({ id: 'b', step: 'web_search', status: 'done', title: '搜索完成' })]
    )
    expect(merged).toHaveLength(1)
    expect(merged[0]?.title).toBe('搜索完成')
  })

  it('合并时保留既有顺序、新增按插入顺序追加', () => {
    const merged = mergeReasoningSteps(
      [
        step({ id: '1', step: 'intent_routing' }),
        step({ id: '2', step: 'kb_retrieve' })
      ],
      [
        step({ id: '3', step: 'kb_retrieve' }),
        step({ id: '4', step: 'answer_generate' })
      ]
    )
    expect(merged.map(s => s.step)).toEqual(['intent_routing', 'kb_retrieve', 'answer_generate'])
  })

  it('空入参安全', () => {
    expect(mergeReasoningSteps([], [])).toEqual([])
    expect(mergeReasoningSteps([], [step({ id: 'x' })])).toHaveLength(1)
    expect(mergeReasoningSteps([step({ id: 'x' })], [])).toHaveLength(1)
  })
})
