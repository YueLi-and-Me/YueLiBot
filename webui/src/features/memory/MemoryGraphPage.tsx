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
import { Network, RefreshCw, Sparkles } from 'lucide-react'
import { useMemo, useState } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import {
  Button,
  Card,
  CardBody,
  Chip,
  Empty,
  ErrorText,
  Loading,
  Metric,
  SectionHeading,
  Toggle,
} from '@/components/ui'
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
        className="flex w-full cursor-pointer items-baseline gap-2 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-muted"
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
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI · CONSOLE"
        title="记忆联想网络"
        subtitle="记忆之间「总是一起被想起」的那张边，以及从任意一条出发的扩散结果。"
        actions={
          <>
            <Toggle
              checked={includeFrozen}
              onChange={setIncludeFrozen}
              label="包含已冻结的边"
            />
            <Button variant="ghost" onClick={reload} disabled={loading}>
              <RefreshCw className="size-4" aria-hidden="true" />
              刷新
            </Button>
          </>
        }
      />

      <Card>
        <CardBody className="grid grid-cols-2 gap-x-6 gap-y-0 sm:grid-cols-3 lg:grid-cols-6">
          <Metric label="节点" value={stats.nodeTotal} detail="三层记忆的指针" />
          <Metric label="边" value={stats.edgeTotal} detail="一起被想起过的配对" />
          <Metric label="活跃边" value={stats.activeEdges} detail="正在参与扩散" />
          <Metric label="已冻结" value={stats.frozenEdges} detail="暂停参与，未删除" />
          <Metric label="孤立节点" value={stats.isolated} detail="还没和谁一起出现过" />
          <Metric
            label="扩散跑过"
            value={`${stats.spreadRuns} 次`}
            detail="来自事件账本"
          />
        </CardBody>
      </Card>

      {stats.spreadRuns === 0 && stats.edgeTotal > 0 ? (
        <Card>
          <CardBody className="text-[13px] leading-relaxed text-muted-foreground">
            <strong className="text-foreground">边在长，但还没有被读过。</strong>
            {' '}
            已经建起 {stats.edgeTotal} 条边，扩散却一次都没跑过——扩散只在她主动
            选择「回忆」这个动作时才会发生。可以先用下面的扩散预览，看看这些边
            真的被用起来时会牵出什么。
          </CardBody>
        </Card>
      ) : null}

      {error ? <ErrorText>{error}</ErrorText> : null}

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
        <Card className="min-h-[560px]">
          <SectionHeading
            title="联想网络"
            subtitle={
              stats.truncated
                ? `节点过多，只画出连接最密的 ${nodes.length} 个`
                : `${nodes.length} 个节点 · ${edges.length} 条边`
            }
            icon={<Network />}
            tint="olive"
          />
          <div className="h-[520px] w-full">
            {loading ? (
              <div className="grid h-full place-items-center">
                <Loading>正在读取联想网络…</Loading>
              </div>
            ) : nodes.length === 0 ? (
              <div className="grid h-full place-items-center px-6">
                <Empty>
                  还没有任何记忆被连起来。边只在两条记忆「一起被点亮」时才长出来：
                  同一批被提取，或同一次回忆里一起进了提示词。
                </Empty>
              </div>
            ) : (
              <AssociationGraph
                nodes={nodes}
                edges={edges}
                selectedId={selectedId}
                onSelect={selectNode}
                spreadIds={spreadIds}
                seedId={spread.seedNodeId}
              />
            )}
          </div>
        </Card>

        <Card className="h-fit">
          <SectionHeading
            title={selected ? `${KIND_LABEL[selected.kind]} #${selected.refId}` : '怎么读这张图'}
            subtitle={selected ? selected.label || '未分类' : '点击节点查看详情'}
            icon={<Sparkles />}
            tint="coral"
          />
          <CardBody>
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

                <div className="border-t border-border pt-3">
                  <Button
                    onClick={() => spread.run(selected)}
                    disabled={spread.loading}
                    className="w-full"
                  >
                    <Sparkles className="size-4" aria-hidden="true" />
                    从这里扩散
                  </Button>
                  <p className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
                    跑的是她真实使用的那份扩散实现，只读不建边。与真机的唯一差别是
                    面板没有对话上下文，因此不含「刚才聊到过」的短期加成。
                  </p>
                </div>

                {spread.error ? <ErrorText>{spread.error}</ErrorText> : null}
                {spread.seedNodeId === selected.id && !spread.loading ? (
                  <div>
                    <h3 className="mb-1 text-xs font-semibold tracking-wide text-muted-foreground">
                      顺带想起来的
                    </h3>
                    {spread.hits.length === 0 ? (
                      <p className="px-2 text-xs text-muted-foreground">
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
                  <h3 className="mb-1 text-xs font-semibold tracking-wide text-muted-foreground">
                    直接相连（{neighbours.length}）
                  </h3>
                  <ul className="flex max-h-64 flex-col overflow-y-auto">
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
          </CardBody>
        </Card>
      </div>
    </div>
  )
}
