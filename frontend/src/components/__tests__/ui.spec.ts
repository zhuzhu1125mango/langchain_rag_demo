import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import AppButton from '../ui/AppButton.vue'
import AppCard from '../ui/AppCard.vue'
import AppBadge from '../ui/AppBadge.vue'
import AppInput from '../ui/AppInput.vue'
import AppSelect from '../ui/AppSelect.vue'
import AppSkeleton from '../ui/AppSkeleton.vue'
import AppStateView from '../ui/AppStateView.vue'

describe('AppButton', () => {
  it('默认渲染 secondary/md 并透传属性', () => {
    const wrapper = mount(AppButton, {
      attrs: { 'aria-label': '保存' },
      slots: { default: '保存' }
    })
    const button = wrapper.get('button')
    expect(button.attributes('type')).toBe('button')
    expect(button.attributes('aria-label')).toBe('保存')
    expect(button.classes()).toContain('bg-surface-subtle')
    expect(button.classes()).toContain('px-4')
  })

  it('variant/size/block 生效', () => {
    const wrapper = mount(AppButton, {
      props: { variant: 'primary', size: 'sm', block: true }
    })
    const button = wrapper.get('button')
    expect(button.classes()).toContain('bg-primary-500')
    expect(button.classes()).toContain('px-3')
    expect(button.classes()).toContain('w-full')
  })

  it('loading 时禁用并显示加载图标', () => {
    const wrapper = mount(AppButton, { props: { loading: true } })
    expect(wrapper.get('button').attributes('disabled')).toBeDefined()
    expect(wrapper.find('svg').exists()).toBe(true)
  })

  it('点击事件正常透传', async () => {
    const wrapper = mount(AppButton)
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('click')).toHaveLength(1)
  })
})

describe('AppCard', () => {
  it('默认使用 surface/card 语义并支持 padding', () => {
    const wrapper = mount(AppCard, { props: { padding: 'lg' } })
    const classes = wrapper.get('div').classes()
    expect(classes).toContain('bg-surface')
    expect(classes).toContain('rounded-card')
    expect(classes).toContain('shadow-card')
    expect(classes).toContain('p-6')
  })
})

describe('AppBadge', () => {
  it('variant 映射到状态色', () => {
    const wrapper = mount(AppBadge, { props: { variant: 'success' }, slots: { default: '已完成' } })
    const classes = wrapper.get('span').classes()
    expect(classes).toContain('bg-success-soft')
    expect(classes).toContain('text-success-content')
    expect(classes).toContain('rounded-pill')
  })
})

describe('AppInput', () => {
  it('v-model 更新', async () => {
    const wrapper = mount(AppInput, { props: { modelValue: '' } })
    await wrapper.get('input').setValue('hello')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['hello'])
  })

  it('multiline 渲染 textarea', () => {
    const wrapper = mount(AppInput, { props: { multiline: true, rows: 5 } })
    expect(wrapper.find('textarea').exists()).toBe(true)
    expect(wrapper.get('textarea').attributes('rows')).toBe('5')
  })

  it('disabled 状态生效', () => {
    const wrapper = mount(AppInput, { props: { disabled: true } })
    expect(wrapper.get('input').attributes('disabled')).toBeDefined()
  })

  it('modelModifiers.number 将输入转为数字', async () => {
    const wrapper = mount(AppInput, {
      props: { modelValue: 1, modelModifiers: { number: true } }
    })
    await wrapper.get('input').setValue('3')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([3])
  })
})

describe('AppSelect', () => {
  it('v-model 更新', async () => {
    const wrapper = mount(AppSelect, {
      props: { modelValue: 'a' },
      slots: { default: '<option value="a">A</option><option value="b">B</option>' }
    })
    await wrapper.get('select').setValue('b')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['b'])
  })
})

describe('AppSkeleton', () => {
  it('按 rows 渲染骨架行并支持头像', () => {
    const wrapper = mount(AppSkeleton, { props: { rows: 4, avatar: true } })
    expect(wrapper.findAll('.skeleton')).toHaveLength(5)
  })
})

describe('AppStateView', () => {
  it('empty 渲染标题、描述与操作槽', () => {
    const wrapper = mount(AppStateView, {
      props: { state: 'empty', title: '暂无会话', description: '开始一次对话吧' },
      slots: { action: '<button>新建</button>' }
    })
    expect(wrapper.text()).toContain('暂无会话')
    expect(wrapper.text()).toContain('开始一次对话吧')
    expect(wrapper.find('button').exists()).toBe(true)
  })

  it('loading 渲染骨架且不渲染标题', () => {
    const wrapper = mount(AppStateView, { props: { state: 'loading', skeletonRows: 2 } })
    expect(wrapper.findAll('.skeleton')).toHaveLength(2)
    expect(wrapper.text()).not.toContain('加载失败')
  })

  it('error 使用 alert 语义并在重试时派发事件', async () => {
    const wrapper = mount(AppStateView, { props: { state: 'error', title: '加载失败' } })
    expect(wrapper.get('[role="alert"]').exists()).toBe(true)
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('retry')).toHaveLength(1)
  })

  it('error 不可重试时不渲染重试按钮', () => {
    const wrapper = mount(AppStateView, { props: { state: 'error', retryable: false } })
    expect(wrapper.find('button').exists()).toBe(false)
  })
})
