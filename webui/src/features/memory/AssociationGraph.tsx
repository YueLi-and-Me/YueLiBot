/**
 * 联想网络的力导向图渲染。
 *
 * 把 `memory_nodes` / `memory_edges` 画成一张可拖拽、可缩放的 SVG 图：节点颜色
 * 区分三层记忆，半径随度数增长，边的粗细表示存量强度、透明度表示衰减之后的
 * 实际强度——「这条边有多强」与「它多久没被用了」是两件事，用同一个通道表达
 * 就分不出「强而久未用」和「弱而刚用过」。
 *
 * 布局数值迭代在 `./force-layout` 里，本组件只负责驱动迭代、坐标换算与绘制。
 * 依赖 `@/hooks/use-memory-graph` 的数据类型，被 `./MemoryGraphPage` 使用。
 */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'

import { cn } from '@/components/ui'
import type { MemoryEdge, MemoryKind, MemoryNode } from '@/hooks/use-memory-graph'

import {
  DRAG_TICKS,
  RELAX_TICKS,
  SETTLE_TICKS,
  fitViewBox,
  seedParticles,
  settleLayout,
  stepLayout,
  type Particle,
  type Spring,
  type ViewBox,
} from './force-layout'

/** 三层记忆的取色，全部走主题变量，跟随亮暗色自动切换。 */
export const KIND_COLOR: Record<MemoryKind, string> = {
  fact: 'var(--color-tint-coral)',
  episode: 'var(--color-tint-amber)',
  knowledge: 'var(--color-tint-olive)',
}

/** 三层记忆的中文名，图例与详情面板共用。 */
export const KIND_LABEL: Record<MemoryKind, string> = {
  fact: '事实',
  episode: '情节',
  knowledge: '知识',
}

/** 节点标签的字符数上限，超出以省略号截断。 */
const LABEL_MAX_CHARS = 12
/** 超过这个节点数就不再默认画标签，只在聚焦时显示，否则文字会糊成一片。 */
const LABEL_NODE_LIMIT = 60
/** 视口留白（像素），需容纳最大节点半径与标签高度。 */
const VIEW_PADDING = 74
/** 缩放的上下限，相对自动适配后的视口。 */
const ZOOM_MIN = 0.35
const ZOOM_MAX = 4
/** 一次滚轮的缩放步进比例。 */
const ZOOM_STEP = 1.14
/** 按下到松开之间超过这个像素位移就算拖拽，不再触发选中。 */
const CLICK_SLOP = 4

interface AssociationGraphProps {
  nodes: MemoryNode[]
  edges: MemoryEdge[]
  /** 当前选中的节点 ID；`null` 表示未选中。 */
  selectedId: number | null
  /** 点击节点的回调。 */
  onSelect: (node: MemoryNode) => void
  /** 扩散预览命中的节点 ID 集合，会在图上高亮为「顺带想起」。 */
  spreadIds: ReadonlySet<number>
  /** 扩散预览的种子节点 ID。 */
  seedId: number | null
}

/**
 * 把正文截成节点标签。
 *
 * @param text 记忆正文。
 * @returns 截断后的短标签。
 */
function shortLabel(text: string): string {
  const clean = text.replace(/\s+/g, ' ').trim()
  return clean.length > LABEL_MAX_CHARS ? `${clean.slice(0, LABEL_MAX_CHARS)}…` : clean
}

/**
 * 按度数算节点半径。
 *
 * @param degree 连接的边数。
 * @returns 半径（视口单位）。
 * @remarks 用平方根而不是线性：度数分布长尾，线性会让一个枢纽节点把其余全部
 * 压成看不见的小点。
 */
function nodeRadius(degree: number): number {
  return 7 + 3.2 * Math.sqrt(degree)
}

/**
 * 渲染联想网络图。
 *
 * @param props 见 {@link AssociationGraphProps}。
 * @returns 铺满容器的 SVG 图元素。
 */
export function AssociationGraph({
  nodes,
  edges,
  selectedId,
  onSelect,
  spreadIds,
  seedId,
}: AssociationGraphProps) {
  const svgRef = useRef<SVGSVGElement | null>(null)
  const particlesRef = useRef<Particle[]>([])
  const springsRef = useRef<Spring[]>([])
  /** 跨数据刷新保留的坐标：切换「包含已冻结的边」时图不该整个跳一次。 */
  const positionsRef = useRef(new Map<number, { x: number; y: number }>())
  const rafRef = useRef(0)
  const dragRef = useRef<{ id: number; moved: number } | null>(null)
  const panRef = useRef<{ x: number; y: number; viewX: number; viewY: number } | null>(null)
  const [, repaint] = useReducer((count: number) => count + 1, 0)
  const [viewBox, setViewBox] = useState<ViewBox>({ x: -200, y: -150, width: 400, height: 300 })
  /** 自动适配得到的基准视口，缩放与平移都相对它计算。 */
  const baseRef = useRef<ViewBox>(viewBox)
  const [hoverId, setHoverId] = useState<number | null>(null)

  const nodeById = useMemo(() => {
    const map = new Map<number, MemoryNode>()
    for (const node of nodes) map.set(node.id, node)
    return map
  }, [nodes])

  /** 与选中/悬停节点直接相连的节点，聚焦时其余节点淡出。 */
  const neighbours = useMemo(() => {
    const focus = hoverId ?? selectedId
    if (focus === null) return null
    const set = new Set<number>([focus])
    for (const edge of edges) {
      if (edge.source === focus) set.add(edge.target)
      if (edge.target === focus) set.add(edge.source)
    }
    return set
  }, [edges, hoverId, selectedId])

  // 数据变化时重建粒子并同步收敛。同步跑完再渲染，用户看到的是已经成形的图，
  // 而不是一团从原点炸开的动画。
  useEffect(() => {
    const ids = nodes.map((node) => node.id)
    const seeded = seedParticles(ids)
    for (const particle of seeded) {
      const remembered = positionsRef.current.get(particle.id)
      if (remembered) {
        particle.x = remembered.x
        particle.y = remembered.y
      }
    }
    particlesRef.current = seeded
    springsRef.current = edges.map((edge) => ({
      source: edge.source,
      target: edge.target,
      // 用衰减之后的实际强度而非存量强度：久未被一起点亮的两条记忆理应离得更远。
      strength: edge.retention,
    }))
    settleLayout(seeded, springsRef.current, SETTLE_TICKS)
    for (const particle of seeded) {
      positionsRef.current.set(particle.id, { x: particle.x, y: particle.y })
    }
    const fitted = fitViewBox(seeded, VIEW_PADDING)
    baseRef.current = fitted
    setViewBox(fitted)
    repaint()
  }, [nodes, edges])

  useEffect(() => () => cancelAnimationFrame(rafRef.current), [])

  /**
   * 把浏览器坐标换算到视口坐标。
   *
   * @param clientX 浏览器 X。
   * @param clientY 浏览器 Y。
   * @returns 视口坐标；SVG 尚未挂载时返回原点。
   */
  const toViewPoint = useCallback((clientX: number, clientY: number) => {
    const svg = svgRef.current
    if (!svg) return { x: 0, y: 0 }
    const rect = svg.getBoundingClientRect()
    return {
      x: viewBox.x + ((clientX - rect.left) / rect.width) * viewBox.width,
      y: viewBox.y + ((clientY - rect.top) / rect.height) * viewBox.height,
    }
  }, [viewBox])

  /**
   * 以指针位置为锚点缩放视口。
   *
   * @remarks 非被动监听器，需要 `preventDefault` 阻止页面跟着滚动；React 的
   * onWheel 在根节点上注册为被动监听，在其中调用 preventDefault 无效并告警，
   * 因此这里直接向元素注册。
   */
  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const rect = svg.getBoundingClientRect()
      const ratio = event.deltaY > 0 ? ZOOM_STEP : 1 / ZOOM_STEP
      setViewBox((current) => {
        const base = baseRef.current
        const nextWidth = current.width * ratio
        // 以基准视口为参照夹住缩放级别，避免无限放大后再也找不回原图。
        const level = base.width / nextWidth
        if (level < ZOOM_MIN || level > ZOOM_MAX) return current
        const nextHeight = current.height * ratio
        const px = (event.clientX - rect.left) / rect.width
        const py = (event.clientY - rect.top) / rect.height
        return {
          x: current.x + (current.width - nextWidth) * px,
          y: current.y + (current.height - nextHeight) * py,
          width: nextWidth,
          height: nextHeight,
        }
      })
    }
    svg.addEventListener('wheel', onWheel, { passive: false })
    return () => svg.removeEventListener('wheel', onWheel)
  }, [])

  /** 拖拽期间每帧补跑几步迭代，让邻居跟着被拖的节点移动。 */
  const runDragFrame = useCallback(() => {
    stepLayoutTimes(particlesRef.current, springsRef.current, DRAG_TICKS)
    repaint()
    rafRef.current = requestAnimationFrame(runDragFrame)
  }, [])

  const onNodePointerDown = useCallback((event: React.PointerEvent, id: number) => {
    // 不做 setPointerCapture：拖拽的移动与松手都监听在 svg 上，指针离开画布时
    // onPointerLeave 会收尾。捕获只在「拖到画布之外还要继续」时才有意义，而那
    // 时节点已经跑出视口，继续跟随并没有价值。
    event.stopPropagation()
    const particle = particlesRef.current.find((item) => item.id === id)
    if (!particle) return
    particle.fixed = true
    dragRef.current = { id, moved: 0 }
    cancelAnimationFrame(rafRef.current)
    rafRef.current = requestAnimationFrame(runDragFrame)
  }, [runDragFrame])

  const onPointerMove = useCallback((event: React.PointerEvent) => {
    const drag = dragRef.current
    if (drag) {
      const point = toViewPoint(event.clientX, event.clientY)
      const particle = particlesRef.current.find((item) => item.id === drag.id)
      if (particle) {
        drag.moved += Math.abs(point.x - particle.x) + Math.abs(point.y - particle.y)
        particle.x = point.x
        particle.y = point.y
      }
      return
    }
    const pan = panRef.current
    if (pan) {
      const svg = svgRef.current
      if (!svg) return
      const rect = svg.getBoundingClientRect()
      const dx = ((event.clientX - pan.x) / rect.width) * viewBox.width
      const dy = ((event.clientY - pan.y) / rect.height) * viewBox.height
      setViewBox((current) => ({ ...current, x: pan.viewX - dx, y: pan.viewY - dy }))
    }
  }, [toViewPoint, viewBox.width, viewBox.height])

  const onPointerUp = useCallback(() => {
    const drag = dragRef.current
    panRef.current = null
    if (!drag) return
    dragRef.current = null
    cancelAnimationFrame(rafRef.current)
    const particle = particlesRef.current.find((item) => item.id === drag.id)
    if (particle) particle.fixed = false
    // 松手后回稳一小段，把拖拽期间的形变收敛掉；温度取一个低值，避免整张图重排。
    settleLayout(particlesRef.current, springsRef.current, RELAX_TICKS, 0.45)
    for (const item of particlesRef.current) {
      positionsRef.current.set(item.id, { x: item.x, y: item.y })
    }
    repaint()
    // 位移没超过阈值就当成一次点击：拖拽和选中共用同一个按下动作。
    if (drag.moved <= CLICK_SLOP) {
      const node = nodeById.get(drag.id)
      if (node) onSelect(node)
    }
  }, [nodeById, onSelect])

  const onBackgroundPointerDown = useCallback((event: React.PointerEvent) => {
    panRef.current = {
      x: event.clientX,
      y: event.clientY,
      viewX: viewBox.x,
      viewY: viewBox.y,
    }
  }, [viewBox.x, viewBox.y])

  /** 恢复到自动适配的视口。 */
  const resetView = useCallback(() => setViewBox(baseRef.current), [])

  // 每次渲染重建一次坐标索引：粒子数组是原地迭代的，缓存住引用就读不到新坐标，
  // 而节点上限只有 1000，重建一个 Map 的代价远低于一次误判的缓存。
  const positionOf = new Map<number, Particle>()
  for (const particle of particlesRef.current) positionOf.set(particle.id, particle)
  const showLabels = nodes.length <= LABEL_NODE_LIMIT

  return (
    <div className="relative h-full w-full">
      <svg
        ref={svgRef}
        viewBox={`${viewBox.x} ${viewBox.y} ${viewBox.width} ${viewBox.height}`}
        className="h-full w-full cursor-grab touch-none select-none active:cursor-grabbing"
        onPointerDown={onBackgroundPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={onPointerUp}
        role="img"
        aria-label={`联想网络：${nodes.length} 个节点，${edges.length} 条边`}
      >
        <g>
          {edges.map((edge) => {
            const a = positionOf.get(edge.source)
            const b = positionOf.get(edge.target)
            if (!a || !b) return null
            const focused = neighbours === null
              || (neighbours.has(edge.source) && neighbours.has(edge.target))
            return (
              <line
                key={edge.id}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke="currentColor"
                className={edge.active ? 'text-foreground' : 'text-muted-foreground'}
                // 冻结的边画成虚线：它还在，只是不再参与扩散。
                strokeDasharray={edge.active ? undefined : '5 5'}
                strokeWidth={1 + 3.4 * edge.strength}
                // 留存度映射到 0.28~0.78 而不是直接当作不透明度：真机里的边强度
                // 长期停在建边初值 0.35 附近，直接乘出来是 0.35 的不透明度，
                // 在浅色底上几乎看不见，整张图会显得没有边。
                strokeOpacity={(0.28 + 0.5 * edge.retention) * (focused ? 1 : 0.16)}
                strokeLinecap="round"
              />
            )
          })}
        </g>
        <g>
          {nodes.map((node) => {
            const particle = positionOf.get(node.id)
            if (!particle) return null
            const radius = nodeRadius(node.degree)
            const focused = neighbours === null || neighbours.has(node.id)
            const isSeed = seedId === node.id
            const isHit = spreadIds.has(node.id)
            const selected = selectedId === node.id
            return (
              <g
                key={node.id}
                transform={`translate(${particle.x} ${particle.y})`}
                className="cursor-pointer"
                onPointerDown={(event) => onNodePointerDown(event, node.id)}
                onPointerEnter={() => setHoverId(node.id)}
                onPointerLeave={() => setHoverId(null)}
                opacity={focused ? 1 : 0.16}
              >
                <title>{`${KIND_LABEL[node.kind]} #${node.refId}｜${node.text}`}</title>
                {/* 扩散命中与种子各有一圈外环：一眼分出「起点」和「被牵出来的」。 */}
                {isSeed || isHit ? (
                  <circle
                    r={radius + 7}
                    fill="none"
                    stroke={isSeed ? 'var(--color-primary-strong)' : KIND_COLOR[node.kind]}
                    strokeWidth={isSeed ? 3 : 2}
                    strokeDasharray={isSeed ? undefined : '4 3'}
                    className={isSeed ? 'animate-pulse-dot' : undefined}
                  />
                ) : null}
                <circle
                  r={radius}
                  fill={KIND_COLOR[node.kind]}
                  // 留存度直接映射到不透明度：正在被遗忘的记忆在图上就是淡的。
                  fillOpacity={node.alive ? Math.max(0.32, node.retention) : 0.12}
                  stroke="var(--color-card)"
                  strokeWidth={selected ? 3.5 : 1.6}
                />
                {selected ? (
                  <circle r={radius + 3.5} fill="none" stroke="var(--color-ink)" strokeWidth={1.6} />
                ) : null}
                {showLabels || focused ? (
                  <text
                    y={radius + 14}
                    textAnchor="middle"
                    className="pointer-events-none fill-foreground"
                    style={{ fontSize: 11 }}
                  >
                    {shortLabel(node.text)}
                  </text>
                ) : null}
              </g>
            )
          })}
        </g>
      </svg>
      <button
        type="button"
        onClick={resetView}
        className={cn(
          'absolute right-3 bottom-3 rounded-lg border border-border bg-card/90 px-2.5 py-1.5',
          'text-[11px] font-medium text-muted-foreground backdrop-blur',
          'cursor-pointer transition-colors hover:text-foreground',
        )}
      >
        复位视图
      </button>
    </div>
  )
}

/**
 * 连续推进若干步布局迭代。
 *
 * @param particles 粒子数组，原地修改。
 * @param springs 边列表。
 * @param times 迭代次数。
 * @remarks 拖拽期间用固定的低温推进，不做温度衰减：拖拽是持续输入，温度衰减
 * 会让拖到后面邻居逐渐不再跟随。
 */
function stepLayoutTimes(particles: Particle[], springs: readonly Spring[], times: number): void {
  for (let i = 0; i < times; i += 1) stepLayout(particles, springs, 0.6)
}
