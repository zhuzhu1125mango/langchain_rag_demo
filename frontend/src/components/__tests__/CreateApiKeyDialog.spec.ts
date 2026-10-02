import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { mount, flushPromises } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import CreateApiKeyDialog from '../settings/CreateApiKeyDialog.vue'

/**
 * 创建 API 密钥两步对话框组件测试（E.6 §E.6.6）。
 *
 * 覆盖：Step1 表单校验（空名称/达上限前端拦截）→ Step2 一次性明文展示
 * （渲染 + 复制 + 关闭守卫）——重点锁定"明文未保存前关闭需二次确认"的
 * 防丢保护流。
 */

const { createMutateAsync, confirmMock, copyMock, toastSuccess, handleMutationErrorMock } = vi.hoisted(() => ({
  createMutateAsync: vi.fn(),
  confirmMock: vi.fn(),
  copyMock: vi.fn(),
  toastSuccess: vi.fn(),
  handleMutationErrorMock: vi.fn()
}))

vi.mock('@/queries/apiKeys', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../apiKeys')>()
  return {
    ...actual,
    useCreateApiKey: () => ({ mutateAsync: createMutateAsync, isPending: ref(false) })
  }
})

vi.mock('element-plus', async (importOriginal) => {
  const actual = await importOriginal<typeof import('element-plus')>()
  return {
    ...actual,
    ElMessageBox: { ...actual.ElMessageBox, confirm: confirmMock }
  }
})

vi.mock('@vueuse/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@vueuse/core')>()
  return {
    ...actual,
    useClipboard: () => ({ copy: copyMock, copied: ref(false), isSupported: ref(true) })
  }
})

vi.mock('@/composables/useToast', () => ({
  useToast: () => ({ success: toastSuccess, error: vi.fn(), warning: vi.fn(), info: vi.fn() }),
  handleMutationError: handleMutationErrorMock
}))

const CREATED = {
  id: 'k-1',
  name: 'ci',
  key_prefix: 'rag_a1b2c3d4',
  key: 'rag_secret_key_plaintext_value',
  created_at: '2026-09-30T00:00:00Z',
  expires_at: null
}

/** ElDialog 在 onMounted 中才置 visible/rendered（use-dialog.mjs L168-174），
 *  首帧 DOM 为 display:none + 空 overlay——mount 后必须 flush 等待重渲染。 */
async function mountDialog(props: Record<string, unknown> = {}) {
  const wrapper = mount(CreateApiKeyDialog, {
    props: { visible: true, activeCount: 0, maxPerUser: 5, ...props },
    global: {
      plugins: [ElementPlus],
      stubs: {
        Teleport: true,
        // Transition 不能 stub：ElDialog 的 update:modelValue 在 afterLeave
        // 过渡钩子里才 emit（use-dialog.mjs L83-88），stub 后钩子不触发，
        // 点 × 关闭不会向父级发 update:visible。jsdom 无 CSS 时真实
        // transition 的 leave 立即完成，钩子正常走。
        Transition: false,
        // jsdom 下 ElSelect popper 触发递归更新（已知环境问题）；
        // 有效期在本套用例中不做交互（默认永久），stub 掉即可
        ElSelect: true,
        ElOption: true
      }
    }
  })
  await flushPromises()
  return wrapper
}

/** 打开对话框后填写名称并点击创建。 */
async function fillAndCreate(wrapper: ReturnType<typeof mountDialog>, name: string) {
  await wrapper.find('input').setValue(name)
  await wrapper.find('[data-testid="api-key-create-btn"]').trigger('click')
  await flushPromises()
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('Step 1 表单校验', () => {
  it('空名称点创建：显示必填错误且不发起请求', async () => {
    const wrapper = await mountDialog()
    await wrapper.find('[data-testid="api-key-create-btn"]').trigger('click')
    expect(createMutateAsync).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('请填写密钥名称')
  })

  it('达上限（activeCount >= maxPerUser）：前端拦截不发请求', async () => {
    const wrapper = await mountDialog({ activeCount: 5, maxPerUser: 5 })
    await fillAndCreate(wrapper, 'ci')
    expect(createMutateAsync).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('已达到密钥数量上限（5）')
  })

  it('创建失败（422 等）：错误透出且停留表单态', async () => {
    createMutateAsync.mockRejectedValueOnce(new Error('已达到密钥数量上限（5），请先吊销'))
    const wrapper = await mountDialog()
    await fillAndCreate(wrapper, 'ci')
    expect(wrapper.text()).toContain('已达到密钥数量上限（5），请先吊销')
    expect(handleMutationErrorMock).toHaveBeenCalled()
    // 停留在表单态：创建按钮仍在
    expect(wrapper.find('[data-testid="api-key-create-btn"]').exists()).toBe(true)
  })
})

describe('Step 2 一次性明文展示', () => {
  async function enterStep2() {
    createMutateAsync.mockResolvedValueOnce(CREATED)
    const wrapper = await mountDialog()
    await fillAndCreate(wrapper, 'ci')
    return wrapper
  }

  it('创建成功：渲染完整明文 + 复制按钮 + 不可再查看警示', async () => {
    const wrapper = await enterStep2()
    expect(wrapper.text()).toContain('rag_secret_key_plaintext_value')
    expect(wrapper.text()).toContain('关闭后无法再次查看')
    expect(wrapper.find('[data-testid="api-key-copy-btn"]').exists()).toBe(true)
    // Step1 的创建按钮不再存在
    expect(wrapper.find('[data-testid="api-key-create-btn"]').exists()).toBe(false)
  })

  it('点击复制：调用剪贴板写入并提示', async () => {
    const wrapper = await enterStep2()
    await wrapper.find('[data-testid="api-key-copy-btn"]').trigger('click')
    expect(copyMock).toHaveBeenCalledWith('rag_secret_key_plaintext_value')
    expect(toastSuccess).toHaveBeenCalledWith('已复制到剪贴板')
  })

  it('点「我已保存，关闭」：关闭 + created 事件', async () => {
    const wrapper = await enterStep2()
    await wrapper.find('[data-testid="api-key-saved-btn"]').trigger('click')
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
    expect(wrapper.emitted('created')).toHaveLength(1)
  })

  it('防丢保护：Step2 关闭需 confirm 确认，确认后才关闭', async () => {
    confirmMock.mockResolvedValueOnce('confirm')
    const wrapper = await enterStep2()
    // el-dialog header 关闭按钮（×）触发 before-close
    await wrapper.find('.el-dialog__headerbtn').trigger('click')
    await settleTransition()
    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })

  it('防丢保护：confirm 取消则留在明文页不关闭', async () => {
    confirmMock.mockRejectedValueOnce('cancel')
    const wrapper = await enterStep2()
    await wrapper.find('.el-dialog__headerbtn').trigger('click')
    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('update:visible')).toBeUndefined()
    // 仍在 Step2：保存按钮还在
    expect(wrapper.find('[data-testid="api-key-saved-btn"]').exists()).toBe(true)
  })
})

/** 等待真实 Transition leave 完成（jsdom 无 CSS，Vue 依赖 rAF/timeout
 *  推进 afterLeave——ElDialog 的 update:modelValue 在 afterLeave 才 emit）。 */
async function settleTransition() {
  await new Promise((r) => setTimeout(r, 100))
  await flushPromises()
}

describe('表单态关闭不经确认', () => {
  it('Step1 点 ×：直接关闭，不弹确认', async () => {
    const wrapper = await mountDialog()
    await wrapper.find('.el-dialog__headerbtn').trigger('click')
    await settleTransition()
    expect(confirmMock).not.toHaveBeenCalled()
    expect(wrapper.emitted('update:visible')).toEqual([[false]])
  })

  it('重新打开：表单重置回 Step1', async () => {
    createMutateAsync.mockResolvedValueOnce(CREATED)
    const wrapper = await mountDialog()
    await fillAndCreate(wrapper, 'ci')
    expect(wrapper.text()).toContain('rag_secret_key_plaintext_value')
    await wrapper.setProps({ visible: false })
    await wrapper.setProps({ visible: true })
    expect(wrapper.find('[data-testid="api-key-create-btn"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('rag_secret_key_plaintext_value')
  })
})
