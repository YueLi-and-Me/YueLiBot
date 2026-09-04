/**
 * 只读开发者命令目录的数据状态。
 *
 * 对应后端 /api/developer/commands；页面不提供注册、执行或配置写入能力。
 */
import { useCallback, useEffect, useState } from 'react'

import { apiFetch, UnauthorizedError } from '@/lib/api'
import { useAuth } from './use-auth'

/** 一条可在 owner 私聊中匹配的已注册命令。 */
export interface DeveloperCommandItem {
  name: string
  pattern: string
  description: string
  ownerRequired: boolean
}

/** 开发者命令通道的只读快照。 */
export interface DeveloperCommandsSnapshot {
  enabled: boolean
  ownerRequired: boolean
  commands: DeveloperCommandItem[]
}

/** 读取开发者命令通道状态与目录。 */
export function useDeveloperCommands() {
  const { authenticated } = useAuth()
  const [snapshot, setSnapshot] = useState<DeveloperCommandsSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const reload = useCallback(async () => {
    if (!authenticated) return
    setLoading(true)
    setError(null)
    try {
      setSnapshot(await apiFetch<DeveloperCommandsSnapshot>('/api/developer/commands'))
    } catch (err) {
      if (!(err instanceof UnauthorizedError)) setError(String(err))
    } finally {
      setLoading(false)
    }
  }, [authenticated])

  useEffect(() => {
    void reload()
  }, [reload])

  return { snapshot, error, loading, reload }
}
