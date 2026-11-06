import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AxiosResponse } from 'axios'
import { exportSessionMarkdown } from '../chat'
import { api } from '@/utils/axios'

/**
 * 会话导出（UX-5）的前端逻辑测试。
 *
 * 导出这条链路有两个容易静默出错的地方，都不报错、只是结果不对：
 * 1. 文件名：中文标题必须从 `filename*=UTF-8''...` 里解码，取错就是一堆 `%E5%BC%B9`；
 * 2. 下载钩子：`a.click()` 之后立刻 revoke URL，部分浏览器会取消下载。
 */

vi.mock('@/utils/axios', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }
}))

const mockedApi = vi.mocked(api)

/** jsdom 没实现 createObjectURL/revokeObjectURL，用桩替代。 */
const createdUrls: string[] = []
const revokedUrls: string[] = []
let clickedDownload: string | null = null
// 单调递增：每个测试的对象 URL 各不相同，否则上一个测试迟到的
// `setTimeout(revoke)` 会以同一个字符串混进本测试的断言里
let urlSeq = 0

beforeEach(() => {
  vi.clearAllMocks()
  createdUrls.length = 0
  revokedUrls.length = 0
  clickedDownload = null

  vi.stubGlobal('URL', {
    ...globalThis.URL,
    createObjectURL: vi.fn(() => {
      const url = `blob:mock/${++urlSeq}`
      createdUrls.push(url)
      return url
    }),
    revokeObjectURL: vi.fn((url: string) => {
      revokedUrls.push(url)
    })
  })

  // 拦截 a.click()：jsdom 里点击不会真的下载，这里只记录 download 属性
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    clickedDownload = this.download
  })
})

afterEach(async () => {
  // 先把本测试遗留的 setTimeout(…, 0) 跑完，再去清桩：
  // 否则它会在下一个测试的等待里执行，往新数组里塞旧 URL
  await new Promise(resolve => setTimeout(resolve, 5))
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

function blobResponse(disposition?: string): AxiosResponse<Blob> {
  return {
    data: new Blob(['# 标题'], { type: 'text/markdown' }),
    status: 200,
    statusText: 'OK',
    headers: disposition ? { 'content-disposition': disposition } : {},
    config: {} as AxiosResponse['config']
  } as AxiosResponse<Blob>
}

describe('exportSessionMarkdown', () => {
  it('用后端 RFC 5987 文件名解码中文标题', async () => {
    const disposition =
      'attachment; filename="session-0b7f8c1e.md"; filename*=UTF-8\'\'%E5%BC%B9%E6%80%A7%E6%89%93%E5%8D%A1.md'
    mockedApi.get = vi.fn().mockResolvedValue(blobResponse(disposition))

    await exportSessionMarkdown('0b7f8c1e-3a4d-4c5e-9f00-112233445566', '弹性打卡')

    expect(clickedDownload).toBe('弹性打卡.md')
  })

  it('没有 filename* 时回退到普通 filename', async () => {
    mockedApi.get = vi.fn().mockResolvedValue(blobResponse('attachment; filename="plain-name.md"'))

    await exportSessionMarkdown('sid', '任意标题')

    expect(clickedDownload).toBe('plain-name.md')
  })

  it('没有响应头时用标题兜底，并剔除文件系统不允许的字符', async () => {
    mockedApi.get = vi.fn().mockResolvedValue(blobResponse())

    await exportSessionMarkdown('sid', 'a/b:c*d?')

    expect(clickedDownload).toBe('abcd.md')
  })

  it('标题为空时用会话 ID 前 8 位兜底', async () => {
    mockedApi.get = vi.fn().mockResolvedValue(blobResponse())

    await exportSessionMarkdown('0b7f8c1e-3a4d', '')

    expect(clickedDownload).toBe('session-0b7f8c1e.md')
  })

  it('传了 rawResponse 才能拿到响应头（否则拦截器已解包成 Blob）', async () => {
    mockedApi.get = vi.fn().mockResolvedValue(blobResponse())

    await exportSessionMarkdown('sid', 't')

    const [, config] = (mockedApi.get as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(config).toMatchObject({
      params: { format: 'markdown' },
      responseType: 'blob',
      rawResponse: true
    })
  })

  it('下载后才 revoke，不提前释放对象 URL', async () => {
    mockedApi.get = vi.fn().mockResolvedValue(blobResponse())

    await exportSessionMarkdown('sid', 't')

    expect(createdUrls).toHaveLength(1)
    expect(clickedDownload).toBe('t.md')
    // revoke 走的是 setTimeout(…, 0)，此时应尚未释放
    expect(revokedUrls).toEqual([])
    await new Promise(resolve => setTimeout(resolve, 5))
    expect(revokedUrls).toEqual(createdUrls)
  })

  it('每个会话拿到各自的对象 URL（不复用已 revoke 的）', () => {
    expect(new Set(createdUrls).size).toBe(createdUrls.length)
  })

  it('请求失败时把异常抛给调用方（由页面提示错误）', async () => {
    mockedApi.get = vi.fn().mockRejectedValue(new Error('boom'))

    await expect(exportSessionMarkdown('sid', 't')).rejects.toThrow('boom')
    expect(clickedDownload).toBeNull()
  })
})
