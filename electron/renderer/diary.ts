/**
 * 读取并渲染后端生成的日记条目、日期分组和记忆事实。
 *
 * 页面只通过 preload/diary.ts 访问数据，视图层负责空状态、加载错误和关闭动作；
 * 日期标签与事实拆分委托给 diaryState.ts。
 */
import type { DiaryEntry, DiaryPayload } from '../shared/ipc.ts'
import { diaryDayLabel, splitRememberedFacts } from './diaryState.ts'

/** 日记界面只读渲染器，按日期展示对话摘要、梦境记录、日程和记忆事实。 */

const list = document.getElementById('list') as HTMLElement
const today = document.getElementById('today') as HTMLElement
const memories = document.getElementById('memories') as HTMLElement
const changeSection = document.getElementById('change-section') as HTMLElement

/**
 * 将日记条目按日期分组并写入列表容器。
 *
 * @param entries 后端返回的日记条目，按结束时间排序。
 * @param now 后端提供的当前时间戳，传给日期标签计算器以保持时区和测试结果一致。
 * @returns 无返回值。
 * @remarks 内容全部通过 `textContent` 和 DOM 节点写入，避免把模型生成文本解释为 HTML。
 * @throws 不主动抛出；浏览器 DOM 操作失败时传播原生异常。
 */
function render(entries: DiaryEntry[], now: number): void {
  list.replaceChildren()

  if (!entries.length) {
    const empty = document.createElement('div')
    empty.className = 'empty'
    empty.textContent = '还没有记下什么。\n多聊几句后，这里会逐渐出现日记和梦。'
    empty.style.whiteSpace = 'pre-line'
    list.append(empty)
    return
  }

  let lastDay = ''
  for (const e of entries) {
    // 只在日期变化时创建分组标题，避免每条记录重复写入相同日期。
    const day = diaryDayLabel(e.endedAt, now)
    if (day !== lastDay) {
      lastDay = day
      const h = document.createElement('div')
      h.className = 'day'
      h.textContent = day
      list.append(h)
    }

    const row = document.createElement('div')
    row.className = e.kind === 'dream' ? 'entry dream' : 'entry'

    const body = document.createElement('div')
    body.className = 'body'

    if (e.kind !== 'conversation') {
      const tag = document.createElement('div')
      tag.className = 'tag'
      tag.textContent = e.kind === 'dream' ? '梦' : '你不在的时候'
      body.append(tag)
    }

    const summary = document.createElement('div')
    summary.className = 'summary'
    summary.textContent = e.summary
    body.append(summary)

    if (e.cues.length) {
      // 提示词线索单独渲染为标签，便于区分摘要正文和触发记忆的依据。
      const cues = document.createElement('div')
      cues.className = 'cues'
      for (const c of e.cues) {
        const chip = document.createElement('span')
        chip.className = 'cue'
        chip.textContent = c
        cues.append(chip)
      }
      body.append(cues)
    }

    row.append(body)
    list.append(row)
  }
}

/**
 * 创建带文本内容的标题元素。
 *
 * @param text 标题文本。
 * @param level 标题级别，默认使用 `h2`，仅允许 `h2` 或 `h3`。
 * @returns 已设置文本内容的标题元素。
 */
function heading(text: string, level: 'h2' | 'h3' = 'h2'): HTMLHeadingElement {
  const element = document.createElement(level)
  element.textContent = text
  return element
}

/**
 * 渲染当天主题和日程段。
 *
 * @param payload 后端返回的完整日记数据。
 * @returns 无返回值。
 */
function renderToday(payload: DiaryPayload): void {
  today.replaceChildren(heading('今天'))
  const theme = document.createElement('p')
  theme.className = 'today-theme'
  theme.textContent = payload.today.theme
  today.append(theme)

  for (const slot of payload.today.slots) {
    const row = document.createElement('div')
    row.className = 'today-slot'
    const doing = document.createElement('span')
    doing.textContent = slot.doing
    row.append(doing)
    today.append(row)
  }
}

/**
 * 渲染已记忆事实，并将仍有效与逐渐淡化的事实分组展示。
 *
 * @param payload 后端返回的完整日记数据。
 * @returns 无返回值。
 * @remarks 事实文本作为纯文本插入；分组规则由 `diaryState.ts` 统一提供。
 */
function renderMemories(payload: DiaryPayload): void {
  memories.replaceChildren(heading('我记得的'))
  const groups = splitRememberedFacts(payload.memories)
  if (!groups.remembered.length && !groups.fading.length) {
    const empty = document.createElement('p')
    empty.textContent = '还没有什么特别想记住的。'
    memories.append(empty)
    return
  }

  /**
   * 将事实文本列表渲染为无序列表并追加到指定容器。
   *
   * @param items 待显示的事实文本；元素按输入顺序渲染为空间独立的 ``li`` 节点。
   * @param parent 接收新列表的 DOM 容器。
   * @returns {void} 无返回值；列表节点创建并追加完成后结束。
   * @remarks 使用 ``textContent`` 写入事实正文，避免后端文本被当作 HTML 解析；
   *   每次调用都会创建一个新的列表节点，不复用已有子节点。
   */
  const appendList = (items: string[], parent: HTMLElement): void => {
    const list = document.createElement('ul')
    list.className = 'remembered'
    for (const content of items) {
      const item = document.createElement('li')
      item.textContent = content
      list.append(item)
    }
    parent.append(list)
  }
  if (groups.remembered.length) appendList(groups.remembered, memories)
  if (groups.fading.length) {
    const fading = document.createElement('div')
    fading.className = 'fading'
    fading.append(heading('有点想不起来了', 'h3'))
    appendList(groups.fading, fading)
    memories.append(fading)
  }
}

/**
 * 渲染快照变化说明；没有有效文本时隐藏整个变化区域。
 *
 * @param change 后端生成的变化描述，可为 `undefined`。
 * @returns 无返回值。
 */
function renderChange(change: string | undefined): void {
  changeSection.replaceChildren()
  changeSection.hidden = !change
  if (!change) return
  changeSection.append(heading('变化'))
  const line = document.createElement('p')
  line.id = 'change'
  line.textContent = change
  changeSection.append(line)
}

/**
 * 读取日记数据并依次渲染日程、记忆、变化说明和条目列表。
 *
 * @returns 页面初始化完成后的 Promise。
 * @throws 不向上抛出读取或渲染异常；错误会转换为列表中的失败提示。
 */
async function main(): Promise<void> {
  try {
    const diary = await window.diary?.read()
    if (!diary) throw new Error('preload 未提供日记接口')
    renderToday(diary)
    renderMemories(diary)
    renderChange(diary.change)
    render(diary.entries, diary.now)
  } catch (err) {
    list.replaceChildren()
    const box = document.createElement('div')
    box.className = 'empty'
    box.textContent = `读不到日记：${err instanceof Error ? err.message : String(err)}`
    list.append(box)
  }
}

void main()
