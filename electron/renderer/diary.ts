import type { DiaryEntry, DiaryPayload } from '../shared/ipc.ts'
import { diaryDayLabel, splitRememberedFacts } from './diaryState.ts'

/**
 * 日记界面。
 *
 * 只读，按日期分组。梦和对话摘要用不同样貌区分 ——
 * 梦不是「发生过的事」，是她脑子里的东西，混在一起看会分不清哪些真的聊过。
 */

const list = document.getElementById('list') as HTMLElement
const today = document.getElementById('today') as HTMLElement
const memories = document.getElementById('memories') as HTMLElement
const changeSection = document.getElementById('change-section') as HTMLElement

/** 全部走 textContent 与 createElement，不拼 HTML —— 内容来自模型生成，不可信。 */
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

function heading(text: string, level: 'h2' | 'h3' = 'h2'): HTMLHeadingElement {
  const element = document.createElement(level)
  element.textContent = text
  return element
}

/** “今天”是她的一天，不是带情绪数字的养成状态。 */
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

/** 直接展示 L3 原文；已冻结的记忆单独留下，不让它悄悄消失。 */
function renderMemories(payload: DiaryPayload): void {
  memories.replaceChildren(heading('我记得的'))
  const groups = splitRememberedFacts(payload.memories)
  if (!groups.remembered.length && !groups.fading.length) {
    const empty = document.createElement('p')
    empty.textContent = '还没有什么特别想记住的。'
    memories.append(empty)
    return
  }

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

/** 没有快照历史或模型没有合格叙述时，整个区块不出现。 */
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
