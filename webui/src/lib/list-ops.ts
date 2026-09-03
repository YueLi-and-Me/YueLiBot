/**
 * 词表类页面（表情包库、黑话词表、表达方式）共用的排序口径与批量写入口径。
 *
 * 三个页面的数据形态不同，但「怎么排」和「一次能批多少」是同一套规矩，集中在
 * 这里定义，避免三处各写一份、改一处忘两处。被 hooks/use-emojis.ts、
 * hooks/use-jargon.ts、hooks/use-expressions.ts 与对应的 features 页面引用。
 */

/**
 * 列表排序口径，取值与后端 `ListOrder` 逐字对应。
 *
 * `time_*` 按入库时间排，`use_*` 按使用计数排（表情包与表达方式是 use_count，
 * 黑话是查表命中数 hits）。
 */
export type ListOrder = 'time_desc' | 'time_asc' | 'use_desc' | 'use_asc'

/** 排序依据，即界面上的两个按钮；方向不占按钮，由重复点击切换。 */
export type ListOrderField = 'time' | 'use'

/** 每个依据的按钮文案。 */
const FIELD_LABEL: Record<ListOrderField, string> = {
  time: '按时间',
  use: '按使用次数',
}

/** 激活按钮上追加的方向说明，同时是「再点一次会变成什么」的提示。 */
const DIRECTION_HINT: Record<ListOrder, string> = {
  time_desc: '当前：最新在前。再点一次改为最早在前。',
  time_asc: '当前：最早在前。再点一次改回最新在前。',
  use_desc: '当前：用得最多在前。再点一次改为用得最少在前。',
  use_asc: '当前：用得最少在前。再点一次改回用得最多在前。',
}

/**
 * 取一个排序口径的依据部分。
 *
 * @param order 当前排序口径。
 * @returns `time` 或 `use`，即哪个按钮处于激活态。
 */
export function listOrderField(order: ListOrder): ListOrderField {
  return order.startsWith('time') ? 'time' : 'use'
}

/**
 * 算出点击某个按钮后的排序口径。
 *
 * 点当前已激活的按钮是翻方向，点另一个按钮是换依据并回到降序。方向不单独占
 * 按钮：四种口径都要到得了，但界面上只有两个按钮——降序是每个依据的常用向
 * （最新的、用得最多的），升序按需再点一次。
 *
 * @param order 当前排序口径。
 * @param field 被点击的按钮。
 * @returns 目标排序口径。
 */
export function nextListOrder(order: ListOrder, field: ListOrderField): ListOrder {
  if (listOrderField(order) !== field) return `${field}_desc`
  return order.endsWith('_desc') ? `${field}_asc` : `${field}_desc`
}

/**
 * 按当前排序口径生成两个按钮的定义。
 *
 * 只有激活的那个带方向箭头：非激活按钮的方向还没生效，标出来只会让人以为两个
 * 方向同时起作用。箭头朝下代表降序（新→旧、多→少）。
 *
 * @param order 当前排序口径。
 * @returns 供 `SegmentedTabs` 使用的两项定义，含悬停提示。
 */
export function listOrderTabs(
  order: ListOrder,
): Array<{ value: ListOrderField; label: string; title: string }> {
  const active = listOrderField(order)
  const arrow = order.endsWith('_desc') ? '↓' : '↑'
  return (['time', 'use'] as ListOrderField[]).map((field) => ({
    value: field,
    label: field === active ? `${FIELD_LABEL[field]} ${arrow}` : FIELD_LABEL[field],
    title: field === active ? DIRECTION_HINT[order] : `改为${FIELD_LABEL[field]}排序`,
  }))
}

/**
 * 单次批量请求的条数上限，与后端各 `Body` 模型的 `max_length` 同值。
 *
 * 分批是必需的而非优化：页面的选择集跨页累积、没有上限，一次选过这个数就会撞
 * 上后端的长度校验并返回 422，使用者只会看到一句无从下手的报错。
 */
export const BATCH_CHUNK = 200

/**
 * 把一批 ID 按 :data:`BATCH_CHUNK` 切段，逐段执行并收集每段的结果。
 *
 * 段与段之间不是一个事务，中途失败会留下已完成的部分。这对复核与删除都可接受
 * ——两者都幂等，重试只会跳过已经生效的 ID，比整批拒绝好用。
 *
 * @param ids 待处理的 ID 列表；不限长度。
 * @param run 单段的执行函数，参数为一段 ID。
 * @returns 每段返回值组成的数组，顺序与发出顺序一致。
 * @throws 任一段抛出的错误原样传播，由调用方展示；此前各段已经生效。
 */
export async function inChunks<T, R>(
  ids: T[],
  run: (chunk: T[]) => Promise<R>,
): Promise<R[]> {
  const results: R[] = []
  for (let start = 0; start < ids.length; start += BATCH_CHUNK) {
    results.push(await run(ids.slice(start, start + BATCH_CHUNK)))
  }
  return results
}
