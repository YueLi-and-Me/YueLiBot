/**
 * 检索调优中心页：参数白名单、命名 profile 与弱监督评估。
 *
 * 事实召回的排序参数（词面 / 留存度权重、PPR、top-k、池百分位）此前散在
 * 代码常量与配置里，改一处要看哪里生效只能靠重启后翻日志。本页把它们
 * 收进一张白名单参数表，配上三件事：
 *
 * 1. 评估——重放事件账本里的召回留痕：检索词是她当时实际用的（本轮消息
 *    与会话印象），正例是当时实际进了提示词的记忆，按参数重排后打分。界面默认
 *    「现行 vs 草稿」一键对照：先后跑两次评估，按回合配对后用大白话给出
 *    「更接近 / 更远 / 差不多」的结论；也可以只跑现行参数看单份报告；
 * 2. profile 的保存 / 应用 / 回滚 / 导出——应用立即生效并进启动横幅；
 * 3. 参数表本身只允许白名单内的键，越界值在后端被拒。
 *
 * 评估分数只衡量「和她以往实际用的记忆像不像」，不衡量「挑得对不对」，
 * 页面文案必须保持这个口径，不得把「更接近」写成「更好」。
 *
 * 数据来自 `@/hooks/use-retrieval-tuning`，对照计算在 `./tuning-compare`；
 * 评估是同步请求，样本量受事件账本保留时长限制。
 */
import { Download, Minus, Play, RotateCcw, Save, TrendingDown, TrendingUp } from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'

import { PageFrame } from '@/components/layout/PageFrame'
import { Pane, PaneHeader, PaneTitle, Panes, ScrollArea } from '@/components/layout/panes'
import {
  Button,
  Chip,
  Empty,
  ErrorText,
  Loading,
} from '@/components/ui'
import {
  useRetrievalTuning,
  type TuningEvalReport,
  type TuningParamSpec,
} from '@/hooks/use-retrieval-tuning'
import { displayValue } from '@/lib/format'

import { compareReports, NOISE_BAND, sameParams, type ReportComparison } from './tuning-compare'

/** 参数行的展示顺序：打分组、PPR 组、池与 top-k 组、重排下限。 */
const PARAM_ORDER = [
  'bm25_weight',
  'retention_weight_floor',
  'ppr_alpha',
  'ppr_hops',
  'pool_score_percentile',
  'fact_recall_limit',
  'recalled_episode_limit',
  'recent_episode_limit',
  'rerank_min_score',
] as const

/** 当前激活的编辑草稿：参数名到值；空值表示沿用现状。 */
type ParamDraft = Record<string, number | ''>

/** 评估区展示的内容：只跑了现行参数，或现行与草稿的对照。 */
type EvalResult =
  | { mode: 'single'; report: TuningEvalReport }
  | {
      mode: 'compare'
      baseline: TuningEvalReport
      draft: TuningEvalReport
      /** 两次评估没有共同回合时为 `null`。 */
      comparison: ReportComparison | null
    }

export function RetrievalTuningPage() {
  const {
    overview,
    error,
    loading,
    reload,
    saveProfile,
    applyProfile,
    rollback,
    exportProfile,
    evaluate,
  } = useRetrievalTuning()

  const [draft, setDraft] = useState<ParamDraft>({})
  const [profileName, setProfileName] = useState('')
  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<EvalResult | null>(null)
  /** 评估进行中的步骤说明；对照要先后跑两次，需要告诉用户走到哪一步。 */
  const [progress, setProgress] = useState<string | null>(null)

  const whitelist = overview?.whitelist ?? {}
  const activeName = overview?.active.profile ?? 'default'
  const activeOverrides = overview?.active.overrides ?? {}

  const orderedParams = useMemo(
    () =>
      PARAM_ORDER.filter((name) => whitelist[name]).map((name) => ({
        name,
        spec: whitelist[name] as TuningParamSpec,
      })),
    [whitelist],
  )

  const draftValue = (name: string): string => {
    const edited = draft[name]
    if (edited !== undefined && edited !== '') return String(edited)
    if (activeOverrides[name] !== undefined) return String(activeOverrides[name])
    return ''
  }

  const runCurrent = async () => {
    setBusy(true)
    setMessage(null)
    setProgress('正在用现行参数，把最近的回合重新挑一遍记忆……')
    try {
      setResult({ mode: 'single', report: await evaluate({}) })
    } catch (err) {
      setMessage(`评估失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
      setProgress(null)
    }
  }

  const runCompare = async () => {
    const draftParams = collectParams(draft, activeOverrides)
    if (sameParams(draftParams, activeOverrides)) {
      setMessage('草稿和现行参数一样：先在参数表的「草稿」列改几个值，再来对比')
      return
    }
    setBusy(true)
    setMessage(null)
    try {
      // 两次评估先后执行，以便逐步提示进度。每次评估各自从事件账本取样本，
      // 期间可能有新回合进入窗口，因此对照只比较两份报告共有的回合。
      setProgress('第 1 步，共 2 步：用现行参数把最近的回合重新挑一遍记忆……')
      const baseline = await evaluate({})
      setProgress('第 2 步，共 2 步：用草稿参数把同一批回合再挑一遍……')
      const drafted = await evaluate({ params: draftParams })
      setResult({
        mode: 'compare',
        baseline,
        draft: drafted,
        comparison: compareReports(baseline, drafted),
      })
    } catch (err) {
      setMessage(`对比失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
      setProgress(null)
    }
  }

  const handleSave = async () => {
    const name = profileName.trim()
    if (!name) {
      setMessage('profile 名不能为空')
      return
    }
    const params: Record<string, number> = {}
    for (const [key, value] of Object.entries(draft)) {
      if (value !== '') params[key] = Number(value)
    }
    setBusy(true)
    setMessage(null)
    try {
      await saveProfile(name, params)
      setMessage(`profile「${name}」已保存`)
    } catch (err) {
      setMessage(`保存被拒绝：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
    }
  }

  const handleApply = async (name: string) => {
    setBusy(true)
    setMessage(null)
    try {
      await applyProfile(name)
      setDraft({})
      setMessage(`profile「${name}」已生效，检索链路即时切换`)
    } catch (err) {
      setMessage(`应用失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
    }
  }

  const handleRollback = async () => {
    setBusy(true)
    setMessage(null)
    try {
      await rollback()
      setDraft({})
      setMessage('已回滚到 default，检索链路回到配置初值')
    } catch (err) {
      setMessage(`回滚失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
    }
  }

  const handleExport = async (name: string) => {
    try {
      const exported = await exportProfile(name)
      const blob = new Blob([JSON.stringify(exported, null, 2)], {
        type: 'application/json',
      })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `retrieval-profile-${name}.json`
      link.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      setMessage(`导出失败：${err instanceof Error ? err.message : String(err)}`)
    }
  }

  return (
    <PageFrame
      hint="事实召回的排序参数、命名 profile 与弱监督评估；当前生效项同时出现在启动横幅里"
      actions={
        <Button variant="secondary" onClick={() => void reload()}>
          <RotateCcw className="size-4" aria-hidden="true" />
          刷新
        </Button>
      }
    >
      <Panes>
        <Pane aria-label="参数与评估">
          <PaneHeader>
            <PaneTitle>参数与评估</PaneTitle>
          </PaneHeader>
          <ScrollArea>
      {error ? <div className="px-6 pt-3"><ErrorText>{error}</ErrorText></div> : null}
      {message ? (
        <div className="border-b border-border bg-primary-soft px-6 py-2.5 text-[13px]">{message}</div>
      ) : null}
      {loading && !overview ? <Loading>正在读取调优状态…</Loading> : null}

      {overview && (
        <>
          <section className="space-y-3 border-b border-border px-6 py-5">
              <SectionTitle
                title="当前生效"
                actions={
                  <div className="flex items-center gap-2">
                    <Chip label="profile" value={activeName} />
                    {activeName !== 'default' && (
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={busy}
                        onClick={() => void handleRollback()}
                      >
                        回滚到 default
                      </Button>
                    )}
                  </div>
                }
              />
              <p className="text-sm text-muted-foreground">
                覆盖 {Object.keys(activeOverrides).length} 项参数
                {Object.keys(activeOverrides).length > 0 &&
                  `：${Object.keys(activeOverrides).join('、')}`}
                。应用新 profile 后立即生效，无需重启。
              </p>
          </section>

          <section className="space-y-3 border-b border-border px-6 py-5">
              <SectionTitle
                title="可调的参数"
                actions={
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      disabled={busy}
                      onClick={() => void runCompare()}
                    >
                      <Play className="h-4 w-4" /> 对比草稿与现行
                    </Button>
                  </div>
                }
              />
              <p className="text-[13px] text-muted-foreground">
                {'在「草稿」列填想试的值，留空表示不改；然后点「对比草稿与现行」，看换成草稿后她挑记忆的结果会怎么变。'}
              </p>
              <div className="overflow-x-auto">
                {/* 中文含义作主名、参数名降为副行：窄屏下四列也不会把含义挤成一字一行。 */}
                <table className="w-full text-[13px]">
                  <thead>
                    <tr className="border-b text-left text-xs text-muted-foreground">
                      <th className="py-2 pr-4 font-medium">参数</th>
                      <th className="py-2 pr-4 font-medium whitespace-nowrap">现在的值</th>
                      <th className="py-2 pr-4 font-medium whitespace-nowrap">可填范围</th>
                      <th className="py-2 font-medium">草稿</th>
                    </tr>
                  </thead>
                  <tbody>
                    {orderedParams.map(({ name, spec }) => (
                      <tr key={name} className="border-b border-border-subtle last:border-0">
                        <td className="py-2 pr-4">
                          <span className="block font-medium">{spec.label}</span>
                          <span className="block font-mono text-[11px] break-all text-subtle-foreground">{name}</span>
                        </td>
                        <td className="py-2 pr-4 whitespace-nowrap tabular-nums">
                          {activeOverrides[name] !== undefined
                            ? activeOverrides[name]
                            : (spec.legacy ?? '跟随月璃设置')}
                        </td>
                        <td className="py-2 pr-4 whitespace-nowrap text-muted-foreground tabular-nums">
                          {spec.min} ~ {spec.max}
                        </td>
                        <td className="py-2">
                          <input
                            className="w-20 rounded border bg-transparent px-2 py-1 text-sm"
                            type="number"
                            min={spec.min}
                            max={spec.max}
                            step={spec.kind === 'int' ? 1 : 0.05}
                            value={draftValue(name)}
                            placeholder="不改"
                            onChange={(event) =>
                              setDraft((prev) => ({
                                ...prev,
                                [name]: event.target.value === '' ? '' : Number(event.target.value),
                              }))
                            }
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <input
                  className="w-48 rounded border bg-transparent px-2 py-1 text-sm"
                  value={profileName}
                  placeholder="新 profile 名"
                  onChange={(event) => setProfileName(event.target.value)}
                />
                <Button size="sm" variant="secondary" disabled={busy} onClick={() => void handleSave()}>
                  <Save className="h-4 w-4" /> 保存草稿为 profile
                </Button>
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={busy}
                  onClick={() => void runCurrent()}
                >
                  <Play className="h-4 w-4" /> 只看现行参数
                </Button>
              </div>
          </section>

          <section className="space-y-3 border-b border-border px-6 py-5">
              <SectionTitle title="评估结果" />
              {busy && progress ? <Loading>{progress}</Loading> : null}
              {!busy && !result && (
                <Empty>
                  还没跑过。在上面的草稿列改几个参数，点「对比草稿与现行」，就能看到换成草稿后她挑记忆的结果会怎么变。
                </Empty>
              )}
              {!busy && result?.mode === 'single' && <SingleReport report={result.report} />}
              {!busy && result?.mode === 'compare' && (
                <CompareReport
                  baseline={result.baseline}
                  draft={result.draft}
                  comparison={result.comparison}
                  whitelist={whitelist}
                />
              )}
              {!busy && result && <ReadingGuide />}
          </section>
        </>
      )}
          </ScrollArea>
        </Pane>
        {overview ? (
          <Pane width="inspector" aria-label="已保存的 profile">
            <PaneHeader>
              <PaneTitle count={overview.profiles.length}>已保存的 profile</PaneTitle>
            </PaneHeader>
            <ScrollArea className="px-6 py-2">
              {overview.profiles.length === 0 ? (
                <Empty>还没有保存过 profile</Empty>
              ) : (
                <div>
                  {overview.profiles.map((profile) => (
                    <div
                      key={profile.name}
                      className="flex flex-wrap items-center gap-2 border-b border-border-subtle py-3 text-[13px] last:border-b-0"
                    >
                      <span className="font-mono text-xs">{profile.name}</span>
                      {profile.name === activeName && (
                        <Chip label="状态" value="生效中" />
                      )}
                      <span className="text-muted-foreground">
                        {Object.keys(profile.params).length} 项覆盖
                        {Object.keys(profile.params).length > 0 &&
                          `：${Object.entries(profile.params)
                            .map(([key, value]) => `${key}=${value}`)
                            .join('、')}`}
                      </span>
                      <span className="ml-auto flex items-center gap-2">
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busy || profile.name === activeName}
                          onClick={() => void handleApply(profile.name)}
                        >
                          应用
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => void handleExport(profile.name)}
                        >
                          <Download className="h-4 w-4" /> 导出
                        </Button>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </ScrollArea>
          </Pane>
        ) : null}
      </Panes>
    </PageFrame>
  )
}

/** 三种对照结论的文案与图标。「更接近」只代表和以往挑法更像，文案不得写成「更好」。 */
const VERDICTS = {
  closer: {
    icon: TrendingUp,
    title: '草稿挑出的记忆，和她实际用过的更接近',
    note: '换成草稿后，她挑记忆的结果比现在更贴近她以往的挑法。这不等于挑得更对。',
  },
  farther: {
    icon: TrendingDown,
    title: '草稿挑出的记忆，和她实际用过的差得更远',
    note: '换成草稿后，她会挑出不少和以往不同的记忆。不同不一定是坏事，上线前先确认这是你想要的变化。',
  },
  similar: {
    icon: Minus,
    title: '草稿和现行参数挑出的记忆差不多',
    note: `平均吻合度变化不到 ${NOISE_BAND}，属于正常波动；换成草稿，她挑记忆的结果基本不变。`,
  },
} as const

/** 吻合度（nDCG）保留三位小数：差不多的判定线是 0.02，两位小数会把临界值四舍五入得自相矛盾。 */
function score(value: number): string {
  return value.toFixed(3)
}

/** 带正负号的吻合度变化。 */
function signed(value: number): string {
  return `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(3)}`
}

/** 报告生成时刻（ISO 8601）转为本地时间。 */
function generatedAt(iso: string): string {
  return new Date(iso).toLocaleString('zh-CN')
}

/**
 * 列出草稿相对现行参数改了哪些项，用参数的中文含义展示。
 *
 * @param before 现行参数报告的覆盖表。
 * @param after 草稿报告的覆盖表。
 * @param whitelist 参数白名单，提供中文含义与配置现状值。
 * @returns 形如「词面相关度权重 1 → 0.7」的条目，按参数表顺序排列。
 */
function paramChanges(
  before: Record<string, number>,
  after: Record<string, number>,
  whitelist: Record<string, TuningParamSpec>,
): string[] {
  const keys = new Set([...Object.keys(before), ...Object.keys(after)])
  const order = (key: string) => {
    const index = PARAM_ORDER.indexOf(key as (typeof PARAM_ORDER)[number])
    return index === -1 ? PARAM_ORDER.length : index
  }
  return [...keys]
    .filter((key) => before[key] !== after[key])
    .sort((left, right) => order(left) - order(right))
    .map((key) => {
      const spec = whitelist[key]
      // 覆盖表里没有的项按配置现状走；现状由配置文件提供时白名单的 legacy 为 null。
      const fallback = spec?.legacy ?? '跟随月璃设置'
      return `${spec?.label ?? key} ${before[key] ?? fallback} → ${after[key] ?? fallback}`
    })
}

/**
 * 评估结果里的一行读数：左列固定宽度放名称，右列放读数与一句说明，
 * 所有读数左对齐在同一条竖线上。须放在 `<dl>` 内。
 *
 * @param props.label 读数名称。
 * @param props.value 读数，可以是一整句大白话。
 * @param props.detail 读数下方的说明，可选。
 * @returns 一行读数。
 */
function Reading({ label, value, detail }: { label: string; value: ReactNode; detail?: string }) {
  return (
    <div className="grid grid-cols-[6.5rem_minmax(0,1fr)] gap-x-4 border-b border-border-subtle py-2 last:border-b-0">
      <dt className="text-[13px] text-muted-foreground">{label}</dt>
      <dd className="min-w-0">
        <span className="text-[13px] font-medium tabular-nums">{value}</span>
        {detail ? <span className="mt-0.5 block text-xs text-muted-foreground">{detail}</span> : null}
      </dd>
    </div>
  )
}

/**
 * 只用现行参数跑一次时的读数。
 *
 * @param props.report 评估报告。
 * @returns 报告区内容。
 */
function SingleReport({ report }: { report: TuningEvalReport }) {
  return (
    <div className="space-y-3">
      <p className="text-[13px]">
        用现行参数，把最近 {report.sample_count} 次她回了话的对话重新挑了一遍记忆。
      </p>
      <dl>
        <Reading
          label="重演的回合"
          value={`${report.sample_count} 次`}
          detail={`共重演 ${report.replayed_count} 次，其中她回了话、当时也用上了记忆的回合参与打分；数量取决于事件记录保留了多久`}
        />
        <Reading
          label="跟当时对不上"
          value={`${report.reconcile_mismatched} / ${report.replayed_count} 次`}
          detail="按她当时的检索词重新找一遍候选记忆，和当时留下的记录逐条比，顺序或条目有出入的回合。多半是这些记忆后来又被用过、强度变了；这个数越大，吻合度越要打折看"
        />
        <Reading
          label="吻合度"
          value={`${score(report.ndcg_mean)}（满分 1）`}
          detail="重新挑出的记忆和她当时实际用的有多接近"
        />
        <Reading
          label="每回合挑出"
          value={`中位 ${report.recall_count_median} 条 · 平均 ${report.recall_count_mean} 条`}
        />
        <Reading
          label="漏挑的记忆"
          value={`${report.displaced_positive_total} 条`}
          detail="当时用上了、这次没挑上的，所有回合加起来"
        />
      </dl>
      {report.per_turn.length > 0 && (
        <details className="rounded-md border border-border p-2">
          <summary className="cursor-pointer text-[13px] text-muted-foreground">
            逐个回合看（{report.per_turn.length} 个）
          </summary>
          <div className="mt-2 max-h-72 overflow-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b text-left text-muted-foreground">
                  <th className="py-1 pr-3 font-medium">回合编号</th>
                  <th className="py-1 pr-3 font-medium">会话</th>
                  <th className="py-1 pr-3 font-medium">当时用了</th>
                  <th className="py-1 pr-3 font-medium">这次挑出</th>
                  <th className="py-1 pr-3 font-medium">吻合度</th>
                  <th className="py-1 font-medium">漏挑</th>
                </tr>
              </thead>
              <tbody>
                {report.per_turn.map((row) => (
                  <tr key={row.turn_id} className="border-b border-border-subtle last:border-0">
                    <td className="py-1 pr-3 font-mono">{row.turn_id}</td>
                    <td className="py-1 pr-3">{displayValue(row.stream_kind)}</td>
                    <td className="py-1 pr-3">{row.positive_count} 条</td>
                    <td className="py-1 pr-3">{row.recall_count} 条</td>
                    <td className="py-1 pr-3 font-mono">{score(row.ndcg)}</td>
                    <td className="py-1">{row.displaced_positive} 条</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
      <p className="text-xs text-subtle-foreground">生成于 {generatedAt(report.generated_at)}</p>
    </div>
  )
}

/**
 * 现行参数与草稿的对照：先给一句话结论，再给支撑它的几个数和变化最大的回合。
 *
 * @param props.baseline 现行参数的报告。
 * @param props.draft 草稿参数的报告。
 * @param props.comparison 按回合配对的对照结果；没有共同回合时为 `null`。
 * @param props.whitelist 参数白名单，用于把参数名换成中文含义。
 * @returns 报告区内容。
 */
function CompareReport({
  baseline,
  draft,
  comparison,
  whitelist,
}: {
  baseline: TuningEvalReport
  draft: TuningEvalReport
  comparison: ReportComparison | null
  whitelist: Record<string, TuningParamSpec>
}) {
  if (!comparison) {
    return <ErrorText>两次评估没有共同的回合，无法对比。稍后再点一次「对比草稿与现行」。</ErrorText>
  }
  const verdict = VERDICTS[comparison.verdict]
  const Icon = verdict.icon
  const changes = paramChanges(baseline.params, draft.params, whitelist)
  return (
    <div className="space-y-3">
      <div className="rounded-md border border-border bg-nav-active px-4 py-3">
        <p className="flex items-center gap-2 text-sm font-semibold">
          <Icon className="size-4 flex-none" aria-hidden="true" />
          {verdict.title}
        </p>
        <p className="mt-1 text-[13px] text-muted-foreground">{verdict.note}</p>
      </div>
      <dl>
        <Reading label="草稿改了" value={changes.join('；')} />
        <Reading
          label="吻合度"
          value={`${score(comparison.baselineMean)} → ${score(comparison.draftMean)}（${signed(comparison.delta)}）`}
          detail={`现行 → 草稿；变化不到 ${NOISE_BAND} 算正常波动`}
        />
        <Reading
          label="逐个回合看"
          value={`更接近 ${comparison.closerTurns} · 更远 ${comparison.fartherTurns} · 没变 ${comparison.unchangedTurns}`}
          detail={`两次都重演到的回合共 ${comparison.pairedTurns} 个`}
        />
        <Reading
          label="漏挑的记忆"
          value={`${comparison.baselineMissed} → ${comparison.draftMissed} 条`}
          detail="当时用上了、重演时没挑上的，所有回合加起来"
        />
      </dl>
      {comparison.changed.length > 0 ? (
        <details className="rounded-md border border-border p-2">
          <summary className="cursor-pointer text-[13px] text-muted-foreground">
            有变化的回合（{comparison.changed.length} 个，变化大的在前）
          </summary>
          <div className="mt-2 max-h-72 overflow-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b text-left text-muted-foreground">
                  <th className="py-1 pr-3 font-medium">回合编号</th>
                  <th className="py-1 pr-3 font-medium">会话</th>
                  <th className="py-1 pr-3 font-medium">现行吻合度</th>
                  <th className="py-1 pr-3 font-medium">草稿吻合度</th>
                  <th className="py-1 font-medium">变化</th>
                </tr>
              </thead>
              <tbody>
                {comparison.changed.map((row) => (
                  <tr key={row.turnId} className="border-b border-border-subtle last:border-0">
                    <td className="py-1 pr-3 font-mono">{row.turnId}</td>
                    <td className="py-1 pr-3">{displayValue(row.streamKind)}</td>
                    <td className="py-1 pr-3 font-mono">{score(row.baseline)}</td>
                    <td className="py-1 pr-3 font-mono">{score(row.draft)}</td>
                    <td className="py-1 font-mono">{signed(row.delta)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ) : (
        <p className="text-[13px] text-muted-foreground">没有哪个回合的结果发生变化。</p>
      )}
      <p className="text-xs text-subtle-foreground">生成于 {generatedAt(draft.generated_at)}</p>
    </div>
  )
}

/**
 * 读数说明：每份结果下方都显示，防止把单个分数或「更接近」误读成好坏。
 *
 * @returns 说明框。
 */
function ReadingGuide() {
  return (
    <div className="rounded-md border border-border px-4 py-3 text-[13px] text-muted-foreground">
      <p className="font-medium text-foreground">怎么读这些数</p>
      <ul className="mt-1.5 list-disc space-y-1 pl-5">
        <li>
          {'吻合度单看没有意义。重演用的是她当时的检索词，但那些记忆的强度后来又变过（用过的会变强、久不用的会变淡），当时在场的人也只能按聊天记录估，所以即使参数不变，分数也到不了 1。只拿两组参数的分数对比。'}
        </li>
        <li>
          {'分数高只说明和她以往的挑法更像，不说明挑得更对。她以往用的记忆本来就是现行参数挑出来的，所以这里真正能看的是：换成草稿后，她挑记忆的结果会变多少。'}
        </li>
        <li>平均吻合度变化不到 {NOISE_BAND}，算正常波动。</li>
        <li>真要换参数，还得看换上以后她聊天的实际表现。</li>
      </ul>
    </div>
  )
}

/**
 * 渲染面板内分区的标题行。
 *
 * @param props.title 分区标题。
 * @param props.actions 标题右侧的操作，可选。
 * @returns 标题行。
 */
function SectionTitle({ title, actions }: { title: string; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 className="text-[13px] font-semibold">{title}</h2>
      {actions}
    </div>
  )
}

/** 把草稿折叠成评估用的参数集：空草稿沿用当前生效覆盖。 */
function collectParams(
  draft: ParamDraft,
  activeOverrides: Record<string, number>,
): Record<string, number> {
  const params: Record<string, number> = { ...activeOverrides }
  for (const [key, value] of Object.entries(draft)) {
    if (value === '') delete params[key]
    else params[key] = Number(value)
  }
  return params
}
