/**
 * 会话流、观察快照与阶段看板的数据加载 hooks。
 *
 * useStreams 读取可观察会话列表；useSnapshot 按当前会话读取快照并支持 15 秒
 * 自动刷新；useStages 以秒级轮询各会话当前处理阶段。401 统一上报认证上下文，
 * 组件卸载时自动清理定时器。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { apiFetch, UnauthorizedError } from '@/lib/api'
import { dateTime } from '@/lib/format'
import type {
  ObservabilityPayload,
  ObservabilityStream,
  ObservabilityStreamsPayload,
} from '../../../electron/shared/ipc.ts'
import { useAuth } from './use-auth'

/** 快照自动刷新间隔，单位毫秒。 */
const SNAPSHOT_REFRESH_MS = 15_000
/** 阶段看板轮询间隔；阶段状态需要秒级精度以反映后端当前处理步骤。 */
const STAGE_POLL_MS = 1_000

/**
 * 比较两轮阶段记录是否实质相同。
 *
 * 轮询每秒触发，但绝大多数时候阶段没有变化；逐字段比较后复用旧数组引用，
 * 可以避免 ObservePage 每秒整页无意义重渲染（事件账本等重面板靠引用相等跳
 * 过 reconciliation）。阶段活跃时 stageElapsedMs 持续增长，会自然触发更新。
 */
function sameStages(current: StageEntry[], next: StageEntry[]): boolean {
  if (current.length !== next.length) return false
  return current.every((entry, index) => {
    const other = next[index]
    return (
      !!other &&
      entry.streamId === other.streamId &&
      entry.stage === other.stage &&
      entry.detail === other.detail &&
      entry.turnId === other.turnId &&
      entry.stageElapsedMs === other.stageElapsedMs
    )
  })
}

/** 单条阶段看板记录，对应后端 /stages 响应数组元素。 */
export interface StageEntry {
  streamId: number
  streamName: string
  stage: string
  stageLabel: string
  detail: string
  turnId: number | null
  stageElapsedMs: number
  stageStartedAtTruncated: boolean
}

/**
 * 读取可观察会话流列表。
 *
 * @returns 会话流数组与加载错误文本；列表为空时错误文本说明原因。
 */
export function useStreams(): { streams: ObservabilityStream[]; error: string } {
  const { handleUnauthorized } = useAuth()
  const [streams, setStreams] = useState<ObservabilityStream[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    apiFetch<ObservabilityStreamsPayload>('/streams')
      .then((payload) => {
        if (cancelled) return
        if (!payload.streams.length) {
          setError('后端没有可观察的 stream')
          return
        }
        setStreams(payload.streams)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof UnauthorizedError) {
          handleUnauthorized(err)
        } else {
          setError(`stream 列表请求失败：${err instanceof Error ? err.message : String(err)}`)
        }
      })
    return () => {
      cancelled = true
    }
  }, [handleUnauthorized])

  return { streams, error }
}

/** useSnapshot 返回的快照状态。 */
interface SnapshotState {
  /** 最新快照；尚未读取成功时为 `null`。 */
  payload: ObservabilityPayload | null
  /** 「读取于 xx」的展示文本；尚未读取为空字符串。 */
  fetchedLabel: string
  /** 是否正在读取（用于禁用刷新按钮）。 */
  refreshing: boolean
  /** 读取失败的错误文本；成功时为空字符串。 */
  error: string
  /** 手动触发一次读取。 */
  refresh: () => void
}

/**
 * 按会话流读取观察快照，并支持 15 秒自动刷新。
 *
 * @param streamId 当前选中的会话流 ID 文本；为空时不发起请求。
 * @param autoRefresh 是否开启自动刷新。
 * @returns 快照状态与手动刷新方法。
 */
export function useSnapshot(streamId: string, autoRefresh: boolean): SnapshotState {
  const { handleUnauthorized } = useAuth()
  const [payload, setPayload] = useState<ObservabilityPayload | null>(null)
  const [fetchedLabel, setFetchedLabel] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')
  // 手动刷新与定时器共用同一份读取逻辑，用 ref 保证定时器内始终调用最新闭包。
  const refreshRef = useRef<() => void>(() => {})

  const fetchSnapshot = useCallback(async () => {
    if (!streamId) return
    setRefreshing(true)
    try {
      const snapshot = await apiFetch<ObservabilityPayload>(
        `/observability?streamId=${encodeURIComponent(streamId)}`,
      )
      setPayload(snapshot)
      setError('')
      const fetched = new Date()
      setFetchedLabel(`读取于 ${dateTime(fetched.getTime())}`)
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        handleUnauthorized(err)
      } else {
        setError(`读不到内部状态：${err instanceof Error ? err.message : String(err)}`)
      }
    } finally {
      setRefreshing(false)
    }
  }, [streamId, handleUnauthorized])

  useEffect(() => {
    refreshRef.current = () => void fetchSnapshot()
  }, [fetchSnapshot])

  useEffect(() => {
    void fetchSnapshot()
  }, [fetchSnapshot])

  useEffect(() => {
    if (!autoRefresh) return
    const timer = setInterval(() => refreshRef.current(), SNAPSHOT_REFRESH_MS)
    return () => clearInterval(timer)
  }, [autoRefresh])

  const refresh = useCallback(() => void fetchSnapshot(), [fetchSnapshot])
  return { payload, fetchedLabel, refreshing, error, refresh }
}

/**
 * 以秒级间隔轮询各会话当前处理阶段。
 *
 * @returns 阶段记录数组；请求失败时保留上一次结果（与旧版行为一致）。
 * @remarks 非成功响应静默忽略：阶段看板是近实时展示，下一次轮询会自然恢复。
 */
export function useStages(): StageEntry[] {
  const [stages, setStages] = useState<StageEntry[]>([])

  useEffect(() => {
    let cancelled = false
    const poll = async () => {
      try {
        const response = await fetch('/stages', { credentials: 'same-origin' })
        if (!response.ok || cancelled) return
        const payload = await response.json() as { stages?: StageEntry[] }
        const next = payload.stages ?? []
        setStages((current) => (sameStages(current, next) ? current : next))
      } catch {
        // 网络抖动静默跳过，等待下一次轮询。
      }
    }
    void poll()
    const timer = setInterval(() => void poll(), STAGE_POLL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  return stages
}
