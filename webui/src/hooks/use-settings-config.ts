/**
 * 月璃设置页的数据状态机。
 *
 * 对应后端 /settings/config 接口；后端在响应中同时返回 settings_schema.json
 * 的字段映射和五个 TOML 的业务值，前端按 schema 动态渲染表单，不写死字段名。
 */
import { useCallback, useEffect, useState } from 'react'

import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'
import { useAuth } from './use-auth'

/** 单个可编辑字段的展示与类型约束。 */
export interface SettingsFieldSchema {
  key: string
  type: 'string' | 'password' | 'textarea' | 'integer' | 'number' | 'boolean'
    | 'enum' | 'multi_enum' | 'string_list' | 'string_map' | 'json' | 'date' | 'time'
  label: string
  help: string
  required?: boolean
  nullable?: boolean
  min?: number
  max?: number
  step?: number
  maxLength?: number
  options?: Array<{ value: string; label: string }>
  only_for_entries?: string[]
}

/** 普通 object 配置段。 */
export interface SettingsObjectSection {
  kind: 'object'
  key: string
  label: string
  description: string
  /** 保留写盘但不在通用可编辑设置页展示。 */
  hidden?: boolean
  fields: SettingsFieldSchema[]
}

/** 以固定 key 为一组的 map 配置段（如 model_tasks、generation）。 */
export interface SettingsMapSection {
  kind: 'map'
  key: string
  label: string
  description: string
  hidden?: boolean
  entries: Array<{ key: string; label: string; description: string }>
  fields: SettingsFieldSchema[]
}

/** TOML array of tables 配置段（如 api_providers、models）。 */
export interface SettingsTableListSection {
  kind: 'table_list'
  key: string
  label: string
  description: string
  hidden?: boolean
  fields: SettingsFieldSchema[]
}

export type SettingsSection = SettingsObjectSection | SettingsMapSection | SettingsTableListSection

/** 单个配置文件的 schema 节点。 */
export interface SettingsFileSchema {
  file: string
  label: string
  description: string
  sections: SettingsSection[]
}

/** 后端 /settings/config 响应形状。 */
export interface SettingsSnapshot {
  schema: { version: number; files: SettingsFileSchema[] }
  values: Record<string, Record<string, unknown>>
}

/** useSettingsConfig 返回的状态与操作。 */
interface SettingsConfigState {
  snapshot: SettingsSnapshot | null
  loading: boolean
  error: string
  status: string
  busy: boolean
  reload: () => void
  save: (values: SettingsSnapshot['values']) => Promise<boolean>
  clearStatus: () => void
}

/**
 * 维护月璃设置页状态。
 *
 * @returns 配置快照、读取/保存反馈与操作函数。
 */
export function useSettingsConfig(): SettingsConfigState {
  const { handleUnauthorized } = useAuth()
  const [snapshot, setSnapshot] = useState<SettingsSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)

  const reportError = useCallback((prefix: string, err: unknown) => {
    if (err instanceof UnauthorizedError) {
      handleUnauthorized(err)
      return
    }
    setError(`${prefix}：${err instanceof Error ? err.message : String(err)}`)
  }, [handleUnauthorized])

  const reload = useCallback(() => {
    setLoading(true)
    apiFetch<SettingsSnapshot>('/settings/config')
      .then((payload) => {
        setSnapshot(payload)
        setError('')
      })
      .catch((err: unknown) => reportError('月璃设置读取失败', err))
      .finally(() => setLoading(false))
  }, [reportError])

  useEffect(() => {
    reload()
  }, [reload])

  const save = useCallback(async (values: SettingsSnapshot['values']) => {
    setBusy(true)
    setStatus('正在校验并保存…')
    try {
      const result = await apiMutate<{ ok: boolean; detail?: string }>(
        '/settings/config',
        'PUT',
        { values },
      )
      setStatus(result.detail ?? '已保存')
      return true
    } catch (err) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else setStatus(`保存失败：${err instanceof Error ? err.message : String(err)}`)
      return false
    } finally {
      setBusy(false)
    }
  }, [handleUnauthorized])

  return {
    snapshot,
    loading,
    error,
    status,
    busy,
    reload,
    save,
    clearStatus: useCallback(() => setStatus(''), []),
  }
}
