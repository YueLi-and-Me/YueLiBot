/**
 * 事件账本的数据通道：WebSocket 增量事件流与历史检索合并。
 *
 * WebSocket 按序号游标接收增量事件，断线后指数退避重连（1 秒起、上限 30 秒）；
 * 历史检索结果与实时事件按 seq 去重合并并排序，总量上限 1000 条。401 统一
 * 上报认证上下文；组件卸载时关闭连接并清理重连定时器。
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { apiFetch, UnauthorizedError } from '@/lib/api'
import { record } from '@/lib/format'
import type { TraceEntry } from '../../../electron/shared/ipc.ts'
import { useAuth } from './use-auth'

/** 事件账本在内存中保留的最大事件条数。 */
const MAX_TRACE_ENTRIES = 1_000

/** useTraces 返回的事件账本状态与操作。 */
interface TracesState {
  /** 合并后的全量事件（实时 + 检索历史）。 */
  traces: TraceEntry[]
  /** 服务端报告截断时被跳过的事件条数。 */
  skippedCount: number
  /** 更早历史的翻页游标；`null` 表示没有更早记录。 */
  historyCursor: number | null
  /**
   * 执行历史事件检索。
   *
   * @param params 检索查询参数（limit/streamId/turnId/kind/since/until/cursor）。
   * @param append 为 `true` 时把更早一页追加到当前结果，否则整体替换。
   * @returns 检索完成后的状态文本；401 时返回空字符串并已切换登录页。
   */
  search: (params: URLSearchParams, append: boolean) => Promise<string>
}

/**
 * 维护事件账本的实时事件流与历史检索。
 *
 * @param enabled 是否建立事件通道；会话观察页挂载期间为 `true`。
 * @returns 事件状态与检索方法。
 */
export function useTraces(enabled: boolean): TracesState {
  const { handleUnauthorized } = useAuth()
  const [traces, setTraces] = useState<TraceEntry[]>([])
  const [skippedCount, setSkippedCount] = useState(0)
  const [historyCursor, setHistoryCursor] = useState<number | null>(null)
  const lastSeqRef = useRef(0)

  useEffect(() => {
    if (!enabled) return
    let socket: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let reconnectDelay = 1_000
    let stopped = false

    const connect = () => {
      if (stopped) return
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(
        `${scheme}://${location.host}/ws/events?since=${encodeURIComponent(lastSeqRef.current)}`,
      )
      socket.addEventListener('open', () => {
        reconnectDelay = 1_000
      })
      socket.addEventListener('message', (message) => {
        try {
          const payload = JSON.parse(String(message.data)) as {
            events?: unknown
            truncated?: unknown
            from?: unknown
          }
          if (!Array.isArray(payload.events)) return
          if (payload.truncated === true && typeof payload.from === 'number') {
            setSkippedCount((count) => count + Math.max(0, payload.from as number - lastSeqRef.current - 1))
          }
          const incoming: TraceEntry[] = []
          for (const value of payload.events) {
            const entry = record(value) as TraceEntry
            if (typeof entry.kind !== 'string') continue
            if (typeof entry.seq === 'number') {
              if (entry.seq <= lastSeqRef.current) continue
              lastSeqRef.current = entry.seq
            }
            incoming.push(entry)
          }
          if (!incoming.length) return
          setTraces((current) => [...current, ...incoming].slice(-MAX_TRACE_ENTRIES))
        } catch {
          // 单条事件解析失败按连接异常处理：关闭后由退避重连恢复同步。
          socket?.close()
        }
      })
      socket.addEventListener('close', () => {
        socket = null
        if (stopped) return
        reconnectTimer = setTimeout(connect, reconnectDelay)
        reconnectDelay = Math.min(reconnectDelay * 2, 30_000)
      })
    }

    connect()
    return () => {
      stopped = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      socket?.close()
    }
  }, [enabled])

  const search = useCallback(async (params: URLSearchParams, append: boolean): Promise<string> => {
    try {
      const payload = await apiFetch<{ events?: unknown; nextCursor?: unknown }>(`/events?${params}`)
      const incoming = Array.isArray(payload.events)
        ? payload.events.map((value) => record(value) as TraceEntry)
        : []
      setTraces((current) => {
        const combined = append ? [...current, ...incoming] : incoming
        const unique = new Map<number | string, TraceEntry>()
        for (const entry of combined) unique.set(entry.seq ?? `live-${entry.at}-${entry.kind}`, entry)
        return [...unique.values()].sort((left, right) => {
          if (typeof left.seq === 'number' && typeof right.seq === 'number') return left.seq - right.seq
          return left.at - right.at
        }).slice(-MAX_TRACE_ENTRIES)
      })
      const cursor = typeof payload.nextCursor === 'number' ? payload.nextCursor : null
      setHistoryCursor(cursor)
      return `已读取 ${incoming.length} 条${cursor === null ? '，没有更早记录' : ''}`
    } catch (error) {
      if (error instanceof UnauthorizedError) {
        handleUnauthorized(error)
        return ''
      }
      return `检索失败：${error instanceof Error ? error.message : String(error)}`
    }
  }, [handleUnauthorized])

  return { traces, skippedCount, historyCursor, search }
}
