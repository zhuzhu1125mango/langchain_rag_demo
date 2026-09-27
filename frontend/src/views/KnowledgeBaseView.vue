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
                  @keyup.enter="onPerformSearch"
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
                @click="onClearSearch"
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
            @close="onClearSearch"
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
import { useDocuments, useKnowledgeBases } from '@/queries/kb'
import type { KnowledgeBase } from '@/queries/kb'
import { Trash2, Upload, Edit3, FileText, Network, Search, FolderOpen, Database, BookOpen } from '@lucide/vue'
import { useQueryClient } from '@tanstack/vue-query'
import { useToast } from '@/composables/useToast'
import { useWebSocketNotifications } from '@/composables/useNotifications'
import KnowledgeGraph from '@/components/KnowledgeGraph.vue'
import KnowledgeBaseSidebar from '@/components/knowledge-base/KnowledgeBaseSidebar.vue'
import DocumentTable from '@/components/knowledge-base/DocumentTable.vue'
import DocumentSearchResults from '@/components/knowledge-base/DocumentSearchResults.vue'
import UploadDocumentDialog from '@/components/knowledge-base/UploadDocumentDialog.vue'
import DocumentPreviewDialog from '@/components/knowledge-base/DocumentPreviewDialog.vue'
import DocumentAnalysisDialogs from '@/components/knowledge-base/DocumentAnalysisDialogs.vue'
import { useDocumentActions } from '@/composables/useDocumentActions'
import { useKbManage } from '@/composables/useKbManage'
import { useDocumentDialogs } from '@/composables/useDocumentDialogs'
import { useContentSearch } from '@/composables/useContentSearch'

/**
 * 知识库管理页面主视图（编排层）。
 *
 * 模板与页面状态在此汇聚；具体逻辑已拆分至 composables：
 * - useDocumentActions：文档删除/批量删除/重处理/上下架/全选
 * - useKbManage：知识库创建/编辑/删除/批量删除弹窗与 CRUD
 * - useDocumentDialogs：预览/分类/质量/查重弹窗状态
 * - useContentSearch：全文搜索
 */

const kbStore = useKBStore()
const queryClient = useQueryClient()
const toast = useToast()
const searchQuery = ref('')
const searchMode = ref<'filename' | 'content'>('filename')
const filterStatus = ref('all')
const showUploadDialog = ref(false)
const showWikiDrawer = ref(false)
const activeTab = ref<'documents' | 'graph'>('documents')

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

// 文档操作：删除/批量删除/重处理/上下架/全选
const {
  toggleSelectAll,
  deleteDocument,
  reprocessDocument,
  batchDelete,
  toggleStatus
} = useDocumentActions(documentsData)

// 知识库管理：创建/编辑/删除/批量删除弹窗与 CRUD
const {
  showCreateKBModal,
  showEditKBModal,
  newKBName,
  newKBDescription,
  editKBName,
  editKBDescription,
  createKnowledgeBase,
  updateKnowledgeBase,
  deleteKnowledgeBaseConfirm,
  batchDeleteKnowledgeBases
} = useKbManage(currentKB)

// 文档弹窗：预览/分类/质量/查重
const {
  showPreviewDialog,
  previewDocId,
  previewFilename,
  showClassifyModal,
  showQualityModal,
  showDuplicateModal,
  selectedDocForAction,
  previewDocument,
  previewDocumentById,
  classifyDocument,
  evaluateQuality,
  detectDuplicates
} = useDocumentDialogs()

// 全文搜索
const {
  searchResults,
  isSearching,
  showSearchResults,
  performSearch,
  clearSearch
} = useContentSearch(computed(() => kbStore.currentKB?.id))

/** 清除搜索：视图侧同时清空输入框内容（输入框 v-model 在本视图）。 */
function onClearSearch(): void {
  searchQuery.value = ''
  clearSearch()
}

/** 回车触发搜索（转发当前输入与模式）。 */
function onPerformSearch(): void {
  void performSearch(searchQuery.value, searchMode.value)
}

/** 切换当前选中的知识库并清空文档选择。 */
function selectKnowledgeBase(kb: KnowledgeBase): void {
  kbStore.setCurrentKB(kb)
  kbStore.clearSelection()
}

/** 上传完成回调：文档列表由 WebSocket 通知自动刷新，此处无需手动处理。 */
function onUploadComplete(): void {
  // 依赖 useWebSocketNotifications 监听的 doc_list_changed 通知刷新列表
}
</script>
