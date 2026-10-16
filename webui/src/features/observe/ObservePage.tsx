/**
 * 会话观察页：收件箱式的三栏工作区。
 *
 * - 左栏「会话流」：全部可观察的会话，合并阶段看板的实时状态（停在哪一步、
 *   停了多久），点选即切换右侧快照所读的会话；
 * - 中栏：页签切换事件账本、分阶段调用记录、提示词工作台与实时日志；
 * - 右栏「她现在」：关键状态摘要与七个快照分区（精力心情、活动、方向、预算、
 *   感知、会话、语音）。
 * 数据全部来自 hooks/use-observability、use-traces 与各面板自带的 hook；本组件
 * 只负责会话流选中态、页签、自动刷新开关与三栏编排，不直接发起请求。
 */
import { MessagesSquare, RefreshCw } from 'lucide-react'
import { memo, useEffect, useMemo, useState } from 'react'

import { PageFrame } from '@/components/layout/PageFrame'
import { Pane, PaneEmpty, PaneHeader, PaneTitle, Panes, ScrollArea } from '@/components/layout/panes'
import { Button, ErrorText, FlatCards, Loading, SegmentedTabs, Toggle, UnderlineTabs, cn } from '@/components/ui'
import { useSnapshot, useStages, useStreams, type StageEntry } from '@/hooks/use-observability'
import { useTraces } from '@/hooks/use-traces'
import { elapsedLabel, streamLabel } from '@/lib/format'
import type { ObservabilityStream } from '../../../../electron/shared/ipc.ts'
import { LogPanel } from './LogPanel'
import { PromptRecordPanel } from './PromptRecordPanel'
import { PromptWorkbench } from './PromptWorkbench'
import { SnapshotSections } from './SnapshotSections'
import { StatusStrip } from './StatusStrip'
import { TracePanel } from './TracePanel'

/*
 * 重面板 memo 化：阶段看板每秒轮询会带着 ObservePage 一起重渲染，这些面板的
 * props 全部是稳定引用（state 数组、useCallback 方法、原始值），引用不变时
 * 整体跳过 reconciliation，每秒的开销只剩会话流列表自身。
 */
const MemoStatusStrip = memo(StatusStrip)
const MemoSnapshotSections = memo(SnapshotSections)
const MemoTracePanel = memo(TracePanel)
const MemoPromptRecordPanel = memo(PromptRecordPanel)
const MemoPromptWorkbench = memo(PromptWorkbench)
const MemoLogPanel = memo(LogPanel)

/** 中栏页签。 */
type ObserveTab = 'events' | 'records' | 'prompts' | 'logs'

const TABS: Array<{ value: ObserveTab; label: string }> = [
  { value: 'events', label: '事件账本' },
  { value: 'records', label: '调用记录' },
  { value: 'prompts', label: '提示词工作台' },
  { value: 'logs', label: '实时日志' },
]

/**
 * 计算会话流在列表里的标题与副标题。
 *
 * @param stream 后端返回的会话流。
 * @returns 标题取群名或对方昵称（未知时回落平台号）；副标题为平台、类型、平台号与
 *   内部 ID，标题已是平台号时不再重复。
 */
function describeStream(stream: ObservabilityStream): { title: string; sub: string } {
  if (stream.kind === 'desktop') return { title: '桌面', sub: `#${stream.id}` }
  const kind = stream.kind === 'direct' ? '私聊' : '群聊'
  const title = stream.displayName.trim() || stream.externalId
  const externalId = title === stream.externalId ? '' : ` · ${stream.externalId}`
  return {
    title,
    sub: `${stream.platform.toUpperCase()} ${kind}${externalId} · #${stream.id}`,
  }
}

interface StreamRowProps {
  stream: ObservabilityStream
  /** 阶段看板里这条会话的实时状态；没有记录表示当前空闲。 */
  stage: StageEntry | undefined
  selected: boolean
  onSelect: () => void
}

/**
 * 渲染会话流列表的一行。
 *
 * @param props.stream 会话流。
 * @param props.stage 实时阶段；有阶段时第二行显示阶段与详情，否则显示会话类型。
 * @param props.selected 是否为当前选中会话。
 * @param props.onSelect 点选回调。
 * @returns 可点选的一行；状态点：进行中为绿色并闪烁、失败为红色、空闲为灰色。
 */
function StreamRow({ stream, stage, selected, onSelect }: StreamRowProps) {
  const { title, sub } = describeStream(stream)
  const tone = !stage ? 'idle' : stage.stage === 'failed' ? 'failed' : 'active'
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      title={streamLabel(stream)}
      className={cn(
        'flex w-full cursor-pointer gap-2.5 border-b border-border-subtle px-6 py-2.5 text-left transition-colors',
        selected ? 'bg-primary-soft' : 'hover:bg-muted/70',
      )}
    >
      <span
        className={cn(
          'mt-1.5 size-2 flex-none rounded-full',
          tone === 'active' ? 'animate-pulse-dot bg-success' : tone === 'failed' ? 'bg-destructive' : 'bg-input',
        )}
        aria-hidden="true"
      />
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline justify-between gap-2">
          <strong className={cn('truncate text-[13px] font-medium', selected && 'text-primary-strong')}>{title}</strong>
          {stage ? (
            <span className="flex-none font-mono text-[11px] text-subtle-foreground tabular-nums">
              {stage.stageStartedAtTruncated ? '≥' : ''}
              {elapsedLabel(stage.stageElapsedMs)}
            </span>
          ) : null}
        </span>
        <span
          className={cn(
            'block truncate text-xs',
            tone === 'failed' ? 'text-destructive' : tone === 'active' ? 'text-success' : 'text-subtle-foreground',
          )}
        >
          {stage ? [stage.stageLabel, stage.detail].filter(Boolean).join(' · ') : sub}
        </span>
      </span>
    </button>
  )
}

/**
 * 渲染会话观察页。
 *
 * @returns 页面外框与三栏工作区。
 * @remarks 会话流列表异步到达，首个流到达后自动选中；用户手动改选后不再被
 * 列表刷新覆盖（选中值已存在于列表中即保持不变）。四个中栏面板始终挂载、只切换
 * 显隐：实时日志与提示词的数据通道在页面挂载期间持续工作，切页签不会断流重连。
 */
export function ObservePage() {
  const { streams, error: streamsError } = useStreams()
  const [streamId, setStreamId] = useState('')
  const [autoRefresh, setAutoRefresh] = useState(false)
  const [tab, setTab] = useState<ObserveTab>('events')
  const [onlyActive, setOnlyActive] = useState(false)
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

  const stageByStream = useMemo(() => new Map(stages.map((entry) => [entry.streamId, entry])), [stages])
  const listed = onlyActive ? streams.filter((stream) => stageByStream.has(stream.id)) : streams

  return (
    <PageFrame
      hint="按会话流读取的只读运行快照"
      actions={
        <>
          <span className="font-mono text-xs text-subtle-foreground tabular-nums">{fetchedLabel || '尚未读取'}</span>
          <Toggle checked={autoRefresh} onChange={setAutoRefresh} label="自动刷新" />
          <Button variant="secondary" onClick={refresh} disabled={refreshing || !streamId}>
            <RefreshCw className="size-4" aria-hidden="true" />
            刷新快照
          </Button>
        </>
      }
    >
      <Panes>
        <Pane width="list" aria-label="会话流">
          <PaneHeader
            actions={
              <SegmentedTabs
                label="筛选会话流"
                tabs={[
                  { value: 'all', label: '全部' },
                  { value: 'active', label: '进行中' },
                ]}
                value={onlyActive ? 'active' : 'all'}
                onChange={(next) => setOnlyActive(next === 'active')}
              />
            }
          >
            <PaneTitle count={streams.length}>会话流</PaneTitle>
          </PaneHeader>
          <ScrollArea>
            {streamsError ? <div className="px-6 pt-3"><ErrorText>{streamsError}</ErrorText></div> : null}
            {listed.map((stream) => (
              <StreamRow
                key={stream.id}
                stream={stream}
                stage={stageByStream.get(stream.id)}
                selected={String(stream.id) === streamId}
                onSelect={() => setStreamId(String(stream.id))}
              />
            ))}
            {onlyActive && streams.length > 0 && listed.length === 0 ? (
              <PaneEmpty icon={<MessagesSquare />}>此刻没有正在进行的会话。</PaneEmpty>
            ) : null}
          </ScrollArea>
        </Pane>

        <Pane aria-label="会话详情">
          <PaneHeader>
            <UnderlineTabs label="会话详情" tabs={TABS} value={tab} onChange={setTab} />
          </PaneHeader>
          <ScrollArea>
            <FlatCards hideTitles>
              <div hidden={tab !== 'events'}>
                <MemoTracePanel
                  traces={traces.traces}
                  skippedCount={traces.skippedCount}
                  historyCursor={traces.historyCursor}
                  search={traces.search}
                  streamId={streamId}
                />
              </div>
              <div hidden={tab !== 'records'}>
                <MemoPromptRecordPanel enabled />
              </div>
              <div hidden={tab !== 'prompts'}>
                <MemoPromptWorkbench enabled />
              </div>
              <div hidden={tab !== 'logs'}>
                <MemoLogPanel enabled />
              </div>
            </FlatCards>
          </ScrollArea>
        </Pane>

        <Pane width="inspector" aria-label="她现在">
          <PaneHeader>
            <PaneTitle>她现在</PaneTitle>
          </PaneHeader>
          <ScrollArea>
            {snapshotError ? <div className="px-6 pt-3"><ErrorText>{snapshotError}</ErrorText></div> : null}
            {payload ? (
              <>
                <div className="border-b border-border px-6 py-4">
                  <MemoStatusStrip payload={payload} />
                </div>
                <FlatCards>
                  <MemoSnapshotSections payload={payload} />
                </FlatCards>
              </>
            ) : snapshotError ? null : (
              <Loading>正在读取快照…</Loading>
            )}
          </ScrollArea>
        </Pane>
      </Panes>
    </PageFrame>
  )
}
