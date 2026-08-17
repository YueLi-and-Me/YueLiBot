/**
 * 模型与厂商工作台的数据状态机。
 *
 * 对应后端 /models/config、/models/test-connection* 与 /models/list* 接口；
 * 表单数据使用后端 snake_case 字段名，避免额外映射。401 统一上报认证上下文。
 */
import { useCallback, useEffect, useState } from 'react'

import { apiFetch, apiMutate, UnauthorizedError } from '@/lib/api'
import { useAuth } from './use-auth'

/** 厂商连接配置，api_key 仅用于保存，读取时为空字符串。 */
export interface ProviderConfig {
  name: string
  kind: string
  base_url: string
  api_key: string
  apiKeySet: boolean
  auth_type: 'bearer' | 'header' | 'query' | 'none'
  auth_name: string
  client_type: 'openai' | 'volcengine'
  app_id: string
  model_list_endpoint: string
  default_headers: Record<string, string>
  default_query: Record<string, string>
  timeout_ms: number
  max_retries: number
  retry_interval_ms: number
}

/** 模型定义。 */
export interface ModelConfig {
  name: string
  model_identifier: string
  api_provider: string
  extra_body: Record<string, unknown>
  reasoning_parse_mode: 'field' | 'tag' | 'none'
  visual: boolean
  temperature: number | null
  max_tokens: number | null
  price_in: number
  price_out: number
  embedding_dim: number
}

/** 一个模型任务的路由配置。 */
export interface TaskConfig {
  model_list: string[]
  selection_strategy: 'sequential' | 'random'
  first_token_timeout_ms: number
  slow_threshold_ms: number
}

/** 一个任务的生成参数。 */
export interface GenerationConfig {
  temperature: number
  max_tokens: number
  enabled?: boolean
}

/** 后端 /models/config 响应形状。 */
export interface ModelConfigSnapshot {
  providers: ProviderConfig[]
  models: ModelConfig[]
  tasks: Record<string, TaskConfig>
  generation: Record<string, GenerationConfig>
  vision_enabled: boolean
  chat_image_enabled: boolean
}

/** 连通性探测结果。 */
export interface ConnectionResult {
  network_ok: boolean
  api_key_valid: boolean | null
  latency_ms: number | null
  http_status: number | null
  error: string | null
}

/** 上游模型列表项。 */
export interface RemoteModel {
  id: string
  name: string
}

/** useModelConfig 返回的工作台状态与操作。 */
interface ModelConfigState {
  snapshot: ModelConfigSnapshot | null
  loading: boolean
  error: string
  status: string
  busy: boolean
  connection: ConnectionResult | null
  remoteModels: RemoteModel[]
  reload: () => void
  save: (snapshot: ModelConfigSnapshot) => Promise<boolean>
  clearStatus: () => void
  testProviderByName: (providerName: string) => Promise<void>
  listModelsByName: (providerName: string) => Promise<void>
  testProviderByFields: (fields: Record<string, string>) => Promise<void>
  listModelsByFields: (fields: Record<string, string>) => Promise<void>
}

/**
 * 维护模型与厂商工作台状态。
 *
 * @returns 快照、操作反馈与探测结果。
 */
export function useModelConfig(): ModelConfigState {
  const { handleUnauthorized } = useAuth()
  const [snapshot, setSnapshot] = useState<ModelConfigSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [status, setStatus] = useState('')
  const [busy, setBusy] = useState(false)
  const [connection, setConnection] = useState<ConnectionResult | null>(null)
  const [remoteModels, setRemoteModels] = useState<RemoteModel[]>([])

  const reportError = useCallback((prefix: string, err: unknown) => {
    if (err instanceof UnauthorizedError) {
      handleUnauthorized(err)
      return
    }
    setError(`${prefix}：${err instanceof Error ? err.message : String(err)}`)
  }, [handleUnauthorized])

  const reload = useCallback(() => {
    setLoading(true)
    apiFetch<ModelConfigSnapshot>('/models/config')
      .then((payload) => {
        setSnapshot(payload)
        setError('')
      })
      .catch((err: unknown) => reportError('模型配置读取失败', err))
      .finally(() => setLoading(false))
  }, [reportError])

  useEffect(() => {
    reload()
  }, [reload])

  const save = useCallback(async (next: ModelConfigSnapshot) => {
    setBusy(true)
    setStatus('正在校验并保存…')
    try {
      const result = await apiMutate<{ ok: boolean; detail?: string }>(
        '/models/config',
        'PUT',
        next,
      )
      setStatus(result.detail ?? '已保存')
      setSnapshot(next)
      return true
    } catch (err) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else setStatus(`保存失败：${err instanceof Error ? err.message : String(err)}`)
      return false
    } finally {
      setBusy(false)
    }
  }, [handleUnauthorized])

  const runByName = useCallback(async (path: 'test-connection-by-name' | 'list-by-name', name: string) => {
    setBusy(true)
    setStatus(path.startsWith('test') ? '正在测试连通性…' : '正在拉取模型列表…')
    try {
      const payload = await apiFetch<Record<string, unknown>>(
        `/models/${path}?provider_name=${encodeURIComponent(name)}`,
      )
      if (path.startsWith('test')) {
        const item = payload as unknown as ConnectionResult
        setConnection(item)
        setStatus(
          item.network_ok
            ? item.api_key_valid
              ? '连通正常，API Key 有效'
              : '网络可达，但未确认模型列表端点'
            : '连接失败',
        )
      } else {
        const list = Array.isArray(payload.models) ? payload.models as RemoteModel[] : []
        setRemoteModels(list)
        setStatus(`已获取 ${list.length} 个模型`)
      }
    } catch (err) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else setStatus(`探测失败：${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setBusy(false)
    }
  }, [handleUnauthorized])

  const runByFields = useCallback(async (
    path: 'test-connection' | 'list',
    fields: Record<string, string>,
  ) => {
    setBusy(true)
    setStatus(path === 'test-connection' ? '正在测试连通性…' : '正在拉取模型列表…')
    const query = new URLSearchParams(fields)
    try {
      const payload = await apiFetch<Record<string, unknown>>(`/models/${path}?${query.toString()}`)
      if (path === 'test-connection') {
        setConnection(payload as unknown as ConnectionResult)
        setStatus((payload as unknown as ConnectionResult).network_ok ? '连接完成' : '连接失败')
      } else {
        const list = Array.isArray(payload.models) ? payload.models as RemoteModel[] : []
        setRemoteModels(list)
        setStatus(`已获取 ${list.length} 个模型`)
      }
    } catch (err) {
      if (err instanceof UnauthorizedError) handleUnauthorized(err)
      else setStatus(`探测失败：${err instanceof Error ? err.message : String(err)}`)
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
    connection,
    remoteModels,
    reload,
    save,
    clearStatus: useCallback(() => setStatus(''), []),
    testProviderByName: useCallback((name: string) => runByName('test-connection-by-name', name), [runByName]),
    listModelsByName: useCallback((name: string) => runByName('list-by-name', name), [runByName]),
    testProviderByFields: useCallback((fields: Record<string, string>) => runByFields('test-connection', fields), [runByFields]),
    listModelsByFields: useCallback((fields: Record<string, string>) => runByFields('list', fields), [runByFields]),
  }
}
