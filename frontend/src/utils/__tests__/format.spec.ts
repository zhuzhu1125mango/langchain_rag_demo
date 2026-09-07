import { describe, it, expect } from 'vitest'
import { formatDate, formatChatDate } from '../format'

describe('formatDate', () => {
  it('空入参返回空字符串', () => {
    expect(formatDate()).toBe('')
    expect(formatDate(null)).toBe('')
    expect(formatDate('')).toBe('')
  })

  it('有效日期返回 YYYY/MM/DD 开头的格式', () => {
    const result = formatDate('2026-09-06T10:30:00Z')
    expect(result).toMatch(/^\d{4}\/\d{2}\/\d{2}/)
  })

  it('无效日期字符串返回 Invalid Date 而非抛错', () => {
    expect(formatDate('not-a-date')).toContain('Invalid')
  })
})

describe('formatChatDate', () => {
  it('空入参返回空字符串', () => {
    expect(formatChatDate()).toBe('')
    expect(formatChatDate(null)).toBe('')
    expect(formatChatDate('')).toBe('')
  })

  it('今天的日期仅返回 HH:MM 时间', () => {
    const now = new Date()
    const todayStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}T08:05:00`
    expect(formatChatDate(todayStr)).toMatch(/^\d{2}:\d{2}$/)
  })

  it('非今天返回短日期（含月字）', () => {
    const result = formatChatDate('2020-01-01T10:00:00Z')
    expect(result).toContain('月')
  })
})
