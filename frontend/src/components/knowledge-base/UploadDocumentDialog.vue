<template>
  <el-dialog
    :model-value="visible"
    @update:model-value="onVisibleChange"
    title="上传文档"
    width="500px"
  >
    <div class="space-y-4">
      <div>
        <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">选择文件</label>
        <input
          type="file"
          ref="fileInput"
          @change="onFileSelect"
          multiple
          accept=".pdf,.docx,.doc,.xlsx,.xls,.pptx,.ppt,.md,.txt,.csv,.json,.html,.epub"
          class="hidden"
        />
        <button
          @click="fileInput?.click()"
          :disabled="isUploading"
          class="w-full p-6 border-2 border-dashed border-gray-300 dark:border-dark-600 rounded-lg hover:border-primary-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <Upload class="w-8 h-8 text-gray-400 mx-auto mb-2" />
          <p class="text-sm text-gray-600 dark:text-gray-400">点击或拖拽上传文件</p>
          <p class="text-xs text-gray-400 mt-1">支持 PDF, DOCX, XLSX, PPTX, MD, TXT 等格式</p>
        </button>
      </div>

      <!-- 实时进度条 -->
      <div v-if="isUploading" class="space-y-2">
        <div class="flex items-center justify-between text-sm">
          <span class="text-gray-600 dark:text-gray-300">{{ currentUploadFile }}</span>
          <span class="text-gray-500">{{ uploadProgress }}%</span>
        </div>
        <div class="w-full bg-gray-200 dark:bg-dark-600 rounded-full h-2">
          <div
            class="bg-primary-500 h-2 rounded-full transition-all duration-300"
            :style="{ width: uploadProgress + '%' }"
          ></div>
        </div>
        <p class="text-xs text-gray-500">{{ uploadMessage }}</p>
      </div>

      <div v-if="selectedFiles.length > 0 && !isUploading" class="space-y-2">
        <p class="text-sm text-gray-600 dark:text-gray-300">已选择 {{ selectedFiles.length }} 个文件</p>
        <div v-for="file in selectedFiles" :key="file.name" class="flex items-center justify-between">
          <span class="text-sm text-gray-800 dark:text-white">{{ file.name }}</span>
          <button @click="removeFile(file)" class="text-gray-400 hover:text-red-500">
            <X class="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
    <template #footer>
      <div class="flex items-center justify-between">
        <span class="text-sm text-gray-500">
          进度: {{ uploadProgress }}%
        </span>
        <div class="flex gap-2">
          <button
            @click="cancelUpload"
            class="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
          >
            {{ isUploading ? '取消上传' : '取消' }}
          </button>
          <button
            @click="uploadFiles"
            :disabled="selectedFiles.length === 0 || isUploading"
            :class="[
              'px-4 py-2 text-sm rounded-lg transition-colors',
              selectedFiles.length > 0 && !isUploading
                ? 'bg-primary-500 text-white hover:bg-primary-600'
                : 'bg-gray-200 dark:bg-dark-700 text-gray-400 cursor-not-allowed'
            ]"
          >
            {{ isUploading ? '上传中...' : '上传' }}
          </button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
/**
 * 上传文档对话框组件
 * @description 支持多文件选择与逐个上传，并通过 WebSocket 实时回传解析/向量化进度。
 *
 * 上传时序设计：
 * 1. 为本次上传生成 uploadId（crypto.randomUUID），作为后端关联 HTTP 上传任务与 WebSocket 进度通道的唯一标识；
 * 2. 先建立 WebSocket 进度连接（setupProgressWebSocket），确保服务端后续推送的进度消息不会丢失；
 * 3. 再发起 HTTP multipart 上传请求（api.post /documents/upload），接口立即返回，文档在后台异步处理；
 * 4. WebSocket 通过 onmessage 接收 data.progress / data.message / data.status 字段并更新 UI；
 * 5. 上传结束（成功或失败）后关闭对应 WebSocket 连接。
 *
 * @props visible - 对话框显隐（v-model:visible）
 * @props kbId - 目标知识库 ID
 *
 * @emits update:visible - 显隐变化
 * @emits uploaded - 上传任务提交完成（文档仍在后台处理）
 */
import { ref } from 'vue'
import { Upload, X } from '@lucide/vue'
import { api } from '@/utils/axios'
import { buildWsUrl } from '@/utils/ws'
import { useToast } from '@/composables/useToast'

const props = defineProps<{
  visible: boolean
  kbId?: string
}>()

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'uploaded'): void
}>()

const toast = useToast()
const selectedFiles = ref<File[]>([])
const uploadProgress = ref(0)
const fileInput = ref<HTMLInputElement | null>(null)
const isUploading = ref(false)
const currentUploadFile = ref('')
const uploadMessage = ref('')
const wsConnections = ref<Map<string, WebSocket>>(new Map())

/** 处理弹窗显隐切换，并在非上传态关闭时重置内部状态。 */
function onVisibleChange(val: boolean): void {
  emit('update:visible', val)
  if (!val && !isUploading.value) {
    selectedFiles.value = []
    uploadProgress.value = 0
    currentUploadFile.value = ''
    uploadMessage.value = ''
  }
}

/** 文件选择后将文件加入待上传列表。 */
function onFileSelect(event: Event): void {
  const target = event.target as HTMLInputElement
  const files = Array.from(target.files || [])
  selectedFiles.value = [...selectedFiles.value, ...files]
}

/** 从待上传列表中移除指定文件。 */
function removeFile(file: File): void {
  selectedFiles.value = selectedFiles.value.filter(f => f !== file)
}

/** 逐个上传文件，并为每个上传任务建立 WebSocket 进度连接。 */
async function uploadFiles(): Promise<void> {
  if (selectedFiles.value.length === 0) {
    toast.warning('请先选择要上传的文件')
    return
  }

  const kbId = props.kbId
  if (!kbId) {
    toast.warning('请先选择一个知识库')
    return
  }

  isUploading.value = true
  uploadProgress.value = 0
  let successCount = 0
  const failedFiles: string[] = []

  for (let i = 0; i < selectedFiles.value.length; i++) {
    const file = selectedFiles.value[i]
    if (!file) continue

    currentUploadFile.value = file.name
    uploadMessage.value = '准备上传...'

    let uploadId = ''

    try {
      // uploadId 作为本次上传任务的唯一标识，同时用于关联 HTTP 上传请求与 WebSocket 进度通道
      uploadId = crypto.randomUUID()
      // 先建立 WebSocket 进度连接，确保服务端后续推送的进度消息不会因连接未就绪而丢失
      await setupProgressWebSocket(uploadId)

      const formData = new FormData()
      formData.append('file', file as File)

      uploadMessage.value = '正在上传...'
      const response = await api.post('/documents/upload', formData, {
        params: { kb_id: kbId, upload_id: uploadId },
        timeout: 30000, // 减小超时时间，因为现在是异步处理
        // 删除默认 Content-Type，让浏览器根据 FormData 自动生成带 boundary 的 multipart 请求头，
        // 否则后端无法正确解析 multipart 边界
        transformRequest: [(data, headers) => {
          delete headers['Content-Type']
          return data
        }]
      })

      // API立即返回，但文档在后台处理
      successCount++
      console.log('[Upload] File uploaded, processing in background:', response)

    } catch (error) {
      failedFiles.push(file.name)
      uploadMessage.value = `上传失败: ${error instanceof Error ? error.message : '未知错误'}`
    } finally {
      uploadProgress.value = Math.round(((i + 1) / selectedFiles.value.length) * 100)
      if (uploadId) {
        closeWebSocket(uploadId)
      }
    }
  }

  isUploading.value = false

  if (failedFiles.length > 0) {
    if (successCount === 0) {
      toast.error('所有文件上传失败', `失败文件：${failedFiles.join(', ')}`)
    } else {
      toast.info(`已提交 ${successCount} 个上传任务`, `失败 ${failedFiles.length} 个，文档正在后台处理中`)
    }
  } else {
    toast.info(`已提交 ${successCount} 个上传任务`, '文档正在后台处理中，处理完成后会自动更新列表')
  }

  selectedFiles.value = []
  uploadProgress.value = 0
  currentUploadFile.value = ''
  uploadMessage.value = ''

  emit('update:visible', false)
  emit('uploaded')
}

/** 为指定上传任务建立 WebSocket 进度连接。 */
function setupProgressWebSocket(uploadId: string): Promise<void> {
  return new Promise((resolve) => {
    // 首帧鉴权：api_key 不走 URL query（避免进反向代理访问日志），
    // 连接建立后发送 auth 帧，收到服务端 auth_ok 确认后再开始上传流程
    const apiKey = localStorage.getItem('api_key') || import.meta.env.VITE_API_KEY
    const wsUrl = buildWsUrl(`/api/documents/upload/progress/ws/${uploadId}`)
    const ws = new WebSocket(wsUrl)

    wsConnections.value.set(uploadId, ws)

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'auth', api_key: apiKey || '' }))
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        // 首帧鉴权确认：服务端已认证，后续进度推送可达
        if (data.type === 'auth_ok') {
          resolve()
          return
        }
        // data.progress：当前处理进度百分比（0-100）
        if (data.progress !== undefined) {
          uploadProgress.value = Math.round(data.progress)
        }
        // data.message：当前阶段的人类可读描述（如"正在解析 PDF"、"正在向量化"）
        if (data.message) {
          uploadMessage.value = data.message
        }
        // data.status：任务状态，'completed' 表示后台处理完成，强制进度为 100%
        if (data.status === 'completed') {
          uploadProgress.value = 100
        }
      } catch {
        // 忽略解析错误
      }
    }

    ws.onerror = () => {
      closeWebSocket(uploadId)
      // 鉴权或连接失败也放行上传：进度退化为仅轮询/最终结果展示
      resolve()
    }

    ws.onclose = () => {
      wsConnections.value.delete(uploadId)
      // 兜底：鉴权被拒（1008）时不一定触发 onerror，放行上传避免流程卡死
      resolve()
    }
  })
}

/** 关闭并移除指定上传任务的 WebSocket 连接。 */
function closeWebSocket(uploadId: string): void {
  const ws = wsConnections.value.get(uploadId)
  if (ws) {
    try {
      ws.close()
    } catch {
      // 忽略关闭错误
    }
    wsConnections.value.delete(uploadId)
  }
}

/** 取消上传过程或关闭上传弹窗。 */
function cancelUpload(): void {
  if (isUploading.value) {
    wsConnections.value.forEach((ws) => {
      try {
        ws.close()
      } catch {
        // 忽略错误
      }
    })
    wsConnections.value.clear()

    isUploading.value = false
    uploadProgress.value = 0
    currentUploadFile.value = ''
    uploadMessage.value = ''
  } else {
    emit('update:visible', false)
  }
}
</script>
