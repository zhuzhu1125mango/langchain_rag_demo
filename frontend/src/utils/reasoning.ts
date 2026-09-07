import type { ReasoningStep } from '@/queries/chat'

/**
 * reasoning 步骤合并工具。
 *
 * 与后端 ReasoningTracker 按 step 去重的语义对齐：
 * 同一 step 的新事件覆盖旧条目（如「搜索中 → 搜索完成」），新的 step 追加到末尾。
 */

/** 将 reasoning 步骤合并到现有列表：同一 step 的新事件覆盖旧条目，新的追加。 */
export function mergeReasoningSteps(existing: ReasoningStep[], incoming: ReasoningStep[]): ReasoningStep[] {
  const map = new Map<string, ReasoningStep>()
  existing.forEach(s => map.set(s.step, s))
  incoming.forEach(s => map.set(s.step, s))
  return Array.from(map.values())
}
