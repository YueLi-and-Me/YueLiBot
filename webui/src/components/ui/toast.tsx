/**
 * Toast 全局通知组件，替代各页面样式不一的内联状态条。
 *
 * 采用模块级发布订阅存储：任何位置调用 `toast.success/error/info(文本)` 即可
 * 弹出通知，无需 Context 包装；Toaster 组件在 App 根部挂载一次，负责渲染右下
 * 角堆叠的通知列表。每条通知 3.5 秒后自动消失，入场为滑入 + 淡入动画。
 */
import { CheckCircle2, Info, XCircle } from 'lucide-react'
import { useSyncExternalStore } from 'react'

import { cn } from './cn'

type ToastKind = 'success' | 'error' | 'info'

interface ToastItem {
  id: number
  kind: ToastKind
  message: string
}

/** 通知自动消失时长（毫秒）。 */
const TOAST_DURATION = 3500

let nextId = 1
let items: Array<ToastItem> = []
const listeners = new Set<() => void>()

function emit() {
  for (const listener of listeners) listener()
}

function push(kind: ToastKind, message: string) {
  const id = nextId++
  items = [...items, { id, kind, message }]
  emit()
  setTimeout(() => {
    items = items.filter((item) => item.id !== id)
    emit()
  }, TOAST_DURATION)
}

/**
 * 全局 Toast 调用入口：`toast.success('已保存')`。
 */
export const toast = {
  success: (message: string) => push('success', message),
  error: (message: string) => push('error', message),
  info: (message: string) => push('info', message),
}

const KIND_STYLES: Record<ToastKind, { icon: typeof Info; className: string }> = {
  success: { icon: CheckCircle2, className: 'text-success' },
  error: { icon: XCircle, className: 'text-destructive' },
  info: { icon: Info, className: 'text-primary-strong' },
}

/**
 * 渲染右下角堆叠的 Toast 列表；在 App 根部挂载一次。
 *
 * @returns Toast 容器；无通知时渲染空容器。
 */
export function Toaster() {
  const current = useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => items,
  )
  return (
    <div className="pointer-events-none fixed right-4 bottom-4 z-[60] flex w-80 flex-col gap-2">
      {current.map((item) => {
        const { icon: Icon, className } = KIND_STYLES[item.kind]
        return (
          <div
            key={item.id}
            role="status"
            className={cn(
              'pointer-events-auto flex animate-toast-in items-center gap-2.5 rounded-lg border border-border bg-card px-4 py-3 text-sm text-card-foreground shadow-lifted',
            )}
          >
            <Icon className={cn('size-4 flex-none', className)} aria-hidden="true" />
            <span className="min-w-0 flex-1">{item.message}</span>
          </div>
        )
      })}
    </div>
  )
}
