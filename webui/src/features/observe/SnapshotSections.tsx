/**
 * 观察快照的七个业务分区：自身状态、睡眠、日程、打扰预算、感知视觉、会话与语音。
 *
 * 分区以 12 列网格排布，常规分区占 4 列、宽分区占 8 列，dense 自动流回填空隙；
 * 数据全部来自后端已聚合的快照值，本模块只做展示映射。被会话观察页引用。
 */
import {
  BatteryCharging,
  BedDouble,
  CalendarClock,
  Heart,
  MessageSquare,
  Mic,
  ScanEye,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router'

import { Card, CardBody, Chip, Metric, Progress, SectionHeading, cn } from '@/components/ui'
import { dateTime, displayValue, durationCn, fixed, numeric, qqSenderLabel, record, text } from '@/lib/format'
import type { ObservabilityPayload } from '../../../../electron/shared/ipc.ts'

/** 网格跨度：常规分区。 */
const SPAN_NORMAL = 'md:col-span-6 xl:col-span-4'
/** 网格跨度：宽分区。 */
const SPAN_WIDE = 'md:col-span-12 xl:col-span-8'

interface SectionCardProps {
  /** 分区标题。 */
  title: string
  /** 分区标识或补充说明。 */
  subtitle: string
  /** 标题图标。 */
  icon: ReactNode
  /** 图标色调。 */
  tint: 'coral' | 'amber' | 'olive' | 'plum'
  /** 是否宽版布局，默认值为 `false`。 */
  wide?: boolean
  children: ReactNode
}

/**
 * 渲染单个快照分区卡片。
 *
 * @param props.wide 宽版分区占 8 列，常规分区占 4 列。
 * @returns 分区卡片。
 */
function SectionCard({ title, subtitle, icon, tint, wide = false, children }: SectionCardProps) {
  return (
    <Card className={cn('animate-rise', wide ? SPAN_WIDE : SPAN_NORMAL)}>
      <SectionHeading title={title} subtitle={subtitle} icon={icon} tint={tint} />
      <CardBody>{children}</CardBody>
    </Card>
  )
}

/**
 * 渲染带分割线的指标列表容器。
 *
 * @param props.children 指标行节点。
 * @returns 指标列表容器。
 */
function MetricList({ children }: { children: ReactNode }) {
  return <div className="flex flex-col divide-y divide-border/60">{children}</div>
}

/**
 * 渲染自身精力与心情指标和进度条。
 *
 * @param props.payload 后端观察快照。
 * @returns 自身状态分区。
 */
function SelfStateSection({ payload }: { payload: ObservabilityPayload }) {
  const energy = payload.selfState.energy
  const mood = payload.selfState.mood
  return (
    <SectionCard title="自身状态" subtitle="当前精力与心情" icon={<Heart />} tint="plum">
      <div className="flex flex-col gap-2">
        <MetricList>
          <Metric label="精力" value={fixed(energy, 1)} />
          <Metric label="心情" value={fixed(mood, 1)} />
        </MetricList>
        <Progress value={energy} max={100} label="精力" />
        <Progress value={mood} max={100} label="心情" />
      </div>
    </SectionCard>
  )
}

/**
 * 渲染睡意概率、睡眠判定线、计划时间和睡眠债指标。
 *
 * @param props.payload 后端观察快照。
 * @returns 睡眠状态分区。
 * @remarks 入睡前倒数到计划就寝，入睡后倒数到自然醒；两个剩余量都由后端按
 * 同一 now 计算。
 */
function SleepSection({ payload }: { payload: ObservabilityPayload }) {
  const sleep = record(payload.sleep)
  const probability = numeric(sleep.probability)
  const asleep = sleep.asleep === true
  const untilBedtime = numeric(sleep.minutesFromBedtime)
  const remaining = asleep
    ? numeric(sleep.minutesUntilWake)
    : untilBedtime === null ? null : -untilBedtime
  const countdownValue = remaining === null
    ? '—'
    : remaining > 0 ? `预计还有 ${durationCn(remaining)}` : `已过 ${durationCn(-remaining)}`
  return (
    <SectionCard title="睡眠状态" subtitle="生理时钟" icon={<BedDouble />} tint="coral">
      <div className="flex flex-col gap-2">
        {probability !== null ? <Progress value={probability} max={1} label="睡意概率" /> : null}
        <MetricList>
          <Metric
            label="当前判断"
            value={payload.selfState.statusLabel}
          />
          <Metric label="睡意概率" value={fixed(sleep.probability, 3)} />
          <Metric label="睡眠判定线" value={fixed(sleep.cutoff, 3)} />
          <Metric label={asleep ? '距离起床' : '距离入睡'} value={countdownValue} />
          <Metric label="自然醒目标" value={dateTime(sleep.naturalWakeTargetAt)} />
          <Metric label="有效醒来时刻" value={dateTime(sleep.effectiveWakeAt)} />
          <Metric label="睡眠债延迟" value={`${fixed(sleep.sleepDebtDelayMinutes)} 分钟`} />
        </MetricList>
      </div>
    </SectionCard>
  )
}

/**
 * 渲染当天主题、睡眠提示和日程时间线。
 *
 * @param props.payload 后端观察快照；`schedule` 为空时显示服务不可用状态。
 * @returns 日程分区（宽版）。
 */
function ScheduleSection({ payload }: { payload: ObservabilityPayload }) {
  const schedule = payload.schedule
  return (
    <SectionCard
      title="今天的日程"
      subtitle={schedule?.date ?? '今日安排'}
      icon={<CalendarClock />}
      tint="amber"
      wide
    >
      {schedule === null ? (
        <p className="text-sm text-muted-foreground">日程服务当前不可用。</p>
      ) : (
        <div className="flex flex-col gap-4">
          <div className="flex flex-wrap gap-1.5">
            <Chip label="主题" value={schedule.theme} />
            {schedule.sleepEnabled ? (
              <>
                <Chip label="入睡" value={schedule.bedtimeHint} />
                <Chip label="醒来" value={schedule.wakeHint} />
              </>
            ) : (
              <Chip label="自动睡眠" value="已关闭" />
            )}
            <Chip label="承接" value={schedule.carryOver} />
          </div>
          <ol className="relative ml-1.5 flex flex-col gap-3 border-l border-border pl-5">
            {schedule.slots.map((slot) => (
              <li key={`${slot.from}-${slot.doing}`} className="relative">
                <span
                  className="absolute top-[7px] -left-[23.5px] size-2 rounded-full bg-primary"
                  aria-hidden="true"
                />
                <strong className="text-[13px] font-semibold">{slot.from} · {slot.doing}</strong>
                <p className="text-xs text-muted-foreground">{slot.mood}</p>
              </li>
            ))}
          </ol>
        </div>
      )}
    </SectionCard>
  )
}

/**
 * 渲染主动打扰预算、场景剩余额度和兴趣指标。
 *
 * @param props.payload 后端观察快照。
 * @returns 打扰预算分区。
 * @remarks 场景可用额度扣除固定预留槽位，保持与后端预算语义一致。
 */
function BudgetSection({ payload }: { payload: ObservabilityPayload }) {
  const impulse = record(payload.impulse)
  const used = numeric(impulse.used) ?? 0
  const remaining = numeric(impulse.remaining) ?? 0
  const total = used + remaining
  return (
    <SectionCard title="打扰预算" subtitle="今日主动额度" icon={<BatteryCharging />} tint="olive">
      <div className="flex flex-col gap-2">
        <Progress value={used} max={Math.max(1, total)} label="今日主动开口预算" />
        <MetricList>
          <Metric label="已用 / 总额" value={`${used} / ${total}`} />
          <Metric label="场景可用" value={`${Math.max(0, total - 2 - used)} / ${Math.max(0, total - 2)}`} />
          <Metric label="连续未回应" value={fixed(impulse.ignored)} />
          <Metric label="当前兴趣值" value={fixed(impulse.interest, 2)} />
          <Metric label="攒满还需" value={`${fixed(impulse.minutesToFull)} 分钟`} />
        </MetricList>
      </div>
    </SectionCard>
  )
}

/**
 * 渲染前台活动、静默状态、视觉开关和按原因统计的视觉事件。
 *
 * @param props.payload 后端观察快照。
 * @returns 感知与视觉分区；仅展示后端已聚合的统计值。
 */
function SensingSection({ payload }: { payload: ObservabilityPayload }) {
  const sensing = record(payload.sensing)
  const vision = record(sensing.visionStats)
  const looks = numeric(vision.looks) ?? 0
  const spoke = numeric(vision.spoke) ?? 0
  const byReason = record(vision.byReason)
  return (
    <SectionCard title="感知与视觉" subtitle="最近视觉统计" icon={<ScanEye />} tint="amber">
      <div className="flex flex-col gap-2">
        {/* 看过/开口占比进度条：只有发生过视觉事件时才渲染，避免空进度条。 */}
        {looks > 0 ? (
          <>
            <Progress value={spoke} max={looks} label="视觉开口占比" />
            <MetricList>
              <Metric label="视觉开口占比" value={`${Math.round((spoke / looks) * 100)}%`} />
            </MetricList>
          </>
        ) : null}
        <MetricList>
          <Metric label="当前活动" value={displayValue(sensing.activity)} />
          <Metric label="活动描述" value={text(sensing.description)} />
          <Metric label="持续时间" value={`${fixed(sensing.minutes)} 分钟`} />
          <Metric label="静默场景" value={displayValue(sensing.silent)} />
          <Metric label="视觉启用" value={text(vision.enabled)} />
          <Metric label="看过 / 开口" value={`${fixed(vision.looks)} / ${fixed(vision.spoke)}`} />
          {Object.entries(byReason).map(([reason, count]) => (
            <Metric key={reason} label={`视觉原因 · ${displayValue(reason)}`} value={fixed(count)} />
          ))}
        </MetricList>
      </div>
    </SectionCard>
  )
}

/**
 * 渲染当前会话的工作消息数量和参与人物链接。
 *
 * @param props.payload 后端观察快照。
 * @returns 会话状态分区（宽版）。
 */
function ConversationSection({ payload }: { payload: ObservabilityPayload }) {
  const participants = payload.conversation.participants
  return (
    <SectionCard title="会话状态" subtitle="当前工作记忆" icon={<MessageSquare />} tint="coral" wide>
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap gap-1.5">
          <Chip label="工作消息" value={`${payload.conversation.workingMessages} 条`} />
          <Chip label="出现人物" value={`${participants.length} 人`} />
        </div>
        {!participants.length ? (
          <p className="text-sm text-muted-foreground">这条会话还没有人物发言。</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {participants.map((person) => (
              <Link
                key={person.id}
                to={`/persons/${person.id}`}
                className="inline-flex max-w-full items-center truncate rounded-full border border-border bg-card px-3 py-1 text-xs font-medium text-accent-foreground transition-colors hover:border-primary/40 hover:bg-accent"
              >
                {qqSenderLabel(person.displayName, person.nickname, person.externalId, person.groupCard)}
              </Link>
            ))}
          </div>
        )}
      </div>
    </SectionCard>
  )
}

/**
 * 渲染语音合成开关、模型音色和缓存统计。
 *
 * @param props.payload 后端观察快照。
 * @returns 语音与缓存分区。
 */
function VoiceSection({ payload }: { payload: ObservabilityPayload }) {
  const voice = record(payload.voice)
  const cache = record(voice.cache)
  const cacheHits = numeric(voice.cacheHits) ?? 0
  const cacheMisses = numeric(voice.cacheMisses) ?? 0
  const cacheTotal = cacheHits + cacheMisses
  return (
    <SectionCard title="语音与缓存" subtitle="语音服务状态" icon={<Mic />} tint="olive">
      <div className="flex flex-col gap-2">
        {/* 命中率进度条补足卡片视觉密度，同时给出比计数更直观的比率。 */}
        {cacheTotal > 0 ? (
          <>
            <Progress value={cacheHits} max={cacheTotal} label="语音缓存命中率" />
            <MetricList>
              <Metric label="缓存命中率" value={`${Math.round((cacheHits / cacheTotal) * 100)}%`} />
            </MetricList>
          </>
        ) : null}
        <MetricList>
          <Metric label="运行状态" value={voice.enabled === true ? '已启用' : '未启用'} />
          <Metric label="服务配置" value={voice.configured === true ? '已配置' : '未配置'} />
          <Metric label="模型 / 音色" value={`${text(voice.model)} / ${text(voice.voice)}`} />
          <Metric label="缓存命中 / 未命中" value={`${fixed(voice.cacheHits)} / ${fixed(voice.cacheMisses)}`} />
          <Metric label="连续失败" value={fixed(voice.failures)} />
          <Metric label="缓存文件" value={`${fixed(cache.files)} 个`} />
          <Metric label="缓存体积" value={`${fixed((numeric(cache.bytes) ?? 0) / 1024, 1)} KB`} />
        </MetricList>
      </div>
    </SectionCard>
  )
}

/**
 * 按固定顺序渲染观察快照的全部分区。
 *
 * @param props.payload 后端观察快照。
 * @returns 分区网格。
 */
export function SnapshotSections({ payload }: { payload: ObservabilityPayload }) {
  return (
    <div className="grid grid-cols-1 gap-4 [grid-auto-flow:dense] md:grid-cols-12">
      <SelfStateSection payload={payload} />
      <SleepSection payload={payload} />
      <ScheduleSection payload={payload} />
      <BudgetSection payload={payload} />
      <SensingSection payload={payload} />
      <ConversationSection payload={payload} />
      <VoiceSection payload={payload} />
    </div>
  )
}
