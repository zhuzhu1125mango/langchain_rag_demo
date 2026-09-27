/**
 * JWT Token 存取与有效性校验。
 *
 * 安全（W1-3）：token 仅存 sessionStorage（标签页级，关闭即失效），
 * 不再落 localStorage——降低 XSS 持久化窃取面。所有 token 读写
 * 统一走本模块，禁止组件内散落直访存储。
 */

const TOKEN_KEY = 'token'

/** 读取当前 token（sessionStorage）。 */
export function getToken(): string | null {
  return sessionStorage.getItem(TOKEN_KEY)
}

/** 写入 token（sessionStorage）。 */
export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token)
}

/** 清除 token（sessionStorage）。 */
export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY)
}

/** 解码 JWT payload（base64url → JSON）。解析失败返回 null。 */
function decodeJwtPayload(token: string): Record<string, unknown> | null {
  try {
    const payloadPart = token.split('.')[1]
    if (!payloadPart) {
      return null
    }
    const base64 = payloadPart.replace(/-/g, '+').replace(/_/g, '/')
    const json = decodeURIComponent(
      atob(base64)
        .split('')
        .map(c => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2))
        .join('')
    )
    const payload = JSON.parse(json)
    return typeof payload === 'object' && payload !== null ? payload : null
  } catch {
    return null
  }
}

/**
 * 校验 token 是否有效：存在、可解析且未过期。
 *
 * 路由守卫不能只凭"存在"放行——过期/伪造 token 也会发出必然 401 的请求。
 * 缺少 exp 或无法解析时视为无效（后端签发的 JWT 恒含 exp）。
 */
export function isTokenValid(token: string | null): boolean {
  if (!token) {
    return false
  }
  const exp = decodeJwtPayload(token)?.exp
  if (typeof exp !== 'number') {
    return false
  }
  // 60s 宽松量：容忍客户端时钟略慢，避免刚登录即被误判过期
  return exp > Math.floor(Date.now() / 1000) - 60
}
