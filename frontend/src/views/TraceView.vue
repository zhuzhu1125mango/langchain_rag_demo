<template>
  <div class="flex-1 flex flex-col h-full bg-gray-50 dark:bg-dark-900 overflow-hidden">
    <header class="bg-white dark:bg-dark-800 border-b border-gray-200 dark:border-dark-600 px-6 py-4">
      <div>
        <h1 class="text-xl font-semibold text-gray-800 dark:text-white">链路追踪</h1>
        <p class="text-sm text-gray-500 dark:text-gray-400 mt-1">查看每次问答的完整执行链路：意图路由、检索、生成各阶段耗时与 token 用量</p>
      </div>
    </header>

    <!-- 过滤栏 -->
    <div class="bg-white dark:bg-dark-800 border-b border-gray-200 dark:border-dark-600 px-6 py-3 flex flex-wrap items-center gap-3">
      <el-input
        v-model="sessionIdFilter"
        placeholder="按会话 ID 过滤（可选）"
        clearable
        class="w-72"
        size="default"
      />
      <el-date-picker
        v-model="dateRange"
        type="daterange"
        range-separator="至"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
        value-format="YYYY-MM-DDT00:00:00"
        class="w-72"
      />
      <el-button type="primary" :icon="Search" @click="applyFilters">查询</el-button>
      <el-button :icon="RotateCcw" @click="resetFilters">重置</el-button>
    </div>

    <!-- 列表 -->
    <div class="flex-1 overflow-auto p-6">
      <el-table :data="tracesData?.items || []" v-loading="isLoading" stripe>
        <el-table-column label="时间" width="170">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="问题" min-width="260" show-overflow-tooltip>
          <template #default="{ row }">{{ row.question || '（空）' }}</template>
        </el-table-column>
        <el-table-column label="模式" width="130">
          <template #default="{ row }">
            <el-tag v-if="row.primary_mode" size="small" type="info">{{ row.primary_mode }}</el-tag>
            <span v-else class="text-gray-400">-</span>
          </template>
        </el-table-column>
        <el-table-column label="阶段数" width="80" prop="stage_count" />
        <el-table-column label="总耗时" width="100">
          <template #default="{ row }">{{ formatLatency(row.total_latency_ms) }}</template>
        </el-table-column>
        <el-table-column label="Token" width="150">
          <template #default="{ row }">
            <span v-if="row.token_usage" class="text-xs">
              {{ row.token_usage.prompt_tokens ?? '?' }} + {{ row.token_usage.completion_tokens ?? '?' }}
              <el-tag v-if="row.token_usage.estimated" size="small" type="warning" class="ml-1">估</el-tag>
            </span>
            <span v-else class="text-gray-400">-</span>
          </template>
        </el-table-column>
        <el-table-column label="降级" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.fallback_triggered" size="small" type="danger">是</el-tag>
            <span v-else class="text-gray-400">否</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openDetail(row.id)">详情</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="mt-4 flex justify-end">
        <el-pagination
          v-model:current-page="currentPage"
          :page-size="pageSize"
          :total="tracesData?.total || 0"
          layout="total, prev, pager, next"
          background
        />
      </div>

      <div v-if="!isLoading && !tracesData?.items?.length" class="flex flex-col items-center justify-center py-16">
        <Activity class="w-16 h-16 text-gray-300 dark:text-gray-600 mb-4" />
        <p class="text-gray-500 dark:text-gray-400">暂无链路记录，进行一次对话后即可查看</p>
      </div>
    </div>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" title="链路详情" size="55%" :destroy-on-close="true">
      <div v-loading="detailLoading" class="space-y-6 pr-2">
        <template v-if="detail">
          <!-- 概览 -->
          <div class="grid grid-cols-2 gap-3">
            <div class="bg-gray-50 dark:bg-dark-700 rounded-lg p-3">
              <p class="text-xs text-gray-500 dark:text-gray-400 mb-1">总耗时</p>
              <p class="text-lg font-semibold text-gray-800 dark:text-white">{{ formatLatency(detail.total_latency_ms) }}</p>
            </div>
            <div class="bg-gray-50 dark:bg-dark-700 rounded-lg p-3">
              <p class="text-xs text-gray-500 dark:text-gray-400 mb-1">Token 用量</p>
              <p class="text-lg font-semibold text-gray-800 dark:text-white">
                {{ detail.token_usage?.prompt_tokens ?? '-' }} + {{ detail.token_usage?.completion_tokens ?? '-' }}
                <el-tag v-if="detail.token_usage?.estimated" size="small" type="warning">字符估算</el-tag>
              </p>
            </div>
          </div>

          <div>
            <p class="text-xs text-gray-500 dark:text-gray-400 mb-1">用户问题</p>
            <p class="text-sm text-gray-800 dark:text-white">{{ detail.question }}</p>
          </div>
          <div v-if="detail.resolved_question && detail.resolved_question !== detail.question">
            <p class="text-xs text-gray-500 dark:text-gray-400 mb-1">改写后问题</p>
            <p class="text-sm text-gray-800 dark:text-white">{{ detail.resolved_question }}</p>
          </div>

          <!-- 分阶段时间线 -->
          <div>
            <p class="text-sm font-medium text-gray-800 dark:text-white mb-3">执行阶段</p>
            <el-timeline>
              <el-timeline-item
                v-for="(stage, idx) in detail.stages || []"
                :key="idx"
                :type="stage.status === 'error' ? 'danger' : 'primary'"
                :timestamp="formatLatency(stage.latency_ms)"
              >
                <p class="text-sm text-gray-800 dark:text-white">
                  {{ STAGE_LABELS[stage.name] || stage.name }}
                  <el-tag v-if="stage.status === 'error'" size="small" type="danger" class="ml-1">失败</el-tag>
                </p>
                <p v-if="stage.detail" class="text-xs text-gray-500 dark:text-gray-400 mt-1">{{ JSON.stringify(stage.detail) }}</p>
              </el-timeline-item>
            </el-timeline>
          </div>

          <!-- 检索与工具摘要 -->
          <div v-if="detail.kb_results?.length">
            <p class="text-sm font-medium text-gray-800 dark:text-white mb-2">知识库检索命中（{{ detail.kb_results.length }} 条）</p>
            <div class="space-y-2">
              <div
                v-for="(r, idx) in detail.kb_results.slice(0, 5)"
                :key="idx"
                class="text-xs text-gray-600 dark:text-gray-300 bg-gray-50 dark:bg-dark-700 rounded-lg p-2 line-clamp-2"
              >
                {{ (r as Record<string, unknown>).page_content || JSON.stringify(r) }}
              </div>
            </div>
          </div>
          <div v-if="detail.tool_calls?.length">
            <p class="text-sm font-medium text-gray-800 dark:text-white mb-2">工具调用（{{ detail.tool_calls.length }} 次）</p>
            <div class="space-y-2">
              <div
                v-for="(t, idx) in detail.tool_calls"
                :key="idx"
                class="text-xs text-gray-600 dark:text-gray-300 bg-gray-50 dark:bg-dark-700 rounded-lg p-2"
              >
                {{ (t as Record<string, unknown>).tool_name || (t as Record<string, unknown>).name || JSON.stringify(t).slice(0, 200) }}
              </div>
            </div>
          </div>

          <!-- 最终答案（可折叠） -->
          <div v-if="detail.final_answer">
            <p class="text-sm font-medium text-gray-800 dark:text-white mb-2">最终回答</p>
            <el-collapse>
              <el-collapse-item :title="`${detail.final_answer.slice(0, 40)}...`">
                <p class="text-sm text-gray-700 dark:text-gray-200 whitespace-pre-wrap">{{ detail.final_answer }}</p>
              </el-collapse-item>
            </el-collapse>
          </div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
/**
 * 链路追踪页面（P1-2）。
 *
 * 展示当前用户的问答链路列表（支持会话 ID / 时间范围过滤与分页），
 * 详情抽屉以时间线呈现各执行阶段耗时、检索命中、工具调用与最终回答。
 * 支持从历史对话页携带 session_id 跳转进入。
 */
import { computed, ref } from 'vue'
import { useRoute } from 'vue-router'
import { Search, RotateCcw, Activity } from '@lucide/vue'
import { useTraces, useTraceDetail, STAGE_LABELS } from '@/queries/trace'
import { formatDate } from '@/utils/format'

const route = useRoute()

const sessionIdFilter = ref((route.query.session_id as string) || '')
const dateRange = ref<[string, string] | null>(null)
const appliedSessionId = ref(sessionIdFilter.value)
const appliedDateRange = ref<[string, string] | null>(null)

const currentPage = ref(1)
const pageSize = 20

const listParams = computed(() => ({
  session_id: appliedSessionId.value || undefined,
  start: appliedDateRange.value?.[0],
  end: appliedDateRange.value?.[1],
  limit: pageSize,
  offset: (currentPage.value - 1) * pageSize
}))

const { data: tracesData, isLoading } = useTraces(listParams)

function applyFilters(): void {
  appliedSessionId.value = sessionIdFilter.value.trim()
  appliedDateRange.value = dateRange.value
  currentPage.value = 1
}

function resetFilters(): void {
  sessionIdFilter.value = ''
  dateRange.value = null
  applyFilters()
}

// 详情抽屉
const detailVisible = ref(false)
const selectedTraceId = ref<string | null>(null)
const { data: detail, isLoading: detailLoading } = useTraceDetail(selectedTraceId)

function openDetail(id: string): void {
  selectedTraceId.value = id
  detailVisible.value = true
}

function formatLatency(ms?: number | null): string {
  if (ms == null) return '-'
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`
}
</script>
