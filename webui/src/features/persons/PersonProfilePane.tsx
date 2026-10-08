/**
 * 人物画像面板：人物与关系页右侧的详情面板。
 *
 * 自上而下为人物抬头（头像、名字、认识与更新时间）、一条指标带（好感度、事实
 * 条数、所在的群、出现过的会话），以及「事实记忆」「身份与会话」两个分区，分别
 * 对应后端画像的 bond、facts 与 identities/streams 字段。数据来自
 * hooks/use-persons 的 usePersonProfile；被 PersonsPage 引用。
 */
import { UserRound } from 'lucide-react'
import type { ReactNode } from 'react'

import { PaneEmpty, PaneHeader, PaneTitle, ScrollArea } from '@/components/layout/panes'
import { Card, CardBody, Chip, Empty, ErrorText, FlatCards, Loading, Progress, SectionHeading, cn } from '@/components/ui'
import { usePersonProfile } from '@/hooks/use-persons'
import { dateTime, fixed, streamLabel } from '@/lib/format'
import type { ObservabilityFact, PersonProfile } from '../../../../electron/shared/ipc.ts'
import { IdentityChips } from './IdentityChips'
import { PersonAvatar } from './PersonAvatar'

/** 事实记忆表格的列标题。 */
const FACT_COLUMNS = ['内容', '类型', '保留度', '复习到期', '状态'] as const

/**
 * 渲染事实记忆表格。
 *
 * @param props.facts 该人物的全部长期事实。
 * @returns 可横向滚动的表格容器。
 * @remarks 冻结（渐淡）的事实只通过整行降低对比度标识，不改写事实原文。
 */
function FactTable({ facts }: { facts: ObservabilityFact[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[36rem] border-collapse text-left text-[13px]">
        <thead>
          <tr className="border-b border-border">
            {FACT_COLUMNS.map((column, index) => (
              <th
                key={column}
                className={cn('py-2 text-xs font-medium text-subtle-foreground', index === 0 ? 'pr-3' : 'px-3')}
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {facts.map((fact) => (
            <tr
              key={fact.id}
              className={cn('border-b border-border-subtle last:border-0', fact.frozen && 'text-subtle-foreground')}
            >
              <td className="py-2.5 pr-3">{fact.content}</td>
              <td className="px-3 py-2.5 font-mono text-xs">{fact.kind}</td>
              <td className="px-3 py-2.5 font-mono text-xs tabular-nums">{fact.retention.toFixed(2)}</td>
              <td className="px-3 py-2.5 font-mono text-xs whitespace-nowrap tabular-nums">{dateTime(fact.dueAt)}</td>
              <td className={cn('px-3 py-2.5 text-xs', fact.frozen ? null : 'text-success')}>
                {fact.frozen ? '渐淡' : '清晰'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * 渲染指标带中的一格。
 *
 * @param props.label 指标名。
 * @param props.value 指标值。
 * @param props.unit 值后的弱色单位，可选。
 * @param props.children 值下方的附加内容（如进度条），可选。
 * @returns 指标格；相邻格之间以竖向分隔线隔开。
 */
function MetricCell({ label, value, unit, children }: { label: string; value: ReactNode; unit?: string; children?: ReactNode }) {
  return (
    <div className="border-border px-6 py-4 first:pl-0 sm:border-l sm:first:border-l-0">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-[22px] leading-7 font-semibold tabular-nums">
        {value}
        {unit ? <span className="ml-1 text-[13px] font-normal text-subtle-foreground">{unit}</span> : null}
      </p>
      {children}
    </div>
  )
}

/**
 * 渲染画像正文：抬头、指标带与两个分区。
 *
 * @param props.profile 人物完整画像。
 * @returns 画像正文。
 */
function ProfileBody({ profile }: { profile: PersonProfile }) {
  const groups = [...new Map(profile.groupMemberships.map((m) => [m.streamId, m])).values()]
  // 详情接口不带列表里的 factCount，条数从 facts 自己数；渐淡与表格「状态」列同一口径。
  const fadedCount = profile.facts.filter((fact) => fact.frozen).length
  return (
    <>
      <div className="flex items-center gap-4 px-6 pt-6 pb-5">
        <PersonAvatar name={profile.displayName} className="size-14 text-xl" selected />
        <div className="min-w-0">
          <p className="truncate text-lg font-semibold">{profile.displayName}</p>
          <p className="mt-0.5 text-xs text-muted-foreground">
            {profile.kind === 'owner' ? '用户本人的独立画像' : `联系人 #${profile.id}`}
            {' · '}认识于 {dateTime(profile.firstSeenAt)}
            {' · '}画像更新于 {dateTime(profile.bond.updatedAt)}
          </p>
        </div>
      </div>

      <div className="mx-6 grid grid-cols-2 border-y border-border sm:grid-cols-4">
        <MetricCell label="好感度" value={fixed(profile.bond.intimacy, 1)}>
          <Progress value={profile.bond.intimacy} max={100} label="好感度" className="mt-2 [&>div]:bg-tint-coral" />
        </MetricCell>
        <MetricCell label="事实记忆" value={profile.facts.length} unit="条" />
        <MetricCell label="所在的群" value={groups.length} unit="个" />
        <MetricCell label="出现过的会话" value={profile.streams.length} unit="条" />
      </div>

      <FlatCards>
        <Card>
          <SectionHeading
            title="事实记忆"
            subtitle={
              fadedCount > 0
                ? `${profile.facts.length} 条 · 其中 ${fadedCount} 条渐淡`
                : `${profile.facts.length} 条`
            }
          />
          <CardBody>
            {profile.facts.length ? (
              <FactTable facts={profile.facts} />
            ) : (
              <Empty>当前没有关于这个人的事实记忆。</Empty>
            )}
          </CardBody>
        </Card>
        {/* 身份、群成员关系与会话流分组展示，便于区分「是谁」「在哪个群」「出现在哪个出口」。 */}
        <Card>
          <SectionHeading title="身份与会话" />
          <CardBody className="flex flex-col gap-3">
            <IdentityChips identities={profile.identities} groupMemberships={profile.groupMemberships} />
            <div className="flex flex-wrap gap-1.5">
              {profile.streams.map((stream) => (
                <Chip key={stream.id} label={stream.kind} value={streamLabel(stream)} />
              ))}
              {profile.streams.length === 0 ? <Chip label="会话" value="尚未在会话中发言" /> : null}
            </div>
          </CardBody>
        </Card>
      </FlatCards>
    </>
  )
}

/**
 * 渲染人物画像面板的标题栏与正文。
 *
 * @param props.personId 选中的人物 ID；`null` 表示尚未选中，显示引导空态。
 * @returns 面板内容（标题栏 + 滚动区）。
 */
export function PersonProfilePane({ personId }: { personId: number | null }) {
  const { profile, loading, missing, error } = usePersonProfile(personId)

  if (personId === null) {
    return (
      <PaneEmpty icon={<UserRound />}>从左边选一个人，查看 TA 的关系、身份与事实记忆。</PaneEmpty>
    )
  }

  let body: ReactNode = null
  if (loading) {
    body = <Loading>正在读取人物画像…</Loading>
  } else if (missing) {
    body = <div className="px-6 pt-4"><ErrorText>找不到这个人物画像。</ErrorText></div>
  } else if (error) {
    body = <div className="px-6 pt-4"><ErrorText>{error}</ErrorText></div>
  } else if (profile) {
    body = <ProfileBody profile={profile} />
  }

  return (
    <>
      <PaneHeader>
        <PaneTitle>{profile ? profile.displayName : '人物画像'}</PaneTitle>
        {profile?.kind === 'owner' ? (
          <span className="rounded bg-muted px-1.5 py-0.5 text-[11px] text-muted-foreground">本人</span>
        ) : null}
      </PaneHeader>
      <ScrollArea>{body}</ScrollArea>
    </>
  )
}
