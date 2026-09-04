/**
 * 记忆人工管理页：事实检视与手工修正、同槽冲突裁决、操作流水与撤销。
 *
 * 自动链路（n4 纠错、衰减、取代）之外，部署者需要一个能逐条过目的入口：
 * 看错了的手工标失效或恢复，永远不想衰减的永久保留，写得不对的用新正文
 * 取代；同一槽位并存多条活跃事实时在冲突组里裁决留一条；任何一步做错了，
 * 都能在操作流水里按操作前的状态撤销回去。
 *
 * 数据来自 `@/hooks/use-memory-manage`，人物列表复用 `@/hooks/use-persons`；
 * 后端对不允许的状态转移返回 409（中文 detail），页面原样展示。三块内容
 * 用分段选项卡切换，人物选择器在页头，对三个分区同时生效。
 */
import { RotateCcw } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'

import { PageHeader } from '@/components/layout/PageHeader'
import {
  Button,
  Card,
  CardBody,
  Chip,
  cn,
  ConfirmDialog,
  Dialog,
  Empty,
  ErrorText,
  Loading,
  SectionHeading,
  SegmentedTabs,
  Select,
  Textarea,
  Toggle,
} from '@/components/ui'
import {
  useMemoryManage,
  type ConflictGroup,
  type ConflictMember,
  type MemoryFact,
  type MemoryOperation,
} from '@/hooks/use-memory-manage'
import { usePersons } from '@/hooks/use-persons'
import { dateTime } from '@/lib/format'
import type { PersonSummary } from '../../../../electron/shared/ipc.ts'

/** 页面分区。 */
type Section = 'facts' | 'conflicts' | 'operations'

/** 操作流水里操作者的中文名。 */
const ACTOR_LABELS: Record<string, string> = {
  auto: '系统自动',
  manual: '人工',
  n4: '自动纠错',
}

/** 操作流水里操作类型的中文名。 */
const OP_LABELS: Record<string, string> = {
  adjudicate: '冲突裁决',
  invalidate: '标失效',
  pin: '永久保留',
  replace: '人工取代',
  restore: '恢复',
  supersede: '标记取代',
  undo: '撤销',
  unpin: '取消永久保留',
}

/** 人物选择器里的展示标签。 */
function personLabel(person: PersonSummary): string {
  const name = person.displayName || `#${person.id}`
  return person.kind === 'owner' ? `${name}（主人）` : `${name}（#${person.id}）`
}

/** 从异常中取展示文本。 */
function describeError(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

export function MemoryManagePage() {
  const { persons } = usePersons()
  const [section, setSection] = useState<Section>('facts')
  const [personId, setPersonId] = useState<number | null>(null)
  const [includeInvalid, setIncludeInvalid] = useState(true)
  const {
    facts,
    conflicts,
    operations,
    error,
    loading,
    reload,
    mutateFact,
    replaceFact,
    resolveConflict,
    undoOperation,
  } = useMemoryManage(personId, includeInvalid)

  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [invalidateTarget, setInvalidateTarget] = useState<MemoryFact | null>(null)
  const [replaceTarget, setReplaceTarget] = useState<MemoryFact | null>(null)
  const [replaceText, setReplaceText] = useState('')
  const [resolveTarget, setResolveTarget] = useState<{
    group: ConflictGroup
    keep: ConflictMember
  } | null>(null)
  const [undoTarget, setUndoTarget] = useState<MemoryOperation | null>(null)

  // 人物列表就绪后默认选中第一个人物，让事实区直接有内容；只自动选一次，
  // 之后用户切到「全部人物」时不再被顶回去。
  const autoSelected = useRef(false)
  useEffect(() => {
    const first = persons[0]
    if (!autoSelected.current && first) {
      autoSelected.current = true
      setPersonId(first.id)
    }
  }, [persons])

  /** 冲突组全部成员的 ID 集合，用于事实表里的冲突行高亮。 */
  const conflictFactIds = useMemo(
    () => new Set(conflicts.flatMap((group) => group.members.map((member) => member.id))),
    [conflicts],
  )
  /** 待渲染的冲突组；成员不足 2 条的组不构成冲突，防御性过滤。 */
  const visibleGroups = useMemo(
    () => conflicts.filter((group) => group.members.length >= 2),
    [conflicts],
  )

  const handleFactAction = async (
    fact: MemoryFact,
    action: 'invalidate' | 'restore' | 'pin' | 'unpin',
    okText: string,
  ) => {
    setBusy(true)
    setMessage(null)
    try {
      await mutateFact(fact.id, action)
      setMessage(okText)
    } catch (err) {
      setMessage(`操作被拒绝：${describeError(err)}`)
    } finally {
      setBusy(false)
    }
  }

  const confirmInvalidate = async () => {
    if (!invalidateTarget) return
    const target = invalidateTarget
    setInvalidateTarget(null)
    await handleFactAction(target, 'invalidate', `事实 #${target.id} 已标失效，不再进入召回与提示词`)
  }

  const openReplace = (fact: MemoryFact) => {
    setReplaceTarget(fact)
    setReplaceText(fact.content)
  }

  const confirmReplace = async () => {
    if (!replaceTarget) return
    const content = replaceText.trim()
    if (!content) {
      setMessage('新正文不能为空')
      return
    }
    const target = replaceTarget
    setBusy(true)
    setMessage(null)
    try {
      const receipt = await replaceFact(target.id, content)
      setMessage(
        `事实 #${target.id} 已取代为 #${receipt.newFactId}` +
          (receipt.conflictWith.length > 0
            ? `；与 ${receipt.conflictWith.length} 条事实同槽冲突，请到「冲突组」裁决`
            : ''),
      )
      setReplaceTarget(null)
      setReplaceText('')
    } catch (err) {
      setMessage(`取代被拒绝：${describeError(err)}`)
    } finally {
      setBusy(false)
    }
  }

  const confirmResolve = async () => {
    if (!resolveTarget) return
    const { group, keep } = resolveTarget
    const drops = group.members.filter((member) => member.id !== keep.id)
    setBusy(true)
    setMessage(null)
    try {
      for (const drop of drops) {
        await resolveConflict(keep.id, drop.id)
      }
      await reload()
      setMessage(`已裁决：保留 #${keep.id}，其余 ${drops.length} 条已标失效`)
    } catch (err) {
      setMessage(`裁决被拒绝：${describeError(err)}（可能已有部分成员生效，请刷新确认）`)
    } finally {
      setBusy(false)
      setResolveTarget(null)
    }
  }

  const confirmUndo = async () => {
    if (!undoTarget) return
    const target = undoTarget
    setBusy(true)
    setMessage(null)
    try {
      await undoOperation(target.id)
      setMessage(`操作 #${target.id} 已撤销，相关事实回到操作前状态`)
    } catch (err) {
      setMessage(`撤销被拒绝：${describeError(err)}`)
    } finally {
      setBusy(false)
      setUndoTarget(null)
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        eyebrow="YUELI · MEMORY"
        title="记忆管理"
        subtitle="逐条检视人物事实：标失效与恢复、永久保留、人工取代、同槽冲突裁决，误操作可在流水里撤销"
        actions={
          <>
            <Select
              className="w-52"
              aria-label="选择人物"
              value={personId === null ? '' : String(personId)}
              onChange={(event) =>
                setPersonId(event.target.value === '' ? null : Number(event.target.value))
              }
            >
              <option value="">全部人物</option>
              {persons.map((person) => (
                <option key={person.id} value={person.id}>
                  {personLabel(person)}
                </option>
              ))}
            </Select>
            <Button variant="ghost" size="sm" onClick={() => void reload()}>
              <RotateCcw className="h-4 w-4" /> 刷新
            </Button>
          </>
        }
      />

      <SegmentedTabs<Section>
        tabs={[
          { value: 'facts', label: '人物事实' },
          { value: 'conflicts', label: '冲突组' },
          { value: 'operations', label: '操作流水' },
        ]}
        value={section}
        onChange={setSection}
      />

      {error && <ErrorText>{error}</ErrorText>}
      {message && (
        <Card>
          <CardBody className="py-2 text-sm">{message}</CardBody>
        </Card>
      )}

      {section === 'facts' && (
        <Card>
          <CardBody className="space-y-3">
            <SectionHeading
              title="人物事实"
              actions={
                <div className="flex items-center gap-3">
                  <span className="text-xs text-muted-foreground">粉色行处于待裁决冲突组</span>
                  <Toggle checked={includeInvalid} onChange={setIncludeInvalid} label="显示已失效" />
                </div>
              }
            />
            {personId === null ? (
              <Empty>在右上角选择具体人物后逐条管理；「全部人物」口径只对冲突组与操作流水有效</Empty>
            ) : loading && facts.length === 0 ? (
              <Loading>正在读取事实…</Loading>
            ) : facts.length === 0 ? (
              <Empty>该人物还没有记忆事实</Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left text-muted-foreground">
                      <th className="py-2 pr-4 font-medium">ID</th>
                      <th className="py-2 pr-4 font-medium">正文</th>
                      <th className="py-2 pr-4 font-medium">槽位</th>
                      <th className="py-2 pr-4 font-medium">类型</th>
                      <th className="py-2 pr-4 font-medium">来源</th>
                      <th className="py-2 pr-4 font-medium">留存度</th>
                      <th className="py-2 pr-4 font-medium">状态</th>
                      <th className="py-2 font-medium">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {facts.map((fact) => (
                      <tr
                        key={fact.id}
                        className={cn(
                          'border-b last:border-0',
                          conflictFactIds.has(fact.id) && 'bg-tint-coral/10',
                        )}
                      >
                        <td className="py-2 pr-4 font-mono text-xs">#{fact.id}</td>
                        <td className="max-w-md py-2 pr-4">
                          <span className="line-clamp-2" title={fact.content}>
                            {fact.content}
                          </span>
                        </td>
                        <td className="py-2 pr-4">{fact.slot?.trim() ? fact.slot : '—'}</td>
                        <td className="py-2 pr-4">{fact.kind}</td>
                        <td className="py-2 pr-4">{fact.originKind}</td>
                        <td className="py-2 pr-4 tabular-nums">
                          {Math.round(fact.retention * 100)}%
                        </td>
                        <td className="py-2 pr-4">
                          <span className="flex flex-wrap gap-1">
                            <Chip label="状态" value={fact.invalid ? '已失效' : '活跃'} />
                            {fact.pinned && <Chip label="保留" value="永久保留" />}
                          </span>
                        </td>
                        <td className="py-2">
                          <span className="flex flex-wrap gap-1.5">
                            {fact.invalid ? (
                              <Button
                                size="sm"
                                variant="ghost"
                                disabled={busy}
                                onClick={() =>
                                  void handleFactAction(fact, 'restore', `事实 #${fact.id} 已恢复`)
                                }
                              >
                                恢复
                              </Button>
                            ) : (
                              <>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  disabled={busy}
                                  onClick={() => setInvalidateTarget(fact)}
                                >
                                  标失效
                                </Button>
                                {fact.pinned ? (
                                  <Button
                                    size="sm"
                                    variant="ghost"
                                    disabled={busy}
                                    onClick={() =>
                                      void handleFactAction(
                                        fact,
                                        'unpin',
                                        `事实 #${fact.id} 已取消永久保留，恢复参与衰减`,
                                      )
                                    }
                                  >
                                    取消永久保留
                                  </Button>
                                ) : (
                                  <Button
                                    size="sm"
                                    variant="ghost"
                                    disabled={busy}
                                    onClick={() =>
                                      void handleFactAction(
                                        fact,
                                        'pin',
                                        `事实 #${fact.id} 已永久保留，不再衰减`,
                                      )
                                    }
                                  >
                                    永久保留
                                  </Button>
                                )}
                                <Button
                                  size="sm"
                                  variant="secondary"
                                  disabled={busy}
                                  onClick={() => openReplace(fact)}
                                >
                                  改成…
                                </Button>
                              </>
                            )}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardBody>
        </Card>
      )}

      {section === 'conflicts' && (
        <Card>
          <CardBody className="space-y-3">
            <SectionHeading
              title="冲突组"
              actions={
                <span className="text-xs text-muted-foreground">
                  同一槽位并存多条活跃事实；裁决保留一条，其余标失效
                </span>
              }
            />
            {loading && visibleGroups.length === 0 ? (
              <Loading>正在读取冲突组…</Loading>
            ) : visibleGroups.length === 0 ? (
              <Empty>当前没有待裁决的冲突</Empty>
            ) : (
              <div className="space-y-3">
                {visibleGroups.map((group) => (
                  <div key={`${group.personId}:${group.slot}`} className="space-y-2 rounded border p-3">
                    <p className="text-sm font-medium">
                      {group.personName?.trim() || `人物 #${group.personId}`} · 槽位{' '}
                      {group.slot?.trim() || '—'}
                    </p>
                    <div className="grid gap-2 md:grid-cols-2">
                      {group.members.map((member) => (
                        <div
                          key={member.id}
                          className="flex flex-col gap-2 rounded border bg-muted/30 p-3 text-sm"
                        >
                          <p className="font-mono text-xs text-muted-foreground">#{member.id}</p>
                          <p>{member.content}</p>
                          <p className="text-xs text-muted-foreground">
                            来源 {member.originKind} · 留存 {Math.round(member.retention * 100)}% ·
                            更新于 {dateTime(member.updatedAt)}
                          </p>
                          <div>
                            <Button
                              size="sm"
                              variant="secondary"
                              disabled={busy}
                              onClick={() => setResolveTarget({ group, keep: member })}
                            >
                              保留这条
                            </Button>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardBody>
        </Card>
      )}

      {section === 'operations' && (
        <Card>
          <CardBody className="space-y-3">
            <SectionHeading
              title="操作流水"
              actions={
                <span className="text-xs text-muted-foreground">
                  {personId === null ? '全部人物' : '按当前人物过滤'} · 最近 50 条
                </span>
              }
            />
            {loading && operations.length === 0 ? (
              <Loading>正在读取操作流水…</Loading>
            ) : operations.length === 0 ? (
              <Empty>还没有记忆操作记录</Empty>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-left text-muted-foreground">
                      <th className="py-2 pr-4 font-medium">时间</th>
                      <th className="py-2 pr-4 font-medium">操作者</th>
                      <th className="py-2 pr-4 font-medium">操作</th>
                      <th className="py-2 pr-4 font-medium">事实</th>
                      <th className="py-2 pr-4 font-medium">关联事实</th>
                      <th className="py-2 pr-4 font-medium">状态</th>
                      <th className="py-2 font-medium">撤销</th>
                    </tr>
                  </thead>
                  <tbody>
                    {operations.map((op) => (
                      <tr key={op.id} className="border-b last:border-0">
                        <td className="py-2 pr-4 whitespace-nowrap tabular-nums">
                          {dateTime(op.at)}
                        </td>
                        <td className="py-2 pr-4">{ACTOR_LABELS[op.actor] ?? op.actor}</td>
                        <td className="py-2 pr-4">{OP_LABELS[op.op] ?? op.op}</td>
                        <td className="max-w-72 py-2 pr-4">
                          <span className="font-mono text-xs">#{op.factId}</span>
                          {op.factContent && (
                            <span
                              className="block truncate text-xs text-muted-foreground"
                              title={op.factContent}
                            >
                              {op.factContent}
                            </span>
                          )}
                        </td>
                        <td className="py-2 pr-4 font-mono text-xs">
                          {op.relatedFactId === null ? '—' : `#${op.relatedFactId}`}
                        </td>
                        <td className="py-2 pr-4">
                          <span className="flex flex-wrap gap-1">
                            {op.undoneBy !== null && <Chip label="状态" value="已撤销" />}
                            {op.undoOf !== null && <Chip label="状态" value="撤销条目" />}
                          </span>
                        </td>
                        <td className="py-2">
                          {op.undoneBy === null && op.op !== 'undo' && (
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={busy}
                              onClick={() => setUndoTarget(op)}
                            >
                              撤销
                            </Button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardBody>
        </Card>
      )}

      <ConfirmDialog
        open={invalidateTarget !== null}
        title={`标失效事实 #${invalidateTarget?.id ?? ''}`}
        description="标失效后该事实不再进入召回与提示词，但保留记录，随时可以恢复。"
        confirmText="标失效"
        onConfirm={() => void confirmInvalidate()}
        onCancel={() => setInvalidateTarget(null)}
      />

      <Dialog
        open={replaceTarget !== null}
        onClose={() => setReplaceTarget(null)}
        title={`改成新事实（取代 #${replaceTarget?.id ?? ''}）`}
        description="原事实被取代并失效，新正文作为一条新事实入库；误改可在操作流水里撤销。"
        footer={
          <>
            <Button variant="ghost" onClick={() => setReplaceTarget(null)}>
              取消
            </Button>
            <Button
              variant="primary"
              disabled={busy || !replaceText.trim()}
              onClick={() => void confirmReplace()}
            >
              确认取代
            </Button>
          </>
        }
      >
        <Textarea
          className="min-h-28"
          placeholder="新的事实正文"
          value={replaceText}
          onChange={(event) => setReplaceText(event.target.value)}
        />
      </Dialog>

      <ConfirmDialog
        open={resolveTarget !== null}
        title={`裁决冲突：保留 #${resolveTarget?.keep.id ?? ''}`}
        description={
          resolveTarget
            ? `保留该成员，组内其余 ${
                resolveTarget.group.members.length - 1
              } 条将被标失效；裁决可在操作流水里撤销。`
            : ''
        }
        confirmText="保留这条"
        onConfirm={() => void confirmResolve()}
        onCancel={() => setResolveTarget(null)}
      />

      <ConfirmDialog
        open={undoTarget !== null}
        title={`撤销操作 #${undoTarget?.id ?? ''}`}
        description="按操作前的状态回滚该次变更；撤销本身也会记入操作流水。"
        confirmText="确认撤销"
        danger={false}
        onConfirm={() => void confirmUndo()}
        onCancel={() => setUndoTarget(null)}
      />
    </div>
  )
}
