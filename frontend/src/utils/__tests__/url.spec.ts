import { describe, it, expect, vi, afterEach } from 'vitest'
import { isSafeExternalUrl, openExternalUrl } from '../url'

describe('isSafeExternalUrl', () => {
  it('http/https 协议放行', () => {
    expect(isSafeExternalUrl('http://example.com')).toBe(true)
    expect(isSafeExternalUrl('https://example.com/path?q=1')).toBe(true)
  })

  it('危险协议拒绝', () => {
    expect(isSafeExternalUrl('javascript:alert(1)')).toBe(false)
    expect(isSafeExternalUrl('data:text/html,<script>alert(1)</script>')).toBe(false)
    expect(isSafeExternalUrl('vbscript:msgbox(1)')).toBe(false)
  })

  it('空串与非合法 URL 拒绝', () => {
    expect(isSafeExternalUrl('')).toBe(false)
    expect(isSafeExternalUrl('not a url')).toBe(false)
  })
})

describe('openExternalUrl', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('安全 URL 以 _blank + noopener 打开', () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null)
    openExternalUrl('https://example.com')
    expect(openSpy).toHaveBeenCalledWith('https://example.com', '_blank', 'noopener,noreferrer')
  })

  it('不安全 URL 静默忽略', () => {
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null)
    openExternalUrl('javascript:alert(1)')
    expect(openSpy).not.toHaveBeenCalled()
  })
})
