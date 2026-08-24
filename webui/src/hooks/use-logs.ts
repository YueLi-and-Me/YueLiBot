/**
 * 实时日志的 WebSocket 数据通道。
 *
 * 接收后端日志行并解析 ANSI 样式，内存中最多保留 500 行；连接关闭后 3 秒
 * 重连。单条日志 JSON 解析失败只跳过该行，不关闭连接。组件卸载时关闭连接
 * 并清理重连定时器与批量冲刷定时器。
 *
 * 每行携带模块级自增 id 作为 React key：旧行的引用与 key 在新行到达时保持
 * 不变，配合 LogPanel 的 memo 行组件，单条日志只渲染新增的行，而不是整页
 * 500 行全量重渲染。
 *
 * 渲染合批（关键性能约束）：后端日志源头（logger 事件、trace_console 的 Rich
 * 面板）不做速率限制，打开页面瞬间还会回放 300 条积压。若按消息逐条 setState，
 * 每条消息都是一次独立渲染提交与强制滚动布局，日志风暴时主线程被渲染占满。
 * 因此入站行先入缓冲，最多每 100ms 合批提交一次，渲染频率被封顶在 10 次/秒。
 *
 * 单行截断（关键性能约束）：后端对单行日志没有长度上限，超长行（巨型 JSON、
 * base64、整段堆栈）在 `whitespace-pre-wrap` + `overflow-wrap:anywhere` 下的
 * 断行布局是秒级主线程阻塞，因此入站一律截断到 MAX_LINE_CHARS。
 */
import { useEffect, useState } from 'react'

import { parseAnsiLine, type AnsiSegment } from '@/lib/ansi'

/** 日志面板保留的最大行数。 */
const MAX_LOG_ROWS = 500
/** 单条日志的最大字符数，超出部分截断并附加标记；防御超长行引发的布局炸弹。 */
const MAX_LINE_CHARS = 4_000
/** 入站日志合批提交的间隔（毫秒）；渲染频率上限即 1000/FLUSH_MS。 */
const FLUSH_MS = 100

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
    let flushTimer: ReturnType<typeof setTimeout> | null = null
    let stopped = false
    /* 入站缓冲：消息到达只入队，由定时器合批提交，避免按消息逐条渲染。 */
    const pending: LogRowData[] = []

    const flush = () => {
      flushTimer = null
      if (stopped || !pending.length) return
      const batch = pending.splice(0, pending.length)
      setLines((current) => [...current, ...batch].slice(-MAX_LOG_ROWS))
    }
    const scheduleFlush = () => {
      if (flushTimer === null) flushTimer = setTimeout(flush, FLUSH_MS)
    }

    const connect = () => {
      if (stopped) return
      const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
      socket = new WebSocket(`${scheme}://${location.host}/ws/logs`)
      socket.addEventListener('open', () => setStatus('已连接'))
      socket.addEventListener('message', (event) => {
        try {
          const item = JSON.parse(String(event.data)) as { line?: unknown }
          if (typeof item.line === 'string') {
            const truncated =
              item.line.length > MAX_LINE_CHARS
                ? `${item.line.slice(0, MAX_LINE_CHARS)} …[超长已截断]`
                : item.line
            pending.push({ id: nextRowId++, segments: parseAnsiLine(truncated) })
            scheduleFlush()
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
      if (flushTimer) clearTimeout(flushTimer)
      socket?.close()
    }
  }, [enabled])

  return { lines, status }
}
