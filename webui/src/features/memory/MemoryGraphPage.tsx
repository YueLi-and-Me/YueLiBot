/**
 * 记忆联想网络页：把「哪些记忆总是一起被想起」画出来。
 *
 * 三层记忆（事实 / 情节 / 知识）本身在别的页面已经能逐条看，唯独把它们连起来
 * 的那张边一直只存在于库里。本页负责两件在别处看不到的事：
 *
 * 1. 网络的当前形状——谁是枢纽、哪些记忆抱成一团、哪些边正在被遗忘；
 * 2. 扩散的实际结果——选中一条记忆，跑一次真实的扩散，看她会顺带想起什么。
 *
 * 扩散预览调用的是运行时同一份实现（`/api/memory/spread`），不是另写一套近似，
 * 因此看到的就是她真会想起的东西。数据来自 `@/hooks/use-memory-graph`，图形
 * 渲染在 `./AssociationGraph`。
 */
import { RefreshCw, Sparkles } from 'lucide-react'
import { useMemo, useState } from 'react'

import { PageFrame } from '@/components/layout/PageFrame'
import { Pane, PaneHeader, PaneTitle, Panes, ScrollArea, Toolbar } from '@/components/layout/panes'
import { Button, Chip, Empty, ErrorText, Loading, Toggle } from '@/components/ui'
import {
  useMemoryGraph,
  useSpreadPreview,
  type MemoryEdge,
  type MemoryKind,
  type MemoryNode,
} from '@/hooks/use-memory-graph'
import { dateTime } from '@/lib/format'

import { AssociationGraph, KIND_COLOR, KIND_LABEL } from './AssociationGraph'

/** 图例条目：三层记忆各一条。 */
const LEGEND_KINDS: MemoryKind[] = ['fact', 'episode', 'knowledge']

/** 三层记忆在图例上的一句话解释。 */
const KIND_HINT: Record<MemoryKind, string> = {
  fact: '她记得的关于某个人的事',
  episode: '你们一起经历过的一段对话',
  knowledge: '与具体某个人无关的概念与常识',
}

/**
 * 渲染一个圆点色块。
 *
 * @param props.kind 记忆所在层。
 * @returns 与图上节点同色的小圆点。
 */
function KindDot({ kind }: { kind: MemoryKind }) {
  return (
    <span
      className="inline-block size-2.5 flex-none rounded-full"
      style={{ background: KIND_COLOR[kind] }}
      aria-hidden="true"
    />
  )
}

/**
 * 渲染图例与读图说明。
 *
 * @returns 未选中节点时展示在右栏的说明卡片内容。
 */
function GraphLegend() {
  return (
    <div className="flex flex-col gap-4">
      <ul className="flex flex-col gap-2">
        {LEGEND_KINDS.map((kind) => (
          <li key={kind} className="flex items-baseline gap-2 text-[13px]">
            <KindDot kind={kind} />
            <span className="font-medium">{KIND_LABEL[kind]}</span>
            <span className="text-xs text-muted-foreground">{KIND_HINT[kind]}</span>
          </li>
        ))}
      </ul>
      <dl className="flex flex-col gap-1.5 border-t border-border pt-3 text-xs text-muted-foreground">
        <div className="flex gap-2">
          <dt className="w-16 flex-none font-medium text-foreground">圆点大小</dt>
          <dd>连着几条边。越大越像枢纽，牵一发动全身。</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-16 flex-none font-medium text-foreground">圆点深浅</dt>
          <dd>当前留存度。越淡表示越接近被遗忘。</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-16 flex-none font-medium text-foreground">连线粗细</dt>
          <dd>这两条记忆一起被想起过多少次。</dd>
        </div>
        <div className="flex gap-2">
          <dt className="w-16 flex-none font-medium text-foreground">虚线</dt>
          <dd>已跌破冻结线，暂停参与联想，但没有被删除。</dd>
        </div>
      </dl>
      <p className="text-xs leading-relaxed text-muted-foreground">
        点击任意节点查看正文，并可从它出发跑一次扩散；拖拽节点可以把缠在一起的
        部分拉开，滚轮缩放，空白处拖动平移。
      </p>
    </div>
  )
}

/**
 * 渲染一条记忆的紧凑行，用于邻居列表与扩散结果。
 *
 * @param props.node 记忆节点。
 * @param props.badge 行尾的标注文本（边强度或扩散打分）。
 * @param props.onClick 点击回调。
 * @returns 一行可点击的记忆条目。
 */
function MemoryRow({
  node,
  badge,
  onClick,
}: {
  node: MemoryNode
  badge: string
  onClick: () => void
}) {
  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        className="-mx-2 flex w-[calc(100%+1rem)] cursor-pointer items-baseline gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-muted"
      >
        <KindDot kind={node.kind} />
        <span className="min-w-0 flex-1 truncate text-[13px]" title={node.text}>
          {node.text}
        </span>
        <span className="flex-none font-mono text-[11px] text-muted-foreground">{badge}</span>
      </button>
    </li>
  )
}

/**
 * 渲染工具栏里的一项网络统计。
 *
 * @param props.label 统计名。
 * @param props.value 统计值。
 * @param props.detail 悬停时显示的口径说明。
 * @returns 「名称 值」的行内片段。
 */
function GraphStat({ label, value, detail }: { label: string; value: number | string; detail: string }) {
  return (
    <span title={detail} className="whitespace-nowrap">
      {label} <strong className="font-mono text-[13px] font-semibold text-foreground tabular-nums">{value}</strong>
    </span>
  )
}

/**
 * 渲染记忆联想网络页。
 *
 * @returns 页面容器元素。
 */
export function MemoryGraphPage() {
  const [includeFrozen, setIncludeFrozen] = useState(false)
  const { nodes, edges, stats, loading, error, reload } = useMemoryGraph(includeFrozen)
  const spread = useSpreadPreview()
  const [selectedId, setSelectedId] = useState<number | null>(null)

  const nodeById = useMemo(() => {
    const map = new Map<number, MemoryNode>()
    for (const node of nodes) map.set(node.id, node)
    return map
  }, [nodes])

  const selected = selectedId === null ? null : nodeById.get(selectedId) ?? null

  /** 选中节点的邻居，按边的实际强度降序。 */
  const neighbours = useMemo(() => {
    if (selectedId === null) return []
    const paired: Array<{ node: MemoryNode; edge: MemoryEdge }> = []
    for (const edge of edges) {
      const otherId = edge.source === selectedId
        ? edge.target
        : edge.target === selectedId ? edge.source : null
      if (otherId === null) continue
      const node = nodeById.get(otherId)
      if (node) paired.push({ node, edge })
    }
    return paired.sort((a, b) => b.edge.retention - a.edge.retention)
  }, [edges, nodeById, selectedId])

  const spreadIds = useMemo(
    () => new Set(spread.hits.map((hit) => hit.id)),
    [spread.hits],
  )

  const selectNode = (node: MemoryNode) => {
    setSelectedId(node.id)
    // 切换选中就清掉上一次的扩散结果：留着会让高亮圈指向另一个种子的命中，
    // 看起来像是新选中的节点牵出了它们。
    if (spread.seedNodeId !== node.id) spread.clear()
  }

  return (
    <PageFrame
      hint="记忆之间「总是一起被想起」的那张边，以及从任意一条出发的扩散结果。"
      actions={
        <>
          <Toggle checked={includeFrozen} onChange={setIncludeFrozen} label="包含已冻结的边" />
          <Button variant="secondary" onClick={reload} disabled={loading}>
            <RefreshCw className="size-4" aria-hidden="true" />
            刷新
          </Button>
        </>
      }
    >
      <Panes>
        <Pane aria-label="联想网络">
          <Toolbar className="gap-x-5 text-xs text-muted-foreground">
            <GraphStat label="节点" value={stats.nodeTotal} detail="三层记忆的指针" />
            <GraphStat label="边" value={stats.edgeTotal} detail="一起被想起过的配对" />
            <GraphStat label="活跃边" value={stats.activeEdges} detail="正在参与扩散" />
            <GraphStat label="已冻结" value={stats.frozenEdges} detail="暂停参与，未删除" />
            <GraphStat label="孤立节点" value={stats.isolated} detail="还没和谁一起出现过" />
            <GraphStat label="扩散跑过" value={`${stats.spreadRuns} 次`} detail="来自事件账本" />
            <span className="ml-auto">
              {stats.truncated
                ? `节点过多，只画出连接最密的 ${nodes.length} 个`
                : `画出 ${nodes.length} 个节点 · ${edges.length} 条边`}
            </span>
          </Toolbar>

          {stats.spreadRuns === 0 && stats.edgeTotal > 0 ? (
            <div className="flex-none border-b border-border bg-warning-soft px-6 py-2.5 text-[13px] leading-relaxed text-muted-foreground">
              <strong className="text-warning">边在长，但还没有被读过。</strong>
              {' '}
              已经建起 {stats.edgeTotal} 条边，扩散却一次都没跑过——扩散只在她主动
              选择「回忆」这个动作时才会发生。可以先在右侧的扩散预览里看看这些边
              真的被用起来时会牵出什么。
            </div>
          ) : null}

          {error ? <div className="flex-none px-6 pt-3"><ErrorText>{error}</ErrorText></div> : null}

          <div
            className="relative min-h-[420px] flex-1 bg-muted/40"
            style={{
              backgroundImage: 'radial-gradient(hsl(var(--border)) 1px, transparent 1px)',
              backgroundSize: '20px 20px',
            }}
          >
            {loading ? (
              <div className="absolute inset-0 grid place-items-center">
                <Loading>正在读取联想网络…</Loading>
              </div>
            ) : nodes.length === 0 ? (
              <div className="absolute inset-0 grid place-items-center px-6">
                <Empty>
                  还没有任何记忆被连起来。边只在两条记忆「一起被点亮」时才长出来：
                  同一批被提取，或同一次回忆里一起进了提示词。
                </Empty>
              </div>
            ) : (
              <div className="absolute inset-0">
                <AssociationGraph
                  nodes={nodes}
                  edges={edges}
                  selectedId={selectedId}
                  onSelect={selectNode}
                  spreadIds={spreadIds}
                  seedId={spread.seedNodeId}
                />
              </div>
            )}
          </div>
        </Pane>

        <Pane width="inspector" aria-label="节点详情">
          <PaneHeader>
            <PaneTitle>{selected ? `${KIND_LABEL[selected.kind]} #${selected.refId}` : '怎么读这张图'}</PaneTitle>
            {selected ? <span className="truncate text-xs text-subtle-foreground">{selected.label || '未分类'}</span> : null}
          </PaneHeader>
          <ScrollArea className="px-6 py-4">
            {selected === null ? (
              <GraphLegend />
            ) : (
              <div className="flex flex-col gap-4">
                <p className="text-[13px] leading-relaxed">{selected.text}</p>
                <div className="flex flex-wrap gap-1.5">
                  <Chip label="留存" value={selected.retention.toFixed(2)} />
                  <Chip label="连边" value={`${selected.degree} 条`} />
                  {selected.hits !== null ? <Chip label="命中" value={`${selected.hits} 次`} /> : null}
                  {selected.personId !== null ? (
                    <Chip label="人物" value={`#${selected.personId}`} />
                  ) : null}
                  <Chip label="更新" value={dateTime(selected.updatedAt)} />
                </div>
                {!selected.alive ? (
                  <ErrorText>这条记忆已被删除，图上只剩下指向它的指针。</ErrorText>
                ) : null}

                <div className="border-t border-border pt-4">
                  <Button
                    onClick={() => spread.run(selected)}
                    disabled={spread.loading}
                    className="w-full"
                  >
                    <Sparkles className="size-4" aria-hidden="true" />
                    从这里扩散
                  </Button>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-subtle-foreground">
                    跑的是她真实使用的那份扩散实现，只读不建边。与真机的唯一差别是
                    面板没有对话上下文，因此不含「刚才聊到过」的短期加成。
                  </p>
                </div>

                {spread.error ? <ErrorText>{spread.error}</ErrorText> : null}
                {spread.seedNodeId === selected.id && !spread.loading ? (
                  <div>
                    <h3 className="mb-1 text-[11px] font-semibold tracking-[0.04em] text-subtle-foreground">
                      顺带想起来的
                    </h3>
                    {spread.hits.length === 0 ? (
                      <p className="text-xs text-muted-foreground">
                        没有牵出任何东西。它的邻居要么留存度太低被跳过，要么本身就是种子。
                      </p>
                    ) : (
                      <ul className="flex flex-col">
                        {spread.hits.map((hit) => (
                          <MemoryRow
                            key={hit.id}
                            node={hit}
                            badge={`${hit.hops} 跳 · ${hit.score.toFixed(3)}`}
                            onClick={() => selectNode(hit)}
                          />
                        ))}
                      </ul>
                    )}
                  </div>
                ) : null}

                <div>
                  <h3 className="mb-1 text-[11px] font-semibold tracking-[0.04em] text-subtle-foreground">
                    直接相连（{neighbours.length}）
                  </h3>
                  <ul className="flex flex-col">
                    {neighbours.map(({ node, edge }) => (
                      <MemoryRow
                        key={edge.id}
                        node={node}
                        badge={edge.active ? edge.retention.toFixed(2) : '已冻结'}
                        onClick={() => selectNode(node)}
                      />
                    ))}
                  </ul>
                </div>
              </div>
            )}
          </ScrollArea>
        </Pane>
      </Panes>
    </PageFrame>
  )
}
