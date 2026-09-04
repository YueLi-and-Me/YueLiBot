/**
 * 记忆人工管理页的数据 hook。
 *
 * 对应后端 `/api/memory/facts*`、`/api/memory/conflicts*` 与 `/api/memory/operations*`
 * 三组接口：按人物读取事实（可含已失效条目）、同槽冲突组与操作流水。状态类写操作
 * （标失效 / 恢复 / 永久保留 / 人工取代 / 撤销）成功后整体重读，保证三块数据口径
 * 一致；冲突裁决因为多成员组要逐成员调用，是否重读交给页面控制。后端对不允许的
 * 状态转移返回 409（中文 detail），错误原样抛给页面展示。
 */
import { useCallback, useEffect, useState } from 'react'

import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'
import { useAuth } from './use-auth'

/** 一条人物事实，字段与后端 `/api/memory/facts` 响应一一对应。 */
export interface MemoryFact {
  id: number
  kind: string
  content: string
  /** 冲突槽位；无槽位为 `null`，不参与同槽裁决。 */
  slot: string | null
  originKind: string
  strength: number
  /** 留存度，取值 0 到 1；页面按百分比展示。 */
  retention: number
  halfLifeHours: number
  active: boolean
  /** 已被标失效（可恢复）。 */
  invalid: boolean
  /** 永久保留：不参与衰减与清理。 */
  pinned: boolean
  /** 取代该条的新事实 ID；未被取代为 `null`。 */
  supersededBy: number | null
  updatedAt: number
  createdAt: number
  hitCount: number
  lastHitAt: number | null
}

/** 冲突组里的一条成员事实。 */
export interface ConflictMember {
  id: number
  kind: string
  content: string
  originKind: string
  retention: number
  strength: number
  halfLifeHours: number
  updatedAt: number
}

/** 一组同槽冲突：同一人物同一槽位下并存的多条活跃事实；后端保证成员不少于 2 条。 */
export interface ConflictGroup {
  personId: number
  personName?: string
  slot: string
  members: ConflictMember[]
}

/** 一条记忆操作流水。 */
export interface MemoryOperation {
  id: number
  /** 操作发生的毫秒时间戳。 */
  at: number
  /** manual=人工，n4=自动纠错，auto=系统自动。 */
  actor: string
  op: string
  personId: number
  factId: number
  factContent: string
  relatedFactId: number | null
  /** 操作前的状态快照（JSON 对象），撤销时据此回滚。 */
  prev: Record<string, unknown> | null
  /** 撤销该条的操作 ID；未被撤销为 `null`。 */
  undoneBy: number | null
  /** 该条本身是撤销条目时，指向被撤销的操作。 */
  undoOf: number | null
}

/** 写操作的统一回执。 */
interface MutationReceipt {
  ok: boolean
  operationId: number
}

/** 人工取代的回执，附带新事实 ID 与随之暴露的同槽冲突。 */
export interface ReplaceReceipt extends MutationReceipt {
  newFactId: number
  conflictWith: number[]
}

/**
 * 读取并维护记忆人工管理页的三块数据。
 *
 * @param personId 当前选中人物；`null` 表示「全部人物」，此时事实列表为空
 * （事实接口必须按人物取），冲突组与操作流水不按人物过滤。
 * @param includeInvalid 事实列表是否包含已失效条目。
 * @returns 三块数据、加载态、错误文本与全部写操作。
 */
export function useMemoryManage(personId: number | null, includeInvalid: boolean) {
  const { authenticated } = useAuth()
  const [facts, setFacts] = useState<MemoryFact[]>([])
  const [conflicts, setConflicts] = useState<ConflictGroup[]>([])
  const [operations, setOperations] = useState<MemoryOperation[]>([])
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const reload = useCallback(async () => {
    if (!authenticated) return
    setLoading(true)
    setError(null)
    const personQuery = personId === null ? '' : `personId=${personId}`
    try {
      const factsPromise =
        personId === null
          ? Promise.resolve<MemoryFact[]>([])
          : apiFetch<{ personId: number; facts: MemoryFact[] }>(
              `/api/memory/facts?personId=${personId}&includeInvalid=${includeInvalid ? 'true' : 'false'}`,
            ).then((payload) => payload.facts)
      const [nextFacts, nextConflicts, nextOperations] = await Promise.all([
        factsPromise,
        apiFetch<{ groups: ConflictGroup[] }>(
          `/api/memory/conflicts${personQuery ? `?${personQuery}` : ''}`,
        ).then((payload) => payload.groups),
        apiFetch<{ operations: MemoryOperation[] }>(
          `/api/memory/operations?${personQuery ? `${personQuery}&` : ''}limit=50`,
        ).then((payload) => payload.operations),
      ])
      setFacts(nextFacts)
      setConflicts(nextConflicts)
      setOperations(nextOperations)
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) {
        setError(`读取失败：${err instanceof Error ? err.message : String(err)}`)
      }
    } finally {
      setLoading(false)
    }
  }, [authenticated, personId, includeInvalid])

  useEffect(() => {
    void reload()
  }, [reload])

  /** 对单条事实执行状态类操作（标失效 / 恢复 / 永久保留 / 取消永久保留）。 */
  const mutateFact = useCallback(
    async (factId: number, action: 'invalidate' | 'restore' | 'pin' | 'unpin') => {
      await apiMutate<MutationReceipt>(`/api/memory/facts/${factId}/${action}`, 'POST')
      await reload()
    },
    [reload],
  )

  /** 人工取代：以新正文生成新事实，原事实被取代失效。 */
  const replaceFact = useCallback(
    async (factId: number, content: string) => {
      const receipt = await apiMutate<ReplaceReceipt>(
        `/api/memory/facts/${factId}/replace`,
        'POST',
        { content },
      )
      await reload()
      return receipt
    },
    [reload],
  )

  /**
   * 裁决一对冲突：保留 keepFactId，dropFactId 被标失效。
   *
   * 不在此处 reload——多成员组需要对其余成员逐个调用，由页面在全部成功后统一
   * 重读，避免中途刷新掩盖部分失败。
   */
  const resolveConflict = useCallback(async (keepFactId: number, dropFactId: number) => {
    await apiMutate<MutationReceipt>('/api/memory/conflicts/resolve', 'POST', {
      keepFactId,
      dropFactId,
    })
  }, [])

  /** 撤销一条操作流水，按操作前状态回滚。 */
  const undoOperation = useCallback(
    async (operationId: number) => {
      await apiMutate<MutationReceipt>(`/api/memory/operations/${operationId}/undo`, 'POST')
      await reload()
    },
    [reload],
  )

  return {
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
  }
}
