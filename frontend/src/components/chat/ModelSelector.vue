<template>
  <div
    v-if="visible"
    class="relative flex items-center"
    :title="title"
  >
    <Cpu
      class="pointer-events-none absolute left-2.5 w-3.5 h-3.5"
      :class="modelValue ? 'text-primary-600 dark:text-primary-400' : 'text-content-muted'"
    />
    <select
      :value="modelValue"
      aria-label="选择回答使用的模型"
      :class="selectClass"
      @change="onChange"
    >
      <option value="">{{ defaultOptionLabel }}</option>
      <option
        v-for="m in models"
        :key="m.id"
        :value="m.id"
      >
        {{ m.display_name }}
      </option>
    </select>
    <ChevronDown class="pointer-events-none absolute right-2 w-3 h-3 text-content-muted" />
  </div>
</template>

<script setup lang="ts">
/**
 * 对话页的模型选择器（工具条里的 pill）。
 *
 * 设计取舍：
 * - 用原生 `<select>` 而不是自绘下拉：键盘、触摸、读屏、点击外部收起全都不用自己写，
 *   这个组件只负责「样式像 pill」。自绘下拉在这个位置最容易出的 bug 是
 *   打开后不收起、或 Esc 不响应，而它带来的视觉收益很小。
 * - 空值代表「默认模型」而不是「不选」：用户清空选择时必须能回到默认，
 *   所以永远保留第一项。
 * - 只有一个可选项且它就是默认模型时不渲染：一个只有一个选项的下拉是噪音。
 *   但若「唯一可选项 ≠ 默认模型」，仍然渲染——否则用户永远选不到那个模型。
 */
import { computed } from 'vue'
import { ChevronDown, Cpu } from '@lucide/vue'
import type { AvailableModel } from '@/queries/models'

const props = defineProps<{
  /** 可选的模型列表（后端已过滤：user_selectable 且具备 chat 能力） */
  models: AvailableModel[]
  /** 当前选中的模型 id；空串表示使用默认模型 */
  modelValue: string
  /** answer 路由的默认模型 id，用于在「默认」项上标出它到底是哪个 */
  defaultModelId: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()

/** 默认模型的展示名：默认模型可能本身不可选，所以不能只从 models 里找 */
const defaultModelName = computed(() => {
  const hit = props.models.find(m => m.id === props.defaultModelId)
  return hit?.display_name || '默认模型'
})

const defaultOptionLabel = computed(() => `默认 · ${defaultModelName.value}`)

/** 唯一可选项恰好就是默认模型时隐藏：没有第二个选择，控件只是噪音。 */
const visible = computed(() => {
  if (props.models.length === 0) return false
  if (props.models.length === 1 && props.models[0]?.id === props.defaultModelId) return false
  return true
})

const title = computed(() =>
  props.modelValue
    ? `当前使用：${props.models.find(m => m.id === props.modelValue)?.display_name ?? props.modelValue}\n切换后对下一条消息生效`
    : `当前使用默认模型：${defaultModelName.value}\n选择其它模型只影响你之后发送的消息`
)

/**
 * 选中态与未选中态的样式必须**整串二选一**，不能把公共部分写在 `class`、
 * 状态部分写在 `:class`：`text-content-muted` 与 `text-primary-600` 同时存在时，
 * 谁生效取决于 Tailwind 产物里的顺序而不是模板里的顺序，会出现「选中了但还是灰色」。
 */
const selectClass = computed(() => {
  const base =
    'appearance-none max-w-44 rounded-full pl-7 pr-6 py-1.5 text-xs cursor-pointer truncate ' +
    'transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500'
  return props.modelValue
    ? `${base} bg-primary-50 dark:bg-primary-900/30 text-primary-600 dark:text-primary-400`
    : `${base} bg-transparent text-content-muted hover:bg-surface-hover`
})

function onChange(event: Event): void {
  emit('update:modelValue', (event.target as HTMLSelectElement).value)
}
</script>
