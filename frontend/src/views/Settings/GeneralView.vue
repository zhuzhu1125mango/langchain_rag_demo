<template>
  <div class="flex-1 overflow-auto bg-gray-50 dark:bg-dark-900 p-6">
    <div class="max-w-2xl mx-auto space-y-6">
      <div>
        <h1 class="text-xl font-semibold text-gray-800 dark:text-white">常规设置</h1>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">管理您的偏好设置和系统配置</p>
      </div>

      <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-6">
        <div class="flex items-center justify-between mb-4">
          <h2 class="font-medium text-gray-800 dark:text-white">外观设置</h2>
        </div>
        
        <div class="space-y-4">
          <div class="flex items-center justify-between p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <div>
              <p class="font-medium text-gray-800 dark:text-white">主题模式</p>
              <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">选择浅色或深色主题</p>
            </div>
            <div class="flex gap-2">
              <button
                @click="setTheme('light')"
                :class="[
                  'px-4 py-2 rounded-lg text-sm font-medium transition-colors',
                  appStore.theme === 'light'
                    ? 'bg-primary-500 text-white'
                    : 'bg-white dark:bg-dark-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-500'
                ]"
              >
                <Sun class="w-4 h-4 inline mr-2" />
                浅色
              </button>
              <button
                @click="setTheme('dark')"
                :class="[
                  'px-4 py-2 rounded-lg text-sm font-medium transition-colors',
                  appStore.theme === 'dark'
                    ? 'bg-primary-500 text-white'
                    : 'bg-white dark:bg-dark-600 text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-500'
                ]"
              >
                <Moon class="w-4 h-4 inline mr-2" />
                深色
              </button>
            </div>
          </div>
        </div>
      </div>

      <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-6">
        <div class="flex items-center justify-between mb-4">
          <h2 class="font-medium text-gray-800 dark:text-white">对话设置</h2>
        </div>
        
        <div class="space-y-4">
          <div class="flex items-center justify-between p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <div>
              <p class="font-medium text-gray-800 dark:text-white">打字机效果</p>
              <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">逐字显示回答内容</p>
            </div>
            <el-switch v-model="localSettings.typingEffect" class="w-12 h-6" />
          </div>
          
          <div class="flex items-center justify-between p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <div>
              <p class="font-medium text-gray-800 dark:text-white">自动滚动</p>
              <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">新消息到达时自动滚动到底部</p>
            </div>
            <el-switch v-model="localSettings.autoScroll" class="w-12 h-6" />
          </div>
          
          <div class="p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">最大输入长度</label>
            <div class="flex items-center gap-4">
              <input
                v-model.number="localSettings.maxInputLength"
                type="range"
                min="500"
                max="5000"
                step="100"
                class="flex-1 h-2 bg-gray-200 dark:bg-dark-600 rounded-lg appearance-none cursor-pointer"
              />
              <span class="text-sm font-medium text-gray-800 dark:text-white w-20 text-right">{{ localSettings.maxInputLength }}</span>
            </div>
          </div>
        </div>
      </div>

      <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-6">
        <div class="flex items-center justify-between mb-4">
          <h2 class="font-medium text-gray-800 dark:text-white">知识库设置</h2>
          <button
            @click="resetToDefault"
            :disabled="updateConfigMutation.isPending.value"
            class="text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition-colors disabled:opacity-50"
          >
            重置为默认值
          </button>
        </div>
        
        <div class="space-y-4">
          <div class="p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">文本分片大小</label>
            <div class="flex items-center gap-4">
              <input
                v-model.number="localSettings.chunkSize"
                type="range"
                min="200"
                max="2000"
                step="100"
                class="flex-1 h-2 bg-gray-200 dark:bg-dark-600 rounded-lg appearance-none cursor-pointer"
              />
              <span class="text-sm font-medium text-gray-800 dark:text-white w-28 text-right">{{ localSettings.chunkSize }} 字符</span>
            </div>
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-2">影响文档切分后的每块大小，较大的值会保留更多上下文但可能增加检索时间</p>
          </div>
          
          <div class="p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">重叠字符数</label>
            <div class="flex items-center gap-4">
              <input
                v-model.number="localSettings.chunkOverlap"
                type="range"
                min="0"
                max="500"
                step="50"
                class="flex-1 h-2 bg-gray-200 dark:bg-dark-600 rounded-lg appearance-none cursor-pointer"
              />
              <span class="text-sm font-medium text-gray-800 dark:text-white w-28 text-right">{{ localSettings.chunkOverlap }} 字符</span>
            </div>
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-2">相邻分片之间的重叠部分，用于保持上下文连贯性</p>
          </div>
          
          <div class="p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">检索数量 (Top-K)</label>
            <div class="flex items-center gap-4">
              <input
                v-model.number="localSettings.topK"
                type="range"
                min="1"
                max="20"
                step="1"
                class="flex-1 h-2 bg-gray-200 dark:bg-dark-600 rounded-lg appearance-none cursor-pointer"
              />
              <span class="text-sm font-medium text-gray-800 dark:text-white w-20 text-right">{{ localSettings.topK }}</span>
            </div>
            <p class="text-xs text-gray-500 dark:text-gray-400 mt-2">检索时返回的最相关文档块数量</p>
          </div>
        </div>
      </div>

      <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-6">
        <div class="flex items-center justify-between mb-4">
          <h2 class="font-medium text-gray-800 dark:text-white">模型配置</h2>
        </div>
        
        <div class="space-y-4 text-sm">
          <div class="flex items-center justify-between p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <span class="text-gray-600 dark:text-gray-300">嵌入模型</span>
            <span class="font-medium text-gray-800 dark:text-white">{{ modelConfig?.embedding_model_name }}</span>
          </div>
          <div class="flex items-center justify-between p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <span class="text-gray-600 dark:text-gray-300">LLM 模型</span>
            <span class="font-medium text-gray-800 dark:text-white">{{ modelConfig?.ollama_model_name }}</span>
          </div>
        </div>
      </div>

      <div class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 p-6">
        <div class="flex items-center justify-between mb-4">
          <h2 class="font-medium text-gray-800 dark:text-white">关于</h2>
        </div>
        
        <div class="space-y-4 text-sm">
          <div class="flex items-center justify-between p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <span class="text-gray-600 dark:text-gray-300">版本</span>
            <span class="font-medium text-gray-800 dark:text-white">1.0.0</span>
          </div>
          <div class="flex items-center justify-between p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <span class="text-gray-600 dark:text-gray-300">技术栈</span>
            <span class="font-medium text-gray-800 dark:text-white">Vue 3 + FastAPI</span>
          </div>
          <div class="flex items-center justify-between p-4 bg-gray-50 dark:bg-dark-700 rounded-lg">
            <span class="text-gray-600 dark:text-gray-300">模型</span>
            <span class="font-medium text-gray-800 dark:text-white">Ollama Local</span>
          </div>
        </div>
      </div>

      <button
        @click="saveSettings"
        :disabled="updateConfigMutation.isPending.value"
        class="w-full py-3 bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors font-medium disabled:opacity-50 disabled:cursor-not-allowed"
      >
        <Save class="w-4 h-4 inline mr-2" />
        保存设置
      </button>
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
    const parsed = JSON.parse(saved)
    localSettings.value = { ...localSettings.value, ...parsed }
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