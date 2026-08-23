/**
 * 人物画像详情页：单个人物的关系状态、身份会话与事实记忆。
 *
 * 三个分区分别对应后端画像的 bond、identities/streams 与 facts 字段：关系状态
 * 展示好感度数值与进度条，身份与会话展示认识时间、画像更新时间及标签组，事实
 * 记忆以表格列出全部 L3 长期事实。数据来自 hooks/use-persons 的 usePersonProfile。
 */
import { BookMarked, Heart, IdCard } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link, useParams } from 'react-router'

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
  Progress,
  SectionHeading,
  cn,
} from '@/components/ui'
import { usePersonProfile } from '@/hooks/use-persons'
import { dateTime, fixed, streamLabel } from '@/lib/format'
import type { ObservabilityFact, PersonProfile } from '../../../../electron/shared/ipc.ts'
import { IdentityChips } from './IdentityChips'

/** 事实记忆表格的列标题。 */
const FACT_COLUMNS = ['内容', '类型', '保留度', '复习到期', '状态'] as const

/**
 * 解析路由参数中的人物 ID。
 *
 * @param raw 路由参数原文；缺失时为 `undefined`。
 * @returns 正整数 ID；参数非法时返回 `null`，由页面转换为「找不到」状态。
 */
function parsePersonId(raw: string | undefined): number | null {
  if (raw === undefined || !/^\d+$/.test(raw)) return null
  const value = Number(raw)
  return value > 0 ? value : null
}

/**
 * 渲染事实记忆表格。
 *
 * @param props.facts 该人物的全部长期事实。
 * @returns 可横向滚动的表格容器。
 * @remarks 冻结（渐淡）的事实只通过整行降低对比度标识，不改写事实原文。
 */
function FactTable({ facts }: { facts: ObservabilityFact[] }) {
  return (
    <div className="-mx-1 overflow-x-auto px-1">
      <table className="w-full min-w-[36rem] border-collapse text-left text-[13px]">
        <thead>
          <tr className="border-b border-border">
            {FACT_COLUMNS.map((column) => (
              <th key={column} className="px-2 py-2 text-xs font-medium text-muted-foreground">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {facts.map((fact) => (
            <tr
              key={fact.id}
              className={cn('border-b border-border/60 last:border-0', fact.frozen && 'text-muted-foreground')}
            >
              <td className="px-2 py-2">{fact.content}</td>
              <td className="px-2 py-2 font-mono text-xs">{fact.kind}</td>
              <td className="px-2 py-2 font-mono text-xs tabular-nums">{fact.retention.toFixed(2)}</td>
              <td className="px-2 py-2 font-mono text-xs tabular-nums">{dateTime(fact.dueAt)}</td>
              <td className="px-2 py-2 text-xs">{fact.frozen ? '渐淡' : '清晰'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * 渲染画像的三个内容分区。
 *
 * @param props.profile 人物完整画像。
 * @returns 12 列网格排布的分区集合。
 */
function ProfileSections({ profile }: { profile: PersonProfile }) {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-12">
      <Card className="animate-rise md:col-span-12 xl:col-span-4">
        <SectionHeading title="关系状态" subtitle="persona_bond" icon={<Heart />} tint="plum" />
        <CardBody className="flex flex-col gap-2">
          <div className="flex flex-col divide-y divide-border/60">
            <Metric label="好感度" value={fixed(profile.bond.intimacy, 1)} />
          </div>
          <Progress value={profile.bond.intimacy} max={100} label="好感度" />
        </CardBody>
      </Card>

      {/* 身份、群成员关系与会话流分三组展示，便于区分「是谁」「在哪个群」「出现在哪个出口」。 */}
      <Card className="animate-rise md:col-span-12 xl:col-span-8">
        <SectionHeading
          title="身份与会话"
          subtitle="identities / streams"
          icon={<IdCard />}
          tint="coral"
        />
        <CardBody className="flex flex-col gap-3">
          <div className="flex flex-col divide-y divide-border/60">
            <Metric label="认识时间" value={dateTime(profile.firstSeenAt)} />
            <Metric label="画像更新时间" value={dateTime(profile.bond.updatedAt)} />
          </div>
          <IdentityChips identities={profile.identities} groupMemberships={profile.groupMemberships} />
          <div className="flex flex-wrap gap-1.5">
            {profile.streams.map((stream) => (
              <Chip key={stream.id} label={stream.kind} value={streamLabel(stream)} />
            ))}
            {profile.streams.length === 0 ? <Chip label="会话" value="尚未在会话中发言" /> : null}
          </div>
        </CardBody>
      </Card>

      <Card className="animate-rise md:col-span-12">
        <SectionHeading
          title="事实记忆"
          subtitle={`${profile.facts.length} 条`}
          icon={<BookMarked />}
          tint="olive"
        />
        <CardBody>
          {profile.facts.length ? (
            <FactTable facts={profile.facts} />
          ) : (
            <Empty>当前没有关于这个人的事实记忆。</Empty>
          )}
        </CardBody>
      </Card>
    </div>
  )
}

/**
 * 渲染人物画像详情页。
 *
 * @returns 页面容器元素；加载中、人物不存在与请求失败各有独立提示。
 */
export function PersonDetailPage() {
  const params = useParams()
  const personId = parsePersonId(params.personId)
  const { profile, loading, missing, error } = usePersonProfile(personId)

  let body: ReactNode = null
  if (loading) {
    body = <Loading>正在读取人物画像…</Loading>
  } else if (missing) {
    body = <ErrorText>找不到这个人物画像。</ErrorText>
  } else if (error) {
    body = <ErrorText>{error}</ErrorText>
  } else if (profile) {
    body = <ProfileSections profile={profile} />
  }

  const subtitle = profile
    ? profile.kind === 'owner'
      ? '用户本人的独立画像'
      : `联系人 #${profile.id} 的人物画像`
    : '每个人的身份、关系与事实记忆彼此独立。'

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI · CONSOLE"
        title={profile ? profile.displayName : '人物画像'}
        subtitle={subtitle}
        actions={
          <Link to="/persons">
            <Button variant="secondary">返回人物列表</Button>
          </Link>
        }
      />
      {body}
    </div>
  )
}
