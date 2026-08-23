/**
 * 人物画像列表页：展示机器人已认识的全部人物摘要。
 *
 * 每个人物一张卡片，包含认识时间、身份数量、出现会话数量三项指标，身份与
 * 群名片标签组，以及进入完整画像的入口。数据来自 hooks/use-persons 的
 * usePersons，本组件不直接发起请求。
 */
import { UserRound } from 'lucide-react'
import { Link } from 'react-router'

import { PageHeader } from '@/components/layout/PageHeader'
import { Button, Card, CardBody, Empty, ErrorText, Loading, Metric, SectionHeading } from '@/components/ui'
import { usePersons } from '@/hooks/use-persons'
import { dateTime } from '@/lib/format'
import type { PersonSummary } from '../../../../electron/shared/ipc.ts'
import { IdentityChips } from './IdentityChips'

/**
 * 渲染单个人物摘要卡片。
 *
 * @param props.person 人物摘要数据。
 * @returns 人物卡片元素。
 */
function PersonCard({ person }: { person: PersonSummary }) {
  return (
    <Card className="animate-rise flex flex-col">
      <SectionHeading
        title={person.displayName}
        subtitle={person.kind === 'owner' ? '本人' : `联系人 #${person.id}`}
        icon={<UserRound />}
        tint={person.kind === 'owner' ? 'plum' : 'coral'}
      />
      <CardBody className="flex flex-1 flex-col gap-3">
        <div className="flex flex-col divide-y divide-border/60">
          <Metric label="认识时间" value={dateTime(person.firstSeenAt)} />
          <Metric label="平台身份" value={`${person.identities.length} 个`} />
          <Metric label="出现会话" value={`${person.streams.length} 个`} />
        </div>
        <IdentityChips identities={person.identities} groupMemberships={person.groupMemberships} />
        <Link to={`/persons/${person.id}`} className="mt-auto pt-1">
          <Button variant="secondary" size="sm" className="w-full">
            查看完整画像
          </Button>
        </Link>
      </CardBody>
    </Card>
  )
}

/**
 * 渲染人物画像列表页。
 *
 * @returns 页面容器元素；卡片按屏幕宽度铺 1 至 3 列。
 */
export function PersonsPage() {
  const { persons, loading, error } = usePersons()

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
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {persons.map((person) => (
          <PersonCard key={person.id} person={person} />
        ))}
      </div>
    </div>
  )
}
