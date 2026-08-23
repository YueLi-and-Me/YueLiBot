/**
 * 实时日志的 WebSocket 数据通道。
 *
 * 接收后端日志行并解析 ANSI 样式，内存中最多保留 500 行；连接关闭后 3 秒
 * 重连。单条日志 JSON 解析失败只跳过该行，不关闭连接。组件卸载时关闭连接
 * 并清理重连定时器。
 *
 * 每行携带模块级自增 id 作为 React key：旧行的引用与 key 在新行到达时保持
 * 不变，配合 LogPanel 的 memo 行组件，单条日志只渲染新增的一行，而不是整页
 * 500 行全量重渲染。
 */
import { useEffect, useState } from 'react'

import { parseAnsiLine, type AnsiSegment } from '@/lib/ansi'

/** 日志面板保留的最大行数。 */
const MAX_LOG_ROWS = 500

/** 一条已解析的日志行。 */
export interface LogRowData {
  /** 稳定自增 id，用作 React key。 */
  id: number
  /** 带样式的片段数组。 */
  segments: AnsiSegment[]
}

/** useLogs 返回的日志状态。 */
interface LogsState {
  /** 已解析的日志行。 */
  lines: LogRowData[]
  /** 连接状态文本（正在连接/已连接/重连提示）。 */
  status: string
}

/* 模块级自增计数：跨重连保持唯一，避免 key 重复导致整列表重挂载 */
let nextRowId = 1

/**
 * 维护实时日志 WebSocket 连接。
 *
 * @param enabled 是否建立日志通道；会话观察页挂载期间为 `true`。
 * @returns 日志行与连接状态。
 */
export function useLogs(enabled: boolean): LogsState {
  const [lines, setLines] = useState<LogRowData[]>([])
  const [status, setStatus] = useState('正在连接')

  useEffect(() => {
    if (!enabled) return
    let socket: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let stopped = false

    const connect = () => {
      if (stopped) return
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${scheme}://${location.host}/ws/logs`)
      socket.addEventListener('open', () => setStatus('已连接'))
      socket.addEventListener('message', (event) => {
        try {
          const item = JSON.parse(String(event.data)) as { line?: unknown }
          if (typeof item.line === 'string') {
            const row: LogRowData = { id: nextRowId++, segments: parseAnsiLine(item.line as string) }
            setLines((current) => [...current, row].slice(-MAX_LOG_ROWS))
          }
        } catch {
          // 单条格式损坏只跳过该行，连接会继续接收后续日志。
        }
      })
      socket.addEventListener('close', () => {
        if (stopped) return
        setStatus('已断开，3 秒后重连')
        reconnectTimer = setTimeout(connect, 3_000)
      })
    }

    connect()
    return () => {
      stopped = true
      if (reconnectTimer) clearTimeout(reconnectTimer)
      socket?.close()
    }
  }, [enabled])

  return { lines, status }
}
