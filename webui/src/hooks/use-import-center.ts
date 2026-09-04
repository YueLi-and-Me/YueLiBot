/**
 * 导入中心的数据 hook。
 *
 * 对应后端 `/api/memory/import*` 一组接口：批次列表与上限常量、执行一次导入
 * （粘贴或上传共用，上传在页面侧解码为文本）、批次删除预览与执行删除。
 * 导入是同步请求，期间后端设导入闸，并发导入返回 409。
 */
import { useCallback, useEffect, useState } from 'react'

import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'
import { useAuth } from './use-auth'

/** 一次导入的三个上限常量。 */
export interface ImportLimits {
  pasteChars: number
  fileBytes: number
  batchItems: number
}

/** 一个来源批次；liveCount 按库实时数出，与 added 不一致即被删除过。 */
export interface ImportBatch {
  id: number
  kind: string
  originName: string
  summary: string
  submitted: number
  added: number
  status: string
  createdAt: number
  finishedAt: number | null
  error: string
  liveCount: number
}

/** 批次详情：批次字段 + 条目样本。 */
export interface ImportBatchDetail extends ImportBatch {
  items: Array<{ id: number; content: string }>
}

/** 一次导入的结果统计。 */
export interface ImportResult {
  batch_id: number
  submitted: number
  added: number
  duplicated: number
  embedded: number
}

/** 删除预览：会删掉的条数与样本。 */
export interface ImportDeletePreview {
  batch_id: number
  to_delete: number
  items: Array<{ id: number; content: string }>
}

/** 使用导入中心状态与操作的 hook。 */
export function useImportCenter() {
  const { authenticated } = useAuth()
  const [batches, setBatches] = useState<ImportBatch[]>([])
  const [limits, setLimits] = useState<ImportLimits | null>(null)
  const [inProgress, setInProgress] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const reload = useCallback(async () => {
    if (!authenticated) return
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<{
        batches: ImportBatch[]
        limits: ImportLimits
        inProgress: boolean
      }>('/api/memory/import/batches')
      setBatches(data.batches)
      setLimits(data.limits)
      setInProgress(data.inProgress)
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) setError(String(err))
    } finally {
      setLoading(false)
    }
  }, [authenticated])

  useEffect(() => {
    void reload()
  }, [reload])

  const runImport = useCallback(
    async (kind: 'paste' | 'upload', text: string, originName: string) => {
      setInProgress(true)
      try {
        return await apiMutate<ImportResult>('/api/memory/import', 'POST', {
          kind,
          text,
          originName,
        })
      } finally {
        setInProgress(false)
        await reload()
      }
    },
    [reload],
  )

  const fetchDetail = useCallback(async (batchId: number) => {
    return apiFetch<ImportBatchDetail>(`/api/memory/import/batches/${batchId}`)
  }, [])

  const fetchDeletePreview = useCallback(async (batchId: number) => {
    return apiFetch<ImportDeletePreview>(
      `/api/memory/import/batches/${batchId}/preview`,
    )
  }, [])

  const deleteBatch = useCallback(
    async (batchId: number) => {
      const result = await apiMutate<{ batch_id: number; deleted: number }>(
        `/api/memory/import/batches/${batchId}/delete`,
        'POST',
      )
      await reload()
      return result
    },
    [reload],
  )

  return {
    batches,
    limits,
    inProgress,
    error,
    loading,
    reload,
    runImport,
    fetchDetail,
    fetchDeletePreview,
    deleteBatch,
  }
}
