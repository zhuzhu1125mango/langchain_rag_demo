<template>
  <div class="flex-1 flex h-full bg-gray-50 dark:bg-dark-900 overflow-hidden">
    <KnowledgeBaseSidebar
      :knowledge-bases="knowledgeBases || []"
      :current-k-b="currentKB"
      @select-kb="selectKnowledgeBase"
      @create-kb="showCreateKBModal = true"
      @batch-delete="batchDeleteKnowledgeBases"
    />

    <div class="flex-1 flex flex-col overflow-hidden">
      <header class="bg-white dark:bg-dark-800 border-b border-gray-200 dark:border-dark-600 px-6 py-4">
        <div class="flex items-center justify-between">
          <div>
            <h1 class="text-xl font-semibold text-gray-800 dark:text-white">
              {{ currentKB?.name || '选择知识库' }}
            </h1>
            <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">
              {{ currentKB?.description || '管理当前知识库的文档' }}
            </p>
          </div>
          <div v-if="currentKB" class="flex items-center gap-3">
            <button
              @click="showWikiDrawer = true"
              class="flex items-center gap-2 px-3 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
            >
              <BookOpen class="w-4 h-4" />
              <span>Wiki 页面</span>
            </button>
            <button
              @click="showEditKBModal = true"
              class="flex items-center gap-2 px-3 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
            >
              <Edit3 class="w-4 h-4" />
              <span>编辑</span>
            </button>
            <button
              @click="deleteKnowledgeBaseConfirm"
              class="flex items-center gap-2 px-3 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg transition-colors"
            >
              <Trash2 class="w-4 h-4" />
              <span>删除</span>
            </button>
            <button
              @click="batchDelete"
              :disabled="kbStore.selectedCount === 0"
              :class="[
                'flex items-center gap-2 px-4 py-2 text-sm rounded-lg transition-colors',
                kbStore.selectedCount > 0
                  ? 'bg-red-500 text-white hover:bg-red-600'
                  : 'bg-gray-200 dark:bg-dark-700 text-gray-400 cursor-not-allowed'
              ]"
            >
              <Trash2 class="w-4 h-4" />
              <span>批量删除 ({{ kbStore.selectedCount }})</span>
            </button>
            <button
              @click="showUploadDialog = true"
              class="flex items-center gap-2 px-4 py-2 text-sm bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors"
            >
              <Upload class="w-4 h-4" />
              <span>上传文档</span>
            </button>
          </div>
        </div>
      </header>

      <div v-if="currentKB" class="flex-1 overflow-auto p-6">
        <!-- 标签页切换 -->
        <div class="flex items-center gap-1 bg-gray-100 dark:bg-dark-700 rounded-lg p-1 mb-6">
          <button
            @click="activeTab = 'documents'"
            :class="[
              'px-4 py-2 text-sm font-medium rounded-md transition-colors',
              activeTab === 'documents'
                ? 'bg-white dark:bg-dark-600 text-gray-800 dark:text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200'
            ]"
          >
            <FileText class="w-4 h-4 inline-block mr-2" />
            文档列表
          </button>
          <button
            @click="activeTab = 'graph'"
            :class="[
              'px-4 py-2 text-sm font-medium rounded-md transition-colors',
              activeTab === 'graph'
                ? 'bg-white dark:bg-dark-600 text-gray-800 dark:text-white shadow-sm'
                : 'text-gray-600 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200'
            ]"
          >
            <Network class="w-4 h-4 inline-block mr-2" />
            知识图谱
          </button>
        </div>

        <!-- 文档列表视图 -->
        <div v-if="activeTab === 'documents'" class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600">
          <div class="p-4 border-b border-gray-200 dark:border-dark-600 flex items-center justify-between">
            <div class="flex items-center gap-4">
              <div class="flex items-center gap-2">
                <Search class="w-4 h-4 text-gray-400" />
                <input
                  v-model="searchQuery"
                  type="text"
                  placeholder="搜索文档..."
                  @keyup.enter="performSearch"
                  class="px-3 py-1.5 text-sm bg-gray-100 dark:bg-dark-700 border border-gray-200 dark:border-dark-600 rounded-lg text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
                />
                <select
                  v-model="searchMode"
                  class="px-3 py-1.5 text-sm bg-gray-100 dark:bg-dark-700 border border-gray-200 dark:border-dark-600 rounded-lg text-gray-800 dark:text-white focus:outline-none focus:ring-2 focus:ring-primary-500"
                >
                  <option value="filename">文件名</option>
                  <option value="content">全文</option>
                </select>
              </div>
              <select
                v-model="filterStatus"
                class="px-3 py-1.5 text-sm bg-gray-100 dark:bg-dark-700 border border-gray-200 dark:border-dark-600 rounded-lg text-gray-800 dark:text-white focus:outline-none focus:ring-2 focus:ring-primary-500"
              >
                <option value="all">全部状态</option>
                <option value="active">已上架</option>
                <option value="inactive">已下架</option>
              </select>
            </div>
            <div class="flex items-center gap-3">
              <button
                v-if="searchQuery && searchMode === 'content'"
                @click="clearSearch"
                class="px-3 py-1.5 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
              >
                清除搜索
              </button>
              <label class="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
                <input
                  type="checkbox"
                  @change="toggleSelectAll"
                  :checked="kbStore.selectedCount === documentsData?.items?.length && documentsData?.items?.length > 0"
                  class="rounded border-gray-300 dark:border-dark-500 bg-gray-100 dark:bg-dark-700 text-primary-600 focus:ring-primary-500"
                />
                <span>全选</span>
              </label>
            </div>
          </div>

          <DocumentTable
            :documents="documentsData?.items || []"
            @toggle-select-all="toggleSelectAll"
            @preview="previewDocument"
            @classify="classifyDocument"
            @quality="evaluateQuality"
            @duplicate-detect="detectDuplicates"
            @toggle-status="toggleStatus"
            @reprocess="reprocessDocument"
            @delete="deleteDocument"
          />

          <div v-if="!documentsData?.items?.length && !showSearchResults" class="p-12 text-center">
            <FolderOpen class="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
            <p class="text-gray-500 dark:text-gray-400">暂无文档，请上传文档</p>
          </div>

          <!-- 全文搜索结果 -->
          <DocumentSearchResults
            v-if="showSearchResults"
            :results="searchResults"
            :query="searchQuery"
            :is-searching="isSearching"
            @close="clearSearch"
            @preview="previewDocumentById"
          />
        </div>

        <!-- 知识图谱视图 -->
        <div v-if="activeTab === 'graph'" class="bg-white dark:bg-dark-800 rounded-xl shadow-sm border border-gray-200 dark:border-dark-600 h-[500px]">
          <KnowledgeGraph :kb-ids="currentKB ? [currentKB.id] : []" />
        </div>
      </div>

      <div v-else class="text-center">
        <Database class="w-16 h-16 text-gray-300 dark:text-gray-600 mx-auto mb-4" />
        <h3 class="text-lg font-medium text-gray-500 dark:text-gray-400">请选择一个知识库</h3>
        <p class="text-sm text-gray-400 dark:text-gray-500 mt-2">从左侧列表中选择或创建新的知识库</p>
      </div>
    </div>

    <el-dialog v-model="showCreateKBModal" title="创建知识库" width="450px">
      <div class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">知识库名称 *</label>
          <input
            v-model="newKBName"
            type="text"
            placeholder="输入知识库名称"
            class="w-full px-3 py-2 text-sm border border-gray-300 dark:border-dark-600 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">描述</label>
          <textarea
            v-model="newKBDescription"
            placeholder="输入知识库描述（可选）"
            rows="3"
            class="w-full px-3 py-2 text-sm border border-gray-300 dark:border-dark-600 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none"
          ></textarea>
        </div>
      </div>
      <template #footer>
        <div class="flex gap-2">
          <button
            @click="showCreateKBModal = false"
            class="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
          >
            取消
          </button>
          <button
            @click="createKnowledgeBase"
            :disabled="!newKBName.trim()"
            :class="[
              'px-4 py-2 text-sm rounded-lg transition-colors',
              newKBName.trim()
                ? 'bg-primary-500 text-white hover:bg-primary-600'
                : 'bg-gray-200 dark:bg-dark-700 text-gray-400 cursor-not-allowed'
            ]"
          >
            创建
          </button>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="showEditKBModal" title="编辑知识库" width="450px">
      <div class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">知识库名称 *</label>
          <input
            v-model="editKBName"
            type="text"
            class="w-full px-3 py-2 text-sm border border-gray-300 dark:border-dark-600 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
        </div>
        <div>
          <label class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">描述</label>
          <textarea
            v-model="editKBDescription"
            rows="3"
            class="w-full px-3 py-2 text-sm border border-gray-300 dark:border-dark-600 rounded-lg bg-white dark:bg-dark-700 text-gray-800 dark:text-white placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none"
          ></textarea>
        </div>
      </div>
      <template #footer>
        <div class="flex gap-2">
          <button
            @click="showEditKBModal = false"
            class="px-4 py-2 text-sm text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
          >
            取消
          </button>
          <button
            @click="updateKnowledgeBase"
            :disabled="!editKBName.trim()"
            :class="[
              'px-4 py-2 text-sm rounded-lg transition-colors',
              editKBName.trim()
                ? 'bg-primary-500 text-white hover:bg-primary-600'
                : 'bg-gray-200 dark:bg-dark-700 text-gray-400 cursor-not-allowed'
            ]"
          >
            保存
          </button>
        </div>
      </template>
    </el-dialog>

    <UploadDocumentDialog
      v-model:visible="showUploadDialog"
      :kb-id="currentKB?.id"
      @uploaded="onUploadComplete"
    />

    <DocumentPreviewDialog
      v-model:visible="showPreviewDialog"
      :doc-id="previewDocId"
      :filename="previewFilename"
    />

    <DocumentAnalysisDialogs
      v-model:visible-classify="showClassifyModal"
      v-model:visible-quality="showQualityModal"
      v-model:visible-duplicate="showDuplicateModal"
      :doc="selectedDocForAction"
    />

    <WikiDrawer
      :visible="showWikiDrawer"
      :kb-id="currentKB?.id"
      :kb-name="currentKB?.name"
      @update:visible="showWikiDrawer = $event"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, watch, computed, onMounted } from 'vue'
import { useKBStore } from '@/stores/kb'
import { api } from '@/utils/axios'
import {
  useDocuments,
  useDeleteDocument,
  useBatchDeleteDocuments,
  useUpdateDocument,
  useKnowledgeBases,
  useCreateKnowledgeBase,
  useUpdateKnowledgeBase,
  useDeleteKnowledgeBase,
  useBatchDeleteKnowledgeBases,
  useSearchDocuments
} from '@/queries/kb'
import type { KnowledgeBase, Document, SearchResult } from '@/queries/kb'
import { Trash2, Upload, Edit3, FileText, Network, Search, FolderOpen, Database, BookOpen } from '@lucide/vue'
import { useQueryClient } from '@tanstack/vue-query'
import { useToast } from '@/composables/useToast'
import { ElMessageBox } from 'element-plus'
import { useWebSocketNotifications } from '@/composables/useNotifications'
import KnowledgeGraph from '@/components/KnowledgeGraph.vue'
import KnowledgeBaseSidebar from '@/components/knowledge-base/KnowledgeBaseSidebar.vue'
import DocumentTable from '@/components/knowledge-base/DocumentTable.vue'
import DocumentSearchResults from '@/components/knowledge-base/DocumentSearchResults.vue'
import UploadDocumentDialog from '@/components/knowledge-base/UploadDocumentDialog.vue'
import DocumentPreviewDialog from '@/components/knowledge-base/DocumentPreviewDialog.vue'
import DocumentAnalysisDialogs from '@/components/knowledge-base/DocumentAnalysisDialogs.vue'

/**
 * 知识库管理页面主视图。
 *
 * 负责知识库与文档列表的整体编排：侧边栏、文档表格、全文搜索、知识图谱，
 * 以及上传/预览/分类/质量/查重等弹窗的显隐控制。具体交互与 API 调用已拆分至
 * components/knowledge-base 下的子组件中。
 */

const kbStore = useKBStore()
const queryClient = useQueryClient()
const toast = useToast()
const searchQuery = ref('')
const searchMode = ref<'filename' | 'content'>('filename')
const filterStatus = ref('all')
const showUploadDialog = ref(false)
const showPreviewDialog = ref(false)
const showCreateKBModal = ref(false)
const showEditKBModal = ref(false)
const showWikiDrawer = ref(false)
const previewDocId = ref('')
const previewFilename = ref('')
const newKBName = ref('')
const newKBDescription = ref('')
const editKBName = ref('')
const editKBDescription = ref('')
const activeTab = ref<'documents' | 'graph'>('documents')

// 全文搜索相关状态
const searchResults = ref<SearchResult[]>([])
const isSearching = ref(false)
const showSearchResults = ref(false)

// 文档操作弹窗显隐与目标文档
const showClassifyModal = ref(false)
const showQualityModal = ref(false)
const showDuplicateModal = ref(false)
const selectedDocForAction = ref<Document | null>(null)

// WebSocket实时通知
const { onNotification } = useWebSocketNotifications()

// 监听实时通知
onMounted(() => {
  // 监听知识库列表变更
  onNotification({
    type: ['kb_list_changed', 'kb_created', 'kb_updated', 'kb_deleted'],
    handler: (notification) => {
      console.log('[KB View] KB notification received:', notification)
      queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
    }
  })

  // 监听文档列表变更
  onNotification({
    type: ['doc_list_changed', 'doc_created', 'doc_deleted'],
    handler: (notification) => {
      console.log('[KB View] Doc notification received:', notification)
      queryClient.invalidateQueries({ queryKey: ['documents'] })
      queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })

      // 显示提示信息
      const action = notification.data.action
      if (action === 'deleted') {
        toast.info('文档已删除', '文档已从列表中移除')
      }
    }
  })
})

const { data: knowledgeBases } = useKnowledgeBases()

const { data: documentsData } = useDocuments({
  status: computed(() => filterStatus.value === 'all' ? undefined : filterStatus.value)
})
const deleteMutation = useDeleteDocument()
const batchDeleteMutation = useBatchDeleteDocuments()
const updateMutation = useUpdateDocument()
const createKBMutation = useCreateKnowledgeBase()
const updateKBMutation = useUpdateKnowledgeBase()
const deleteKBMutation = useDeleteKnowledgeBase()
const batchDeleteKBMutation = useBatchDeleteKnowledgeBases()
const searchDocuments = useSearchDocuments()

const currentKB = computed(() => kbStore.currentKB)

watch(knowledgeBases, (newKbs) => {
  if (newKbs) {
    kbStore.setKnowledgeBases(newKbs)
    // 若当前选中的知识库已被删除，自动切换到剩余中的默认或第一个
    if (kbStore.currentKB) {
      const stillExists = newKbs.some(kb => kb.id === kbStore.currentKB?.id)
      if (!stillExists) {
        const defaultKB = newKbs.find(kb => kb.is_default)
        kbStore.setCurrentKB(defaultKB || newKbs[0] || null)
      }
    }
  }
}, { immediate: true })

/** 切换当前选中的知识库并清空文档选择。 */
function selectKnowledgeBase(kb: KnowledgeBase): void {
  kbStore.setCurrentKB(kb)
  kbStore.clearSelection()
}

/** 文档列表全选/取消全选。 */
function toggleSelectAll(event: Event): void {
  const target = event.target as HTMLInputElement
  const data = documentsData.value

  if (target.checked && data?.items?.length) {
    kbStore.selectAllDocuments(data.items)
  } else {
    kbStore.clearSelection()
  }
}

/**
 * 删除单个文档，采用乐观更新策略。
 *
 * 整体流程：1) 二次确认后立即从 queryClient 缓存中移除该文档（乐观更新），
 * 让 UI 即时反馈；2) 调用 deleteMutation 异步删除；3) 若 API 失败则
 * 通过 invalidateQueries 刷新文档与知识库列表以回滚到真实状态。
 */
function deleteDocument(doc: Document): void {
  if (confirm(`确定要删除文档 "${doc.filename}" 吗？`)) {
    // 立即从列表中移除（乐观更新）
    const originalDocs = documentsData.value?.items || []
    const docIndex = originalDocs.findIndex(d => d.id === doc.id)

    // 立即提示用户
    toast.info('正在删除文档...', `正在后台删除 ${doc.filename}`)

    // 立即移除文档（乐观更新）
    if (docIndex > -1) {
      // 创建一个新的数组触发响应式更新
      const newDocs = [...originalDocs]
      newDocs.splice(docIndex, 1)
      // 通过 queryClient 直接更新缓存
      queryClient.setQueryData(['documents'], (old: { items?: Document[] } | undefined) => ({
        ...old,
        items: newDocs
      }))
    }

    // 调用API删除
    deleteMutation.mutate(doc.id, {
      onSuccess: () => {
        // API已返回，但后端是异步删除
        console.log('[Delete] Delete task submitted')
      },
      onError: (error) => {
        // 如果API失败，恢复文档列表
        toast.error('删除失败', error instanceof Error ? error.message : '未知错误')
        queryClient.invalidateQueries({ queryKey: ['documents'] })
        queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
      }
    })
  }
}

/** 提交文档重新处理任务。 */
async function reprocessDocument(doc: Document): Promise<void> {
  if (confirm(`确定要重新处理文档 "${doc.filename}" 吗？`)) {
    try {
      const response = await api.post<{ message?: string }>(`/documents/${doc.id}/reprocess`)
      toast.success('重新处理任务已提交', response.message || '文档将在后台重新处理')
      queryClient.invalidateQueries({ queryKey: ['documents'] })
    } catch (error) {
      toast.error('重新处理失败', error instanceof Error ? error.message : '未知错误')
    }
  }
}

/** 批量删除选中的文档。 */
async function batchDelete(): Promise<void> {
  const selectedCount = kbStore.selectedCount

  if (selectedCount === 0) {
    toast.warning('请先选择要删除的文档')
    return
  }

  if (confirm(`确定要删除选中的 ${selectedCount} 个文档吗？`)) {
    const ids = kbStore.selectedDocuments

    // 立即提示用户
    toast.info('正在删除文档...', `正在后台删除 ${selectedCount} 个文档`)

    // 清空选择（乐观更新）
    kbStore.clearSelection()

    // 调用API删除
    batchDeleteMutation.mutate(ids, {
      onSuccess: () => {
        // API已返回，但后端是异步删除
        console.log('[Batch Delete] Delete tasks submitted')
      },
      onError: (error) => {
        // 如果API失败，恢复文档列表
        toast.error('批量删除失败', error instanceof Error ? error.message : '未知错误')
        queryClient.invalidateQueries({ queryKey: ['documents'] })
        queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
      }
    })
  }
}

/** 切换文档上下架状态。 */
function toggleStatus(doc: Document): void {
  const backendStatus = doc.status === 'active' ? 'draft' : 'published'
  const newStatusText = doc.status === 'active' ? '下架' : '上架'
  updateMutation.mutate({
    id: doc.id,
    data: { status: backendStatus }
  }, {
    onSuccess: () => {
      toast.success(`文档已成功${newStatusText}`)
    },
    onError: (error: unknown) => {
      toast.error(`${newStatusText}失败`, error instanceof Error ? error.message : '未知错误')
    }
  })
}

/** 打开文档预览弹窗。 */
function previewDocument(doc: Document): void {
  previewDocId.value = doc.id
  previewFilename.value = doc.filename || '未知文件'
  showPreviewDialog.value = true
}

/** 根据文档 ID 打开预览弹窗（用于搜索结果）。 */
function previewDocumentById(docId: string): void {
  previewDocId.value = docId
  previewFilename.value = '加载中...'
  showPreviewDialog.value = true
}

/** 触发文档智能分类弹窗。 */
function classifyDocument(doc: Document): void {
  selectedDocForAction.value = doc
  showClassifyModal.value = true
}

/** 触发文档质量评估弹窗。 */
function evaluateQuality(doc: Document): void {
  selectedDocForAction.value = doc
  showQualityModal.value = true
}

/** 触发文档重复检测弹窗。 */
function detectDuplicates(doc: Document): void {
  selectedDocForAction.value = doc
  showDuplicateModal.value = true
}

/** 执行全文搜索并展示结果。 */
async function performSearch(): Promise<void> {
  if (!searchQuery.value.trim() || searchMode.value !== 'content') {
    return
  }

  isSearching.value = true
  showSearchResults.value = true

  try {
    const results = await searchDocuments(searchQuery.value.trim(), kbStore.currentKB?.id)
    searchResults.value = results
    if (results.length === 0) {
      toast.info('未找到匹配的文档内容')
    }
  } catch (error) {
    toast.error('搜索失败', error instanceof Error ? error.message : '未知错误')
    searchResults.value = []
  } finally {
    isSearching.value = false
  }
}

/** 清空全文搜索状态并返回文档列表。 */
function clearSearch(): void {
  searchQuery.value = ''
  searchResults.value = []
  showSearchResults.value = false
}

/** 创建新知识库并切换到该知识库。 */
function createKnowledgeBase(): void {
  if (!newKBName.value.trim()) {
    toast.warning('请输入知识库名称')
    return
  }

  createKBMutation.mutate({
    name: newKBName.value.trim(),
    description: newKBDescription.value.trim() || undefined
  }, {
    onSuccess: (newKB) => {
      toast.success('知识库创建成功')
      newKBName.value = ''
      newKBDescription.value = ''
      showCreateKBModal.value = false
      queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
      if (newKB) {
        kbStore.setCurrentKB(newKB)
      }
    },
    onError: (error) => {
      toast.error('创建失败', error instanceof Error ? error.message : '未知错误')
    }
  })
}

watch(currentKB, (kb) => {
  if (kb) {
    editKBName.value = kb.name
    editKBDescription.value = kb.description || ''
  }
})

/** 更新当前知识库名称与描述。 */
function updateKnowledgeBase(): void {
  if (!editKBName.value.trim()) {
    toast.warning('请输入知识库名称')
    return
  }

  if (!kbStore.currentKB) return

  updateKBMutation.mutate({
    id: kbStore.currentKB.id,
    data: {
      name: editKBName.value.trim(),
      description: editKBDescription.value.trim() || undefined
    }
  }, {
    onSuccess: () => {
      toast.success('知识库更新成功')
      showEditKBModal.value = false
      queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
    },
    onError: (error) => {
      toast.error('更新失败', error instanceof Error ? error.message : '未知错误')
    }
  })
}

/** 删除当前选中的知识库及其全部文档。 */
function deleteKnowledgeBaseConfirm(): void {
  if (!kbStore.currentKB) return

  if (confirm(`确定要删除知识库 "${kbStore.currentKB.name}" 吗？这将删除该知识库中的所有文档。`)) {
    deleteKBMutation.mutate(kbStore.currentKB.id, {
      onSuccess: () => {
        toast.success('知识库删除成功')
        kbStore.setCurrentKB(null)
        queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
        queryClient.invalidateQueries({ queryKey: ['documents'] })
      },
      onError: (error) => {
        toast.error('删除失败', error instanceof Error ? error.message : '未知错误')
      }
    })
  }
}

/** 批量删除选中的知识库。 */
async function batchDeleteKnowledgeBases(): Promise<void> {
  const selectedCount = kbStore.selectedKBCount

  if (selectedCount === 0) {
    toast.warning('请先选择要删除的知识库')
    return
  }

  try {
    await ElMessageBox.confirm(
      `确定要删除选中的 ${selectedCount} 个知识库吗？其中的所有文档也将被删除，操作不可恢复。`,
      '批量删除确认',
      {
        confirmButtonText: '删除',
        cancelButtonText: '取消',
        type: 'warning',
        confirmButtonClass: 'el-button--danger'
      }
    )
  } catch {
    return
  }

  const ids = kbStore.selectedKBs
  toast.info('正在删除知识库...', `正在后台删除 ${selectedCount} 个知识库`)

  batchDeleteKBMutation.mutate(ids, {
    onSuccess: (result) => {
      toast.success(
        '批量删除成功',
        `已删除 ${result.deleted_count} 个知识库${result.skipped_ids.length ? `，跳过 ${result.skipped_ids.length} 个` : ''}`
      )
      kbStore.exitBatchKBMode()
      queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
      queryClient.invalidateQueries({ queryKey: ['documents'] })
    },
    onError: (error) => {
      toast.error('批量删除失败', error instanceof Error ? error.message : '未知错误')
      queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
    }
  })
}

/** 上传完成回调：文档列表由 WebSocket 通知自动刷新，此处无需手动处理。 */
function onUploadComplete(): void {
  // 依赖 useWebSocketNotifications 监听的 doc_list_changed 通知刷新列表
}
</script>
