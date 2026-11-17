import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ModelSelector from '../chat/ModelSelector.vue'
import type { AvailableModel } from '@/queries/models'

function model(id: string, displayName: string, isDefault = false): AvailableModel {
  return {
    id,
    display_name: displayName,
    capabilities: ['chat', 'stream'],
    context_window: 32768,
    is_default: isDefault,
    thinking_control: 'off'
  }
}

function mountSelector(models: AvailableModel[], modelValue = '', defaultModelId = 'm1') {
  return mount(ModelSelector, {
    props: { models, modelValue, defaultModelId }
  })
}

describe('ModelSelector 可见性', () => {
  it('没有可选模型时不渲染（管理员一个都没开放）', () => {
    expect(mountSelector([]).find('select').exists()).toBe(false)
  })

  it('只有一个可选项、且它就是默认模型时不渲染——单一选项的下拉是噪音', () => {
    const wrapper = mountSelector([model('m1', '默认模型', true)], '', 'm1')
    expect(wrapper.find('select').exists()).toBe(false)
  })

  it('唯一可选项不是默认模型时仍然渲染，否则用户永远选不到它', () => {
    const wrapper = mountSelector([model('m2', '备用模型')], '', 'm1')
    expect(wrapper.find('select').exists()).toBe(true)
    // 默认模型不在列表里 → 「默认」项只能泛称，不能凭空编名字
    expect(wrapper.find('option').text()).toBe('默认 · 默认模型')
  })

  it('多个可选项时渲染，且默认项标出具体模型名', () => {
    const wrapper = mountSelector(
      [model('m1', '通义千问 4B', true), model('m2', 'Llama 3.1 8B')],
      '',
      'm1'
    )
    const options = wrapper.findAll('option')
    expect(options).toHaveLength(3)
    expect(options[0]?.text()).toBe('默认 · 通义千问 4B')
    expect(options[1]?.text()).toBe('通义千问 4B')
  })
})

describe('ModelSelector 交互', () => {
  it('切换模型时向上抛 update:modelValue', async () => {
    const wrapper = mountSelector(
      [model('m1', '通义千问 4B', true), model('m2', 'Llama 3.1 8B')],
      '',
      'm1'
    )
    await wrapper.find('select').setValue('m2')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual(['m2'])
  })

  it('可以切回默认（空串）——选了之后必须能回退', async () => {
    const wrapper = mountSelector(
      [model('m1', '通义千问 4B', true), model('m2', 'Llama 3.1 8B')],
      'm2',
      'm1'
    )
    await wrapper.find('select').setValue('')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([''])
  })

  it('选中态与默认态的样式不同，避免「选了但看不出来」', () => {
    const models = [model('m1', '通义千问 4B', true), model('m2', 'Llama 3.1 8B')]
    const selected = mountSelector(models, 'm2', 'm1').find('select').classes()
    const byDefault = mountSelector(models, '', 'm1').find('select').classes()

    expect(selected).toContain('text-primary-600')
    expect(byDefault).toContain('text-content-muted')
    // 互斥：两个状态不能同时带上对方的主色/灰字，否则谁生效取决于 CSS 产物顺序
    expect(byDefault).not.toContain('text-primary-600')
  })
})
