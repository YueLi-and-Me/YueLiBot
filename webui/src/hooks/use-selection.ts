/**
 * 列表页批量操作的选择集 hook。
 *
 * 表情包库、黑话词表、表达方式三页的选择行为完全一致：跨页累积（可以翻几页攒一
 * 批再操作）、筛选变化时清空（换了条件后选择集里剩什么已经看不见了，留着等于埋
 * 雷）、全选只作用于当前页。这里把这套行为收成一处，页面只负责在换筛选时调用
 * `clear`。被 features/emojis、features/jargon、features/expressions 引用。
 */
import { useMemo, useState } from 'react'

/** useSelection 返回的选择集状态与操作。 */
export interface Selection<T> {
  /** 已选中的 ID 集合，跨页累积。 */
  selected: Set<T>
  /** 已选条数。 */
  size: number
  /** 已选 ID 的数组形式，供批量请求直接使用。 */
  ids: T[]
  /** 当前页是否已全部选中；页为空时恒为 false。 */
  pageAllSelected: boolean
  /** 切换单个 ID 的选中态。 */
  toggle: (id: T) => void
  /** 全选或取消全选当前页；不影响其他页已选中的条目。 */
  togglePage: () => void
  /** 清空整个选择集。 */
  clear: () => void
}

/**
 * 维护一个跨页累积的选择集。
 *
 * @param pageIds 当前页全部条目的 ID，按展示顺序排列；用于计算全选态。
 * @returns 选择集状态与操作函数。
 */
export function useSelection<T>(pageIds: T[]): Selection<T> {
  const [selected, setSelected] = useState<Set<T>>(new Set())

  const pageAllSelected = pageIds.length > 0 && pageIds.every((id) => selected.has(id))

  const toggle = (id: T) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const togglePage = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (pageAllSelected) pageIds.forEach((id) => next.delete(id))
      else pageIds.forEach((id) => next.add(id))
      return next
    })
  }

  const clear = () => setSelected(new Set())

  // ids 依赖 selected 本身：集合每次变更都会换引用，这里的缓存只是避免同一次
  // 渲染里多处展开出多个数组。
  const ids = useMemo(() => [...selected], [selected])

  return { selected, size: selected.size, ids, pageAllSelected, toggle, togglePage, clear }
}
