/**
 * 链路 2：会话切换与历史查看。
 *
 * 侧栏「对话历史」展示会话列表；点击切换会话后消息区加载对应历史；
 * 历史对话页可查看全部会话。
 */
import { test, expect } from '@playwright/test'
import { installMocks } from './fixtures'

test.beforeEach(async ({ page }) => {
  await installMocks(page)
})

test('侧栏会话列表切换并加载对应历史消息', async ({ page }) => {
  await page.goto('/')

  // 侧栏展示两个 mock 会话
  await expect(page.getByText('对话历史')).toBeVisible()
  await expect(page.getByRole('button', { name: /RAG 原理讨论/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /Milvus 调优/ })).toBeVisible()

  // 切换到 Milvus 会话：消息区加载该会话历史
  await page.getByRole('button', { name: /Milvus 调优/ }).click()
  await expect(page.getByText('Milvus HNSW 参数怎么设置？')).toBeVisible()
  await expect(page.getByText('HNSW 的 M 建议设为 16，efConstruction 设为 200。')).toBeVisible()

  // 切回 RAG 会话：消息区内容随会话变化
  await page.getByRole('button', { name: /RAG 原理讨论/ }).click()
  await expect(page.getByText('检索增强生成的原理是什么？')).toBeVisible()
  await expect(page.getByText('RAG 通过先检索后生成的方式回答问题。')).toBeVisible()
  // RAG 会话历史消息带引用来源
  await expect(page.getByText('rag.md').first()).toBeVisible()
})

test('历史对话页展示全部会话', async ({ page }) => {
  await page.goto('/history')
  await expect(page.getByText('RAG 原理讨论')).toBeVisible()
  await expect(page.getByText('Milvus 调优')).toBeVisible()
})
