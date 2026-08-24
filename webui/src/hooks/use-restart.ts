/**
 * 后端重启指令的共用 hook。
 *
 * `POST /system/restart` 由 Electron supervisor 负责拉起新进程；这里只负责
 * 发指令、报结果，并在短暂延迟后刷新页面等新进程就位。重启前的确认弹窗与
 * 未保存改动提示由调用方持有——两个配置页共用同一段发令逻辑，避免两份
 * 行为悄悄漂移。
 */
import { useCallback, useState } from 'react'

import { toast } from '@/components/ui'
import { apiMutate } from '@/lib/api'

/** 刷新页面前的等待毫秒数：给 supervisor 一点拉起新进程的时间。 */
const RELOAD_DELAY_MS = 2200

/**
 * 发送重启指令并反馈结果。
 *
 * @returns `restarting` 是否已发出指令（用于禁用触发按钮），`restartBackend`
 *   执行 POST /system/restart，成功与失败都走全局 toast。
 */
export function useRestart(): { restarting: boolean; restartBackend: () => Promise<void> } {
  const [restarting, setRestarting] = useState(false)

  const restartBackend = useCallback(async () => {
    try {
      await apiMutate<{ ok: boolean }>('/system/restart', 'POST')
      toast.success('已发送重启指令，月璃即将重启…')
      setRestarting(true)
      window.setTimeout(() => window.location.reload(), RELOAD_DELAY_MS)
    } catch (error) {
      toast.error(`重启失败：${error instanceof Error ? error.message : String(error)}`)
    }
  }, [])

  return { restarting, restartBackend }
}

/**
 * 判断草稿相对快照是否有未保存的修改。
 *
 * 两页的草稿都从快照深拷贝而来、随后原位修改，因此键序稳定，
 * JSON 序列化逐一对比即可；任一侧尚未就绪（`null`）时视为无改动。
 *
 * @param draft 当前编辑中的草稿。
 * @param snapshot 后端返回的原始快照。
 * @returns 存在未保存修改时为 `true`。
 */
export function hasDraftChanges(draft: unknown, snapshot: unknown): boolean {
  if (draft === null || draft === undefined || snapshot === null || snapshot === undefined) {
    return false
  }
  return JSON.stringify(draft) !== JSON.stringify(snapshot)
}
