/**
 * 弹窗基础组件，替代各页面手写的 fixed 遮罩盒子。
 *
 * 通过 createPortal 挂载到 body，提供：ESC 关闭、遮罩点击关闭、简易焦点陷阱
 * （Tab 在弹窗内循环）、背景滚动锁定与 scale+fade 入场动画；遮罩带轻微
 * backdrop-blur 营造景深。确认类弹窗请使用 confirm-dialog.tsx 的封装。
 */
import { X } from 'lucide-react'
import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

import { cn } from './cn'

/** 弹窗内可聚焦元素的选择器，用于焦点陷阱。 */
const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

interface DialogProps {
  /** 是否打开；false 时不渲染任何内容。 */
  open: boolean
  /** 关闭回调（ESC、遮罩点击、关闭按钮都会触发）。 */
  onClose: () => void
  /** 弹窗标题。 */
  title: ReactNode
  /** 标题下方的辅助说明，可选。 */
  description?: ReactNode
  /** 底部操作区（通常为按钮组），可选。 */
  footer?: ReactNode
  /** 内容宽度类名，默认 `max-w-lg`。 */
  width?: string
  /** 内容区的追加类名。 */
  className?: string
  children: ReactNode
}

/**
 * 渲染模态弹窗。
 *
 * @param props.open 开关状态。
 * @param props.onClose 关闭回调。
 * @returns portal 弹窗；未打开时返回 null。
 */
export function Dialog({
  open,
  onClose,
  title,
  description,
  footer,
  width = 'max-w-lg',
  className,
  children,
}: DialogProps) {
  const contentRef = useRef<HTMLDivElement>(null)

  // ESC 关闭 + 焦点陷阱 + 初始聚焦 + 背景滚动锁定
  useEffect(() => {
    if (!open) return

    const content = contentRef.current
    // 初始聚焦弹窗容器，保证键盘与读屏从弹窗开始
    content?.focus()

    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab' || !content) return
      // 焦点陷阱：Tab 在弹窗内可聚焦元素间循环
      const focusables = Array.from(content.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
      if (focusables.length === 0) {
        event.preventDefault()
        return
      }
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (!first || !last) {
        event.preventDefault()
        return
      }
      const active = document.activeElement
      if (event.shiftKey && (active === first || !content.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && active === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      document.body.style.overflow = previousOverflow
    }
  }, [open, onClose])

  if (!open) return null

  return createPortal(
    <div className="fixed inset-0 z-50">
      {/* 遮罩：半透明前景色 + 轻微模糊，点击关闭 */}
      <div
        className="absolute inset-0 animate-fade-in bg-foreground/35 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden="true"
      />
      <div className="pointer-events-none absolute inset-0 grid place-items-center overflow-y-auto p-4">
        <div
          ref={contentRef}
          role="dialog"
          aria-modal="true"
          tabIndex={-1}
          className={cn(
            'pointer-events-auto w-full animate-scale-in rounded-xl border-[1.5px] border-ink bg-card text-card-foreground shadow-dialog outline-none',
            width,
            className,
          )}
        >
          <header className="flex items-start justify-between gap-4 px-5 pt-4 pb-1">
            <div className="min-w-0">
              <h2 className="text-[15px] font-semibold tracking-tight">{title}</h2>
              {description ? (
                <p className="mt-0.5 text-[13px] text-muted-foreground">{description}</p>
              ) : null}
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="-mt-0.5 -mr-1.5 inline-flex size-7 flex-none cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              <X className="size-4" aria-hidden="true" />
            </button>
          </header>
          <div className="px-5 py-3">{children}</div>
          {footer ? (
            <footer className="flex items-center justify-end gap-2 rounded-b-xl border-t border-border bg-muted/40 px-5 py-3">
              {footer}
            </footer>
          ) : null}
        </div>
      </div>
    </div>,
    document.body,
  )
}
