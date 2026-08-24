/**
 * 人物画像列表页：一人一行展示机器人已认识的全部人物。
 *
 * 每行给出显示名、平台身份、好感度、生效事实条数与最后互动时间，点击行进
 * 入完整画像页。列表可按好感度或最后互动时间排序，排序在前端完成（全部行
 * 已在内存中）。数据来自 hooks/use-persons 的 usePersons，本组件不直接发起
 * 请求。
 */
import { ArrowDownWideNarrow, ArrowUpNarrowWide, ChevronRight } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router'

import { PageHeader } from '@/components/layout/PageHeader'
import { Button, Card, CardBody, Empty, ErrorText, Loading, cn } from '@/components/ui'
import { usePersons } from '@/hooks/use-persons'
import { dateTime, fixed } from '@/lib/format'
import type { PersonSummary } from '../../../../electron/shared/ipc.ts'

/** 可排序列的键。 */
type SortKey = 'intimacy' | 'bondUpdatedAt'

/** 可排序列表头的入参。 */
interface SortButtonProps {
  column: SortKey
  label: string
  sortKey: SortKey
  sortAsc: boolean
  onToggle: (key: SortKey) => void
}

/**
 * 渲染可排序列的表头按钮；当前排序列显示升降序图标。
 *
 * @param props.column 本按钮对应的排序键。
 * @param props.label 列显示名。
 * @param props.sortKey 当前生效的排序键。
 * @param props.sortAsc 当前是否升序。
 * @param props.onToggle 点击回调。
 * @returns 表头按钮元素。
 */
function SortButton({ column, label, sortKey, sortAsc, onToggle }: SortButtonProps) {
  const active = sortKey === column
  return (
    <button
      type="button"
      onClick={() => onToggle(column)}
      aria-pressed={active}
      className={cn(
        'flex cursor-pointer items-center gap-1 justify-self-start select-none',
        active ? 'text-foreground' : 'hover:text-foreground',
      )}
    >
      {label}
      {active ? (
        sortAsc ? (
          <ArrowUpNarrowWide className="size-3.5" aria-hidden="true" />
        ) : (
          <ArrowDownWideNarrow className="size-3.5" aria-hidden="true" />
        )
      ) : null}
    </button>
  )
}

/** 行网格的列模板；表头与数据行共用，保证列对齐。 */
const ROW_GRID =
  'grid grid-cols-[minmax(7rem,1.2fr)_minmax(9rem,1.6fr)_5.5rem_4.5rem_9.5rem_1.25rem] items-center gap-3'

/**
 * 渲染单个人物行。
 *
 * @param props.person 人物摘要数据。
 * @returns 链接到完整画像页的一行。
 * @remarks owner 行的 bondUpdatedAt 会被每小时结算推进，不代表互动，
 * 「最后互动」列对 owner 不渲染。
 */
function PersonRow({ person }: { person: PersonSummary }) {
  const isOwner = person.kind === 'owner'
  const identitiesLabel = person.identities.length
    ? person.identities.map((identity) => `${identity.platform}:${identity.externalId}`).join('、')
    : '无'
  return (
    <Link
      to={`/persons/${person.id}`}
      className={cn(ROW_GRID, 'px-4 py-2.5 text-[13px] transition-colors hover:bg-muted/60')}
    >
      <span className="truncate font-medium">
        {person.displayName}
        {isOwner ? <span className="ml-1.5 text-xs font-normal text-muted-foreground">本人</span> : null}
      </span>
      <span className="truncate font-mono text-xs text-muted-foreground" title={identitiesLabel}>
        {identitiesLabel}
      </span>
      <span className="tabular-nums">{fixed(person.intimacy, 1)}</span>
      <span
        className="tabular-nums"
        title={person.factCount.total !== person.factCount.active ? `含已冻结共 ${person.factCount.total} 条` : undefined}
      >
        {person.factCount.active}
      </span>
      <span className="text-xs text-muted-foreground tabular-nums">
        {isOwner ? '—' : dateTime(person.bondUpdatedAt)}
      </span>
      <ChevronRight className="size-4 text-muted-foreground" aria-hidden="true" />
    </Link>
  )
}

/**
 * 渲染人物画像列表页。
 *
 * @returns 页面容器元素；一人一行的列表，默认可点击进详情页。
 */
export function PersonsPage() {
  const { persons, loading, error } = usePersons()
  const [sortKey, setSortKey] = useState<SortKey>('intimacy')
  const [sortAsc, setSortAsc] = useState(false)

  const sorted = useMemo(() => {
    const rows = [...persons]
    rows.sort((left, right) => {
      const delta = sortKey === 'intimacy' ? left.intimacy - right.intimacy : left.bondUpdatedAt - right.bondUpdatedAt
      return sortAsc ? delta : -delta
    })
    return rows
  }, [persons, sortKey, sortAsc])

  /** 切换排序列；重复点击同一列时翻转升降序。 */
  const toggleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortAsc((current) => !current)
    } else {
      setSortKey(key)
      setSortAsc(false)
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI · CONSOLE"
        title="人物画像"
        subtitle="每个人的身份、关系与事实记忆彼此独立。"
        actions={
          <Link to="/">
            <Button variant="secondary">返回会话观察</Button>
          </Link>
        }
      />
      {error ? <ErrorText>{error}</ErrorText> : null}
      {loading ? <Loading>正在读取人物画像…</Loading> : null}
      {!loading && !error && persons.length === 0 ? <Empty>还没有认识任何人。</Empty> : null}
      {sorted.length ? (
        <Card className="animate-rise overflow-x-auto">
          <CardBody className="min-w-[640px] p-0">
            <div className={cn(ROW_GRID, 'border-b border-border px-4 py-2 text-xs font-medium text-muted-foreground')}>
              <span>显示名</span>
              <span>平台身份</span>
              <SortButton column="intimacy" label="好感度" sortKey={sortKey} sortAsc={sortAsc} onToggle={toggleSort} />
              <span>事实</span>
              <SortButton column="bondUpdatedAt" label="最后互动" sortKey={sortKey} sortAsc={sortAsc} onToggle={toggleSort} />
              <span />
            </div>
            <div className="flex flex-col divide-y divide-border/60">
              {sorted.map((person) => (
                <PersonRow key={person.id} person={person} />
              ))}
            </div>
          </CardBody>
        </Card>
      ) : null}
    </div>
  )
}
