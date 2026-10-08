/**
 * 关键状态摘要：当前状态、今日预算、视觉响应、会话人物四项。
 *
 * 渲染在会话观察页右侧「她现在」检查栏的最上方：第一项（当前状态）放大成
 * 抬头，其余三项为「名称 · 值 · 补充说明」的紧凑行。数据从观察快照实时计算；
 * 被会话观察页引用。
 */
import { Eye, MoonStar, Users, Zap } from 'lucide-react'
import type { ReactNode } from 'react'

import { cn } from '@/components/ui'
import { displayValue, fixed, numeric, qqSenderLabel, record } from '@/lib/format'
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
  const activity = record(payload.activity)
  const impulse = record(payload.impulse)
  const vision = record(record(payload.sensing).visionStats)
  const used = numeric(impulse.used) ?? 0
  const remaining = numeric(impulse.remaining) ?? 0
  const participants = payload.conversation.participants
  return [
    {
      label: '当前状态',
      value: payload.selfState.statusLabel,
      detail: `${displayValue(activity.kind)} · ${displayValue(activity.doing)}`,
      icon: <MoonStar />,
      tintClass: 'bg-tint-coral/10 text-tint-coral',
    },
    {
      label: '今日预算',
      value: `${used} / ${used + remaining}`,
      detail: `剩余 ${remaining} · 攒满还需 ${fixed(impulse.minutesToFull)} 分钟`,
      icon: <Zap />,
      tintClass: 'bg-tint-amber/10 text-tint-amber',
    },
    {
      label: '视觉响应',
      value: `${fixed(vision.looks)} 看 / ${fixed(vision.spoke)} 说`,
      detail: `视觉${vision.enabled === true ? '已开启' : '未开启'} · 静默 ${displayValue(record(payload.sensing).silent)}`,
      icon: <Eye />,
      tintClass: 'bg-tint-olive/10 text-tint-olive',
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
      tintClass: 'bg-tint-plum/10 text-tint-plum',
    },
  ]
}

/**
 * 渲染关键状态摘要。
 *
 * @param props.payload 后端观察快照。
 * @returns 抬头一项加三行紧凑指标。
 */
export function StatusStrip({ payload }: { payload: ObservabilityPayload }) {
  const [head, ...rest] = buildItems(payload)
  return (
    <div className="flex flex-col gap-3">
      {head ? (
        <div className="flex items-center gap-3">
          <span
            className={cn('grid size-9 flex-none place-items-center rounded-full [&>svg]:size-4', head.tintClass)}
            aria-hidden="true"
          >
            {head.icon}
          </span>
          <div className="min-w-0">
            <p className="truncate text-[15px] font-semibold">{head.value}</p>
            <p className="truncate text-xs text-muted-foreground" title={head.detail}>{head.detail}</p>
          </div>
        </div>
      ) : null}
      <dl className="flex flex-col divide-y divide-border-subtle">
        {rest.map((item) => (
          <div key={item.label} className="flex items-baseline justify-between gap-3 py-1.5">
            <dt className="flex-none text-xs text-muted-foreground">{item.label}</dt>
            <dd className="min-w-0 text-right">
              <span className="block truncate font-mono text-[13px] font-medium tabular-nums">{item.value}</span>
              <span className="block truncate text-[11px] text-subtle-foreground" title={item.detail}>{item.detail}</span>
            </dd>
          </div>
        ))}
      </dl>
    </div>
  )
}
