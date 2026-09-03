/**
 * 批量操作工具条，三个词表页共用。
 *
 * 左侧固定是「全选本页 + 已选条数 + 清除选择」，右侧由调用方以 children 传入
 * 具体动作按钮——动作各页不同（表情包是封禁/解封/删除，词表是确认/驳回/删除），
 * 但选择态的表述必须一致，否则同一套勾选框在三个页面上读起来像三件事。
 *
 * 未选中任何条目时仍然渲染：工具条本身就是「这里可以批量操作」的提示，选中后
 * 才冒出来会让人找不到入口。写入在飞期间整条禁用，进行中提示挂在选择态文字上，
 * 不挂在某一个按钮的文案里。
 */
import type { ReactNode } from 'react'

import { Button } from './button'
import { Checkbox } from './checkbox'
import { cn } from './cn'

interface BatchBarProps {
  /** 当前页是否已全选。 */
  pageAllSelected: boolean
  /** 全选本页的切换回调。 */
  onTogglePage: () => void
  /** 已选条数，跨页累积。 */
  selectedCount: number
  /** 清除选择的回调。 */
  onClear: () => void
  /** 批量写入在飞：禁用整条工具条，并把选择态文字换成进行中提示。 */
  busy?: boolean
  /** 右侧动作按钮，由调用方按各自语义提供。 */
  children?: ReactNode
  /** 追加类名。 */
  className?: string
}

/**
 * 渲染批量操作工具条。
 *
 * @param props.pageAllSelected 当前页全选态。
 * @param props.onTogglePage 全选本页回调。
 * @param props.selectedCount 已选条数。
 * @param props.onClear 清除选择回调。
 * @param props.children 右侧动作按钮。
 * @returns 一行工具条元素。
 */
export function BatchBar({
  pageAllSelected,
  onTogglePage,
  selectedCount,
  onClear,
  busy,
  children,
  className,
}: BatchBarProps) {
  return (
    <div className={cn('flex flex-wrap items-center gap-3', className)}>
      <Checkbox
        checked={pageAllSelected}
        onChange={onTogglePage}
        disabled={busy}
        label="全选本页"
      />
      {/* 进行中提示放在这里而不是某个按钮上：一条工具条有三个动作，把「处理中」
          写死在其中一个按钮的文案里，跑的是另一个动作时就会指错地方。 */}
      <span className="text-sm text-muted-foreground">
        {busy ? '处理中…'
          : selectedCount > 0 ? `已选 ${selectedCount} 条（可翻页继续选）`
          : '未选中任何条目'}
      </span>
      {selectedCount > 0 && !busy ? (
        <Button variant="ghost" size="sm" onClick={onClear}>
          清除选择
        </Button>
      ) : null}
      <div className="ml-auto flex flex-wrap items-center gap-2">{children}</div>
    </div>
  )
}
