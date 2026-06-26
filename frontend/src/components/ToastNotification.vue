<template>
  <Teleport to="body">
    <div class="fixed top-4 right-4 z-50 space-y-2">
      <TransitionGroup name="toast">
        <div
          v-for="toast in toasts"
          :key="toast.id"
          :class="[
            'flex items-center gap-3 px-4 py-3 rounded-lg shadow-lg min-w-[280px] max-w-[400px] backdrop-blur-sm',
            toastClass(toast.type)
          ]"
        >
          <component :is="toastIcon(toast.type)" class="w-5 h-5 flex-shrink-0" />
          <div class="flex-1 min-w-0">
            <p class="text-sm font-medium">{{ toast.title }}</p>
            <p v-if="toast.message" class="text-xs opacity-80 mt-0.5 truncate">{{ toast.message }}</p>
          </div>
          <button
            @click="removeToast(toast.id)"
            class="p-1 rounded hover:bg-white/20 transition-colors flex-shrink-0"
          >
            <X class="w-4 h-4" />
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
/**
 * Toast 通知单条组件
 * @description 全局 Toast 容器（Teleport 至 body），消费 useToast composable 提供的 toasts 队列，
 * 按类型渲染对应的背景色与图标，并通过 TransitionGroup 实现进入/离开/移动动画。
 */
import { CheckCircle, AlertCircle, AlertTriangle, Info, X } from '@lucide/vue'
import { useToast, type ToastType } from '@/composables/useToast'

const { toasts, removeToast } = useToast()

/** 根据 Toast 类型返回对应的背景色 + 文本色 Tailwind 类。 */
function toastClass(type: ToastType): string {
  const classes: Record<ToastType, string> = {
    success: 'bg-green-500 text-white',
    error: 'bg-red-500 text-white',
    warning: 'bg-yellow-500 text-white',
    info: 'bg-blue-500 text-white'
  }
  return classes[type]
}

/** 根据 Toast 类型返回对应的 lucide 图标组件（success/error/warning/info）。 */
function toastIcon(type: ToastType) {
  const icons: Record<ToastType, unknown> = {
    success: CheckCircle,
    error: AlertCircle,
    warning: AlertTriangle,
    info: Info
  }
  return icons[type]
}
</script>

<style scoped>
.toast-enter-active {
  transition: all 0.3s ease-out;
}

.toast-leave-active {
  transition: all 0.2s ease-in;
}

.toast-enter-from {
  opacity: 0;
  transform: translateX(100%);
}

.toast-leave-to {
  opacity: 0;
  transform: translateX(100%);
}

.toast-move {
  transition: transform 0.3s ease;
}
</style>