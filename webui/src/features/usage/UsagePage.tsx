/**
 * 用量统计页：模型调用花了多少 token 与钱、花在哪、各模型多快多稳。
 *
 * 顶部切换时间范围（本小时、今天、近 7 天、近 30 天、全部）；正文依次是总览、
 * 「服务商没返回用量」提示、近 48 小时与近 30 天的趋势柱状图、按来源 / 任务 / 模型 /
 * 会话的分组表。数字口径与报告文件 data/reports/usage.html、控制台汇总框相同，都由
 * 后端 summarize_usage 算出。柱状图是手写 SVG，不引图表库。
 */
import { Activity, BarChart3, Coins, RefreshCw } from 'lucide-react'
import { useState } from 'react'

import { PageFrame } from '@/components/layout/PageFrame'
import { ScrollArea, Toolbar } from '@/components/layout/panes'
import {
  Button,
  Card,
  CardBody,
  Empty,
  ErrorText,
  Loading,
  SectionHeading,
  SegmentedTabs,
  UnderlineTabs,
  cn,
} from '@/components/ui'
import { useUsage } from '@/hooks/use-usage'
import { dateTime } from '@/lib/format'
import {
  RANGE_OPTIONS,
  formatCost,
  formatDuration,
  formatTokens,
  groupRows,
  type GroupKind,
  type TrendPoint,
  type UsageSummary,
} from './usage-format'

/** 分组页签，顺序即展示顺序。 */
const GROUP_TABS: Array<{ value: GroupKind; label: string }> = [
  { value: 'source', label: '按来源' },
  { value: 'task', label: '按任务' },
  { value: 'model', label: '按模型' },
  { value: 'stream', label: '按会话' },
]

/** 分组表第一列的列名。 */
const GROUP_FIRST_COLUMN: Record<GroupKind, string> = {
  source: '来源',
  task: '任务',
  model: '模型',
  stream: '会话',
}

/** 趋势图的度量。 */
type TrendMetric = 'tokens' | 'cost'

/**
 * 时区偏移的展示文本。
 *
 * @param minutes 东区为正的分钟数。
 * @returns 如 `UTC+8`、`UTC+5:30`。
 */
function offsetLabel(minutes: number): string {
  const sign = minutes < 0 ? '-' : '+'
  const absolute = Math.abs(minutes)
  const hours = Math.floor(absolute / 60)
  const rest = absolute % 60
  return `UTC${sign}${hours}${rest ? `:${String(rest).padStart(2, '0')}` : ''}`
}

/** 总览的一张数字卡片。 */
function OverviewCard({ title, value, detail }: { title: string; value: string; detail: string }) {
  return (
    <Card className="px-4 py-3">
      <p className="text-xs text-muted-foreground">{title}</p>
      <p className="mt-1 font-mono text-xl font-semibold tabular-nums">{value}</p>
      <p className="mt-0.5 text-xs text-muted-foreground">{detail}</p>
    </Card>
  )
}

/** 总览卡片组：调用、token、费用、回复与每条回复的平均值。 */
function Overview({ summary }: { summary: UsageSummary }) {
  const { overview, perReply } = summary
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      <OverviewCard
        title="调用次数"
        value={formatTokens(overview.calls)}
        detail={`失败 ${overview.failedCalls} · 中断 ${overview.interruptedCalls}`}
      />
      <OverviewCard
        title="输入 token"
        value={formatTokens(overview.promptTokens)}
        detail={`缓存命中 ${formatTokens(overview.cachedTokens)}`}
      />
      <OverviewCard
        title="输出 token"
        value={formatTokens(overview.completionTokens)}
        detail={`其中思考 ${formatTokens(overview.reasoningTokens)}`}
      />
      <OverviewCard title="费用" value={formatCost(overview.cost)} detail="按调用时生效的单价累计" />
      <OverviewCard title="回复条数" value={formatTokens(overview.replies)} detail="她发出的消息条数" />
      <OverviewCard
        title="每条回复"
        value={perReply.cost === null ? '—' : formatCost(perReply.cost)}
        detail={
          perReply.tokens === null || perReply.calls === null
            ? '这段时间没有回复'
            : `${formatTokens(perReply.tokens)} token · ${perReply.calls.toFixed(1)} 次调用`
        }
      />
    </div>
  )
}

/** 成功调用中服务商没返回用量的次数，按模型列出是哪家。 */
function UnreportedNotice({ summary }: { summary: UsageSummary }) {
  if (!summary.overview.unreportedCalls) return null
  return (
    <p role="status" className="rounded-lg border border-warning/40 bg-warning-soft px-4 py-2.5 text-[13px] text-warning">
      成功调用中有 {summary.overview.unreportedCalls} 次服务商没返回用量，这部分 token 与费用不在统计里：
      {summary.unreported.map((item) => `${item.model}（${item.provider}）${item.calls} 次`).join('、')}
    </p>
  )
}

/**
 * 手写 SVG 柱状图。
 *
 * @param props.points 序列，按时间升序。
 * @param props.metric 画 token 还是费用。
 * @param props.label 把桶起点转成悬停与端点标签的函数。
 * @returns 带端点时刻的柱状图；每根柱悬停显示时刻与数值。
 */
function BarChart({ points, metric, label }: {
  points: TrendPoint[]
  metric: TrendMetric
  label: (start: number) => string
}) {
  const width = 480
  const height = 140
  const peak = Math.max(0, ...points.map((point) => point[metric])) || 1
  const step = width / Math.max(points.length, 1)
  const first = points[0]
  const last = points[points.length - 1]
  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" className="block h-36 w-full" role="img" aria-label="趋势柱状图">
        <line x1={0} y1={height} x2={width} y2={height} className="stroke-border" />
        {points.map((point, index) => {
          const value = point[metric]
          const barHeight = (value / peak) * (height - 8)
          return (
            <rect
              key={point.start}
              x={index * step + 1}
              y={height - barHeight}
              width={Math.max(step - 2, 1)}
              height={barHeight}
              className="fill-primary"
            >
              <title>{`${label(point.start)}：${metric === 'cost' ? formatCost(value) : `${formatTokens(value)} token`}`}</title>
            </rect>
          )
        })}
      </svg>
      <div className="mt-1 flex justify-between font-mono text-[11px] text-muted-foreground tabular-nums">
        <span>{first ? label(first.start) : ''}</span>
        <span>{last ? label(last.start) : ''}</span>
      </div>
    </div>
  )
}

/** 补零的两位数。 */
function two(value: number): string {
  return String(value).padStart(2, '0')
}

/** 小时桶标签：`MM-DD HH:00`，按浏览器所在时区。 */
function hourLabel(start: number): string {
  const date = new Date(start)
  return `${two(date.getMonth() + 1)}-${two(date.getDate())} ${two(date.getHours())}:00`
}

/** 天桶标签：`MM-DD`，按浏览器所在时区。 */
function dayLabel(start: number): string {
  const date = new Date(start)
  return `${two(date.getMonth() + 1)}-${two(date.getDate())}`
}

/** 一张趋势卡片：标题、token / 费用切换与柱状图。 */
function TrendCard({ title, points, label }: { title: string; points: TrendPoint[]; label: (start: number) => string }) {
  const [metric, setMetric] = useState<TrendMetric>('tokens')
  const total = points.reduce((sum, point) => sum + point[metric], 0)
  return (
    <Card>
      <SectionHeading
        title={title}
        subtitle={metric === 'cost' ? `合计 ${formatCost(total)}` : `合计 ${formatTokens(total)} token`}
        icon={<BarChart3 />}
        actions={
          <SegmentedTabs
            tabs={[{ value: 'tokens', label: 'token' }, { value: 'cost', label: '费用' }]}
            value={metric}
            onChange={setMetric}
            label={`${title}的度量`}
          />
        }
      />
      <CardBody className="px-5 py-4">
        <BarChart points={points} metric={metric} label={label} />
      </CardBody>
    </Card>
  )
}

/** 分组表卡片：按来源、任务、模型、会话四种口径切换。 */
function GroupsCard({ summary }: { summary: UsageSummary }) {
  const [kind, setKind] = useState<GroupKind>('source')
  const rows = groupRows(kind, summary)
  const withPrice = kind === 'model'
  return (
    <Card>
      <SectionHeading
        title="用量分组"
        subtitle="费用、token 从高到低；平均耗时后是波动（标准差）"
        icon={<Activity />}
        actions={<UnderlineTabs tabs={GROUP_TABS} value={kind} onChange={setKind} label="分组方式" />}
      />
      {rows.length === 0 ? (
        <CardBody className="px-5 py-4"><Empty>这个时间范围内没有模型调用。</Empty></CardBody>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[880px] border-collapse text-left text-[13px]">
            <thead className="bg-muted text-xs text-subtle-foreground">
              <tr>
                <th className="h-9 px-5 font-medium">{GROUP_FIRST_COLUMN[kind]}</th>
                <th className="h-9 px-3 text-right font-medium">调用</th>
                <th className="h-9 px-3 text-right font-medium">失败 / 中断</th>
                <th className="h-9 px-3 text-right font-medium">输入</th>
                <th className="h-9 px-3 text-right font-medium">输出</th>
                <th className="h-9 px-3 text-right font-medium">缓存命中</th>
                <th className="h-9 px-3 text-right font-medium">费用</th>
                <th className="h-9 px-3 text-right font-medium">占比</th>
                <th className={cn('h-9 text-right font-medium', withPrice ? 'px-3' : 'px-5')}>平均耗时</th>
                {withPrice ? <th className="h-9 px-5 text-right font-medium">当前单价（入 / 出 / 缓存）</th> : null}
              </tr>
            </thead>
            <tbody className="font-mono tabular-nums">
              {rows.map((row) => (
                <tr key={row.key} className="border-b border-border-subtle last:border-b-0">
                  <td className="px-5 py-2 font-sans">
                    <span className="block">{row.name}</span>
                    {row.detail ? <span className="block text-xs text-muted-foreground">{row.detail}</span> : null}
                  </td>
                  <td className="px-3 py-2 text-right">{formatTokens(row.calls)}</td>
                  <td className="px-3 py-2 text-right">{row.failedCalls} / {row.interruptedCalls}</td>
                  <td className="px-3 py-2 text-right">{formatTokens(row.promptTokens)}</td>
                  <td className="px-3 py-2 text-right">{formatTokens(row.completionTokens)}</td>
                  <td className="px-3 py-2 text-right">{formatTokens(row.cachedTokens)}</td>
                  <td className="px-3 py-2 text-right">{formatCost(row.cost)}</td>
                  <td className="px-3 py-2 text-right">{row.share}</td>
                  <td className={cn('py-2 text-right', withPrice ? 'px-3' : 'px-5')}>
                    {formatDuration(row.avgDurationMs, row.durationStdMs)}
                  </td>
                  {withPrice ? (
                    <td className={cn('px-5 py-2 text-right font-sans text-xs', row.priceWarning ? 'text-warning' : 'text-muted-foreground')}>
                      {row.price}
                      {row.priceWarning ? <span className="block">单价为 0：未填写或免费；先填单价再看费用</span> : null}
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}

/** 渲染用量统计页。 */
export function UsagePage() {
  const { range, setRange, summary, loading, error, reload } = useUsage()
  return (
    <PageFrame
      hint="模型调用的 token 用量与费用。费用按调用时生效的单价计算，填写单价之前的调用费用为 0；统计只存在本机，不上传。"
      actions={
        <Button variant="secondary" onClick={reload} disabled={loading}>
          <RefreshCw className="size-4" aria-hidden="true" />
          刷新
        </Button>
      }
    >
      <Toolbar>
        <SegmentedTabs tabs={RANGE_OPTIONS} value={range} onChange={setRange} label="时间范围" />
        {summary ? (
          <span className="ml-auto flex items-center gap-1.5 text-xs text-muted-foreground">
            <Coins className="size-3.5" aria-hidden="true" />
            {summary.range.start ? `自 ${dateTime(summary.range.start)} 起` : '全部记录'}
            {` · 按 ${offsetLabel(summary.range.tzOffsetMinutes)} 归日`}
          </span>
        ) : null}
      </Toolbar>
      <ScrollArea>
        <div className="flex flex-col gap-4 px-6 py-5">
          {error ? <ErrorText>{error}</ErrorText> : null}
          {!summary && loading ? <Loading>正在读取用量统计…</Loading> : null}
          {summary ? (
            <>
              <Overview summary={summary} />
              <UnreportedNotice summary={summary} />
              <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
                <TrendCard title="近 48 小时" points={summary.trend.hourly} label={hourLabel} />
                <TrendCard title="近 30 天" points={summary.trend.daily} label={dayLabel} />
              </div>
              <GroupsCard summary={summary} />
            </>
          ) : null}
        </div>
      </ScrollArea>
    </PageFrame>
  )
}
