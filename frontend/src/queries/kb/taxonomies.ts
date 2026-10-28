import { useQuery, useMutation, useQueryClient } from '@tanstack/vue-query'
import { api } from '@/utils/axios'

export interface Category {
  /** 分类唯一 ID。 */
  id: string
  /** 分类名称。 */
  name: string
  /** 描述。 */
  description?: string
  /** 父分类 ID，空表示顶级分类。 */
  parent_id?: string
  /** 排序权重，越小越靠前。 */
  sort_order: number
  /** 分类下文档数量。 */
  count?: number
}

export interface CategoryCreateRequest {
  name: string
  description?: string
  parent_id?: string
  sort_order?: number
}

export interface Tag {
  id: string
  name: string
  color: string
  count?: number
}

export interface TagCreateRequest {
  name: string
  color?: string
}

/** 文档列表查询参数开放袋（调用方键集开放，值经 unref 后作为 axios params）。 */

export function useCategories() {
  return useQuery({
    queryKey: ['categories'],
    queryFn: async (): Promise<Category[]> => {
      const data = await api.get<Category[]>('/categories/')
      return data
    },
    staleTime: 10 * 60 * 1000
  })
}

/** 获取文档标签列表。 */

export function useTags() {
  return useQuery({
    queryKey: ['tags'],
    queryFn: async (): Promise<Tag[]> => {
      const data = await api.get<Tag[]>('/tags/')
      return data
    },
    staleTime: 10 * 60 * 1000
  })
}

/** 创建分类。 */

export function useCreateCategory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (data: CategoryCreateRequest): Promise<{ id: string; name: string }> => {
      const res = await api.post<{ id: string; name: string }>('/categories/', data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['categories'] })
    }
  })
}

/** 更新分类。 */

export function useUpdateCategory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: string; data: CategoryCreateRequest }) => {
      const res = await api.put(`/categories/${id}`, data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['categories'] })
    }
  })
}

/** 删除分类。 */

export function useDeleteCategory() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (categoryId: string) => {
      await api.delete(`/categories/${categoryId}`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['categories'] })
    }
  })
}

/** 创建标签。 */

export function useCreateTag() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (data: TagCreateRequest): Promise<{ id: string; name: string }> => {
      const res = await api.post<{ id: string; name: string }>('/tags/', data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tags'] })
    }
  })
}

/** 更新标签。 */

export function useUpdateTag() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ id, data }: { id: string; data: TagCreateRequest }) => {
      const res = await api.put(`/tags/${id}`, data)
      return res
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tags'] })
    }
  })
}

/** 删除标签。 */

export function useDeleteTag() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (tagId: string) => {
      await api.delete(`/tags/${tagId}`)
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['tags'] })
    }
  })
}

/** 删除单个文档。 */
