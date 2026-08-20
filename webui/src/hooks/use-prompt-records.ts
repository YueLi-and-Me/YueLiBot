/**
 * 分阶段调用记录面板的数据状态机。
 *
 * 管理任务筛选、摘要列表与单份记录详情的加载。摘要与详情分两次请求：一份记录
 * 含完整请求消息，多级 Agent 下一个回合就有好几份，全量塞进列表会让面板每次
 * 刷新都拖上几兆。401 统一上报认证上下文，其余错误转换为面板状态文本。
 */
import { useCallback, useEffect, useState } from 'react'

import { apiFetch, UnauthorizedError } from '@/lib/api'
import { useAuth } from './use-auth'

/** 记录摘要，对应后端 /prompt-records 响应数组元素。 */
export interface PromptRecordSummary {
  task: string
  name: string
  at: string | null
  stage: string | null
  streamId: number | null
  turnId: number | null
  model: string | null
  provider: string | null
  firstTokenMs: number | null
  totalMs: number | null
  chunks: number | null
  textLength: number
  errorType: string | null
}

/** 单份记录的完整内容，对应后端 /prompt-records/{task}/{name} 响应。 */
export interface PromptRecordDetail {
  at?: string
  task?: string
  stage?: string
  streamId?: number
  turnId?: number
  model?: { name?: string; provider?: string; candidate?: unknown }
  timing?: { firstTokenMs?: number | null; totalMs?: number }
  request?: {
    messages?: { role?: string; content?: string }[]
    temperature?: number | null
    maxTokens?: number | null
  }
  response?: { text?: string; reasoning?: string; chunks?: number }
  attempts?: unknown[]
  error?: { type?: string; message?: string } | null
}

/** usePromptRecords 返回的面板状态与操作。 */
interface PromptRecordsState {
  /** 后端是否启用了调用记录；关闭时列表恒为空，面板应提示去开开关。 */
  enabled: boolean
  /** 当前有记录的任务名列表。 */
  tasks: string[]
  /** 当前筛选的任务；空串表示全部任务。 */
  task: string
  /** 切换任务筛选并重新加载。 */
  setTask: (task: string) => void
  /** 摘要列表，最近的在前。 */
  records: PromptRecordSummary[]
  /** 当前展开的记录键（`task/name`）；未展开为空串。 */
  openKey: string
  /** 展开或收起一份记录；重复点击同一份即收起。 */
  toggle: (summary: PromptRecordSummary) => void
  /** 当前展开记录的完整内容；加载中或未展开为 `null`。 */
  detail: PromptRecordDetail | null
  /** 面板状态文本（加载中与错误信息）。 */
  status: string
  /** 重新读取摘要列表。 */
  reload: () => void
}

/** 单次列表请求的摘要条数上限。 */
const LIST_LIMIT = 60

/**
 * 加载并管理分阶段调用记录面板的状态。
 *
 * @param enabled 是否启用数据加载；页面未挂载该面板时传 false 可完全静默。
 * @returns 面板状态与操作集合。
 */
export function usePromptRecords(enabled: boolean): PromptRecordsState {
  const { handleUnauthorized } = useAuth()

  const reportError = useCallback((prefix: string, error: unknown) => {
    if (error instanceof UnauthorizedError) {
      handleUnauthorized(error)
      return
    }
    setStatus(`${prefix}：${error instanceof Error ? error.message : String(error)}`)
  }, [handleUnauthorized])
  const [recordsEnabled, setRecordsEnabled] = useState(true)
  const [tasks, setTasks] = useState<string[]>([])
  const [task, setTask] = useState('')
  const [records, setRecords] = useState<PromptRecordSummary[]>([])
  const [openKey, setOpenKey] = useState('')
  const [detail, setDetail] = useState<PromptRecordDetail | null>(null)
  const [status, setStatus] = useState('')

  const load = useCallback(() => {
    if (!enabled) return
    const query = new URLSearchParams({ limit: String(LIST_LIMIT) })
    if (task) query.set('task', task)
    apiFetch<{ enabled: boolean; tasks: string[]; records: PromptRecordSummary[] }>(
      `/prompt-records?${query.toString()}`,
    )
      .then((payload) => {
        setRecordsEnabled(payload.enabled)
        setTasks(payload.tasks)
        setRecords(payload.records)
        setStatus('')
      })
      .catch((error: unknown) => reportError('读取调用记录失败', error))
  }, [enabled, task, reportError])

  useEffect(load, [load])

  const toggle = useCallback(
    (summary: PromptRecordSummary) => {
      const key = `${summary.task}/${summary.name}`
      if (key === openKey) {
        setOpenKey('')
        setDetail(null)
        return
      }
      setOpenKey(key)
      setDetail(null)
      // 详情按任务目录名寻址，摘要里的 task 是记录内的原始任务名（带点），
      // 与目录名不同；目录名从文件所在任务列表推导会更绕，这里直接用摘要里
      // 已经规范化过的任务目录名。
      const directory = summary.task.replace(/[./\:]/g, '-')
      apiFetch<PromptRecordDetail>(
        `/prompt-records/${encodeURIComponent(directory)}/${encodeURIComponent(summary.name)}`,
      )
        .then((payload) => setDetail(payload))
        .catch((error: unknown) => reportError('读取记录详情失败', error))
    },
    [openKey, reportError],
  )

  return {
    enabled: recordsEnabled,
    tasks,
    task,
    setTask,
    records,
    openKey,
    toggle,
    detail,
    status,
    reload: load,
  }
}
