import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const get = vi.fn()

vi.mock('@/utils/axios', () => ({
  api: { get: (...args: unknown[]) => get(...args) },
}))

import { downloadDocumentFile, extractDownloadError } from '@/utils/download'

describe('downloadDocumentFile', () => {
  const click = vi.fn()

  beforeEach(() => {
    get.mockReset()
    click.mockReset()
    // jsdom 没有实现 createObjectURL / anchor 的真实下载行为
    vi.stubGlobal('URL', {
      createObjectURL: vi.fn(() => 'blob:mock'),
      revokeObjectURL: vi.fn(),
    })
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(click)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('以 blob 方式请求并触发保存，文件名取调用方传入值', async () => {
    const blob = new Blob(['pdf-bytes'], { type: 'application/pdf' })
    get.mockResolvedValue(blob)

    await downloadDocumentFile('doc-1', '体检报告.pdf')

    expect(get).toHaveBeenCalledWith('/documents/doc-1/file', { responseType: 'blob' })
    expect(click).toHaveBeenCalledTimes(1)
    expect(URL.createObjectURL).toHaveBeenCalledWith(blob)
  })

  it('文件名缺失时退化为 document，不产生空文件名', async () => {
    get.mockResolvedValue(new Blob(['x']))
    await downloadDocumentFile('doc-2', '')
    expect(click).toHaveBeenCalledTimes(1)
  })
})

describe('extractDownloadError', () => {
  it('从 blob 错误体里读出后端 detail', async () => {
    const body = new Blob([JSON.stringify({ detail: '原文已不存在（对象存储中缺失）' })], {
      type: 'application/json',
    })
    expect(await extractDownloadError({ response: { data: body } })).toBe(
      '原文已不存在（对象存储中缺失）',
    )
  })

  it('非 JSON 错误体退回原文', async () => {
    const body = new Blob(['Bad Gateway'])
    expect(await extractDownloadError({ response: { data: body } })).toBe('Bad Gateway')
  })

  it('普通 Error 用其 message', async () => {
    expect(await extractDownloadError(new Error('Network Error'))).toBe('Network Error')
  })

  it('完全未知的输入不抛异常', async () => {
    expect(await extractDownloadError(undefined)).toBe('下载失败')
  })
})
