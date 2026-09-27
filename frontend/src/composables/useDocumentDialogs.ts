import { ref } from 'vue'
import type { Document } from '@/queries/kb'

/**
 * 文档弹窗状态编排（从 KnowledgeBaseView.vue 抽取，行为不变）。
 *
 * 覆盖：预览、智能分类、质量评估、重复检测四类弹窗的显隐与目标文档。
 */
export function useDocumentDialogs() {
  const showPreviewDialog = ref(false)
  const previewDocId = ref('')
  const previewFilename = ref('')

  const showClassifyModal = ref(false)
  const showQualityModal = ref(false)
  const showDuplicateModal = ref(false)
  const selectedDocForAction = ref<Document | null>(null)

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

  return {
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
  }
}
