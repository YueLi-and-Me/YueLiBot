/**
 * 确认弹窗组件，替代原生 window.confirm。
 *
 * 基于 Dialog 的轻封装：标题 + 描述 + 取消/确认按钮组；danger 模式把确认按钮
 * 渲染为危险实心红，用于删除、重启等不可逆操作。
 */
import type { ReactNode } from 'react'

import { Button } from './button'
import { Dialog } from './dialog'

interface ConfirmDialogProps {
  /** 是否打开。 */
  open: boolean
  /** 标题，通常是操作名称，如「删除厂商」。 */
  title: string
  /** 描述文本，说明操作后果。 */
  description?: ReactNode
  /** 确认按钮文案，默认「确认」。 */
  confirmText?: string
  /** 取消按钮文案，默认「取消」。 */
  cancelText?: string
  /** 是否为危险操作（确认按钮变红），默认 true。 */
  danger?: boolean
  /** 确认回调。 */
  onConfirm: () => void
  /** 取消/关闭回调。 */
  onCancel: () => void
}

/**
 * 渲染确认弹窗。
 *
 * @param props.open 开关状态。
 * @param props.onConfirm 确认回调。
 * @param props.onCancel 取消回调。
 * @returns 确认弹窗。
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmText = '确认',
  cancelText = '取消',
  danger = true,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      onClose={onCancel}
      title={title}
      width="max-w-sm"
      footer={
        <>
          <Button variant="ghost" onClick={onCancel}>
            {cancelText}
          </Button>
          <Button variant={danger ? 'danger' : 'primary'} onClick={onConfirm}>
            {confirmText}
          </Button>
        </>
      }
    >
      {description ? <p className="text-sm text-muted-foreground">{description}</p> : null}
    </Dialog>
  )
}
