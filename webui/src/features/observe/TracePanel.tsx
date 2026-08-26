/**
 * 事件账本分面板：对话轮次卡片、后台事件列表与历史检索表单。
 *
 * 对话事件按 turnId 聚合为卡片，后台事件按时间顺序单独展示；历史检索支持
 * kind/轮次/时间窗组合过滤与游标翻页；llm_request 事件可隔离重放并对照原
 * 输出与新输出。数据通道由 use-traces hook 提供，被会话观察页引用。
 */
import { List, RotateCcw } from 'lucide-react'
import { memo, useCallback, useMemo, useState } from 'react'
import type { FormEvent } from 'react'

import { Button, Card, CardBody, Chip, Empty, Field, Input, SectionHeading, Select, Toggle, cn } from '@/components/ui'
import { useAuth } from '@/hooks/use-auth'
import { apiMutate, UnauthorizedError } from '@/lib/api'
import {
  dateTime,
  displayValue,
  elapsedLabel,
  fixed,
  formatMessages,
  optionalText,
  record,
  text,
  traceDetailItems,
  traceKindLabel,
  traceKindQueryValue,
  traceSenderLabel,
} from '@/lib/format'
import type { TraceEntry } from '../../../../electron/shared/ipc.ts'

/** 事件类型过滤选项：协议值保持稳定，界面仅显示中文名。 */
const KIND_OPTIONS = [
  { value: 'all', label: '全部事件' },
  { value: 'user_input', label: '收到用户消息' },
  { value: 'observation', label: '旁听消息' },
  { value: 'reply_gate', label: '回复门控判定' },
  { value: 'action_decision', label: '行动决策' },
  { value: 'llm_request', label: '请求模型' },
  { value: 'llm_final', label: '模型输出完成' },
  { value: 'llm_error', label: '模型调用失败' },
  { value: 'expression_select', label: '表达方式选择' },
  { value: 'expression_learned', label: '表达方式学习完成' },
  { value: 'jargon_hit', label: '黑话命中' },
  { value: 'jargon_mined', label: '黑话提取' },
  { value: 'jargon_inferred', label: '黑话推断' },
  { value: 'interest', label: '兴趣度更新' },
  { value: 'proactive_intent', label: '主动意图评估' },
  { value: 'vision_glance', label: '视觉扫视' },
] as const

/** 单次检索请求的事件条数上限。 */
const SEARCH_LIMIT = 200

/** 隔离重放接口的响应结构。 */
interface ReplayResult {
  originalPromptHash: string
  replayPromptHash: string
  originalOutput: string
  replayOutput: string
}

/**
 * 将 datetime-local 控件值转换成毫秒时间戳。
 *
 * @param value 控件值；空字符串或非法日期返回 `null`。
 * @returns 毫秒时间戳或 `null`。
 */
function localDateTimeMs(value: string): number | null {
  if (!value) return null
  const parsed = new Date(value).getTime()
  return Number.isFinite(parsed) ? parsed : null
}

/**
 * 隔离重放一条模型请求，并把原输出与新输出并排展示。
 *
 * @param props.seq 目标 llm_request 事件的序号。
 * @returns 重放按钮与对照结果容器。
 */
function ReplayControl({ seq }: { seq: number }) {
  const { handleUnauthorized } = useAuth()
  const [state, setState] = useState<'idle' | 'loading' | 'done'>('idle')
  const [errorText, setErrorText] = useState('')
  const [result, setResult] = useState<ReplayResult | null>(null)

  const replay = async () => {
    setState('loading')
    setErrorText('')
    try {
      const payload = record(await apiMutate('/replay', 'POST', { seq }))
      setResult({
        originalPromptHash: text(payload.originalPromptHash),
        replayPromptHash: text(payload.replayPromptHash),
        originalOutput: text(payload.originalOutput),
        replayOutput: text(payload.replayOutput),
      })
      setState('done')
    } catch (error) {
      if (error instanceof UnauthorizedError) {
        handleUnauthorized(error)
        return
      }
      setErrorText(error instanceof Error ? error.message : String(error))
      setState('idle')
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Button variant="secondary" size="sm" disabled={state === 'loading'} onClick={() => void replay()}>
          <RotateCcw className="size-3.5" aria-hidden="true" />
          {state === 'loading' ? '重放中…' : state === 'done' ? '再次重放' : '用当前模板重放'}
        </Button>
        {errorText ? <span className="text-xs text-destructive">{errorText}</span> : null}
      </div>
      {result ? (
        <div className="flex flex-col gap-2 rounded-lg border border-border bg-muted/50 p-3">
          <strong className="text-xs font-semibold">
            重放对照 · {result.originalPromptHash} → {result.replayPromptHash}
          </strong>
          <div className="grid gap-2 sm:grid-cols-2">
            {([
              ['原输出', result.originalOutput],
              ['新输出', result.replayOutput],
            ] as Array<[string, string]>).map(([label, value]) => (
              <div key={label} className="flex min-w-0 flex-col gap-1">
                <span className="text-[11px] text-muted-foreground">{label}</span>
                <pre className="max-h-64 overflow-auto rounded-lg border border-terminal-border bg-terminal p-3 font-mono text-xs whitespace-pre-wrap text-terminal-foreground">
                  {value || '（空输出）'}
                </pre>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  )
}

/** 把事件剩余字段渲染成独立中文信息块，避免 JSON 串堆在同一行。 */
function TraceDetails({ entry }: { entry: TraceEntry }) {
  const items = traceDetailItems(entry)
  if (!items.length) return null
  return (
    <div className="flex min-w-0 flex-wrap gap-1.5">
      {items.map((item) => (
        <Chip key={item.rawKey} label={item.label} value={item.value} />
      ))}
    </div>
  )
}

/**
 * 折叠的模型提示词查看器：展开时才把提示词全文挂载进 DOM。
 *
 * @param props.messages llm_request 事件携带的完整请求消息。
 * @returns 折叠条；展开后为终端样式的提示词全文。
 * @remarks 原生 <details> 只是视觉隐藏，子树无论展开与否都会进入 DOM 与布局；
 * 提示词全文可达数万字符，30 个轮次卡片常驻挂载会把页面 DOM 推至上万节点，
 * 事件突发时的整表重渲染表现为百毫秒级长任务。改为受控展开，折叠时零成本。
 */
function PromptDetails({ messages }: { messages: TraceEntry['messages'] }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="flex flex-col gap-1.5">
      <button
        type="button"
        onClick={() => setOpen((current) => !current)}
        className="cursor-pointer self-start text-xs font-medium text-primary-strong select-none"
      >
        {open ? '收起发送给模型的提示词' : '展开发送给模型的提示词'}
      </button>
      {open ? (
        <pre className="max-h-72 overflow-auto rounded-lg border border-terminal-border bg-terminal p-3 font-mono text-xs whitespace-pre-wrap text-terminal-foreground">
          {formatMessages(messages)}
        </pre>
      ) : null}
    </div>
  )
}

/**
 * 逆序找到指定类型的最后一条事件。
 *
 * @param entries 单轮事件列表（按时间升序）。
 * @param kind 目标事件类型。
 * @returns 最后一条命中事件；没有时为 `undefined`。
 */
function findLastKind(entries: TraceEntry[], kind: string): TraceEntry | undefined {
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index]
    if (entry !== undefined && entry.kind === kind) return entry
  }
  return undefined
}

/**
 * 把单轮事件按类型聚合成一行计数文本，供无对话内容的轮次做摘要。
 *
 * @param entries 单轮事件列表。
 * @returns 形如「兴趣度更新 ×28 · 主动意图评估 ×2」的文本。
 */
function kindCountLabel(entries: TraceEntry[]): string {
  const counts = new Map<string, number>()
  for (const entry of entries) counts.set(entry.kind, (counts.get(entry.kind) ?? 0) + 1)
  return [...counts].map(([kind, count]) => `${traceKindLabel(kind)} ×${count}`).join(' · ')
}

/** TurnCard 的入参。 */
interface TurnCardProps {
  turnId: number
  entries: TraceEntry[]
  /** 是否展开为该轮全部事件的完整列表。 */
  expanded: boolean
  onShowTurn: (turnId: number) => void
  onCollapse: (turnId: number) => void
}

/** 一轮对话的终局阶段：一轮正常对话恰好到达其中之一次。 */
const TERMINAL_STAGES = new Set(['replied', 'gated', 'failed'])

/**
 * 判断分组是否混入了多轮对话的事件。
 *
 * 轮次编号是进程内计数器、每次启动从 0 重来（chat.py `ChatService._next_turn`），
 * 而事件账本跨重启持久化，不同启动的回合会共用同一编号。一轮对话内全部
 * user_input 都先于终局阶段发出（批量消息也不例外），因此「终局阶段多于一个」
 * 或「终局之后又出现 user_input」都说明该组是多次对话的拼接，摘要不能跨轮配对。
 *
 * @param entries 单组事件列表（按时间升序）。
 * @returns 该组是否混有多轮对话。
 */
function isMergedTurnGroup(entries: TraceEntry[]): boolean {
  let terminals = 0
  let seenTerminal = false
  for (const entry of entries) {
    if (entry.kind === 'stage' && TERMINAL_STAGES.has(optionalText(entry.stage))) {
      terminals += 1
      seenTerminal = true
    } else if (entry.kind === 'user_input' && seenTerminal) {
      return true
    }
  }
  return terminals > 1
}

/**
 * 轮次卡片的 memo 相等性：轮次事件只会追加（新事件）或从头部淘汰（数量上限
 * 截断），因此比较长度与首尾事件引用即可判定内容是否变化，无需深比较。
 */
function sameTurnCard(prev: TurnCardProps, next: TurnCardProps): boolean {
  if (prev.turnId !== next.turnId || prev.expanded !== next.expanded) return false
  if (prev.onShowTurn !== next.onShowTurn || prev.onCollapse !== next.onCollapse) return false
  const before = prev.entries
  const after = next.entries
  if (before.length !== after.length) return false
  return before[0] === after[0] && before[before.length - 1] === after[after.length - 1]
}

/**
 * 渲染单个对话轮次卡片。
 *
 * @param props.turnId 轮次 ID。
 * @param props.entries 该轮次的全部事件。
 * @param props.expanded 是否展开为全部事件的完整列表。
 * @param props.onShowTurn 「查看该轮全部事件」回调。
 * @param props.onCollapse 「收起事件列表」回调。
 * @returns 轮次卡片；含 llm_error 时描边标红。
 * @remarks 默认只给一屏能看完的摘要：用户原话一行、她的回复一行、结论一行，
 * 全部细节留给展开态。头部只渲染有真实取值的字段：主动评估类事件（interest、
 * proactive_intent 等）由后台回路发出，从来不携带来源字段，这类轮次的头部
 * 不再硬凑「来源 / 会话 / 人物」，改用事件条数与耗时。轮次编号跨重启被复用、
 * 多轮对话混入同组时（见 isMergedTurnGroup）不生成配对摘要与耗时，只提示展开。
 * memo 化：事件流每来
 * 一条新事件整个 TracePanel 都会重渲染，但只有事件真正发生变化的轮次卡片
 * 才需要重新提交，其余卡片按引用比较整体跳过。
 */
const TurnCard = memo(function TurnCard({ turnId, entries, expanded, onShowTurn, onCollapse }: TurnCardProps) {
  const origin = entries.find((entry) => entry.platform !== undefined)
  const hasError = entries.some((entry) => entry.kind === 'llm_error')
  const userInput = entries.find((entry) => entry.kind === 'user_input')
  const botReply = findLastKind(entries, 'llm_final')
  const observation = findLastKind(entries, 'observation')
  const llmError = findLastKind(entries, 'llm_error')
  const firstEntry = entries[0]
  const lastEntry = entries[entries.length - 1]
  const durationMs =
    entries.length > 1 && firstEntry !== undefined && lastEntry !== undefined
      ? Math.max(0, lastEntry.at - firstEntry.at)
      : 0
  const senderName = origin ? optionalText(origin.senderDisplayName) : ''
  /* 编号被多次对话复用的分组不做配对摘要与耗时：跨轮配对会把不存在的
   * 问答组合说成事实，首尾相减的耗时同样失真；条数仍是真实计数，保留。 */
  const mergedTurns = isMergedTurnGroup(entries)
  return (
    <article
      className={cn(
        'flex flex-col gap-2.5 rounded-lg border border-border bg-muted/40 p-4',
        hasError && 'border-destructive/40 bg-destructive-soft/60',
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <strong className="text-[13px] font-semibold">
          第 {turnId} 轮
          {senderName ? ` · ${senderName}` : ''}
          {origin ? ` · 来源：${displayValue(origin.platform)}` : ''}
          {origin?.streamId != null ? ` · 会话 #${origin.streamId}` : ''}
          {origin?.personId != null ? ` · 人物 #${origin.personId}` : ''}
          <span className="ml-2 font-mono text-xs font-normal text-muted-foreground tabular-nums">
            {entries.length} 条{mergedTurns ? '' : ` · 耗时 ${elapsedLabel(durationMs)}`}
          </span>
          {hasError ? (
            <span className="ml-2 rounded-full bg-destructive-soft px-2 py-0.5 text-xs font-medium text-destructive">
              模型错误
            </span>
          ) : null}
        </strong>
        {expanded ? (
          <Button variant="ghost" size="sm" onClick={() => onCollapse(turnId)}>
            收起事件列表
          </Button>
        ) : (
          <Button variant="ghost" size="sm" onClick={() => onShowTurn(turnId)}>
            查看该轮全部事件
          </Button>
        )}
      </div>
      {expanded ? null : (
        <div className="flex flex-col gap-1.5">
          {mergedTurns ? (
            <p className="text-[13px] text-muted-foreground">
              轮次编号在进程重启后被复用，该组混有多轮对话的事件；为避免错配不生成摘要，请展开逐条查看。
            </p>
          ) : (
            <>
              {userInput ? (
                <p className="truncate text-[13px]" title={text(userInput.text)}>
                  {traceSenderLabel(userInput)}：{text(userInput.text)}
                </p>
              ) : null}
              {botReply ? (
                <p className="truncate rounded-md bg-primary-soft px-2.5 py-1.5 text-[13px]" title={text(botReply.text)}>
                  {optionalText(botReply.botName) || 'Bot'}：{text(botReply.text)}
                </p>
              ) : null}
              {observation ? (
                <p className="truncate text-[13px] text-warning">未回复：{displayValue(observation.reason)}</p>
              ) : null}
              {llmError ? (
                <p className="truncate text-[13px] text-destructive">
                  模型调用失败：{displayValue(llmError.errorKind)} · {text(llmError.message)}
                </p>
              ) : null}
              {!userInput && !botReply && !observation && !llmError ? (
                <p className="truncate text-[13px] text-muted-foreground">{kindCountLabel(entries)}</p>
              ) : null}
            </>
          )}
        </div>
      )}
      {expanded ? entries.map((entry, index) => {
        const key = `${entry.seq ?? index}-${entry.kind}`
        if (entry.kind === 'user_input') {
          return (
            <p key={key} className="text-[13px]">
              {traceSenderLabel(entry)}：{text(entry.text)}
            </p>
          )
        }
        if (entry.kind === 'llm_request') {
          return (
            <div key={key} className="flex flex-col gap-2">
              <PromptDetails messages={entry.messages} />
              {typeof entry.seq === 'number' ? <ReplayControl seq={entry.seq} /> : null}
            </div>
          )
        }
        if (entry.kind === 'llm_final') {
          return (
            <p key={key} className="rounded-md bg-primary-soft px-2.5 py-1.5 text-[13px]">
              {optionalText(entry.botName) || 'Bot'}：{text(entry.text)}
            </p>
          )
        }
        if (entry.kind === 'memory_fact') {
          return (
            <div key={key} className="flex flex-wrap gap-1.5">
              <Chip label="记忆" value={`[${displayValue(entry.memoryKind)}] ${text(entry.content)}`} />
            </div>
          )
        }
        if (entry.kind === 'mood_delta') {
          return (
            <div key={key} className="flex flex-wrap gap-1.5">
              <Chip label="心情" value={`好感 ${fixed(entry.favor)} · 精力 ${fixed(entry.energy)}`} />
            </div>
          )
        }
        if (entry.kind === 'llm_error') {
          return (
            <div key={key} className="flex flex-wrap gap-1.5">
              <Chip label="错误" value={`${displayValue(entry.errorKind)} · ${text(entry.message)}`} />
            </div>
          )
        }
        return (
          <div key={key} className="flex flex-col gap-1.5 rounded-md border border-border/70 bg-card/70 px-2.5 py-2">
            <strong className="text-xs font-semibold text-accent-foreground" title={entry.kind}>
              {traceKindLabel(entry.kind)}
            </strong>
            <TraceDetails entry={entry} />
          </div>
        )
      }) : null}
    </article>
  )
}, sameTurnCard)

/**
 * 后台事件列表的单行（memo：事件对象引用不变时跳过重渲染）。
 *
 * @param props.entry 单条后台事件。
 * @returns 事件行元素。
 */
const BackgroundRow = memo(function BackgroundRow({ entry }: { entry: TraceEntry }) {
  return (
    <div className="flex flex-col gap-1.5 rounded-md border border-border/70 bg-card/70 px-2.5 py-2">
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-0.5">
        <span className="flex-none text-muted-foreground tabular-nums">{dateTime(entry.at)}</span>
        <strong className="flex-none font-semibold text-accent-foreground" title={entry.kind}>
          {traceKindLabel(entry.kind)}
        </strong>
      </div>
      {/* 观察事件直接展示原消息和后端门控原因，避免把「未回复」误判为链路故障。 */}
      {entry.kind === 'observation' ? (
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          <span className="min-w-0 break-all">{traceSenderLabel(entry)}：{text(entry.text)}</span>
          <span className="text-warning">未回复：{displayValue(entry.reason)}</span>
        </div>
      ) : (
        <TraceDetails entry={entry} />
      )}
    </div>
  )
})

/** TracePanel 的入参：事件通道状态与当前会话流。 */
interface TracePanelProps {
  traces: TraceEntry[]
  skippedCount: number
  historyCursor: number | null
  search: (params: URLSearchParams, append: boolean) => Promise<string>
  /** 当前选中的会话流 ID 文本（stream 下拉框值）。 */
  streamId: string
}

/**
 * 渲染事件账本分面板。
 *
 * @param props 事件通道状态与检索方法。
 * @returns 事件账本卡片。
 */
export function TracePanel({ traces, skippedCount, historyCursor, search, streamId }: TracePanelProps) {
  const [kindFilter, setKindFilter] = useState('all')
  const [currentStreamOnly, setCurrentStreamOnly] = useState(true)
  const [turnIdInput, setTurnIdInput] = useState('')
  const [kindsInput, setKindsInput] = useState('')
  const [sinceInput, setSinceInput] = useState('')
  const [untilInput, setUntilInput] = useState('')
  const [searchStatus, setSearchStatus] = useState('')
  const [searching, setSearching] = useState(false)
  /** 当前展开为完整事件列表的轮次；`null` 表示全部卡片都是摘要态。 */
  const [expandedTurnId, setExpandedTurnId] = useState<number | null>(null)

  const visible = useMemo(
    () =>
      traces.filter((entry) => {
        const kindMatches = kindFilter === 'all' || entry.kind === kindFilter
        const streamMatches = entry.streamId == null || entry.streamId === Number(streamId)
        return kindMatches && streamMatches
      }),
    [traces, kindFilter, streamId],
  )

  const turnGroups = useMemo(() => {
    const grouped = new Map<number, TraceEntry[]>()
    for (const entry of visible) {
      if (entry.turnId == null) continue
      const group = grouped.get(entry.turnId) ?? []
      group.push(entry)
      grouped.set(entry.turnId, group)
    }
    return [...grouped.entries()].reverse().slice(0, 30)
  }, [visible])

  const backgroundEntries = useMemo(
    () => visible.filter((entry) => entry.turnId == null).slice(-300),
    [visible],
  )

  /** 组装检索查询参数；`cursor` 仅翻页时携带。 */
  const buildParams = (cursor: number | null): URLSearchParams => {
    const params = new URLSearchParams({ limit: String(SEARCH_LIMIT) })
    if (currentStreamOnly && streamId) params.set('streamId', streamId)
    if (turnIdInput) params.set('turnId', turnIdInput)
    for (const kind of kindsInput.split(/[,，]/).map((value) => value.trim()).filter(Boolean)) {
      params.append('kind', traceKindQueryValue(kind))
    }
    const since = localDateTimeMs(sinceInput)
    const until = localDateTimeMs(untilInput)
    if (since !== null) params.set('since', String(since))
    if (until !== null) params.set('until', String(until))
    if (cursor !== null) params.set('cursor', String(cursor))
    return params
  }

  const runSearch = async (append: boolean) => {
    setSearching(true)
    setSearchStatus('正在检索…')
    const message = await search(buildParams(append ? historyCursor : null), append)
    if (message) setSearchStatus(message)
    setSearching(false)
  }

  /** 从任意事件卡片切换到指定轮次的完整历史，并把该卡片展开为完整事件列表。 */
  /* useCallback 固定引用：TurnCard 的 memo 比较依赖 onShowTurn 引用稳定。 */
  const showTurn = useCallback((turnId: number) => {
    setCurrentStreamOnly(false)
    setTurnIdInput(String(turnId))
    setKindsInput('')
    setSinceInput('')
    setUntilInput('')
    setSearching(true)
    setSearchStatus('正在检索…')
    const params = new URLSearchParams({ limit: String(SEARCH_LIMIT), turnId: String(turnId) })
    void search(params, false).then((message) => {
      if (message) setSearchStatus(message)
      setExpandedTurnId(turnId)
      setSearching(false)
    })
  }, [search])

  /** 把展开的轮次卡片收回到摘要态。 */
  /* useCallback 固定引用：TurnCard 的 memo 比较依赖 onCollapse 引用稳定。 */
  const collapseTurn = useCallback((turnId: number) => {
    setExpandedTurnId((current) => (current === turnId ? null : current))
  }, [])

  const onSubmit = (event: FormEvent) => {
    event.preventDefault()
    void runSearch(false)
  }

  return (
    <Card id="events" aria-label="运行时调试追踪" className="animate-rise scroll-mt-6">
      <SectionHeading
        title="事件账本"
        subtitle="按轮次聚合对话，按时间追溯后台事件"
        icon={<List />}
        tint="amber"
        actions={
          <>
            <Field label="事件类型" htmlFor="trace-filter" className="w-56">
              <Select id="trace-filter" value={kindFilter} onChange={(event) => setKindFilter(event.target.value)}>
                {KIND_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </Field>
            <span className="self-end pb-2 font-mono text-xs whitespace-nowrap text-muted-foreground tabular-nums">
              {visible.length} / {traces.length} 条{skippedCount ? ` · 已跳过 ${skippedCount} 条` : ''}
            </span>
          </>
        }
      />
      <CardBody className="flex flex-col gap-5">
        <form onSubmit={onSubmit} className="flex flex-wrap items-end gap-3" aria-label="历史事件检索">
          <div className="pb-2">
            <Toggle checked={currentStreamOnly} onChange={setCurrentStreamOnly} label="当前会话" />
          </div>
          <Field label="轮次" htmlFor="event-turn-id" className="w-24">
            <Input
              id="event-turn-id"
              className="font-mono"
              type="number"
              min={1}
              placeholder="全部"
              value={turnIdInput}
              onChange={(event) => setTurnIdInput(event.target.value)}
            />
          </Field>
          <Field label="类型" htmlFor="event-kinds" className="w-56">
            <Input
              id="event-kinds"
              className="font-mono"
              type="text"
              placeholder="请求模型、模型输出完成"
              value={kindsInput}
              onChange={(event) => setKindsInput(event.target.value)}
            />
          </Field>
          <Field label="开始" htmlFor="event-since" className="w-48">
            <Input
              id="event-since"
              type="datetime-local"
              value={sinceInput}
              onChange={(event) => setSinceInput(event.target.value)}
            />
          </Field>
          <Field label="结束" htmlFor="event-until" className="w-48">
            <Input
              id="event-until"
              type="datetime-local"
              value={untilInput}
              onChange={(event) => setUntilInput(event.target.value)}
            />
          </Field>
          <Button type="submit" disabled={searching}>检索历史</Button>
          <Button
            variant="secondary"
            disabled={searching || historyCursor === null}
            onClick={() => void runSearch(true)}
          >
            更早事件
          </Button>
          <span aria-live="polite" className="pb-2 text-xs text-muted-foreground">
            {searchStatus}
          </span>
        </form>

        <div className="flex flex-col gap-2.5">
          <h3 className="text-[13px] font-semibold text-muted-foreground">对话轮次</h3>
          {turnGroups.length ? (
            turnGroups.map(([turnId, entries]) => (
              <TurnCard
                key={turnId}
                turnId={turnId}
                entries={entries}
                expanded={expandedTurnId === turnId}
                onShowTurn={showTurn}
                onCollapse={collapseTurn}
              />
            ))
          ) : (
            <Empty>当前筛选条件下没有对话轮次。</Empty>
          )}
        </div>

        <div className="flex flex-col gap-2.5">
          <h3 className="text-[13px] font-semibold text-muted-foreground">后台事件</h3>
          {backgroundEntries.length ? (
            <div className="flex max-h-[26rem] flex-col gap-1 overflow-y-auto rounded-lg border border-border bg-muted/40 p-3 font-mono text-xs">
              {backgroundEntries.map((entry, index) => (
                <BackgroundRow key={`${entry.seq ?? `live-${entry.at}-${index}`}`} entry={entry} />
              ))}
            </div>
          ) : (
            <Empty>当前筛选条件下没有后台事件。</Empty>
          )}
        </div>
      </CardBody>
    </Card>
  )
}
