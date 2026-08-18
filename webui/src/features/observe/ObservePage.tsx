/**
 * 会话观察页：按会话流聚合展示后端只读运行快照与实时通道。
 *
 * 页面自上而下为页头工具区（会话流选择、手动刷新、自动刷新开关、读取时刻）、
 * 关键状态条、阶段看板、七个快照分区、事件账本、提示词工作台与实时日志。
 * 数据全部来自 hooks/use-observability、use-traces、use-prompts、use-logs；
 * 本组件只负责会话流选中态、自动刷新开关与各分区的编排，不直接发起请求。
 */
import { RefreshCw } from 'lucide-react'
import { useEffect, useState } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import { Button, ErrorText, Field, Select, Toggle } from '@/components/ui'
import { useSnapshot, useStages, useStreams } from '@/hooks/use-observability'
import { useTraces } from '@/hooks/use-traces'
import { streamLabel } from '@/lib/format'
import { LogPanel } from './LogPanel'
import { PromptWorkbench } from './PromptWorkbench'
import { SnapshotSections } from './SnapshotSections'
import { StageBoard } from './StageBoard'
import { StatusStrip } from './StatusStrip'
import { TracePanel } from './TracePanel'

/**
 * 渲染会话观察页。
 *
 * @returns 页面容器元素；内容区自行纵向滚动，锚点分区由各面板自带 id 提供。
 * @remarks 会话流列表异步到达，首个流到达后自动选中；用户手动改选后不再被
 * 列表刷新覆盖（选中值已存在于列表中即保持不变）。
 */
export function ObservePage() {
  const { streams, error: streamsError } = useStreams()
  const [streamId, setStreamId] = useState('')
  const [autoRefresh, setAutoRefresh] = useState(false)
  const stages = useStages()
  const { payload, fetchedLabel, refreshing, error: snapshotError, refresh } = useSnapshot(
    streamId,
    autoRefresh,
  )
  const traces = useTraces(true)

  useEffect(() => {
    const first = streams[0]
    if (!first) return
    setStreamId((current) =>
      current && streams.some((stream) => String(stream.id) === current) ? current : String(first.id),
    )
  }, [streams])

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-col gap-5 px-4 py-6 sm:px-6 lg:px-8">
      <PageHeader
        eyebrow="YUELI / WEBUI"
        title="机器人观察面板"
        subtitle="按会话流读取的只读运行快照"
        actions={
          <>
            <Field label="会话流" htmlFor="stream-select" className="w-52">
              <Select
                id="stream-select"
                value={streamId}
                onChange={(event) => setStreamId(event.target.value)}
              >
                {streams.map((stream) => (
                  <option key={stream.id} value={stream.id}>
                    {streamLabel(stream)}
                  </option>
                ))}
              </Select>
            </Field>
            <Button variant="secondary" onClick={refresh} disabled={refreshing || !streamId}>
              <RefreshCw className="size-4" aria-hidden="true" />
              刷新快照
            </Button>
            <Toggle checked={autoRefresh} onChange={setAutoRefresh} label="自动刷新" />
            <span className="rounded-full border border-border bg-card px-3 py-1 font-mono text-xs text-muted-foreground tabular-nums">
              {fetchedLabel || '尚未读取'}
            </span>
          </>
        }
      />

      {streamsError ? <ErrorText>{streamsError}</ErrorText> : null}
      {snapshotError ? <ErrorText>{snapshotError}</ErrorText> : null}

      {payload ? <StatusStrip payload={payload} /> : null}

      <StageBoard stages={stages} />

      {payload ? <SnapshotSections payload={payload} /> : null}

      <TracePanel
        traces={traces.traces}
        skippedCount={traces.skippedCount}
        historyCursor={traces.historyCursor}
        search={traces.search}
        streamId={streamId}
      />

      <PromptWorkbench enabled />

      <LogPanel enabled />
    </div>
  )
}
