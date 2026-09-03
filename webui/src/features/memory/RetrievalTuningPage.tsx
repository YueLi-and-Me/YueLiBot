/**
 * 检索调优中心页：参数白名单、命名 profile 与弱监督评估。
 *
 * 事实召回的排序参数（词面 / 留存度权重、PPR、top-k、池百分位）此前散在
 * 代码常量与配置里，改一处要看哪里生效只能靠重启后翻日志。本页把它们
 * 收进一张白名单参数表，配上三件事：
 *
 * 1. 跑一次评估——从事件账本与提示词转储抽弱监督样本，按当前或指定参数
 *    重放检索，给 nDCG@k、召回条数与「正例被挤掉」条数；
 * 2. profile 的保存 / 应用 / 回滚 / 导出——应用立即生效并进启动横幅；
 * 3. 参数表本身只允许白名单内的键，越界值在后端被拒。
 *
 * 数据来自 `@/hooks/use-retrieval-tuning`；评估是同步请求，样本量受
 * 提示词快照滚动保留量限制，报告里的样本数就是窗口内可评估的回合数。
 */
import { Download, Play, RotateCcw, Save, SlidersHorizontal } from 'lucide-react'
import { useMemo, useState } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import {
  Button,
  Card,
  CardBody,
  Chip,
  Empty,
  ErrorText,
  Loading,
  Metric,
  SectionHeading,
} from '@/components/ui'
import {
  useRetrievalTuning,
  type TuningEvalReport,
  type TuningParamSpec,
} from '@/hooks/use-retrieval-tuning'

/** 参数行的展示顺序：打分组、PPR 组、池与 top-k 组。 */
const PARAM_ORDER = [
  'bm25_weight',
  'retention_weight_floor',
  'ppr_alpha',
  'ppr_hops',
  'pool_score_percentile',
  'fact_recall_limit',
  'recalled_episode_limit',
  'recent_episode_limit',
] as const

/** 当前激活的编辑草稿：参数名到值；空值表示沿用现状。 */
type ParamDraft = Record<string, number | ''>

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
  const [report, setReport] = useState<TuningEvalReport | null>(null)

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

  const runEvaluate = async (params: Record<string, number> | undefined) => {
    setBusy(true)
    setMessage(null)
    try {
      setReport(await evaluate(params ? { params } : {}))
    } catch (err) {
      setMessage(`评估失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
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
    <div className="space-y-4">
      <PageHeader
        eyebrow="YUELI · MEMORY"
        title="检索调优"
        subtitle="事实召回的排序参数、命名 profile 与弱监督评估；当前生效项同时出现在启动横幅里"
        actions={
          <Button variant="ghost" size="sm" onClick={() => void reload()}>
            <RotateCcw className="h-4 w-4" /> 刷新
          </Button>
        }
      />

      {error && <ErrorText>{error}</ErrorText>}
      {message && (
        <Card>
          <CardBody className="py-2 text-sm">{message}</CardBody>
        </Card>
      )}
      {loading && !overview && <Loading>正在读取调优状态…</Loading>}

      {overview && (
        <>
          <Card>
            <CardBody className="space-y-3">
              <SectionHeading
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
            </CardBody>
          </Card>

          <Card>
            <CardBody className="space-y-3">
              <SectionHeading
                title="参数表（白名单）"
                actions={
                  <div className="flex items-center gap-2">
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={busy}
                      onClick={() => void runEvaluate(collectParams(draft, activeOverrides))}
                    >
                      <Play className="h-4 w-4" /> 按草稿评估
                    </Button>
                  </div>
                }
              />
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left text-muted-foreground">
                      <th className="py-2 pr-4 font-medium">参数</th>
                      <th className="py-2 pr-4 font-medium">含义</th>
                      <th className="py-2 pr-4 font-medium">现状 / 生效值</th>
                      <th className="py-2 pr-4 font-medium">取值域</th>
                      <th className="py-2 font-medium">草稿</th>
                    </tr>
                  </thead>
                  <tbody>
                    {orderedParams.map(({ name, spec }) => (
                      <tr key={name} className="border-b last:border-0">
                        <td className="py-2 pr-4 font-mono text-xs">{name}</td>
                        <td className="py-2 pr-4">{spec.label}</td>
                        <td className="py-2 pr-4">
                          {activeOverrides[name] !== undefined
                            ? activeOverrides[name]
                            : (spec.legacy ?? '由配置提供')}
                        </td>
                        <td className="py-2 pr-4 text-muted-foreground">
                          [{spec.min}, {spec.max}]
                        </td>
                        <td className="py-2">
                          <input
                            className="w-24 rounded border bg-transparent px-2 py-1 text-sm"
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
                  onClick={() => void runEvaluate(undefined)}
                >
                  <Play className="h-4 w-4" /> 按当前生效评估
                </Button>
              </div>
            </CardBody>
          </Card>

          <Card>
            <CardBody className="space-y-3">
              <SectionHeading title="已保存的 profile" />
              {overview.profiles.length === 0 ? (
                <Empty>还没有保存过 profile</Empty>
              ) : (
                <div className="space-y-2">
                  {overview.profiles.map((profile) => (
                    <div
                      key={profile.name}
                      className="flex flex-wrap items-center gap-2 rounded border px-3 py-2 text-sm"
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
            </CardBody>
          </Card>

          <Card>
            <CardBody className="space-y-3">
              <SectionHeading title="评估报告" />
              {busy && <Loading>正在重放检索……</Loading>}
              {!busy && !report && <Empty>还没跑过评估；点上面的评估按钮开始</Empty>}
              {report && (
                <div className="space-y-3">
                  <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
                    <Metric label="样本回合" value={report.sampleCount} />
                    <Metric label={`nDCG@${report.k}`} value={report.ndcgMean.toFixed(4)} />
                    <Metric
                      label="召回条数 中位/均值"
                      value={`${report.recallCountMedian}/${report.recallCountMean}`}
                    />
                    <Metric
                      label="被挤掉的正例"
                      value={report.displacedPositiveTotal}
                      detail="该进而没进前 k 的条数"
                    />
                  </div>
                  <p className="text-xs text-muted-foreground">
                    弱监督口径：正例 = 进了提示词且该回合产生了回复的事实；报告度量的是
                    「换参数后既定选择的稳定性」。被挤掉的正例是首要观察项——
                    它直接回答「该进的有没有被顶出去」。
                  </p>
                  {report.perTurn.length > 0 && (
                    <details className="rounded border p-2">
                      <summary className="cursor-pointer text-sm text-muted-foreground">
                        逐回合明细（{report.perTurn.length} 条）
                      </summary>
                      <div className="mt-2 max-h-72 overflow-auto">
                        <table className="w-full text-xs">
                          <thead>
                            <tr className="border-b text-left text-muted-foreground">
                              <th className="py-1 pr-3">turn</th>
                              <th className="py-1 pr-3">场合</th>
                              <th className="py-1 pr-3">正例</th>
                              <th className="py-1 pr-3">重放召回</th>
                              <th className="py-1 pr-3">nDCG</th>
                              <th className="py-1">被挤掉</th>
                            </tr>
                          </thead>
                          <tbody>
                            {report.perTurn.map((row) => (
                              <tr key={row.turnId} className="border-b last:border-0">
                                <td className="py-1 pr-3 font-mono">{row.turnId}</td>
                                <td className="py-1 pr-3">{row.streamKind}</td>
                                <td className="py-1 pr-3">{row.positiveCount}</td>
                                <td className="py-1 pr-3">{row.recallCount}</td>
                                <td className="py-1 pr-3">{row.ndcg.toFixed(4)}</td>
                                <td className="py-1">{row.displacedPositive}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </details>
                  )}
                  <p className="text-xs text-muted-foreground">
                    生成于 {new Date(report.generatedAt).toLocaleString()}；参数：
                    {Object.keys(report.params).length > 0
                      ? Object.entries(report.params)
                          .map(([key, value]) => `${key}=${value}`)
                          .join('、')
                      : '（default，无覆盖）'}
                  </p>
                </div>
              )}
            </CardBody>
          </Card>
        </>
      )}
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
