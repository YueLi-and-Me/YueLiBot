/**
 * 表达方式页：只读浏览历史迁移的表达方式。
 *
 * 页面顶部明确标注「尚未接入生成」——这些 situation/style 对来自旧库迁移，
 * 目前没有任何运行时消费方，她说话仍走配置里的固定表达序列；界面不得
 * 暗示她已经在使用这些表达。列表按使用次数排序，一行一条。
 */
import { Quote } from 'lucide-react'
import { useState } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import {
  Card,
  CardBody,
  Chip,
  Empty,
  ErrorText,
  Field,
  Loading,
  Pager,
  Select,
  SegmentedTabs,
} from '@/components/ui'
import { useExpressions, type ExpressionEntry } from '@/hooks/use-expressions'
import { useStreams } from '@/hooks/use-observability'
import { streamLabel } from '@/lib/format'

/** 页大小；一屏多一点为宜，太长要一直滚。后端路由 le=200，取值留足余量。 */
const PAGE_SIZE = 20

/**
 * 渲染单条表达方式行。
 *
 * @param props.entry 表达数据。
 * @param props.streamLabelOf 按 streamId 取会话标签的函数。
 * @returns 一行表达元素。
 */
function ExpressionRow({
  entry,
  streamLabelOf,
}: {
  entry: ExpressionEntry
  streamLabelOf: (id: number) => string
}) {
  return (
    <li className="flex flex-col gap-1.5 border-b border-border/50 px-4 py-3 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-[15px] font-semibold">「{entry.style}」</span>
        <Chip label="用过" value={`${entry.useCount} 次`} />
        {entry.streamId === null ? (
          <Chip label="范围" value="全局通用" />
        ) : (
          <Chip label="范围" value={streamLabelOf(entry.streamId)} />
        )}
        <span className="ml-auto font-mono text-[11px] text-muted-foreground">{entry.source}</span>
      </div>
      <p className="text-sm leading-relaxed text-muted-foreground">{entry.situation}</p>
    </li>
  )
}

/**
 * 渲染表达方式页。
 *
 * @returns 页面容器元素；筛选变化自动回到第一页。
 */
export function ExpressionsPage() {
  const { streams, error: streamsError } = useStreams()
  /** all=全部会话；其余为具体会话 ID。 */
  const [scope, setScope] = useState('all')
  const [order, setOrder] = useState<'use_desc' | 'use_asc'>('use_desc')
  const [page, setPage] = useState(0)

  const streamId = scope !== 'all' ? Number(scope) : null
  const { entries, total, loading, error } = useExpressions({
    streamId,
    order,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  })

  const streamLabelOf = (id: number) => {
    const stream = streams.find((item) => item.id === id)
    return stream ? streamLabel(stream) : `会话 #${id}`
  }

  const changeFilter = (apply: () => void) => {
    apply()
    setPage(0)
  }

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI · CONSOLE"
        title="表达方式"
        subtitle="历史会话里出现过的说法与情境，按使用次数排序。"
      />
      <div
        role="note"
        className="flex items-start gap-2 rounded-xl border border-warning/30 bg-warning-soft px-4 py-3 text-sm text-warning"
      >
        <Quote className="mt-0.5 size-4 flex-none" aria-hidden="true" />
        <p>
          这些表达来自历史数据迁移，<strong>尚未接入回复生成</strong>——她目前说话仍走配置里的固定表达序列。
          本页只读浏览，不做任何修改。
        </p>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <SegmentedTabs
          tabs={[
            { value: 'use_desc', label: '用得最多' },
            { value: 'use_asc', label: '用得最少' },
          ]}
          value={order}
          onChange={(next) => changeFilter(() => setOrder(next))}
        />
        <Field label="会话" htmlFor="expressions-scope" className="w-64">
          <Select
            id="expressions-scope"
            value={scope}
            onChange={(event) => changeFilter(() => setScope(event.target.value))}
          >
            <option value="all">全部会话</option>
            {streams.map((stream) => (
              <option key={stream.id} value={stream.id}>
                {streamLabel(stream)}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      {streamsError ? <ErrorText>{streamsError}</ErrorText> : null}
      {error ? <ErrorText>{error}</ErrorText> : null}
      {loading ? <Loading>正在读取表达方式…</Loading> : null}
      {!loading && !error && entries.length === 0 ? <Empty>没有符合条件的表达方式。</Empty> : null}
      {entries.length > 0 ? (
        <Card>
          <CardBody className="p-0">
            <ol>
              {entries.map((entry) => (
                <ExpressionRow key={entry.id} entry={entry} streamLabelOf={streamLabelOf} />
              ))}
            </ol>
          </CardBody>
        </Card>
      ) : null}

      <Pager page={page} pageCount={pageCount} total={total} onChange={setPage} disabled={loading} />
    </div>
  )
}
