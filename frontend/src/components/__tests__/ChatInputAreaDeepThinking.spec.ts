import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ChatInputArea from '../chat/ChatInputArea.vue'

/**
 * 深度思考开关的可用性。
 *
 * 这里测的是「模型没有思考能力时，开关必须点不动」——它比样式重要：
 * 允许点，用户会得到同一个更慢的回答，却以为开了深度思考。
 *
 * 注意用的是 `aria-disabled` 而不是 `disabled`：禁用按钮在浏览器里不弹 `title`，
 * 用户只会看到一个灰掉的开关、不知道为什么灰。所以「点不动」不靠原生禁用，
 * 而靠事件处理器拦——这条行为必须由测试钉住，因为它在视觉上没有任何提示。
 */
function mountArea(overrides: Record<string, unknown> = {}) {
  return mount(ChatInputArea, {
    props: {
      modelValue: '',
      rewriteResult: null,
      questionClassification: null,
      kbRecommendations: [],
      selectedKbs: [],
      suggestions: [],
      isGeneratingSuggestions: false,
      isRewritingQuestion: false,
      isTyping: false,
      getKbName: (id: string) => id,
      useWebSearch: false,
      searchStatus: 'idle',
      deepThinking: false,
      deepThinkingLabel: '深度思考·关',
      deepThinkingTitle: '开启深度思考',
      availableModels: [],
      selectedModelId: '',
      defaultModelId: '',
      ...overrides
    }
  })
}

function deepThinkingButton(wrapper: ReturnType<typeof mountArea>) {
  const button = wrapper.findAll('button').find(b => b.text().includes('深度思考'))
  if (!button) throw new Error('没找到深度思考按钮')
  return button
}

describe('深度思考开关：按模型能力启用/置灰', () => {
  it('模型支持思考时可点击，点击会通知父组件切换', async () => {
    const wrapper = mountArea({ deepThinkingDisabled: false })

    const button = deepThinkingButton(wrapper)
    expect(button.attributes('aria-disabled')).toBeUndefined()
    await button.trigger('click')

    expect(wrapper.emitted('toggle-deep-thinking')).toHaveLength(1)
  })

  it('模型不支持思考时 aria-disabled=true 且点击无效', async () => {
    const wrapper = mountArea({ deepThinkingDisabled: true })

    const button = deepThinkingButton(wrapper)
    expect(button.attributes('aria-disabled')).toBe('true')
    await button.trigger('click')

    expect(wrapper.emitted('toggle-deep-thinking')).toBeUndefined()
  })

  it('不支持时保留 title 说明原因——灰掉的开关必须能解释自己为什么灰', () => {
    const wrapper = mountArea({
      deepThinkingDisabled: true,
      deepThinkingTitle: '当前模型不具备思考能力，选一个支持思考的模型才能开启'
    })

    expect(deepThinkingButton(wrapper).attributes('title')).toContain('不具备思考能力')
  })

  it('未传 deepThinkingDisabled 时保持可点（老调用方/能力未知时不误伤）', async () => {
    const wrapper = mountArea()

    await deepThinkingButton(wrapper).trigger('click')

    expect(wrapper.emitted('toggle-deep-thinking')).toHaveLength(1)
  })
})
