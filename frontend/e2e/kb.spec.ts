/**
 * 链路 3：知识库增删。
 *
 * 创建对话框提交后列表与选中态更新；删除（原生 confirm）后列表移除。
 * mock 为有状态实现（fixtures.ts 的 state.kbs）。
 */
import { expect, test, type Page } from '@playwright/test'
import { installMocks, createMockState } from './fixtures'

/** KB 侧栏列表项是 div（非 button），用侧栏作用域 + 文本精确定位。 */
function kbItem(page: Page, name: string) {
  return page.locator('aside').filter({ hasText: '知识库列表' }).getByText(name, { exact: true })
}

test('创建知识库后出现在列表并选中', async ({ page }) => {
  await installMocks(page)

  await page.goto('/knowledge-base')
  await expect(kbItem(page, '产品手册')).toBeVisible()

  // 打开创建对话框并填写名称
  await page.getByRole('button', { name: '新建知识库' }).click()
  const dialog = page.locator('.el-dialog', { hasText: '创建知识库' })
  await expect(dialog).toBeVisible()
  await dialog.getByPlaceholder('输入知识库名称').fill('测试知识库')
  await dialog.getByRole('button', { name: '创建', exact: true }).click()

  // 创建成功：对话框关闭，新知识库被自动选中显示在标题区
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('heading', { name: '测试知识库' })).toBeVisible()
})

test('删除知识库后从列表移除', async ({ page }) => {
  const state = createMockState()
  state.kbs.push({
    id: 'kb-del',
    name: '待删除知识库',
    description: '将被删除',
    document_count: 0,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z'
  })
  await installMocks(page, state)

  await page.goto('/knowledge-base')
  await expect(kbItem(page, '待删除知识库')).toBeVisible()

  // 选中后点击删除，接受原生 confirm 弹窗（handler 须在触发前注册）
  await kbItem(page, '待删除知识库').click()
  page.once('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: '删除', exact: true }).click()

  // 删除成功：回退到未选中态，列表中移除（其余知识库保留）
  await expect(kbItem(page, '待删除知识库')).toHaveCount(0)
  await expect(kbItem(page, '产品手册')).toBeVisible()
})
