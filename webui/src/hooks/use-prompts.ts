/**
 * 提示词工作台的数据状态机。
 *
 * 管理模板列表、当前模板详情（含内置对照内容）、版本历史归档与保存/删除
 * 覆盖操作；本机可编辑模板允许写操作，固定模板保持只读。401 统一上报认证
 * 上下文，其他错误转换为工作台状态文本。
 */
import { useCallback, useEffect, useState } from 'react'

import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'
import { record } from '@/lib/format'
import { useAuth } from './use-auth'

/** 提示词模板摘要，对应后端 /prompts 响应数组元素。 */
export interface PromptSummary {
  id: string
  source: 'builtin' | 'override'
  promptHash: string
  placeholders: string[]
  fixed: boolean
}

/** 提示词模板详情，在摘要基础上包含生效内容与内置内容。 */
export interface PromptDetail extends PromptSummary {
  content: string
  builtinContent: string
}

/** 版本历史归档条目。 */
export interface PromptVersion {
  name: string
  updatedAt: number
  content: string
}

/** usePrompts 返回的工作台状态与操作。 */
interface PromptsState {
  /** 全部模板摘要。 */
  summaries: PromptSummary[]
  /** 当前选中模板 id。 */
  selectedId: string
  /** 切换选中模板并加载其详情。 */
  select: (id: string) => void
  /** 当前模板详情；未加载完成为 `null`。 */
  detail: PromptDetail | null
  /** 当前模板的版本历史。 */
  history: PromptVersion[]
  /** 编辑器当前内容。 */
  editorValue: string
  /** 更新编辑器内容。 */
  setEditorValue: (value: string) => void
  /** 工作台状态文本（操作反馈与错误信息）。 */
  status: string
  /** 是否有写操作进行中。 */
  busy: boolean
  /** 重新读取模板列表与当前详情。 */
  reload: () => void
  /** 校验并保存当前编辑器内容为模板覆盖。 */
  save: () => void
  /** 删除当前模板的用户覆盖并恢复内置版本。 */
  reset: () => void
  /**
   * 把历史版本内容载入编辑器（不保存）。
   *
   * @param version 待载入的归档版本。
   */
  loadVersion: (version: PromptVersion) => void
}

/**
 * 维护提示词工作台状态。
 *
 * @param enabled 是否启用；会话观察页挂载期间为 `true`。
 * @returns 工作台状态与操作方法。
 */
export function usePrompts(enabled: boolean): PromptsState {
  const { handleUnauthorized } = useAuth()
  const [summaries, setSummaries] = useState<PromptSummary[]>([])
  const [selectedId, setSelectedId] = useState('')
  const [detail, setDetail] = useState<PromptDetail | null>(null)
  const [history, setHistory] = useState<PromptVersion[]>([])
  const [editorValue, setEditorValue] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)

  const reportError = useCallback((prefix: string, error: unknown) => {
    if (error instanceof UnauthorizedError) {
      handleUnauthorized(error)
      return
    }
    setStatus(`${prefix}：${error instanceof Error ? error.message : String(error)}`)
  }, [handleUnauthorized])

  const fetchHistory = useCallback(async (promptId: string) => {
    try {
      const payload = await apiFetch<{ history?: unknown }>(
        `/prompts/${encodeURIComponent(promptId)}/history`,
      )
      const entries = Array.isArray(payload.history) ? payload.history : []
      setHistory(entries.slice(0, 20).map((value) => {
        const item = record(value)
        return {
          name: typeof item.name === 'string' ? item.name : '',
          updatedAt: typeof item.updatedAt === 'number' ? item.updatedAt : 0,
          content: typeof item.content === 'string' ? item.content : '',
        }
      }))
    } catch (error) {
      if (error instanceof UnauthorizedError) handleUnauthorized(error)
      // 历史读取失败不阻断主流程，保留旧列表。
    }
  }, [handleUnauthorized])

  const fetchDetail = useCallback(async (promptId: string) => {
    try {
      const loaded = await apiFetch<PromptDetail>(`/prompts/${encodeURIComponent(promptId)}`)
      setDetail(loaded)
      setEditorValue(loaded.content)
      setStatus('')
      await fetchHistory(promptId)
    } catch (error) {
      reportError('提示词读取失败', error)
    }
  }, [fetchHistory, reportError])

  const fetchList = useCallback(async () => {
    try {
      const payload = await apiFetch<{ prompts?: unknown }>('/prompts')
      const list = Array.isArray(payload.prompts) ? payload.prompts as PromptSummary[] : []
      setSummaries(list)
      // 保留当前选中项；选中项已不存在时回退到列表首个模板。
      setSelectedId((current) =>
        current && list.some((item) => item.id === current) ? current : (list[0]?.id ?? ''),
      )
    } catch (error) {
      reportError('提示词列表读取失败', error)
    }
  }, [reportError])

  useEffect(() => {
    if (enabled) void fetchList()
  }, [enabled, fetchList])

  // 选中项变化统一触发详情加载，列表回退与手动切换走同一条路径。
  useEffect(() => {
    if (enabled && selectedId) void fetchDetail(selectedId)
  }, [enabled, selectedId, fetchDetail])

  const select = useCallback((id: string) => {
    setSelectedId(id)
  }, [])

  const mutate = useCallback(async (method: 'PUT' | 'DELETE') => {
    if (!selectedId) return
    setBusy(true)
    setStatus(method === 'PUT' ? '正在校验并保存…' : '正在恢复内置版本…')
    try {
      const updated = await apiMutate<PromptDetail>(
        `/prompts/${encodeURIComponent(selectedId)}`,
        method,
        method === 'PUT' ? { content: editorValue } : undefined,
      )
      setDetail(updated)
      setEditorValue(updated.content)
      setStatus(
        method === 'PUT'
          ? `已热重载，当前哈希 ${updated.promptHash}`
          : `已恢复内置版本，当前哈希 ${updated.promptHash}`,
      )
      await fetchHistory(selectedId)
      // 模板来源（内置/覆盖）变化后刷新列表，保持下拉框标签准确。
      void fetchList()
    } catch (error) {
      reportError('操作失败', error)
    } finally {
      setBusy(false)
    }
  }, [selectedId, editorValue, fetchHistory, fetchList, reportError])

  const loadVersion = useCallback((version: PromptVersion) => {
    setEditorValue(version.content)
    setStatus(`已载入历史版本 ${version.name}，尚未保存`)
  }, [])

  return {
    summaries,
    selectedId,
    select,
    detail,
    history,
    editorValue,
    setEditorValue,
    status,
    busy,
    reload: useCallback(() => void fetchList(), [fetchList]),
    save: useCallback(() => void mutate('PUT'), [mutate]),
    reset: useCallback(() => void mutate('DELETE'), [mutate]),
    loadVersion,
  }
}
