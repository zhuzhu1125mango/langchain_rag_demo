import { describe, it, expect } from 'vitest'
import { generateId } from '../id'

describe('generateId', () => {
  it('格式为 时间戳-随机串', () => {
    expect(generateId()).toMatch(/^[\w-]+-[\w-]+$/)
  })

  it('批量生成保持唯一', () => {
    const ids = new Set(Array.from({ length: 1000 }, generateId))
    expect(ids.size).toBe(1000)
  })
})
