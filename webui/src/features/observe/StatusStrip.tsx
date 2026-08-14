/**
 * 顶部状态条：四张关键状态卡（当前状态/今日预算/视觉响应/会话人物）。
 *
 * 每张卡为「色块图标 + 大数字值 + muted 补充细节」的统计卡形态，数据从观察
 * 快照实时计算；被会话观察页引用。
 */
import { Eye, MoonStar, Users, Zap } from 'lucide-react'
import type { ReactNode } from 'react'

import { cn } from '@/components/ui'
import { fixed, numeric, qqSenderLabel, record, text } from '@/lib/format'
import type { ObservabilityPayload } from '../../../../electron/shared/ipc.ts'

/** 单张状态卡的数据结构。 */
interface StatusItem {
  /** 指标名称（小号 muted 文本）。 */
  label: string
  /** 指标主值（大号粗体）。 */
  value: string
  /** 补充细节（极小字 muted）。 */
  detail: string
  /** 色块图标。 */
  icon: ReactNode
  /** 图标色块色调类名。 */
  tintClass: string
}

/**
 * 从快照计算四张状态卡的展示数据。
 *
 * @param payload 后端观察快照。
 * @returns 状态卡数组，顺序固定。
 */
function buildItems(payload: ObservabilityPayload): StatusItem[] {
  const sleep = record(payload.sleep)
  const impulse = record(payload.impulse)
  const vision = record(record(payload.sensing).visionStats)
  const sleepLabel = sleep.asleep === true ? '睡着' : sleep.drowsy === true ? '犯困' : '清醒'
  const used = numeric(impulse.used) ?? 0
  const remaining = numeric(impulse.remaining) ?? 0
  const participants = payload.conversation.participants
  return [
    {
      label: '当前状态',
      value: sleepLabel,
      detail: `睡意 ${fixed(sleep.probability, 2)} · 距入睡 ${fixed(sleep.minutesFromBedtime)} 分钟`,
      icon: <MoonStar />,
      tintClass: 'bg-tint-blue/10 text-tint-blue',
    },
    {
      label: '今日预算',
      value: `${used} / ${used + remaining}`,
      detail: `剩余 ${remaining} · 攒满还需 ${fixed(impulse.minutesToFull)} 分钟`,
      icon: <Zap />,
      tintClass: 'bg-tint-cyan/10 text-tint-cyan',
    },
    {
      label: '视觉响应',
      value: `${fixed(vision.looks)} 看 / ${fixed(vision.spoke)} 说`,
      detail: `视觉${vision.enabled === true ? '已开启' : '未开启'} · 静默 ${text(record(payload.sensing).silent)}`,
      icon: <Eye />,
      tintClass: 'bg-tint-teal/10 text-tint-teal',
    },
    {
      label: '会话人物',
      value: `${participants.length} 人`,
      detail: participants.length
        ? participants
            .map((person) => qqSenderLabel(person.displayName, person.nickname, person.externalId, person.groupCard))
            .join('、')
        : '暂无参与人物',
      icon: <Users />,
      tintClass: 'bg-tint-violet/10 text-tint-violet',
    },
  ]
}

/**
 * 渲染四张关键状态卡。
 *
 * @param props.payload 后端观察快照。
 * @returns 状态卡网格；小屏单列，中屏双列，宽屏四列。
 */
export function StatusStrip({ payload }: { payload: ObservabilityPayload }) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {buildItems(payload).map((item) => (
        <div
          key={item.label}
          className="flex items-start justify-between gap-3 rounded-xl border border-border bg-card p-4 shadow-card transition-shadow duration-200 hover:shadow-lifted"
        >
          <div className="min-w-0">
            <p className="text-xs font-medium text-muted-foreground">{item.label}</p>
            <p className="mt-1 truncate font-mono text-xl font-bold tabular-nums">{item.value}</p>
            <p className="mt-1 truncate text-[11px] text-muted-foreground" title={item.detail}>
              {item.detail}
            </p>
          </div>
          <span
            className={cn('grid size-9 flex-none place-items-center rounded-lg [&>svg]:size-4.5', item.tintClass)}
            aria-hidden="true"
          >
            {item.icon}
          </span>
        </div>
      ))}
    </div>
  )
}
