/**
 * 阶段看板：展示各会话当前停在哪一步、停了多久。
 *
 * 每行只展示当前阶段、详情和停留时间，避免把后端内部状态对象直接暴露到页面；
 * 数据由 useStages 秒级轮询驱动。被会话观察页引用。
 */
import { Activity } from 'lucide-react'

import { Card, CardBody, Empty, SectionHeading, cn } from '@/components/ui'
import { elapsedLabel } from '@/lib/format'
import type { StageEntry } from '@/hooks/use-observability'

/**
 * 渲染阶段看板分区。
 *
 * @param props.stages 秒级轮询得到的阶段记录数组。
 * @returns 阶段看板卡片；无记录时展示空态。
 */
export function StageBoard({ stages }: { stages: StageEntry[] }) {
  return (
    <Card id="overview" aria-label="各会话当前阶段" className="animate-rise scroll-mt-6">
      <SectionHeading
        title="此刻在做什么"
        subtitle="每条会话停在哪一步、停了多久"
        icon={<Activity />}
        tint="coral"
        actions={
          stages.length ? (
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span className="live-dot" aria-hidden="true" />
              实时
            </span>
          ) : undefined
        }
      />
      <CardBody className="flex flex-col gap-1.5">
        {!stages.length ? <Empty>尚未收到任何消息。</Empty> : null}
        {stages.map((entry) => (
          <div
            key={entry.streamId}
            className={cn(
              'flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-transparent bg-muted/60 px-3.5 py-2.5',
              entry.stage === 'failed' && 'border-destructive/40 bg-destructive-soft',
            )}
          >
            <strong className="text-[13px] font-semibold">{entry.streamName}</strong>
            <span className="inline-flex items-center rounded-full bg-primary-soft px-2 py-0.5 text-xs font-medium text-primary-strong">
              {entry.stageLabel}
            </span>
            <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{entry.detail}</span>
            <span className="font-mono text-xs text-muted-foreground tabular-nums">
              {entry.stageStartedAtTruncated ? '至少 ' : ''}
              {elapsedLabel(entry.stageElapsedMs)}
            </span>
            {entry.turnId !== null ? (
              <span className="font-mono text-xs text-muted-foreground tabular-nums">第 {entry.turnId} 轮</span>
            ) : null}
          </div>
        ))}
      </CardBody>
    </Card>
  )
}
