/**
 * 人物画像列表与详情的数据加载 hooks。
 *
 * usePersons 读取 `/api/persons` 摘要列表；usePersonProfile 按人物 ID 读取
 * `/api/persons/{id}` 完整画像。两者均为一次性读取（人物数据变化频率低，不做
 * 轮询），401 统一上报认证上下文，404 转换为 `missing` 状态供页面展示专门提示。
 */
import { useEffect, useState } from 'react'

import { apiFetch, NotFoundError, UnauthorizedError } from '@/lib/api'
import type { PersonProfile, PersonsPayload, PersonSummary } from '../../../electron/shared/ipc.ts'
import { useAuth } from './use-auth'

/** usePersons 返回的人物列表状态。 */
interface PersonsState {
  /** 人物摘要列表；读取完成前为空数组。 */
  persons: PersonSummary[]
  /** 是否仍在首次读取中。 */
  loading: boolean
  /** 读取失败的提示文本；成功为空字符串。 */
  error: string
}

/**
 * 读取全部人物摘要。
 *
 * @returns 人物列表、加载态与错误文本。
 */
export function usePersons(): PersonsState {
  const { handleUnauthorized } = useAuth()
  const [persons, setPersons] = useState<PersonSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    apiFetch<PersonsPayload>('/api/persons')
      .then((payload) => {
        if (!cancelled) setPersons(payload.persons)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof UnauthorizedError) {
          handleUnauthorized(err)
        } else {
          setError(`人物列表请求失败：${err instanceof Error ? err.message : String(err)}`)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [handleUnauthorized])

  return { persons, loading, error }
}

/** usePersonProfile 返回的人物详情状态。 */
interface PersonProfileState {
  /** 人物完整画像；读取完成前或失败时为 `null`。 */
  profile: PersonProfile | null
  /** 是否仍在读取中。 */
  loading: boolean
  /** 后端返回 404，即该人物不存在。 */
  missing: boolean
  /** 其他读取失败的提示文本；成功为空字符串。 */
  error: string
}

/**
 * 按人物 ID 读取完整画像。
 *
 * @param personId 人物 ID；`null` 表示路由参数非法，此时直接置为 `missing`。
 * @returns 画像数据、加载态、不存在标记与错误文本。
 */
export function usePersonProfile(personId: number | null): PersonProfileState {
  const { handleUnauthorized } = useAuth()
  const [profile, setProfile] = useState<PersonProfile | null>(null)
  const [loading, setLoading] = useState(true)
  const [missing, setMissing] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    setProfile(null)
    setError('')
    if (personId === null) {
      setMissing(true)
      setLoading(false)
      return
    }
    let cancelled = false
    setMissing(false)
    setLoading(true)
    apiFetch<PersonProfile>(`/api/persons/${personId}`)
      .then((payload) => {
        if (!cancelled) setProfile(payload)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        if (err instanceof UnauthorizedError) {
          handleUnauthorized(err)
        } else if (err instanceof NotFoundError) {
          setMissing(true)
        } else {
          setError(`人物画像请求失败：${err instanceof Error ? err.message : String(err)}`)
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [personId, handleUnauthorized])

  return { profile, loading, missing, error }
}
