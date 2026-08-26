/**
 * 联想网络的数据加载 hook。
 *
 * 对应后端 `/api/memory/graph`（整张图）与 `/api/memory/spread`（从指定记忆
 * 出发跑一次扩散预览）两个只读接口。整张图一次取完不分页：力导向布局需要
 * 全部边才能算出正确形状，分页拿到的半张图会收敛成一个错误的结构。
 */
import { useCallback, useEffect, useState } from 'react'

import { apiFetch, UnauthorizedError } from '@/lib/api'
import { useAuth } from './use-auth'

/** 记忆所在的层。三层各有各的生命周期，颜色与文案都按此区分。 */
export type MemoryKind = 'fact' | 'episode' | 'knowledge'

/** 图上的一个节点，字段与后端响应一一对应。 */
export interface MemoryNode {
  /** memory_nodes 主键，也是边两端引用的 ID。 */
  id: number
  kind: MemoryKind
  /** 该记忆在自己那一层内的主键。 */
  refId: number
  /** 记忆正文：事实内容 / 情节摘要 / 知识条目。 */
  text: string
  /** 分类标签：事实取 kind，知识取 source，情节取 kind。 */
  label: string
  /** 事实所属人物；情节与知识为 `null`。 */
  personId: number | null
  /** 当前留存度 0~1；情节与知识不衰减，恒为 1。 */
  retention: number
  /** 连接的边数。 */
  degree: number
  /** 被召回命中过几次；情节层没有这个计数，为 `null`。 */
  hits: number | null
  /** 指针指向的记忆是否仍存在；为假表示记忆已被删除而指针残留。 */
  alive: boolean
  updatedAt: number
}

/** 图上的一条边。 */
export interface MemoryEdge {
  id: number
  source: number
  target: number
  /** 上次加强后的存量强度。 */
  strength: number
  /** 折算到此刻的实际强度，即存量强度经过时间衰减后的值。 */
  retention: number
  updatedAt: number
  /** 为假表示已跌破冻结线，停止参与扩散但没有被删除。 */
  active: boolean
}

/** 整张网络的统计口径。 */
export interface MemoryGraphStats {
  nodeTotal: number
  edgeTotal: number
  activeEdges: number
  frozenEdges: number
  /** 一条边都没有的节点数：还没和任何别的记忆一起被点亮过。 */
  isolated: number
  /** 扩散实际跑过的次数，来自事件账本。 */
  spreadRuns: number
  /** 节点数超过上限被截断时为真。 */
  truncated: boolean
}

/** useMemoryGraph 返回的状态。 */
interface MemoryGraphState {
  nodes: MemoryNode[]
  edges: MemoryEdge[]
  stats: MemoryGraphStats
  loading: boolean
  error: string
  /** 重新拉取整张图。 */
  reload: () => void
}

/** 图为空时的统计占位，避免调用方到处判空。 */
const EMPTY_STATS: MemoryGraphStats = {
  nodeTotal: 0,
  edgeTotal: 0,
  activeEdges: 0,
  frozenEdges: 0,
  isolated: 0,
  spreadRuns: 0,
  truncated: false,
}

/**
 * 读取整张联想网络。
 *
 * @param includeFrozen 为真时把已冻结的边一并取回，用于观察被遗忘的连接。
 * @returns 节点、边、统计、加载态、错误文本与手动重载函数。
 */
export function useMemoryGraph(includeFrozen: boolean): MemoryGraphState {
  const { handleUnauthorized } = useAuth()
  const [nodes, setNodes] = useState<MemoryNode[]>([])
  const [edges, setEdges] = useState<MemoryEdge[]>([])
  const [stats, setStats] = useState<MemoryGraphStats>(EMPTY_STATS)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [nonce, setNonce] = useState(0)

  const reload = useCallback(() => setNonce((value) => value + 1), [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const params = new URLSearchParams()
    if (includeFrozen) params.set('includeFrozen', 'true')
    const query = params.toString()
    apiFetch<{ nodes: MemoryNode[]; edges: MemoryEdge[]; stats: MemoryGraphStats }>(
      query ? `/api/memory/graph?${query}` : '/api/memory/graph',
    )
      .then((payload) => {
        if (cancelled) return
        setNodes(payload.nodes)
        setEdges(payload.edges)
        setStats(payload.stats)
        setError('')
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof UnauthorizedError) handleUnauthorized(err)
        else setError(`联想网络请求失败：${err instanceof Error ? err.message : String(err)}`)
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [includeFrozen, nonce, handleUnauthorized])

  return { nodes, edges, stats, loading, error, reload }
}

/** 一条扩散命中，比节点多出打分与跳数两项。 */
export interface SpreadHit extends MemoryNode {
  /** 扩散打分，已含边强度、跳数衰减与该节点自身的留存度。 */
  score: number
  /** 距离种子几跳。 */
  hops: number
}

/** useSpreadPreview 返回的状态。 */
interface SpreadPreviewState {
  hits: SpreadHit[]
  loading: boolean
  error: string
  /** 已跑过预览的种子节点 ID；`null` 表示本次会话还没跑过。 */
  seedNodeId: number | null
  /** 以指定记忆为种子跑一次扩散。 */
  run: (node: MemoryNode) => void
  /** 清空上一次的预览结果。 */
  clear: () => void
}

/**
 * 按需跑一次扩散预览。
 *
 * 走的是运行时同一份 spread 实现，因此结果与她真实的联想一致；唯一差别是
 * 面板没有对话上下文，因而不带「刚才聊到过」的短期激活加成。
 *
 * @returns 命中列表、加载态、错误文本、当前种子与触发/清空函数。
 */
export function useSpreadPreview(): SpreadPreviewState {
  const { handleUnauthorized } = useAuth()
  const [hits, setHits] = useState<SpreadHit[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [seedNodeId, setSeedNodeId] = useState<number | null>(null)

  const clear = useCallback(() => {
    setHits([])
    setError('')
    setSeedNodeId(null)
  }, [])

  const run = useCallback((node: MemoryNode) => {
    setLoading(true)
    setSeedNodeId(node.id)
    const params = new URLSearchParams({ kind: node.kind, refId: String(node.refId) })
    apiFetch<{ hits: SpreadHit[] }>(`/api/memory/spread?${params.toString()}`)
      .then((payload) => {
        setHits(payload.hits)
        setError('')
      })
      .catch((err: unknown) => {
        if (err instanceof UnauthorizedError) handleUnauthorized(err)
        else setError(`扩散预览失败：${err instanceof Error ? err.message : String(err)}`)
        setHits([])
      })
      .finally(() => setLoading(false))
  }, [handleUnauthorized])

  return { hits, loading, error, seedNodeId, run, clear }
}
