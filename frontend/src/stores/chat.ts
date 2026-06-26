import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Session, Message } from '@/queries/chat'
import { useGetQuickQuestions } from '@/queries/chat'
import { generateId } from '@/utils/id'

export interface QuestionHistoryItem {
  /** 历史项唯一 ID。 */
  id: string
  /** 问题文本。 */
  question: string
  /** 提问时间戳（毫秒）。 */
  timestamp: number
}

/**
 * 聊天会话状态管理。
 *
 * 维护当前会话、消息列表、输入状态、快捷问题与历史问题缓存。
 */
export const useChatStore = defineStore('chat', () => {
  // 当前选中的会话
  const currentSession = ref<Session | null>(null)
  // 当前会话的消息列表
  const messages = ref<Message[]>([])
  // 是否正在等待助手回复
  const isTyping = ref(false)
  // 输入框中的当前问题
  const currentQuestion = ref('')
  // 首页快捷问题列表
  const quickQuestions = ref<string[]>([
    '什么是RAG?',
    '介绍一下项目功能',
    '如何上传文档?',
    '支持哪些文件格式?'
  ])
  const isLoadingQuickQuestions = ref(false)
  // 本地输入历史（最近使用的问题）
  const questionHistory = ref<QuestionHistoryItem[]>([])
  const maxHistoryCount = 20

  /** 切换当前会话并清空消息列表。 */
  function setCurrentSession(session: Session | null) {
    currentSession.value = session
    messages.value = []
  }

  /** 更新当前会话标题（流式响应中后端生成标题时使用）。 */
  function updateCurrentSessionTitle(title: string) {
    if (currentSession.value) {
      currentSession.value.title = title
    }
  }

  /** 追加一条消息到列表。 */
  function addMessage(message: Message) {
    messages.value.push(message)
  }

  /** 根据消息 ID 更新指定字段（用于流式更新内容）。 */
  function updateMessage(id: string, updates: Partial<Omit<Message, 'id' | 'role'>>) {
    const index = messages.value.findIndex(m => m.id === id)
    if (index !== -1) {
      messages.value[index] = { ...messages.value[index], ...updates } as Message
    }
  }

  /** 替换整个消息列表（用于加载历史会话消息）。 */
  function setMessages(newMessages: Message[]) {
    messages.value = newMessages
  }

  /** 清空当前会话消息列表。 */
  function clearMessages() {
    messages.value = []
  }

  /** 设置是否正在等待助手回复（控制打字提示状态）。 */
  function setTyping(typing: boolean) {
    isTyping.value = typing
  }

  /** 设置输入框当前问题文本。 */
  function setCurrentQuestion(question: string) {
    currentQuestion.value = question
  }

  /** 从后端加载推荐快捷问题。 */
  async function loadQuickQuestions() {
    try {
      const getQuickQuestions = useGetQuickQuestions()
      const questions = await getQuickQuestions()
      if (questions && questions.length > 0) {
        quickQuestions.value = questions
      }
    } catch (error) {
      console.error('加载快捷问题失败:', error)
    }
  }

  /** 将问题加入本地历史，去重并限制最大数量。 */
  function addQuestionToHistory(question: string) {
    const existingIndex = questionHistory.value.findIndex(
      item => item.question === question
    )

    if (existingIndex !== -1) {
      questionHistory.value.splice(existingIndex, 1)
    }

    const newItem: QuestionHistoryItem = {
      id: generateId(),
      question,
      timestamp: Date.now()
    }

    questionHistory.value.unshift(newItem)

    if (questionHistory.value.length > maxHistoryCount) {
      questionHistory.value = questionHistory.value.slice(0, maxHistoryCount)
    }
  }

  /** 按 ID 移除单条问题历史。 */
  function removeQuestionFromHistory(id: string) {
    const index = questionHistory.value.findIndex(item => item.id === id)
    if (index !== -1) {
      questionHistory.value.splice(index, 1)
    }
  }

  /** 清空全部问题历史。 */
  function clearQuestionHistory() {
    questionHistory.value = []
  }

  return {
    currentSession,
    messages,
    isTyping,
    currentQuestion,
    quickQuestions,
    isLoadingQuickQuestions,
    questionHistory,
    setCurrentSession,
    updateCurrentSessionTitle,
    addMessage,
    updateMessage,
    setMessages,
    clearMessages,
    setTyping,
    setCurrentQuestion,
    loadQuickQuestions,
    addQuestionToHistory,
    removeQuestionFromHistory,
    clearQuestionHistory
  }
})
