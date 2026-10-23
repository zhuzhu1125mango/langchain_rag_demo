<template>
  <div class="flex-1 flex h-full bg-page overflow-hidden">
    <KnowledgeBaseSidebar
      :knowledge-bases="knowledgeBases || []"
      :current-k-b="currentKB"
      @select-kb="selectKnowledgeBase"
      @create-kb="showCreateKBModal = true"
      @batch-delete="batchDeleteKnowledgeBases"
    />

    <div class="flex-1 flex flex-col overflow-hidden">
      <header class="bg-surface border-b border-line px-4 sm:px-6 py-3 sm:py-4">
        <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
          <div class="min-w-0">
            <h1 class="text-lg lg:text-xl font-semibold text-content truncate">
              {{ currentKB?.name || '选择知识库' }}
            </h1>
            <p class="hidden sm:block text-sm text-content-muted mt-1 truncate">
              {{ currentKB?.description || '管理当前知识库的文档' }}
            </p>
            <!-- 移动端知识库切换（桌面端使用左侧列表） -->
            <select
              v-if="knowledgeBases?.length"
              :value="currentKB?.id || ''"
              class="lg:hidden mt-2 w-full px-3 py-2 text-sm rounded-lg border border-line bg-surface text-content-secondary-strong"
              aria-label="切换知识库"
              @change="onMobileKBChange"
            >
              <option v-for="kb in knowledgeBases" :key="kb.id" :value="kb.id">{{ kb.name }}</option>
            </select>
          </div>
          <div v-if="currentKB" class="flex items-center gap-1 sm:gap-3 overflow-x-auto pb-1 -mx-1 px-1 sm:mx-0 sm:px-0 sm:pb-0">
            <button
              @click="showWikiDrawer = true"
              class="flex items-center gap-2 px-2 sm:px-3 py-2 text-sm text-content-tertiary hover:bg-surface-hover rounded-lg transition-colors flex-shrink-0"
              title="Wiki 页面"
            >
              <BookOpen class="w-4 h-4" />
              <span class="hidden sm:inline">Wiki 页面</span>
            </button>
            <button
              @click="showEditKBModal = true"
              class="flex items-center gap-2 px-2 sm:px-3 py-2 text-sm text-content-tertiary hover:bg-surface-hover rounded-lg transition-colors flex-shrink-0"
              title="编辑知识库"
            >
              <Edit3 class="w-4 h-4" />
              <span class="hidden sm:inline">编辑</span>
            </button>
            <button
              @click="deleteKnowledgeBaseConfirm"
              class="flex items-center gap-2 px-2 sm:px-3 py-2 text-sm text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg transition-colors flex-shrink-0"
              title="删除知识库"
            >
              <Trash2 class="w-4 h-4" />
              <span class="hidden sm:inline">删除</span>
            </button>
            <button
              @click="batchDelete"
              :disabled="kbStore.selectedCount === 0"
              :class="[
                'flex items-center gap-2 px-2 sm:px-4 py-2 text-sm rounded-lg transition-colors flex-shrink-0',
                kbStore.selectedCount > 0
                  ? 'bg-red-500 text-white hover:bg-red-600'
                  : 'bg-surface-disabled text-gray-400 cursor-not-allowed'
              ]"
              title="批量删除选中文档"
            >
              <Trash2 class="w-4 h-4" />
              <span class="hidden md:inline">批量删除 ({{ kbStore.selectedCount }})</span>
            </button>
            <button
              @click="showUploadDialog = true"
              class="flex items-center gap-2 px-2 sm:px-4 py-2 text-sm bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors flex-shrink-0"
              title="上传文档"
            >
              <Upload class="w-4 h-4" />
              <span class="hidden sm:inline">上传文档</span>
            </button>
          </div>
        </div>
      </header>

      <div v-if="currentKB" class="flex-1 overflow-auto p-4 sm:p-6">
        <!-- 标签页切换 -->
        <div class="flex items-center gap-1 bg-surface-subtle rounded-lg p-1 mb-6">
          <button
            @click="activeTab = 'documents'"
            :class="[
              'px-4 py-2 text-sm font-medium rounded-md transition-colors',
              activeTab === 'documents'
                ? 'bg-surface-raised-strong text-content shadow-sm'
                : 'text-content-secondary-muted hover:text-content'
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
                ? 'bg-surface-raised-strong text-content shadow-sm'
                : 'text-content-secondary-muted hover:text-content'
            ]"
          >
            <Network class="w-4 h-4 inline-block mr-2" />
            知识图谱
          </button>
        </div>

        <!-- 文档列表视图 -->
        <div v-if="activeTab === 'documents'" class="bg-surface rounded-xl shadow-sm border border-line">
          <div class="p-4 border-b border-line flex items-center justify-between">
            <div class="flex items-center gap-4">
              <div class="flex items-center gap-2">
                <Search class="w-4 h-4 text-gray-400" />
                <input
                  v-model="searchQuery"
                  type="text"
                  placeholder="搜索文档..."
                  @keyup.enter="onPerformSearch"
                  class="px-3 py-1.5 text-sm bg-surface-subtle border border-line rounded-lg text-content placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
                />
                <select
                  v-model="searchMode"
                  class="px-3 py-1.5 text-sm bg-surface-subtle border border-line rounded-lg text-content focus:outline-none focus:ring-2 focus:ring-primary-500"
                >
                  <option value="filename">文件名</option>
                  <option value="content">全文</option>
                </select>
              </div>
              <select
                v-model="filterStatus"
                class="px-3 py-1.5 text-sm bg-surface-subtle border border-line rounded-lg text-content focus:outline-none focus:ring-2 focus:ring-primary-500"
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
                class="px-3 py-1.5 text-sm text-content-tertiary hover:bg-surface-hover rounded-lg transition-colors"
              >
                清除搜索
              </button>
              <label class="flex items-center gap-2 text-sm text-content-tertiary">
                <input
                  type="checkbox"
                  @change="toggleSelectAll"
                  :checked="kbStore.selectedCount === documentsData?.items?.length && documentsData?.items?.length > 0"
                  class="rounded border-line-strong bg-surface-subtle text-primary-600 focus:ring-primary-500"
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
            <FolderOpen class="w-12 h-12 text-content-disabled mx-auto mb-4" />
            <p class="text-content-muted">暂无文档，请上传文档</p>
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
        <div v-if="activeTab === 'graph'" class="bg-surface rounded-xl shadow-sm border border-line h-[500px]">
          <KnowledgeGraph :kb-ids="currentKB ? [currentKB.id] : []" />
        </div>
      </div>

      <div v-else class="text-center">
        <Database class="w-16 h-16 text-content-disabled mx-auto mb-4" />
        <h3 class="text-lg font-medium text-content-muted">请选择一个知识库</h3>
        <p class="text-sm text-content-subtle mt-2">从左侧列表中选择或创建新的知识库</p>
        <!-- 移动端无左侧列表，提供直接创建入口 -->
        <button
          @click="showCreateKBModal = true"
          class="lg:hidden mt-4 inline-flex items-center gap-2 px-4 py-2 text-sm bg-primary-500 text-white rounded-lg hover:bg-primary-600 transition-colors"
        >
          <Plus class="w-4 h-4" />
          新建知识库
        </button>
      </div>
    </div>

    <el-dialog v-model="showCreateKBModal" title="创建知识库" width="450px">
      <div class="space-y-4">
        <div>
          <label class="block text-sm font-medium text-content-secondary mb-1">知识库名称 *</label>
          <input
            v-model="newKBName"
            type="text"
            placeholder="输入知识库名称"
            class="w-full px-3 py-2 text-sm border border-line-strong rounded-lg bg-surface-raised text-content placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
        </div>
        <div>
          <label class="block text-sm font-medium text-content-secondary mb-1">描述</label>
          <textarea
            v-model="newKBDescription"
            placeholder="输入知识库描述（可选）"
            rows="3"
            class="w-full px-3 py-2 text-sm border border-line-strong rounded-lg bg-surface-raised text-content placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none"
          ></textarea>
        </div>
      </div>
      <template #footer>
        <div class="flex gap-2">
          <button
            @click="showCreateKBModal = false"
            class="px-4 py-2 text-sm text-content-tertiary hover:bg-surface-hover rounded-lg transition-colors"
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
                : 'bg-surface-disabled text-gray-400 cursor-not-allowed'
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
          <label class="block text-sm font-medium text-content-secondary mb-1">知识库名称 *</label>
          <input
            v-model="editKBName"
            type="text"
            class="w-full px-3 py-2 text-sm border border-line-strong rounded-lg bg-surface-raised text-content placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500"
          />
        </div>
        <div>
          <label class="block text-sm font-medium text-content-secondary mb-1">描述</label>
          <textarea
            v-model="editKBDescription"
            rows="3"
            class="w-full px-3 py-2 text-sm border border-line-strong rounded-lg bg-surface-raised text-content placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-primary-500 resize-none"
          ></textarea>
        </div>
      </div>
      <template #footer>
        <div class="flex gap-2">
          <button
            @click="showEditKBModal = false"
            class="px-4 py-2 text-sm text-content-tertiary hover:bg-surface-hover rounded-lg transition-colors"
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
                : 'bg-surface-disabled text-gray-400 cursor-not-allowed'
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
import { Trash2, Upload, Edit3, FileText, Network, Search, FolderOpen, Database, BookOpen, Plus } from '@lucide/vue'
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
    handler: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge_bases'] })
    }
  })

  // 监听文档列表变更
  onNotification({
    type: ['doc_list_changed', 'doc_created', 'doc_deleted'],
    handler: (notification) => {
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

/** 移动端下拉切换知识库（桌面端使用左侧列表）。 */
function onMobileKBChange(event: Event): void {
  const id = (event.target as HTMLSelectElement).value
  const kb = (knowledgeBases.value || []).find(item => item.id === id)
  if (kb) selectKnowledgeBase(kb)
}

/** 上传完成回调：文档列表由 WebSocket 通知自动刷新，此处无需手动处理。 */
function onUploadComplete(): void {
  // 依赖 useWebSocketNotifications 监听的 doc_list_changed 通知刷新列表
}
</script>
