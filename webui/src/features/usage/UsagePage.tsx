/**
 * 用量统计页：这段时间花了多少 token 与钱、花在哪、按时间怎么分布。
 *
 * 工具栏下自上而下三块，全部贴边排开、以细线分隔，左右边与顶栏、工具栏对齐：
 * - 总览：token、费用、调用、回复四格；单价没填时费用格只给一句提示并链到模型页；
 * - 趋势：一张柱状图，窗口跟着时间范围走（今天按小时，近 7 / 30 天按天）；
 * - 分组排行：按任务、模型、会话、来源切换，每行一根占比条。
 * 输入输出拆分、缓存命中、中断次数、耗时波动等细节只放进悬停提示，不占版面。数字口径
 * 与报告文件 data/reports/usage.html、控制台汇总框相同，都由后端 summarize_usage 算出。
 * 柱状图用 div 手绘，不引图表库。
 */
import { RefreshCw } from 'lucide-react'
import { Fragment, useState, type ReactNode } from 'react'
import { Link } from 'react-router'

import { PageFrame } from '@/components/layout/PageFrame'
import { PaneEmpty, PaneHeader, PaneTitle, ScrollArea, Toolbar } from '@/components/layout/panes'
import { Button, ErrorText, Loading, SegmentedTabs, UnderlineTabs, cn } from '@/components/ui'
import { useUsage } from '@/hooks/use-usage'
import {
  RANGE_OPTIONS,
  formatCompact,
  formatCost,
  formatSeconds,
  formatShare,
  formatTokens,
  groupRows,
  niceCeiling,
  primaryMetric,
  trendView,
  unpricedModelCount,
  type GroupKind,
  type GroupRow,
  type RangeKey,
  type TrendView,
  type UsageSummary,
  type UsageTotals,
} from './usage-format'

/** 分组页签，顺序即展示顺序；任务最能说明钱花在哪，排第一。 */
const GROUP_TABS: Array<{ value: GroupKind; label: string }> = [
  { value: 'task', label: '按任务' },
  { value: 'model', label: '按模型' },
  { value: 'stream', label: '按会话' },
  { value: 'source', label: '按来源' },
]

/** 分组表第一列的列名。 */
const GROUP_FIRST_COLUMN: Record<GroupKind, string> = {
  source: '来源',
  task: '任务',
  model: '模型',
  stream: '会话',
}

/** 失败率达到这个比例时用警示色标出。 */
const FAILURE_WARN_RATIO = 0.1

/** 趋势图的度量。 */
type TrendMetric = 'tokens' | 'cost'

/** 补零的两位数。 */
function two(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * 悬停提示里的柱子时刻，按浏览器所在时区。
 *
 * @param start 桶起点 Unix 毫秒。
 * @param unit 桶时长。
 * @returns 如 `10月26日 14:00` 或 `10月26日`。
 */
function pointLabel(start: number, unit: TrendView['unit']): string {
  const date = new Date(start)
  const day = `${date.getMonth() + 1}月${date.getDate()}日`
  return unit === 'hour' ? `${day} ${two(date.getHours())}:00` : day
}

/**
 * 横轴刻度文本。
 *
 * @param start 桶起点 Unix 毫秒。
 * @param unit 桶时长。
 * @returns 小时桶只写时刻，如 `06:00`；天桶写 `MM-DD`。
 */
function axisLabel(start: number, unit: TrendView['unit']): string {
  const date = new Date(start)
  return unit === 'hour' ? `${two(date.getHours())}:00` : `${two(date.getMonth() + 1)}-${two(date.getDate())}`
}

/**
 * 横轴上标注刻度的柱子下标。
 *
 * @param count 柱子数。
 * @param unit 桶时长。
 * @returns 小时图每 6 小时一个；不超过 7 根的天图每根都标；更长的天图从最后一天往前
 * 每 7 天一个，保证今天总有刻度，手机宽度下相邻刻度也不会挤在一起。
 */
function axisTicks(count: number, unit: TrendView['unit']): number[] {
  const indexes = Array.from({ length: count }, (_, index) => index)
  if (unit === 'hour') return indexes.filter((index) => index % 6 === 0)
  if (count <= 7) return indexes
  return indexes.filter((index) => (count - 1 - index) % 7 === 0)
}

/** 纵轴的费用刻度：三位有效数字并去掉多余的零，如 `¥0.025`。 */
function costTick(value: number): string {
  return `¥${Number(value.toPrecision(3))}`
}

/** token 数的悬停说明：输入（含缓存命中）与输出（含思考）。 */
function tokenBreakdown(totals: UsageTotals): string {
  return `输入 ${formatTokens(totals.promptTokens)}（缓存命中 ${formatTokens(totals.cachedTokens)}）`
    + `\n输出 ${formatTokens(totals.completionTokens)}（思考 ${formatTokens(totals.reasoningTokens)}）`
}

/** 调用次数的悬停说明：成功、失败、中断各多少；中断多为回复途中被新消息打断。 */
function callBreakdown(totals: UsageTotals): string {
  const succeeded = totals.calls - totals.failedCalls - totals.interruptedCalls
  return `成功 ${formatTokens(succeeded)} · 失败 ${formatTokens(totals.failedCalls)} · 中断 ${formatTokens(totals.interruptedCalls)}`
}

/** 用「·」连接的几段说明；每段内部不折行，窄屏时只在段与段之间换行，不会把「输出 5.9K」拆成两行。 */
function Parts({ items }: { items: ReactNode[] }) {
  return (
    <>
      {items.map((item, index) => (
        <Fragment key={index}>
          {index ? ' · ' : null}
          <span className="whitespace-nowrap">{item}</span>
        </Fragment>
      ))}
    </>
  )
}

/** 总览的一格。 */
function Stat({ label, value, detail, title, muted = false }: {
  label: string
  value: string
  detail: ReactNode
  /** 悬停整格时的补充说明。 */
  title?: string
  /** 值是一句状态（如「未计价」）而不是数字时弱化显示。 */
  muted?: boolean
}) {
  return (
    <div className="min-w-0 bg-background px-6 py-4" title={title}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={cn('mt-1.5 text-2xl font-semibold tracking-tight', muted && 'text-muted-foreground')}>{value}</p>
      <p className="mt-1 text-xs text-muted-foreground">{detail}</p>
    </div>
  )
}

/** 总览四格：token、费用、调用、回复。 */
function Overview({ summary }: { summary: UsageSummary }) {
  const { overview, perReply } = summary
  const unpriced = unpricedModelCount(summary)
  const priceLink = (
    <Link to="/models" className="text-primary hover:underline">{unpriced} 个模型没填单价</Link>
  )
  let cost: { value: string; detail: ReactNode; muted?: boolean }
  if (overview.cost > 0) {
    cost = {
      value: formatCost(overview.cost),
      detail: unpriced ? <>另有 {priceLink}</> : perReply.cost === null ? '这段时间没有回复' : `每条回复 ${formatCost(perReply.cost)}`,
    }
  } else if (unpriced) {
    // 没填单价时费用恒为 0，写「¥0」会被当成免费，所以改写成状态并给出去处。
    cost = { value: '未计价', detail: priceLink, muted: true }
  } else {
    cost = { value: formatCost(0), detail: '这段时间没有产生费用' }
  }
  return (
    // 用 1px 间隙露出底色画格线：两列与四列两种排法下分隔线都自动落对位置。
    <div className="grid grid-cols-2 gap-px border-b border-border bg-border lg:grid-cols-4">
      <Stat
        label="token"
        value={formatCompact(overview.totalTokens)}
        detail={<Parts items={[`输入 ${formatCompact(overview.promptTokens)}`, `输出 ${formatCompact(overview.completionTokens)}`]} />}
        title={tokenBreakdown(overview)}
      />
      <Stat label="费用" value={cost.value} detail={cost.detail} muted={cost.muted} />
      <Stat
        label="调用"
        value={formatTokens(overview.calls)}
        detail={
          <Parts
            items={[
              overview.failedCalls ? <span className="text-warning">失败 {formatTokens(overview.failedCalls)}</span> : null,
              `平均耗时 ${formatSeconds(overview.avgDurationMs)}`,
            ].filter(Boolean)}
          />
        }
        title={callBreakdown(overview)}
      />
      <Stat
        label="回复"
        value={formatTokens(overview.replies)}
        detail={perReply.tokens === null ? '这段时间没有回复' : `每条约 ${formatCompact(perReply.tokens)} token`}
        title={perReply.calls === null ? undefined : `每条回复平均调用模型 ${perReply.calls.toFixed(1)} 次`}
      />
    </div>
  )
}

/** 成功调用中服务商没返回用量的次数，按模型列出是哪家；没有时不渲染。 */
function UnreportedNotice({ summary }: { summary: UsageSummary }) {
  if (!summary.overview.unreportedCalls) return null
  return (
    <p role="status" className="border-b border-border bg-warning-soft px-6 py-2.5 text-[13px] text-warning">
      有 {summary.overview.unreportedCalls} 次成功调用服务商没返回用量，未计入统计：
      {summary.unreported.map((item) => `${item.model}（${item.provider}）${item.calls} 次`).join('、')}
    </p>
  )
}

/**
 * 柱状图：左侧纵轴三条刻度线，底部横轴刻度，悬停整列显示该时段的 token 与费用。
 *
 * @param props.view 要画的序列。
 * @param props.metric 柱高代表 token 还是费用。
 * @param props.showCost 悬停提示是否带上费用。
 * @param props.until 当前时刻 Unix 毫秒；晚于它的桶（今天还没到的小时）不响应悬停。
 * @returns 图表元素。
 */
function TrendChart({ view, metric, showCost, until }: {
  view: TrendView
  metric: TrendMetric
  showCost: boolean
  until: number
}) {
  const [hover, setHover] = useState<number | null>(null)
  const { points, unit } = view
  const ceiling = niceCeiling(Math.max(0, ...points.map((point) => point[metric])))
  const tick = metric === 'cost' ? costTick : formatCompact
  const center = (index: number) => `${((index + 0.5) / points.length) * 100}%`
  const hovered = hover === null ? undefined : points[hover]
  return (
    <div className="pl-12">
      <div
        className="relative h-44"
        role="img"
        aria-label={`${view.caption}的${metric === 'cost' ? '费用' : 'token'}柱状图`}
        onMouseLeave={() => setHover(null)}
      >
        {[1, 0.5, 0].map((fraction) => (
          <div
            key={fraction}
            className={cn('absolute inset-x-0 border-t', fraction === 0 ? 'border-border' : 'border-border-subtle')}
            style={{ top: `${(1 - fraction) * 100}%` }}
          >
            <span className="absolute -left-12 w-10 -translate-y-1/2 text-right text-[11px] text-subtle-foreground tabular-nums">
              {fraction === 0 ? '0' : tick(ceiling * fraction)}
            </span>
          </div>
        ))}
        <div className="absolute inset-0 flex">
          {points.map((point, index) => {
            const value = point[metric]
            // 非零的极小值至少留 2px，否则看起来和没有调用一样。
            const height = value > 0 ? `max(2px, ${(value / ceiling) * 100}%)` : '0'
            return (
              <div
                key={point.start}
                className={cn('flex h-full flex-1 items-end justify-center px-px', hover === index && 'bg-foreground/[0.04]')}
                onMouseEnter={() => setHover(point.start > until ? null : index)}
              >
                <div
                  className={cn(
                    'w-full max-w-6 rounded-t-[4px] bg-primary transition-opacity duration-150',
                    hover !== null && hover !== index && 'opacity-40',
                  )}
                  style={{ height }}
                />
              </div>
            )
          })}
        </div>
        {hovered && hover !== null ? (
          <div
            className="pointer-events-none absolute top-0 z-10 rounded-md border border-border bg-card px-2.5 py-1.5 text-xs whitespace-nowrap shadow-sm"
            // 提示框放在悬停列的外侧（左半边放右侧、右半边放左侧），不压住正在看的柱子。
            style={{
              left: center(hover),
              transform: hover < points.length / 2 ? 'translateX(16px)' : 'translateX(calc(-100% - 16px))',
            }}
          >
            <p className="text-muted-foreground">{pointLabel(hovered.start, unit)}</p>
            <p className="mt-0.5 font-medium tabular-nums">
              {formatTokens(hovered.tokens)} token{showCost ? ` · ${formatCost(hovered.cost)}` : ''}
            </p>
          </div>
        ) : null}
      </div>
      <div className="relative mt-2 h-4" aria-hidden="true">
        {axisTicks(points.length, unit).map((index) => (
          <span
            key={index}
            className={cn(
              'absolute text-[11px] whitespace-nowrap text-subtle-foreground tabular-nums',
              index === points.length - 1 ? 'right-0' : '-translate-x-1/2',
            )}
            // 最后一根柱子的刻度居中会伸出图表右边，改为与右边对齐。
            style={index === points.length - 1 ? undefined : { left: center(index) }}
          >
            {axisLabel(points[index]!.start, unit)}
          </span>
        ))}
      </div>
    </div>
  )
}

/** 趋势区：标题栏带窗口说明；这段时间产生过费用时才给 token / 费用切换。 */
function TrendSection({ range, summary }: { range: RangeKey; summary: UsageSummary }) {
  const view = trendView(range, summary)
  const showCost = view.points.some((point) => point.cost > 0)
  const [picked, setPicked] = useState<TrendMetric>('tokens')
  const metric = showCost ? picked : 'tokens'
  return (
    <section aria-label="用量趋势" className="border-b border-border">
      <PaneHeader
        actions={
          showCost ? (
            <SegmentedTabs
              tabs={[{ value: 'tokens', label: 'token' }, { value: 'cost', label: '费用' }]}
              value={metric}
              onChange={setPicked}
              label="趋势度量"
            />
          ) : undefined
        }
      >
        <PaneTitle count={view.caption}>用量趋势</PaneTitle>
      </PaneHeader>
      <div className="px-6 pt-5 pb-4">
        <TrendChart key={view.caption} view={view} metric={metric} showCost={showCost} until={summary.generatedAt} />
      </div>
    </section>
  )
}

/** 分组表的占比条与百分比。 */
function ShareBar({ share }: { share: number }) {
  return (
    <div className="flex items-center gap-3">
      <div className="h-1.5 min-w-16 flex-1 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-primary" style={{ width: `${share * 100}%` }} />
      </div>
      <span className="w-12 flex-none text-right text-xs text-muted-foreground">{(share * 100).toFixed(1)}%</span>
    </div>
  )
}

/** 失败率单元格的文字颜色：没有失败弱化，达到警示比例用警示色。 */
function failureTone(row: GroupRow): string {
  if (!row.failedCalls) return 'text-subtle-foreground'
  return row.failedCalls / row.calls >= FAILURE_WARN_RATIO ? 'text-warning' : ''
}

/**
 * 分组排行：页签切换口径，表格按费用（没有费用时按 token）从高到低。
 *
 * 费用列只在这段时间产生过费用时出现，失败率列只在有失败时出现，避免整列都是 0。
 */
function GroupsSection({ summary }: { summary: UsageSummary }) {
  const [kind, setKind] = useState<GroupKind>('task')
  const rows = groupRows(kind, summary)
  const metric = primaryMetric(summary)
  const showCost = summary.overview.cost > 0
  const showFailures = rows.some((row) => row.failedCalls > 0)
  const head = 'h-9 px-3 text-right font-medium'
  return (
    <section aria-label="用量分组">
      <PaneHeader actions={<span className="text-xs text-subtle-foreground tabular-nums">{rows.length} 项</span>}>
        <UnderlineTabs tabs={GROUP_TABS} value={kind} onChange={setKind} label="分组方式" />
      </PaneHeader>
      {rows.length === 0 ? (
        <PaneEmpty>这段时间没有模型调用。</PaneEmpty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-left text-[13px] tabular-nums">
            <thead className="text-xs text-subtle-foreground">
              <tr className="border-b border-border">
                <th className="h-9 pr-3 pl-6 font-medium">{GROUP_FIRST_COLUMN[kind]}</th>
                {/* 占比条随宽度伸缩，窄屏时把空间让给名称列，模型名加厂商常有三十来个字符。 */}
                <th className="h-9 w-[20%] px-3 font-medium xl:w-[28%]">{metric === 'cost' ? '费用占比' : 'token 占比'}</th>
                <th className={cn(head, 'w-20')}>token</th>
                {showCost ? <th className={cn(head, 'w-24')}>费用</th> : null}
                <th className={cn(head, 'w-16')}>调用</th>
                {showFailures ? <th className={cn(head, 'w-20')}>失败率</th> : null}
                <th className="h-9 w-24 pr-6 pl-3 text-right font-medium">平均耗时</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.key} className="border-b border-border-subtle transition-colors last:border-b-0 hover:bg-muted/40">
                  <td className="h-11 max-w-0 truncate pr-3 pl-6" title={[row.name, row.detail, row.hint].filter(Boolean).join(' · ')}>
                    <span className="font-medium">{row.name}</span>
                    {row.detail ? <span className="ml-2 text-xs text-subtle-foreground">{row.detail}</span> : null}
                  </td>
                  <td className="px-3"><ShareBar share={row.share} /></td>
                  <td className="px-3 text-right" title={tokenBreakdown(row)}>{formatCompact(row.totalTokens)}</td>
                  {showCost ? (
                    <td className="px-3 text-right">
                      {row.unpriced ? <span className="text-subtle-foreground">未计价</span> : formatCost(row.cost)}
                    </td>
                  ) : null}
                  <td className="px-3 text-right" title={callBreakdown(row)}>{formatTokens(row.calls)}</td>
                  {showFailures ? (
                    <td className={cn('px-3 text-right', failureTone(row))}>
                      {row.failedCalls ? formatShare(row.failedCalls, row.calls) : '—'}
                    </td>
                  ) : null}
                  <td
                    className="pr-6 pl-3 text-right"
                    title={row.durationStdMs === null ? undefined : `波动 ±${(row.durationStdMs / 1000).toFixed(2)} s`}
                  >
                    {formatSeconds(row.avgDurationMs)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

/** 渲染用量统计页。 */
export function UsagePage() {
  const { range, setRange, summary, loading, error, reload } = useUsage()
  return (
    <PageFrame
      hint="模型调用的 token 用量与费用。费用按调用时生效的单价计算，填写单价之前的调用不计费；统计只存在本机，不上传。"
      actions={
        <Button variant="secondary" onClick={reload} disabled={loading}>
          <RefreshCw className="size-4" aria-hidden="true" />
          刷新
        </Button>
      }
    >
      <Toolbar>
        <SegmentedTabs tabs={RANGE_OPTIONS} value={range} onChange={setRange} label="时间范围" />
      </Toolbar>
      <ScrollArea>
        {error ? <div className="px-6 pt-3"><ErrorText>{error}</ErrorText></div> : null}
        {!summary && loading ? <Loading>正在读取用量统计…</Loading> : null}
        {summary ? (
          <>
            <Overview summary={summary} />
            <UnreportedNotice summary={summary} />
            <TrendSection range={range} summary={summary} />
            <GroupsSection summary={summary} />
          </>
        ) : null}
      </ScrollArea>
    </PageFrame>
  )
}
