/**
 * 人物与关系页：左侧人物列表，右侧选中人物的画像。
 *
 * 套用「列表加详情」模板。`/persons` 与 `/persons/:personId` 渲染同一页，选中项
 * 写在 URL 里，因此刷新与分享链接都能回到同一个人；切换选中项只替换右侧面板，
 * 列表的滚动位置与排序保持不动。列表可按好感度或最后互动时间排序、按名字或平台
 * 号搜索，排序、搜索与翻页都在前端完成（`/api/persons` 一次返回全部行）。窄屏下
 * 两栏只显示一栏：未选中时显示列表，选中后显示画像，由顶栏的位置路径返回列表。
 */
import { Search, Users } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router'

import { PageFrame } from '@/components/layout/PageFrame'
import { Pane, PaneEmpty, PaneFooter, PaneHeader, PaneTitle, Panes, ScrollArea } from '@/components/layout/panes'
import { ErrorText, Input, Loading, Pager, SegmentedTabs, cn } from '@/components/ui'
import { usePersons } from '@/hooks/use-persons'
import { dateTime, fixed } from '@/lib/format'
import type { PersonSummary } from '../../../../electron/shared/ipc.ts'
import { PersonAvatar } from './PersonAvatar'
import { PersonProfilePane } from './PersonProfilePane'

/** 可排序字段。 */
type SortKey = 'intimacy' | 'bondUpdatedAt'

/** 每页人数；列表行紧凑，一屏能放下二十来行。 */
const PAGE_SIZE = 50

/**
 * 解析路由参数中的人物 ID。
 *
 * @param raw 路由参数原文；缺失时为 `undefined`。
 * @returns 正整数 ID；参数缺失或非法时返回 `null`。
 */
function parsePersonId(raw: string | undefined): number | null {
  if (raw === undefined || !/^\d+$/.test(raw)) return null
  const value = Number(raw)
  return value > 0 ? value : null
}

/**
 * 判断人物是否命中搜索词。
 *
 * @param person 人物摘要。
 * @param keyword 已转小写的搜索词。
 * @returns 显示名、平台昵称或平台号任一包含搜索词时为 true。
 */
function matches(person: PersonSummary, keyword: string): boolean {
  if (!keyword) return true
  if (person.displayName.toLowerCase().includes(keyword)) return true
  return person.identities.some(
    (identity) =>
      identity.externalId.toLowerCase().includes(keyword) || identity.displayName.toLowerCase().includes(keyword),
  )
}

interface PersonRowProps {
  person: PersonSummary
  selected: boolean
  sortKey: SortKey
}

/**
 * 渲染人物列表的一行。
 *
 * @param props.person 人物摘要。
 * @param props.selected 是否为当前选中项。
 * @param props.sortKey 当前排序字段；右下角的弱色信息随之切换。
 * @returns 链接到该人物的一行。
 * @remarks owner 行的 bondUpdatedAt 会被每小时结算推进，不代表互动，
 * 「最后互动」对 owner 不渲染。
 */
function PersonRow({ person, selected, sortKey }: PersonRowProps) {
  const isOwner = person.kind === 'owner'
  const identity = person.identities[0]
  const secondary =
    sortKey === 'bondUpdatedAt'
      ? isOwner ? '—' : dateTime(person.bondUpdatedAt)
      : `${person.factCount.active} 条事实`
  return (
    <Link
      to={`/persons/${person.id}`}
      aria-current={selected ? 'true' : undefined}
      className={cn(
        'flex items-center gap-3 border-b border-border-subtle px-6 py-2.5 transition-colors',
        selected ? 'bg-primary-soft' : 'hover:bg-muted/70',
      )}
    >
      <PersonAvatar name={person.displayName} selected={selected} className="size-8 text-xs" />
      <span className="min-w-0 flex-1">
        <span className="flex items-center gap-1.5">
          <strong className={cn('truncate text-[13px] font-medium', selected && 'text-primary-strong')}>
            {person.displayName}
          </strong>
          {isOwner ? <span className="flex-none text-[11px] text-tint-coral">本人</span> : null}
        </span>
        <span className="block truncate font-mono text-[11px] text-subtle-foreground">
          {identity ? `${identity.platform}:${identity.externalId}` : '尚未绑定平台身份'}
        </span>
      </span>
      <span className="flex flex-none flex-col items-end">
        <span className={cn('font-mono text-xs tabular-nums', selected ? 'text-primary-strong' : 'text-foreground')}>
          {fixed(person.intimacy, 1)}
        </span>
        <span className="text-[11px] text-subtle-foreground">{secondary}</span>
      </span>
    </Link>
  )
}

/**
 * 渲染人物与关系页。
 *
 * @returns 页面外框；左列表右画像。
 */
export function PersonsPage() {
  const params = useParams()
  const selectedId = parsePersonId(params.personId)
  const { persons, loading, error } = usePersons()
  const [sortKey, setSortKey] = useState<SortKey>('intimacy')
  const [sortAsc, setSortAsc] = useState(false)
  const [keyword, setKeyword] = useState('')
  const [page, setPage] = useState(0)

  const sorted = useMemo(() => {
    const needle = keyword.trim().toLowerCase()
    const rows = persons.filter((person) => matches(person, needle))
    rows.sort((left, right) => {
      /* owner 的 bondUpdatedAt 被每小时结算推进、界面按「—」展示，按最后互动
       * 排序时不能让它凭一个不展示的值占位置，固定沉底（与升降序无关）。 */
      if (sortKey === 'bondUpdatedAt') {
        if (left.kind === 'owner' && right.kind !== 'owner') return 1
        if (right.kind === 'owner' && left.kind !== 'owner') return -1
      }
      const delta = sortKey === 'intimacy' ? left.intimacy - right.intimacy : left.bondUpdatedAt - right.bondUpdatedAt
      return sortAsc ? delta : -delta
    })
    return rows
  }, [persons, sortKey, sortAsc, keyword])

  /** 切换排序字段；点已选中的字段时翻转升降序，并回到第一页。 */
  const toggleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortAsc((ascending) => !ascending)
    } else {
      setSortKey(key)
      setSortAsc(false)
    }
    // 换了排序口径还停在原页码，看到的是一批与刚才无关的人，不如回到榜首。
    setPage(0)
  }

  const arrow = sortAsc ? ' ↑' : ' ↓'
  const pageCount = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE))
  const currentPage = Math.min(page, pageCount - 1)
  const visible = sorted.slice(currentPage * PAGE_SIZE, (currentPage + 1) * PAGE_SIZE)
  const selectedName = persons.find((person) => person.id === selectedId)?.displayName

  return (
    <PageFrame
      detail={selectedId !== null ? selectedName ?? `#${selectedId}` : undefined}
      hint="每个人的身份、关系与事实记忆彼此独立。"
    >
      <Panes>
        <Pane width="list" aria-label="人物列表" className={cn(selectedId !== null && 'hidden lg:flex')}>
          <PaneHeader
            actions={
              <SegmentedTabs
                label="排序"
                tabs={[
                  { value: 'intimacy', label: `好感度${sortKey === 'intimacy' ? arrow : ''}`, title: '再点一次翻转方向' },
                  { value: 'bondUpdatedAt', label: `最近${sortKey === 'bondUpdatedAt' ? arrow : ''}`, title: '按最后互动时间；再点一次翻转方向' },
                ]}
                value={sortKey}
                onChange={toggleSort}
              />
            }
          >
            <PaneTitle count={persons.length}>人物</PaneTitle>
          </PaneHeader>
          <div className="flex-none border-b border-border-subtle px-6 py-2.5">
            <div className="relative">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-subtle-foreground" aria-hidden="true" />
              <Input
                aria-label="搜索人物"
                placeholder="名字或平台号"
                className="pl-8"
                value={keyword}
                onChange={(event) => {
                  setKeyword(event.target.value)
                  setPage(0)
                }}
              />
            </div>
          </div>
          <ScrollArea>
            {error ? <div className="px-6 pt-3"><ErrorText>{error}</ErrorText></div> : null}
            {loading ? <Loading>正在读取人物…</Loading> : null}
            {!loading && !error && persons.length === 0 ? (
              <PaneEmpty icon={<Users />}>还没有认识任何人。</PaneEmpty>
            ) : null}
            {!loading && persons.length > 0 && sorted.length === 0 ? (
              <PaneEmpty>没有名字或平台号包含「{keyword.trim()}」的人。</PaneEmpty>
            ) : null}
            {visible.map((person) => (
              <PersonRow key={person.id} person={person} selected={person.id === selectedId} sortKey={sortKey} />
            ))}
          </ScrollArea>
          {sorted.length > PAGE_SIZE ? (
            <PaneFooter>
              <div className="flex-1">
                <Pager compact page={currentPage} pageCount={pageCount} total={sorted.length} onChange={setPage} />
              </div>
            </PaneFooter>
          ) : null}
        </Pane>
        <Pane aria-label="人物画像" className={cn(selectedId === null && 'hidden lg:flex')}>
          <PersonProfilePane personId={selectedId} />
        </Pane>
      </Panes>
    </PageFrame>
  )
}
