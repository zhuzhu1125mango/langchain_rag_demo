import { ref } from 'vue'
import { useChatStore } from '@/stores/chat'
import { useGetSuggestions, useRewriteQuestion, useClassifyQuestion, useCompareKnowledgeBases } from '@/queries/chat'
import type { CompareResponse } from '@/queries/chat'
import { useRecommendKnowledgeBases } from '@/queries/kb'
import type { KBRecommendation } from '@/queries/kb'
import { useToast } from '@/composables/useToast'
import type { Ref } from 'vue'

/**
 * 聊天输入辅助编排（从 ChatView.vue 抽取，行为不变）。
 *
 * 覆盖三块彼此独立但都围绕输入框的辅助功能：
 * 1. 问题重写 + 自动分类（handleRewriteQuestion / handleClassifyQuestion / useRewrittenQuestion）
 * 2. 防抖推荐（智能问题推荐 / 知识库推荐，各自独立计时器——D1 修复）
 * 3. 多知识库答案对比（handleCompareAnswers）
 */
export function useChatAssist(options: {
  questionInput: Ref<string>
  selectedKBs: Ref<string[]>
}) {
  const { questionInput, selectedKBs } = options
  const toast = useToast()

  const suggestions = ref<string[]>([])
  const isGeneratingSuggestions = ref(false)
  const rewriteResult = ref<{ rewritten: string; original: string; changes: string } | null>(null)
  const isRewritingQuestion = ref(false)
  const questionClassification = ref<{ type: string; subtype: string; confidence: number; description: string } | null>(null)
  const kbRecommendations = ref<KBRecommendation[]>([])
  const showComparePanel = ref(false)
  const isComparing = ref(false)
  const compareResult = ref<CompareResponse | null>(null)

  // fetchSuggestions 与 fetchKBRecommendations 各自独立防抖计时器：
  // 二者共用同一 timer 时后调用的函数会清掉前者刚注册的定时器（D1），
  // 导致 /api/chat/suggestions 永不发出。拆分为独立变量，互不干扰。
  let suggestionDebounceTimer: ReturnType<typeof setTimeout> | null = null
  let kbRecommendDebounceTimer: ReturnType<typeof setTimeout> | null = null

  const getSuggestions = useGetSuggestions()
  const rewriteQuestionMutation = useRewriteQuestion()
  const classifyQuestionMutation = useClassifyQuestion()
  const compareKBsMutation = useCompareKnowledgeBases()
  const recommendKBsMutation = useRecommendKnowledgeBases()

  /** 重写问题并自动分类，失败时回退到原始问题分类。 */
  async function handleRewriteQuestion(): Promise<void> {
    if (!questionInput.value.trim() || isRewritingQuestion.value) return

    isRewritingQuestion.value = true
    questionClassification.value = null

    try {
      const result = await rewriteQuestionMutation.mutateAsync(questionInput.value)
      rewriteResult.value = result

      // 自动进行问题分类
      await handleClassifyQuestion(result.rewritten || result.original || questionInput.value)
    } catch (error) {
      console.error('问题重写失败:', error)
      toast.error('问题重写失败', '请稍后重试')
      // 失败时仍然进行分类
      await handleClassifyQuestion(questionInput.value)
    } finally {
      isRewritingQuestion.value = false
    }
  }

  /** 调用后端对问题进行类型分类。 */
  async function handleClassifyQuestion(question: string): Promise<void> {
    try {
      const result = await classifyQuestionMutation.mutateAsync(question)
      questionClassification.value = result
    } catch (error) {
      console.error('问题分类失败:', error)
      questionClassification.value = null
    }
  }

  /** 将输入框替换为重写后的问题。 */
  function useRewrittenQuestion(): void {
    if (rewriteResult.value) {
      questionInput.value = rewriteResult.value.rewritten
      rewriteResult.value = null
    }
  }

  /** 对选中的多个知识库进行答案对比。 */
  async function handleCompareAnswers(): Promise<void> {
    if (selectedKBs.value.length < 2 || !questionInput.value.trim()) return

    showComparePanel.value = true
    isComparing.value = true
    compareResult.value = null

    try {
      const result = await compareKBsMutation.mutateAsync({
        question: questionInput.value,
        kbIds: selectedKBs.value
      })
      compareResult.value = result
    } catch (error) {
      console.error('答案对比失败:', error)
      toast.error('答案对比失败', '请稍后重试')
    } finally {
      isComparing.value = false
    }
  }

  /** 防抖获取与当前问题相关的知识库推荐。 */
  async function fetchKBRecommendations(): Promise<void> {
    if (kbRecommendDebounceTimer) {
      clearTimeout(kbRecommendDebounceTimer)
    }

    const question = questionInput.value.trim()

    if (question.length < 5) {
      kbRecommendations.value = []
      return
    }

    kbRecommendDebounceTimer = setTimeout(async () => {
      try {
        const result = await recommendKBsMutation.mutateAsync({ question, top_k: 3 })
        kbRecommendations.value = result
      } catch (error) {
        console.error('获取知识库推荐失败:', error)
        kbRecommendations.value = []
      }
    }, 800)
  }

  /** 防抖获取基于当前输入的智能问题推荐。 */
  async function fetchSuggestions(): Promise<void> {
    if (suggestionDebounceTimer) {
      clearTimeout(suggestionDebounceTimer)
    }

    const question = questionInput.value.trim()

    if (question.length < 3) {
      suggestions.value = []
      return
    }

    suggestionDebounceTimer = setTimeout(async () => {
      isGeneratingSuggestions.value = true

      try {
        const result = await getSuggestions(question, undefined, selectedKBs.value.length > 0 ? selectedKBs.value : undefined)
        suggestions.value = result
      } catch (error) {
        console.error('获取推荐问题失败:', error)
      } finally {
        isGeneratingSuggestions.value = false
      }
    }, 500)
  }

  /** 输入框内容变化时清除重写结果并触发推荐。 */
  function onInputChange(): void {
    if (rewriteResult.value) {
      rewriteResult.value = null
    }

    fetchSuggestions()
    fetchKBRecommendations()
  }

  return {
    // 问题重写/分类
    rewriteResult,
    isRewritingQuestion,
    questionClassification,
    handleRewriteQuestion,
    useRewrittenQuestion,
    // 防抖推荐
    suggestions,
    isGeneratingSuggestions,
    kbRecommendations,
    onInputChange,
    // 答案对比
    showComparePanel,
    isComparing,
    compareResult,
    handleCompareAnswers
  }
}
