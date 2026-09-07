/**
 * 链路 1：上传文档 → 解析完成 → 提问命中知识库 → 引用展示。
 *
 * 全程 route mock：上传走真实文件选择 + WS 进度协议（auth_ok 鉴权），
 * 提问走 SSE 流式 mock，断言流式内容与引用来源渲染。
 */
import { test, expect } from '@playwright/test'
import { installMocks } from './fixtures'

test.beforeEach(async ({ page }) => {
  await installMocks(page)
})

test('上传文档后提问并展示流式回答与引用来源', async ({ page }) => {
  // 1. 选中知识库并打开上传对话框
  await page.goto('/knowledge-base')
  // KB 列表项是 div（非 button），用侧栏作用域 + 文本精确定位
  const kbProduct = page.locator('aside').filter({ hasText: '知识库列表' }).getByText('产品手册', { exact: true })
  await expect(kbProduct).toBeVisible()
  await kbProduct.click()
  await page.getByRole('button', { name: '上传文档' }).click()

  const uploadDialog = page.locator('.el-dialog', { hasText: '上传文档' })
  await expect(uploadDialog).toBeVisible()

  // 2. 选择文件并触发上传（服务端解析进度通过 WS mock 推送，auth_ok 后才发起上传）
  await page.setInputFiles('input[type="file"]', {
    name: 'rag.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('# RAG 原理\n\n检索增强生成（RAG）是一种结合检索与生成的技术。')
  })
  await expect(uploadDialog.getByText('已选择 1 个文件')).toBeVisible()
  await uploadDialog.getByRole('button', { name: '上传', exact: true }).click()

  // 3. 上传提交完成：对话框自动关闭（进度事件经 WS 鉴权链路验证）
  await expect(uploadDialog).toBeHidden({ timeout: 10_000 })

  // 4. 切换到对话页提问
  await page.getByRole('link', { name: '对话', exact: true }).click()
  const input = page.getByPlaceholder('输入您的问题，Enter 发送')
  await expect(input).toBeVisible()
  await input.fill('什么是 RAG？')
  await input.press('Enter')

  // 5. 断言：用户消息、流式回答内容、引用来源均渲染
  await expect(page.getByText('什么是 RAG？', { exact: true })).toBeVisible()
  await expect(page.getByText('RAG 是检索增强生成技术').first()).toBeVisible()
  await expect(page.getByText('参考来源')).toBeVisible()
  await expect(page.getByText('rag.md').first()).toBeVisible()

  // 6. reasoning 面板展示检索步骤
  await expect(page.getByText('检索知识库')).toBeVisible()
})
