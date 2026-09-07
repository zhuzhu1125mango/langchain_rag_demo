import { describe, it, expect, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { useChatStore } from '../chat'
import type { Message, Session } from '@/queries/chat'

function fakeSession(id: string): Session {
  return { id, title: `会话${id}`, created_at: '2026-09-06T00:00:00Z', updated_at: '2026-09-06T00:00:00Z' }
}

function fakeMessage(id: string, overrides: Partial<Message> = {}): Message {
  return { id, role: 'assistant', content: `内容${id}`, ...overrides }
}

describe('useChatStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('setCurrentSession 设置会话并清空消息', () => {
    const store = useChatStore()
    store.addMessage(fakeMessage('m1'))
    store.setCurrentSession(fakeSession('s1'))
    expect(store.currentSession?.id).toBe('s1')
    expect(store.messages).toHaveLength(0)
    store.setCurrentSession(null)
    expect(store.currentSession).toBeNull()
  })

  it('updateCurrentSessionTitle 仅对已选中会话生效', () => {
    const store = useChatStore()
    store.updateCurrentSessionTitle('新标题')
    expect(store.currentSession).toBeNull()
    store.setCurrentSession(fakeSession('s1'))
    store.updateCurrentSessionTitle('新标题')
    expect(store.currentSession?.title).toBe('新标题')
  })

  it('addMessage / updateMessage 按 ID 更新指定字段', () => {
    const store = useChatStore()
    store.addMessage(fakeMessage('m1'))
    store.updateMessage('m1', { content: '更新后', isLoading: false })
    expect(store.messages[0]?.content).toBe('更新后')
    expect(store.messages[0]?.isLoading).toBe(false)
    // id/role 不可通过 updates 修改（类型层约束，运行时同字段合并安全）
  })

  it('updateMessage 对不存在的 ID 无副作用', () => {
    const store = useChatStore()
    store.addMessage(fakeMessage('m1'))
    store.updateMessage('nope', { content: 'x' })
    expect(store.messages[0]?.content).toBe('内容m1')
  })

  it('setMessages / clearMessages 替换与清空', () => {
    const store = useChatStore()
    store.setMessages([fakeMessage('a'), fakeMessage('b')])
    expect(store.messages).toHaveLength(2)
    store.clearMessages()
    expect(store.messages).toHaveLength(0)
  })

  it('addQuestionToHistory 去重并置顶', () => {
    const store = useChatStore()
    store.addQuestionToHistory('q1')
    store.addQuestionToHistory('q2')
    store.addQuestionToHistory('q1')
    expect(store.questionHistory.map(i => i.question)).toEqual(['q1', 'q2'])
  })

  it('questionHistory 上限 20 条', () => {
    const store = useChatStore()
    for (let i = 0; i < 25; i++) {
      store.addQuestionToHistory(`q${i}`)
    }
    expect(store.questionHistory).toHaveLength(20)
    // 最新在顶部
    expect(store.questionHistory[0]?.question).toBe('q24')
  })

  it('removeQuestionFromHistory / clearQuestionHistory', () => {
    const store = useChatStore()
    store.addQuestionToHistory('q1')
    const id = store.questionHistory[0]!.id
    store.removeQuestionFromHistory(id)
    expect(store.questionHistory).toHaveLength(0)
    store.addQuestionToHistory('q2')
    store.clearQuestionHistory()
    expect(store.questionHistory).toHaveLength(0)
  })

  it('setTyping / setCurrentQuestion 更新状态', () => {
    const store = useChatStore()
    store.setTyping(true)
    store.setCurrentQuestion('你好')
    expect(store.isTyping).toBe(true)
    expect(store.currentQuestion).toBe('你好')
  })
})
