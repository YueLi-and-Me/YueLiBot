/** 工具管理页的数据入口；配置快照与运行状态分别读取，保存不触发热重载。 */
import { useCallback, useEffect, useState } from 'react'

import { toast } from '@/components/ui'
import { saveToolsSnapshot } from '@/features/tools/save'
import type { McpConfigResponse, McpServerSettings, McpStatusResponse, McpTestResponse, PluginsResponse } from '@/features/tools/types'
import { useAuth } from '@/hooks/use-auth'
import { UnauthorizedError, apiFetch, apiMutate } from '@/lib/api'

export interface ToolsSnapshot { plugins: PluginsResponse; mcp: McpConfigResponse }

export function useToolsConfig() {
  const { handleUnauthorized } = useAuth()
  const [snapshot, setSnapshot] = useState<ToolsSnapshot | null>(null)
  const [status, setStatus] = useState<McpStatusResponse>({ servers: [] })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    try {
      const [plugins, mcp, runtime] = await Promise.all([
        apiFetch<PluginsResponse>('/tools/plugins'), apiFetch<McpConfigResponse>('/tools/mcp/config'),
        apiFetch<McpStatusResponse>('/tools/mcp/status'),
      ])
      setSnapshot({ plugins, mcp }); setStatus(runtime); setError('')
    } catch (cause) {
      if (cause instanceof UnauthorizedError) handleUnauthorized(cause)
      else setError(cause instanceof Error ? cause.message : String(cause))
    }
  }, [handleUnauthorized])
  useEffect(() => { void load() }, [load])

  const save = async (draft: ToolsSnapshot): Promise<boolean> => {
    if (!snapshot) return false
    setBusy(true)
    try {
      const hint = await saveToolsSnapshot(snapshot, draft, (path, body) => apiMutate(path, 'PUT', body))
      await load()
      toast.success(hint)
      return true
    } catch (cause) {
      if (cause instanceof UnauthorizedError) handleUnauthorized(cause)
      else toast.error(`保存失败：${cause instanceof Error ? cause.message : String(cause)}；已成功写入的配置仍保留`)
      return false
    } finally { setBusy(false) }
  }
  const test = async (server: McpServerSettings): Promise<McpTestResponse | null> => {
    setBusy(true)
    try { return await apiMutate<McpTestResponse>('/tools/mcp/test', 'POST', server) }
    catch (cause) {
      if (cause instanceof UnauthorizedError) handleUnauthorized(cause)
      else toast.error(`测试失败：${cause instanceof Error ? cause.message : String(cause)}`)
      return null
    } finally { setBusy(false) }
  }
  return { snapshot, status, error, busy, save, test, load }
}
