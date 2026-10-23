<template>
  <div class="flex-1 overflow-auto bg-page p-4 sm:p-6">
    <div class="max-w-2xl mx-auto space-y-6">
      <div>
        <h1 class="text-xl font-semibold text-content">常规设置</h1>
        <p class="text-sm text-content-muted mt-1">管理您的偏好设置和系统配置</p>
      </div>

      <AppCard padding="lg">
        <div class="flex items-center justify-between mb-4">
          <h2 class="font-medium text-content">外观设置</h2>
        </div>
        
        <div class="space-y-4">
          <div class="flex items-center justify-between p-4 bg-surface-muted rounded-lg">
            <div>
              <p class="font-medium text-content">主题模式</p>
              <p class="text-sm text-content-muted mt-1">选择浅色或深色主题</p>
            </div>
            <div class="flex gap-2">
              <AppButton
                :variant="appStore.theme === 'light' ? 'primary' : 'secondary'"
                @click="setTheme('light')"
              >
                <Sun class="w-4 h-4" />
                浅色
              </AppButton>
              <AppButton
                :variant="appStore.theme === 'dark' ? 'primary' : 'secondary'"
                @click="setTheme('dark')"
              >
                <Moon class="w-4 h-4" />
                深色
              </AppButton>
            </div>
          </div>
        </div>
      </AppCard>

      <AppCard padding="lg">
        <div class="flex items-center justify-between mb-4">
          <h2 class="font-medium text-content">对话设置</h2>
        </div>
        
        <div class="space-y-4">
          <div class="flex items-center justify-between p-4 bg-surface-muted rounded-lg">
            <div>
              <p class="font-medium text-content">打字机效果</p>
              <p class="text-sm text-content-muted mt-1">逐字显示回答内容</p>
            </div>
            <el-switch v-model="localSettings.typingEffect" class="w-12 h-6" />
          </div>
          
          <div class="flex items-center justify-between p-4 bg-surface-muted rounded-lg">
            <div>
              <p class="font-medium text-content">自动滚动</p>
              <p class="text-sm text-content-muted mt-1">新消息到达时自动滚动到底部</p>
            </div>
            <el-switch v-model="localSettings.autoScroll" class="w-12 h-6" />
          </div>
          
          <div class="p-4 bg-surface-muted rounded-lg">
            <label class="block text-sm font-medium text-content-secondary mb-3">最大输入长度</label>
            <div class="flex items-center gap-4">
              <input
                v-model.number="localSettings.maxInputLength"
                type="range"
                min="500"
                max="5000"
                step="100"
                class="flex-1 h-2 bg-surface-strong rounded-lg appearance-none cursor-pointer"
              />
              <span class="text-sm font-medium text-content w-20 text-right">{{ localSettings.maxInputLength }}</span>
            </div>
          </div>
        </div>
      </AppCard>

      <AppCard padding="lg">
        <div class="flex items-center justify-between mb-4">
          <h2 class="font-medium text-content">知识库设置</h2>
          <AppButton
            variant="ghost"
            size="sm"
            :disabled="updateConfigMutation.isPending.value"
            @click="resetToDefault"
          >
            重置为默认值
          </AppButton>
        </div>
        
        <div class="space-y-4">
          <div class="p-4 bg-surface-muted rounded-lg">
            <label class="block text-sm font-medium text-content-secondary mb-3">文本分片大小</label>
            <div class="flex items-center gap-4">
              <input
                v-model.number="localSettings.chunkSize"
                type="range"
                min="200"
                max="2000"
                step="100"
                class="flex-1 h-2 bg-surface-strong rounded-lg appearance-none cursor-pointer"
              />
              <span class="text-sm font-medium text-content w-28 text-right">{{ localSettings.chunkSize }} 字符</span>
            </div>
            <p class="text-xs text-content-muted mt-2">影响文档切分后的每块大小，较大的值会保留更多上下文但可能增加检索时间</p>
          </div>
          
          <div class="p-4 bg-surface-muted rounded-lg">
            <label class="block text-sm font-medium text-content-secondary mb-3">重叠字符数</label>
            <div class="flex items-center gap-4">
              <input
                v-model.number="localSettings.chunkOverlap"
                type="range"
                min="0"
                max="500"
                step="50"
                class="flex-1 h-2 bg-surface-strong rounded-lg appearance-none cursor-pointer"
              />
              <span class="text-sm font-medium text-content w-28 text-right">{{ localSettings.chunkOverlap }} 字符</span>
            </div>
            <p class="text-xs text-content-muted mt-2">相邻分片之间的重叠部分，用于保持上下文连贯性</p>
          </div>
          
          <div class="p-4 bg-surface-muted rounded-lg">
            <label class="block text-sm font-medium text-content-secondary mb-3">检索数量 (Top-K)</label>
            <div class="flex items-center gap-4">
              <input
                v-model.number="localSettings.topK"
                type="range"
                min="1"
                max="20"
                step="1"
                class="flex-1 h-2 bg-surface-strong rounded-lg appearance-none cursor-pointer"
              />
              <span class="text-sm font-medium text-content w-20 text-right">{{ localSettings.topK }}</span>
            </div>
            <p class="text-xs text-content-muted mt-2">检索时返回的最相关文档块数量</p>
          </div>
        </div>
      </AppCard>

      <AppCard padding="lg">
        <div class="flex items-center justify-between mb-4">
          <h2 class="font-medium text-content">模型配置</h2>
        </div>
        
        <div class="space-y-4 text-sm">
          <div class="flex items-center justify-between p-4 bg-surface-muted rounded-lg">
            <span class="text-content-tertiary">嵌入模型</span>
            <span class="font-medium text-content">{{ modelConfig?.embedding_model_name }}</span>
          </div>
          <div class="flex items-center justify-between p-4 bg-surface-muted rounded-lg">
            <span class="text-content-tertiary">对话模型（深度思考·开）</span>
            <span class="font-medium text-content">{{ modelConfig?.ollama_model_name }}</span>
          </div>
          <div class="flex items-center justify-between p-4 bg-surface-muted rounded-lg">
            <span class="text-content-tertiary">对话模型（深度思考·关）</span>
            <span class="font-medium text-content">{{ modelConfig?.ollama_direct_model_name || modelConfig?.ollama_model_name }}</span>
          </div>
        </div>
      </AppCard>

      <AppCard padding="lg">
        <div class="flex items-center justify-between mb-4">
          <h2 class="font-medium text-content">关于</h2>
        </div>
        
        <div class="space-y-4 text-sm">
          <div class="flex items-center justify-between p-4 bg-surface-muted rounded-lg">
            <span class="text-content-tertiary">版本</span>
            <span class="font-medium text-content">1.0.0</span>
          </div>
          <div class="flex items-center justify-between p-4 bg-surface-muted rounded-lg">
            <span class="text-content-tertiary">技术栈</span>
            <span class="font-medium text-content">Vue 3 + FastAPI</span>
          </div>
          <div class="flex items-center justify-between p-4 bg-surface-muted rounded-lg">
            <span class="text-content-tertiary">模型</span>
            <span class="font-medium text-content">Ollama Local</span>
          </div>
        </div>
      </AppCard>

      <AppButton
        variant="primary"
        size="lg"
        block
        :disabled="updateConfigMutation.isPending.value"
        :loading="updateConfigMutation.isPending.value"
        @click="saveSettings"
      >
        <Save class="w-4 h-4" />
        保存设置
      </AppButton>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 通用设置页（主题等）。
 *
 * 管理外观主题、对话偏好、知识库处理参数与模型配置，
 * 设置持久化到 localStorage，处理类参数同步到服务端。
 */
import { ref, watch, onMounted } from 'vue'
import { useAppStore } from '@/stores/app'
import { Sun, Moon, Save } from '@lucide/vue'
import { useToast } from '@/composables/useToast'
import { AppButton, AppCard } from '@/components/ui'
import {
  useProcessingConfig,
  useModelConfig,
  useUpdateProcessingConfig,
  useResetConfig,
  type ProcessingConfig
} from '@/queries/kb'

const appStore = useAppStore()
const toast = useToast()

const { data: processingConfig, refetch: refetchProcessing } = useProcessingConfig()
const { data: modelConfig } = useModelConfig()
const updateConfigMutation = useUpdateProcessingConfig()
const resetMutation = useResetConfig()

interface Settings {
  typingEffect: boolean
  autoScroll: boolean
  maxInputLength: number
  chunkSize: number
  chunkOverlap: number
  topK: number
}

const localSettings = ref<Settings>({
  typingEffect: true,
  autoScroll: true,
  maxInputLength: 2000,
  chunkSize: 500,
  chunkOverlap: 50,
  topK: 3
})

onMounted(() => {
  const saved = localStorage.getItem('rag-settings')
  if (saved) {
    // D8：损坏/非法 JSON 时回退默认设置，避免整个设置页崩溃
    try {
      // 只合并已知键，防止陈旧/异常结构污染当前设置
      const pick: Partial<Settings> = {}
      const parsed = JSON.parse(saved) as Record<string, unknown>
      for (const key of Object.keys(localSettings.value) as (keyof Settings)[]) {
        if (typeof parsed[key] === typeof localSettings.value[key]) {
          // @ts-expect-error —— 已用同类型守卫校验，parsed 值为对应字段类型
          pick[key] = parsed[key]
        }
      }
      localSettings.value = { ...localSettings.value, ...pick }
    } catch {
      console.warn('设置缓存解析失败，已回退默认值')
      localStorage.removeItem('rag-settings')
    }
  }
  
  if (processingConfig.value) {
    localSettings.value.chunkSize = processingConfig.value.chunk_size
    localSettings.value.chunkOverlap = processingConfig.value.chunk_overlap
    localSettings.value.topK = processingConfig.value.top_k
  }
})

watch(processingConfig, (newConfig) => {
  if (newConfig) {
    localSettings.value.chunkSize = newConfig.chunk_size
    localSettings.value.chunkOverlap = newConfig.chunk_overlap
    localSettings.value.topK = newConfig.top_k
  }
})

/** 切换主题模式，委托 appStore.setDark 同步持久化与样式。 */
function setTheme(theme: 'light' | 'dark'): void {
  appStore.setDark(theme === 'dark')
}

/** 保存设置：本地项写入 localStorage，处理参数变更时同步到服务端。 */
async function saveSettings(): Promise<void> {
  localStorage.setItem('rag-settings', JSON.stringify(localSettings.value))
  
  const configChanges: Partial<ProcessingConfig> = {}
  if (processingConfig.value) {
    if (localSettings.value.chunkSize !== processingConfig.value.chunk_size) {
      configChanges.chunk_size = localSettings.value.chunkSize
    }
    if (localSettings.value.chunkOverlap !== processingConfig.value.chunk_overlap) {
      configChanges.chunk_overlap = localSettings.value.chunkOverlap
    }
    if (localSettings.value.topK !== processingConfig.value.top_k) {
      configChanges.top_k = localSettings.value.topK
    }
  }

  if (Object.keys(configChanges).length > 0) {
    try {
      await updateConfigMutation.mutateAsync(configChanges)
      await refetchProcessing()
      toast.success('设置已保存到服务器')
    } catch {
      toast.error('保存到服务器失败，请重试')
    }
  } else {
    toast.success('设置已保存')
  }
}

/** 重置处理参数为默认值并同步本地表单与服务端配置。 */
async function resetToDefault(): Promise<void> {
  try {
    await resetMutation.mutateAsync()
    await refetchProcessing()
    localSettings.value.chunkSize = 500
    localSettings.value.chunkOverlap = 50
    localSettings.value.topK = 3
    toast.success('配置已重置为默认值')
  } catch {
    toast.error('重置失败，请重试')
  }
}
</script>