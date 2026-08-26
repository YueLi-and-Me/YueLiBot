/**
 * 黑话词表页：只读浏览历史迁移的黑话词条。
 *
 * 列表形态（一人一行同款理由：信息密度优先于卡片网格）。一眼要能看出
 * 「这条是全局的还是只在某个群成立」：词条行内用范围徽标区分全局与会话。
 * 默认只显示已确认词条；待定候选需显式切到「待定」页签——历史迁移目前
 * 只导入了已确认词条，待定为空属正常。关键词输入采用提交式（回车或点
 * 按钮），避免逐键请求。
 */
import { Search, X } from 'lucide-react'
import { useState, type FormEvent } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import {
  Button,
  Card,
  CardBody,
  Chip,
  Empty,
  ErrorText,
  Field,
  Input,
  Loading,
  Pager,
  Select,
  SegmentedTabs,
} from '@/components/ui'
import { useJargon, type JargonEntry } from '@/hooks/use-jargon'
import { useStreams } from '@/hooks/use-observability'
import { streamLabel } from '@/lib/format'

/** 页大小；一屏多一点为宜，太长要一直滚。后端路由 le=200，取值留足余量。 */
const PAGE_SIZE = 20

/**
 * 渲染单条黑话词条行。
 *
 * @param props.entry 词条数据。
 * @param props.streamLabelOf 按 streamId 取会话标签的函数。
 * @returns 一行词条元素。
 */
function JargonRow({ entry, streamLabelOf }: { entry: JargonEntry; streamLabelOf: (id: number) => string }) {
  return (
    <li className="flex flex-col gap-1.5 border-b border-border/50 px-4 py-3 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-[15px] font-semibold">「{entry.term}」</span>
        <Chip
          label="范围"
          value={entry.streamId === null ? '全局通用' : streamLabelOf(entry.streamId)}
        />
        <Chip label="命中" value={`${entry.hits} 次`} />
        <span className="ml-auto font-mono text-[11px] text-muted-foreground">{entry.source}</span>
      </div>
      {/* 释义普遍是长段落，默认收两行，完整内容悬停可见。 */}
      <p className="line-clamp-2 text-sm leading-relaxed text-muted-foreground" title={entry.meaning}>
        {entry.meaning}
      </p>
    </li>
  )
}

/**
 * 渲染黑话词表页。
 *
 * @returns 页面容器元素；筛选变化自动回到第一页。
 */
export function JargonPage() {
  const { streams, error: streamsError } = useStreams()
  const [status, setStatus] = useState<'confirmed' | 'pending'>('confirmed')
  /** all=全部；global=仅全局；其余为具体会话 ID。 */
  const [scope, setScope] = useState('all')
  const [keywordInput, setKeywordInput] = useState('')
  const [keyword, setKeyword] = useState('')
  const [page, setPage] = useState(0)

  const streamId = scope !== 'all' && scope !== 'global' ? Number(scope) : null
  const { entries, total, loading, error } = useJargon({
    status,
    streamId,
    globalOnly: scope === 'global',
    keyword,
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

  const submitKeyword = (event: FormEvent) => {
    event.preventDefault()
    changeFilter(() => setKeyword(keywordInput.trim()))
  }

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI · CONSOLE"
        title="黑话词表"
        subtitle="只有他们才懂的说法与含义；词条来自历史迁移，本页只读浏览。"
      />
      <div className="flex flex-wrap items-end gap-3">
        <SegmentedTabs
          tabs={[
            { value: 'confirmed', label: '已确认' },
            { value: 'pending', label: '待定' },
          ]}
          value={status}
          onChange={(next) => changeFilter(() => setStatus(next))}
        />
        <Field label="范围" htmlFor="jargon-scope" className="w-64">
          <Select
            id="jargon-scope"
            value={scope}
            onChange={(event) => changeFilter(() => setScope(event.target.value))}
          >
            <option value="all">全部词条</option>
            <option value="global">仅全局通用</option>
            {streams.map((stream) => (
              <option key={stream.id} value={stream.id}>
                仅 {streamLabel(stream)}
              </option>
            ))}
          </Select>
        </Field>
        <form className="flex items-end gap-2" onSubmit={submitKeyword}>
          <Field label="关键词" htmlFor="jargon-keyword" className="w-56">
            <Input
              id="jargon-keyword"
              value={keywordInput}
              placeholder="匹配词条或释义"
              onChange={(event) => setKeywordInput(event.target.value)}
            />
          </Field>
          <Button type="submit" variant="secondary" disabled={loading}>
            <Search className="size-4" aria-hidden="true" />
            搜索
          </Button>
          {keyword ? (
            <Button
              type="button"
              variant="secondary"
              onClick={() => {
                setKeywordInput('')
                changeFilter(() => setKeyword(''))
              }}
            >
              <X className="size-4" aria-hidden="true" />
              清除
            </Button>
          ) : null}
        </form>
      </div>

      {streamsError ? <ErrorText>{streamsError}</ErrorText> : null}
      {error ? <ErrorText>{error}</ErrorText> : null}
      {loading ? <Loading>正在读取黑话词表…</Loading> : null}
      {!loading && !error && entries.length === 0 ? (
        <Empty>
          {status === 'pending'
            ? '没有待定词条——历史迁移只导入了已确认词条。'
            : '没有符合条件的词条。'}
        </Empty>
      ) : null}
      {entries.length > 0 ? (
        <Card>
          <CardBody className="p-0">
            <ol>
              {entries.map((entry) => (
                <JargonRow key={entry.id} entry={entry} streamLabelOf={streamLabelOf} />
              ))}
            </ol>
          </CardBody>
        </Card>
      ) : null}

      <Pager page={page} pageCount={pageCount} total={total} onChange={setPage} disabled={loading} />
    </div>
  )
}
