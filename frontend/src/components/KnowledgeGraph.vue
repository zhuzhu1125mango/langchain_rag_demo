<template>
  <div class="w-full h-full flex flex-col">
    <div class="flex items-center justify-between mb-4">
      <div class="flex items-center gap-2">
        <Network class="w-4 h-4 text-primary-500" />
        <span class="text-sm font-medium text-gray-700 dark:text-gray-300">知识图谱</span>
      </div>
      <div class="flex items-center gap-2">
        <button
          @click="refreshGraph"
          :disabled="isLoading"
          class="p-1.5 text-gray-400 hover:text-primary-500 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors disabled:opacity-50"
          title="刷新图谱"
        >
          <RefreshCw class="w-4 h-4" />
        </button>
        <button
          @click="showFullGraph"
          class="px-3 py-1.5 text-xs text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-dark-700 rounded-lg transition-colors"
        >
          查看完整图谱
        </button>
      </div>
    </div>

    <div v-if="isLoading" class="flex-1 flex items-center justify-center">
      <div class="flex flex-col items-center gap-3">
        <div class="animate-spin rounded-full h-8 w-8 border-b-2 border-primary-500"></div>
        <p class="text-sm text-gray-500 dark:text-gray-400">正在加载图谱...</p>
      </div>
    </div>

    <div v-else-if="graphData.nodes.length === 0" class="flex-1 flex items-center justify-center">
      <div class="text-center">
        <Network class="w-12 h-12 text-gray-300 dark:text-gray-600 mx-auto mb-3" />
        <p class="text-sm text-gray-500 dark:text-gray-400">暂无图谱数据</p>
        <p class="text-xs text-gray-400 mt-1">请先选择知识库并上传文档</p>
      </div>
    </div>

    <div v-else class="flex-1 relative bg-gray-50 dark:bg-dark-800 rounded-xl border border-gray-200 dark:border-dark-600 overflow-hidden">
      <svg 
        ref="svgRef"
        :width="svgWidth"
        :height="svgHeight"
        class="w-full h-full"
        @click="handleSvgClick"
      >
        <defs>
          <marker
            id="arrowhead"
            markerWidth="10"
            markerHeight="7"
            refX="9"
            refY="3.5"
            orient="auto"
          >
            <polygon
              points="0 0, 10 3.5, 0 7"
              fill="#9CA3AF"
              class="dark:fill-gray-500"
            />
          </marker>
          <filter id="glow">
            <feGaussianBlur stdDeviation="2" result="coloredBlur"/>
            <feMerge>
              <feMergeNode in="coloredBlur"/>
              <feMergeNode in="SourceGraphic"/>
            </feMerge>
          </filter>
        </defs>

        <!-- 连接线 -->
        <g>
          <line
            v-for="(edge, index) in graphData.edges"
            :key="'edge-' + index"
            :x1="getNodePosition(edge.from).x"
            :y1="getNodePosition(edge.from).y"
            :x2="getNodePosition(edge.to).x"
            :y2="getNodePosition(edge.to).y"
            :stroke="getEdgeColor(edge)"
            stroke-width="2"
            marker-end="url(#arrowhead)"
            class="transition-all duration-300 hover:stroke-primary-500"
            :class="{ 'opacity-50': selectedNode && !isConnected(selectedNode.id, edge) }"
          />
        </g>

        <!-- 节点 -->
        <g
          v-for="node in graphData.nodes"
          :key="node.id"
          :transform="`translate(${getNodePosition(node.id).x}, ${getNodePosition(node.id).y})`"
          class="cursor-pointer transition-all duration-300"
          :class="{
            'opacity-50': selectedNode && !isNodeSelected(node) && !isConnected(selectedNode.id, undefined, node),
            'filter:url(#glow)': isNodeSelected(node)
          }"
          @click.stop="selectNode(node)"
        >
          <!-- 节点背景圆 -->
          <circle
            :r="getNodeRadius(node)"
            :fill="getNodeColor(node)"
            :stroke="isNodeSelected(node) ? '#3B82F6' : '#E5E7EB'"
            stroke-width="2"
            class="dark:stroke-gray-600"
          />
          
          <!-- 节点图标 -->
          <text
            text-anchor="middle"
            dominant-baseline="middle"
            font-size="16"
            class="select-none"
          >
            {{ getNodeIcon(node) }}
          </text>

          <!-- 节点标签 -->
          <text
            :y="getNodeRadius(node) + 18"
            text-anchor="middle"
            font-size="11"
            fill="#6B7280"
            class="dark:fill-gray-400 select-none"
            :style="{ maxWidth: getNodeRadius(node) * 2 + 'px' }"
          >
            {{ getNodeLabel(node) }}
          </text>
        </g>
      </svg>

      <!-- 节点详情面板 -->
      <div 
        v-if="selectedNode"
        class="absolute top-4 right-4 w-64 bg-white dark:bg-dark-700 rounded-xl shadow-lg border border-gray-200 dark:border-dark-600 p-4"
      >
        <div class="flex items-center justify-between mb-3">
          <div class="flex items-center gap-2">
            <span class="text-xl">{{ getNodeIcon(selectedNode) }}</span>
            <span class="font-medium text-gray-800 dark:text-white">{{ getNodeLabel(selectedNode) }}</span>
          </div>
          <button
            @click="selectedNode = null"
            class="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
          >
            <X class="w-4 h-4" />
          </button>
        </div>
        <div class="space-y-2 text-sm">
          <div class="flex justify-between">
            <span class="text-gray-500 dark:text-gray-400">类型</span>
            <span class="text-gray-800 dark:text-white">{{ getNodeTypeLabel(selectedNode) }}</span>
          </div>
          <div class="flex justify-between">
            <span class="text-gray-500 dark:text-gray-400">ID</span>
            <span class="text-gray-800 dark:text-white text-xs font-mono">{{ selectedNode.id }}</span>
          </div>
          <div v-if="selectedNode.size" class="flex justify-between">
            <span class="text-gray-500 dark:text-gray-400">大小</span>
            <span class="text-gray-800 dark:text-white">{{ selectedNode.size }}</span>
          </div>
          <div v-if="selectedNode.kb_id" class="flex justify-between">
            <span class="text-gray-500 dark:text-gray-400">知识库</span>
            <span class="text-gray-800 dark:text-white text-xs">{{ selectedNode.kb_id.slice(0, 8) }}...</span>
          </div>
        </div>
      </div>

      <!-- 图例 -->
      <div class="absolute bottom-4 left-4 bg-white dark:bg-dark-700 rounded-lg border border-gray-200 dark:border-dark-600 p-3">
        <p class="text-xs font-medium text-gray-700 dark:text-gray-300 mb-2">图例</p>
        <div class="space-y-1.5">
          <div class="flex items-center gap-2">
            <div class="w-3 h-3 rounded-full bg-blue-500"></div>
            <span class="text-xs text-gray-600 dark:text-gray-400">知识库</span>
          </div>
          <div class="flex items-center gap-2">
            <div class="w-3 h-3 rounded-full bg-green-500"></div>
            <span class="text-xs text-gray-600 dark:text-gray-400">文档</span>
          </div>
          <div class="flex items-center gap-2">
            <div class="w-3 h-3 rounded-full bg-purple-500"></div>
            <span class="text-xs text-gray-600 dark:text-gray-400">相似关系</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 图谱统计 -->
    <div class="mt-4 grid grid-cols-3 gap-4">
      <div class="bg-white dark:bg-dark-800 rounded-lg border border-gray-200 dark:border-dark-600 p-3">
        <div class="flex items-center gap-2 mb-1">
          <CircleDot class="w-4 h-4 text-blue-500" />
          <span class="text-xs text-gray-500 dark:text-gray-400">节点数</span>
        </div>
        <p class="text-xl font-semibold text-gray-800 dark:text-white">{{ graphData.nodes.length }}</p>
      </div>
      <div class="bg-white dark:bg-dark-800 rounded-lg border border-gray-200 dark:border-dark-600 p-3">
        <div class="flex items-center gap-2 mb-1">
          <ArrowRightLeft class="w-4 h-4 text-green-500" />
          <span class="text-xs text-gray-500 dark:text-gray-400">关系数</span>
        </div>
        <p class="text-xl font-semibold text-gray-800 dark:text-white">{{ graphData.edges.length }}</p>
      </div>
      <div class="bg-white dark:bg-dark-800 rounded-lg border border-gray-200 dark:border-dark-600 p-3">
        <div class="flex items-center gap-2 mb-1">
          <Info class="w-4 h-4 text-purple-500" />
          <span class="text-xs text-gray-500 dark:text-gray-400">描述</span>
        </div>
        <p class="text-sm text-gray-600 dark:text-gray-300 truncate">{{ graphData.summary }}</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 知识图谱可视化组件
 * @description 基于 SVG 渲染知识库与其文档的关系图谱，采用自定义分层布局：
 * 知识库节点置顶、文档节点按所属知识库分组排列在下方。
 * 支持节点选中高亮、相邻边/节点强调、孤立节点淡化、节点详情面板、刷新与查看完整图谱。
 *
 * @props kbIds - 限定查询的知识库 ID 列表（为空时查询全部）
 *
 * @emits selectNode - 选中/取消选中节点时触发，携带当前选中节点（取消时为 null）
 */
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { Network, RefreshCw, X, CircleDot, ArrowRightLeft, Info } from '@lucide/vue'
import { api } from '@/utils/axios'

/** 图谱节点 */
interface GraphNode {
  /** 节点唯一 ID */
  id: string
  /** 节点显示标签 */
  label: string
  /** 节点类型：'knowledge_base'（知识库）或 'document'（文档） */
  type: string
  /** 节点大小（文档节点可携带，用于详情面板展示） */
  size?: number
  /** 所属知识库 ID（文档节点必填，用于分组布局与连线） */
  kb_id?: string
  /** 节点 X 坐标（由 calculateNodePositions 计算并写入 nodePositions，此处保留供扩展） */
  x?: number
  /** 节点 Y 坐标（同上） */
  y?: number
}

/** 图谱边 */
interface GraphEdge {
  /** 起点节点 ID */
  from: string
  /** 终点节点 ID */
  to: string
  /** 边标签（含"相似"字样时渲染为紫色相似关系） */
  label?: string
  /** 边权重（保留字段） */
  weight?: number
}

/** 图谱数据 */
interface GraphData {
  /** 节点列表 */
  nodes: GraphNode[]
  /** 边列表 */
  edges: GraphEdge[]
  /** 图谱概述文本 */
  summary: string
}

const props = defineProps<{
  kbIds?: string[]
}>()

const emit = defineEmits<{
  (e: 'selectNode', node: GraphNode | null): void
}>()

const svgRef = ref<SVGSVGElement | null>(null)
const svgWidth = ref(800)
const svgHeight = ref(500)
const isLoading = ref(false)
const selectedNode = ref<GraphNode | null>(null)
const graphData = ref<GraphData>({ nodes: [], edges: [], summary: '' })

const nodePositions = ref<Record<string, { x: number; y: number }>>({})

/**
 * 核心布局算法：计算所有节点的 (x, y) 坐标并写入 nodePositions。
 * 策略为分层布局——
 * 1. 知识库节点统一置顶（y = padding + 50），按数量等间距水平分布；
 * 2. 文档节点按所属 kb_id 分组，每组在垂直方向上下排列在知识库行之下，
 *    组间水平间距 = 可用宽度 / (组数 + 1)，组内文档以 100px 间距居中分布；
 * 3. 文档 y 坐标通过 (index % 2) * 40 做轻微交错，避免同组文档标签重叠；
 * 4. 文档 x 坐标在 [padding+30, width+padding-30] 范围内夹取，防止溢出可视区。
 */
function calculateNodePositions() {
  const nodes = graphData.value.nodes
  if (nodes.length === 0) return

  const padding = 80
  const width = svgWidth.value - padding * 2
  const height = svgHeight.value - padding * 2

  // 按类型分组
  const kbNodes = nodes.filter(n => n.type === 'knowledge_base')
  const docNodes = nodes.filter(n => n.type === 'document')

  // 知识库节点放在顶部：多节点时按 (count+1) 等分间距居中分布，单节点居中
  const kbCount = kbNodes.length
  const kbSpacing = kbCount > 1 ? width / (kbCount + 1) : width / 2

  kbNodes.forEach((node, index) => {
    nodePositions.value[node.id] = {
      x: padding + (index + 1) * kbSpacing,
      y: padding + 50
    }
  })

  // 文档节点放在下方，按知识库分组
  const docsPerKB: Record<string, GraphNode[]> = {}
  docNodes.forEach(doc => {
    const kbId = doc.kb_id || 'unknown'
    if (!docsPerKB[kbId]) docsPerKB[kbId] = []
    docsPerKB[kbId].push(doc)
  })

  // 文档行 y 坐标：在可用高度的中下部
  const docY = padding + height / 2 + 30

  Object.values(docsPerKB).forEach((docs, kbIndex) => {
    const docCount = docs.length
    // 当前知识库分组的水平中心点：按分组序号等分可用宽度
    const startX = padding + ((kbIndex + 1) * width / (Object.keys(docsPerKB).length + 1))
    // 组内文档间距 100px，单文档时不偏移
    const spacing = docCount > 1 ? 100 : 0

    docs.forEach((doc, index) => {
      // 以分组中心为基准，组内文档相对中心对称分布
      const offset = (index - (docCount - 1) / 2) * spacing
      nodePositions.value[doc.id] = {
        // 水平方向夹取在可视区内，防止溢出
        x: Math.max(padding + 30, Math.min(startX + offset, width + padding - 30)),
        // 偶数索引下沉 40px，避免相邻文档标签水平重叠
        y: docY + (index % 2) * 40
      }
    })
  })
}

/** 获取节点坐标，未命中时返回画布中心作为兜底。 */
function getNodePosition(nodeId: string) {
  return nodePositions.value[nodeId] || { x: 400, y: 250 }
}

/** 计算节点半径：选中节点放大至 35，知识库节点 28，文档节点 22。 */
function getNodeRadius(node: GraphNode) {
  if (selectedNode.value?.id === node.id) return 35
  return node.type === 'knowledge_base' ? 28 : 22
}

/** 计算节点填充色：选中态浅蓝，知识库浅蓝，文档浅绿。 */
function getNodeColor(node: GraphNode) {
  if (selectedNode.value?.id === node.id) {
    return '#DBEAFE'
  }
  if (node.type === 'knowledge_base') {
    return '#EFF6FF'
  }
  return '#ECFDF5'
}

/** 返回节点图标 emoji：知识库 📚，文档 📄。 */
function getNodeIcon(node: GraphNode) {
  if (node.type === 'knowledge_base') return '📚'
  return '📄'
}

/** 返回节点显示标签，长度超过 12 字符时截断并追加省略号。 */
function getNodeLabel(node: GraphNode) {
  const label = node.label || node.id
  if (label.length > 12) {
    return label.slice(0, 12) + '...'
  }
  return label
}

/** 返回节点类型的中文显示名，用于详情面板。 */
function getNodeTypeLabel(node: GraphNode) {
  return node.type === 'knowledge_base' ? '知识库' : '文档'
}

/** 计算边颜色：标签含"相似"字样时为紫色（相似关系），否则为灰色（从属关系）。 */
function getEdgeColor(edge: GraphEdge) {
  if (edge.label && edge.label.includes('相似')) {
    return '#A78BFA'
  }
  return '#9CA3AF'
}

/** 判断节点是否为当前选中节点。 */
function isNodeSelected(node: GraphNode) {
  return selectedNode.value?.id === node.id
}

/**
 * 判断给定的边或目标节点是否与选中节点相连。
 * @param selectedId - 选中节点 ID
 * @param edge - 待判断的边（提供时判断边的任一端点是否为选中节点）
 * @param targetNode - 待判断的目标节点（提供时判断是否存在连接选中节点与目标节点的边）
 * @returns 未提供选中 ID 时返回 true（无选中态，全部视为相关）
 */
function isConnected(selectedId: string, edge?: GraphEdge, targetNode?: GraphNode) {
  if (!selectedId) return true

  if (edge) {
    return edge.from === selectedId || edge.to === selectedId
  }

  if (targetNode) {
    return graphData.value.edges.some(
      e => (e.from === selectedId && e.to === targetNode.id) ||
           (e.to === selectedId && e.from === targetNode.id)
    )
  }

  return false
}

/** 点击节点切换选中态：再次点击同一节点取消选中，并通过 emit 通知父组件。 */
function selectNode(node: GraphNode) {
  if (selectedNode.value?.id === node.id) {
    selectedNode.value = null
  } else {
    selectedNode.value = node
  }
  emit('selectNode', selectedNode.value || null)
}

/** 点击 SVG 空白处时清空选中节点（事件由 .stop 修饰符阻止节点点击冒泡）。 */
function handleSvgClick() {
  selectedNode.value = null
}

/** 拉取图谱数据：按 kbIds 过滤，请求成功后重新计算节点布局。失败时清空图谱并显示错误摘要。 */
async function fetchGraphData() {
  isLoading.value = true

  try {
    const params = props.kbIds && props.kbIds.length > 0
      ? { kb_ids: props.kbIds }
      : {}

    const data = await api.post<GraphData>('/knowledge_bases/knowledge_graph', params)
    graphData.value = data
    calculateNodePositions()
  } catch (error) {
    console.error('获取知识图谱失败:', error)
    graphData.value = { nodes: [], edges: [], summary: '加载失败' }
  } finally {
    isLoading.value = false
  }
}

/** 手动刷新图谱：重新拉取数据。 */
function refreshGraph() {
  fetchGraphData()
}

/** 查看完整图谱：清空当前选中态后重新拉取数据。 */
function showFullGraph() {
  selectedNode.value = null
  fetchGraphData()
}

/** 根据容器尺寸更新 SVG 宽高并重新计算节点布局（用于初始化与窗口 resize）。 */
function updateSize() {
  if (svgRef.value) {
    const container = svgRef.value.parentElement
    if (container) {
      svgWidth.value = container.clientWidth
      svgHeight.value = Math.max(400, container.clientHeight - 40)
      calculateNodePositions()
    }
  }
}

onMounted(() => {
  fetchGraphData()
  updateSize()
  window.addEventListener('resize', updateSize)
})

onUnmounted(() => {
  window.removeEventListener('resize', updateSize)
})

watch(() => props.kbIds, () => {
  fetchGraphData()
}, { deep: true })
</script>

<style scoped>
text {
  pointer-events: none;
}
</style>