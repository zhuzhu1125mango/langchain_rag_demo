<template>
  <el-dialog
    :model-value="visible"
    :title="step === 'form' ? '新建 API 密钥' : '密钥已创建'"
    width="520px"
    append-to-body
    :close-on-click-modal="false"
    :before-close="handleBeforeClose"
    @update:model-value="emit('update:visible', $event)"
  >
    <!-- Step 1：表单 -->
    <div v-if="step === 'form'" class="space-y-4">
      <div>
        <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">
          名称 <span class="text-red-500">*</span>
        </label>
        <el-input
          v-model="form.name"
          :maxlength="64"
          show-word-limit
          placeholder="用途备注，如 claude-code-office"
          :data-testid="'api-key-name-input'"
        />
        <p v-if="formError" class="mt-1 text-xs text-red-500">{{ formError }}</p>
      </div>
      <div>
        <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1.5">有效期</label>
        <el-select v-model="form.expiresInDays" class="w-full" data-testid="api-key-expiry-select">
          <el-option label="永久" :value="0" />
          <el-option label="30 天" :value="30" />
          <el-option label="90 天" :value="90" />
          <el-option label="180 天" :value="180" />
        </el-select>
      </div>
      <p class="text-xs text-gray-500 dark:text-gray-400">
        密钥与你的账号绑定，仅能访问你有权限的数据；明文仅在创建后显示一次。
      </p>
    </div>

    <!-- Step 2：一次性明文展示 -->
    <div v-else class="space-y-4">
      <div class="flex items-center gap-2 text-green-600 dark:text-green-400">
        <CheckCircle class="w-5 h-5" />
        <span class="font-medium">密钥已创建</span>
      </div>
      <div class="flex items-center gap-2 bg-gray-900 rounded-lg p-4">
        <code class="flex-1 text-gray-100 font-mono text-sm break-all">{{ created?.key }}</code>
        <button
          class="shrink-0 px-2.5 py-1.5 rounded-md text-xs font-medium bg-gray-700 text-gray-100 hover:bg-gray-600 transition-colors"
          data-testid="api-key-copy-btn"
          @click="copyKey"
        >
          {{ copied ? '已复制' : '复制' }}
        </button>
      </div>
      <div class="p-3 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800">
        <p class="text-sm text-amber-700 dark:text-amber-400 flex items-start gap-1.5">
          <AlertTriangle class="w-4 h-4 shrink-0 mt-0.5" />
          请立即保存到密码管理器，关闭后无法再次查看。
        </p>
      </div>
    </div>

    <template #footer>
      <template v-if="step === 'form'">
        <el-button @click="emit('update:visible', false)">取消</el-button>
        <el-button type="primary" :loading="createMutation.isPending.value" data-testid="api-key-create-btn" @click="handleCreate">
          创建
        </el-button>
      </template>
      <template v-else>
        <el-button type="primary" data-testid="api-key-saved-btn" @click="confirmSaved">我已保存，关闭</el-button>
      </template>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
/**
 * 创建 API 密钥两步对话框（E.6）。
 *
 * Step 1 表单（名称必填 + 可选有效期）→ Step 2 一次性明文展示（深色代码块
 * + 复制 + "关闭后无法再次查看"警示）。Step 2 状态下任何关闭途径（×/ESC）
 * 均经 before-close 二次确认，防止明文未保存即丢失（禁用点击遮罩关闭）。
 */
import { computed, reactive, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'
import { useClipboard } from '@vueuse/core'
import { AlertTriangle, CheckCircle } from '@lucide/vue'
import { useCreateApiKey, type ApiKeyCreated } from '@/queries/apiKeys'
import { handleMutationError, useToast } from '@/composables/useToast'

const props = defineProps<{
  visible: boolean
  /** 当前活跃（未吊销/未过期）密钥数，供表单提交前快速拦截。 */
  activeCount: number
  /** 每用户上限（来自 /config/info）。 */
  maxPerUser: number
}>()

const emit = defineEmits<{
  (e: 'update:visible', value: boolean): void
  (e: 'created'): void
}>()

type Step = 'form' | 'plaintext'

const step = ref<Step>('form')
const formError = ref('')
const created = ref<ApiKeyCreated | null>(null)
const form = reactive({ name: '', expiresInDays: 0 })

const createMutation = useCreateApiKey()
const toast = useToast()
const { copy, copied } = useClipboard({ copiedDuring: 2000 })

watch(
  () => props.visible,
  (open) => {
    if (open) {
      step.value = 'form'
      form.name = ''
      form.expiresInDays = 0
      formError.value = ''
      created.value = null
    }
  }
)

const activeCount = computed(() => props.activeCount)

async function handleCreate(): Promise<void> {
  formError.value = ''
  if (!form.name.trim()) {
    formError.value = '请填写密钥名称'
    return
  }
  if (activeCount.value >= props.maxPerUser) {
    formError.value = `已达到密钥数量上限（${props.maxPerUser}），请先吊销不再使用的密钥`
    return
  }
  try {
    created.value = await createMutation.mutateAsync({
      name: form.name.trim(),
      expires_in_days: form.expiresInDays > 0 ? form.expiresInDays : undefined
    })
    step.value = 'plaintext'
  } catch (error) {
    // 422 超上限等后端校验错误定位到名称字段下方展示
    formError.value = error instanceof Error ? error.message : '创建失败，请稍后重试'
    handleMutationError(error, '创建 API 密钥失败', '创建失败，请稍后重试')
  }
}

async function copyKey(): Promise<void> {
  if (!created.value) return
  await copy(created.value.key)
  toast.success('已复制到剪贴板')
}

/** Step 2 明文未保存前的关闭拦截：×/ESC 与 footer 关闭共用二次确认。 */
async function handleBeforeClose(done: () => void): Promise<void> {
  if (step.value !== 'plaintext') {
    done()
    return
  }
  try {
    await ElMessageBox.confirm('密钥尚未保存，关闭后无法再次查看。确认已保存？', '确认关闭', {
      confirmButtonText: '已保存并关闭',
      cancelButtonText: '返回查看',
      type: 'warning'
    })
    done()
  } catch {
    // 用户取消：留在明文页
  }
}

function confirmSaved(): void {
  emit('update:visible', false)
  emit('created')
}
</script>
