import type { ChatStreamEvent } from '../shared/ipc.ts'
import { VoicePlayer, decodeBase64 } from './audio/player.ts'
import { resolveEmotion, resolveGesture, type CharacterView } from './character/types.ts'
import { settledEmotion } from './chatState.ts'
import { Bubble } from './ui/bubble.ts'

/**
 * 对话交互接线：输入栏 ↔ 主进程 ↔ 气泡 + 角色表情。
 *
 * 一个关键的体感细节：首字延迟实测 2.4 秒（已经是最快的角色模型了）。
 * 用户按下回车后如果画面毫无反应，2.4 秒足够让人以为程序卡死。
 * 所以提交瞬间就切 thinking 表情 —— 视觉反馈必须先于第一个 token 到达。
 */

export interface ChatWiring {
  view: CharacterView
  bubble: HTMLElement
  bubbleText: HTMLElement
  composer: HTMLElement
  input: HTMLInputElement
  /** 由主进程经 IPC 推入的睡眠状态；渲染层不读时钟。 */
  sleepingNow: () => boolean
  /** 注册「点了她一下」的回调。点与拖的区分在 ui/pointer.ts 里做。 */
  onTap: (handler: () => void) => void
}

export function setupChat(w: ChatWiring): void {
  const bubble = new Bubble(w.bubble, w.bubbleText)
  let currentTurn = 0
  // 语音：主进程合成完把音频推过来，这里播放并由实测响度驱动口型
  const player = new VoicePlayer(w.view)

  /** 打字节奏的近似口型定时器。有真语音时它必须让路。 */
  let mouthTimer = 0

  /**
   * 表情回落定时器。
   *
   * 说完一句话后脸不该一直定格 —— 她会顶着「哈哈大笑」或「打哈欠」的脸
   * 在桌面上站几个小时，非常僵。真人说完话表情会自然松回平静。
   *
   * 只在**一整轮讲完**后才排，不在每句 sayEnd 排：她可能连说两三句，
   * 中间回落会让脸一亮一灭地闪。
   */
  let settleTimer = 0
  const SETTLE_MS = 6000

  const cancelSettle = (): void => {
    clearTimeout(settleTimer)
    settleTimer = 0
  }

  const scheduleSettle = (): void => {
    cancelSettle()
    settleTimer = window.setTimeout(() => {
      settleTimer = 0
      w.view.setEmotion(settledEmotion(w.sleepingNow()))
    }, SETTLE_MS)
  }

  const stopMouth = () => {
    clearInterval(mouthTimer)
    mouthTimer = 0
    // 有音频在播时别抢方向盘 —— 那边每帧都在按 RMS 写嘴型，
    // 这里再清零会让嘴一开一合地抽搐
    if (!player.active) w.view.setMouthOpen(0)
  }

  /**
   * 说话时嘴动。
   *
   * 两套驱动：配了 TTS 就用**实测音频响度**（停顿、气声、拖长音都能对上）；
   * 没配就退回按打字节奏的近似 —— 气泡在动而她一脸不动非常出戏，
   * 假口型也比没有强。真音频一开始播，近似立刻停手。
   */
  const startMouth = () => {
    if (mouthTimer || player.active) return
    mouthTimer = window.setInterval(() => {
      if (player.active) return stopMouth()
      w.view.setMouthOpen(0.25 + Math.random() * 0.65)
    }, 110)
  }

  /**
   * 开合输入栏。
   *
   * 打字需要窗口真的拿到焦点 —— 只调 setInteractive 不够，那只管鼠标事件。
   * 平时不主动抢焦点（你在别的窗口打字不该被打断），只在你点了她、
   * 明确要输入的时候才拿过来。
   */
  const showComposer = (show: boolean): void => {
    if (w.composer.classList.contains('show') === show) return
    w.composer.classList.toggle('show', show)
    if (show) {
      window.pet?.setInteractive(true)
      window.pet?.focusInput(true)
      w.input.focus()
    } else {
      w.input.blur()
      window.pet?.focusInput(false)
    }
  }

  // 点她身上开合输入栏。拖动不触发 —— 见 ui/pointer.ts 的位移阈值
  w.onTap(() => showComposer(!w.composer.classList.contains('show')))

  // 开发期把播放器挂出去，好在浏览器里用合成音频验证口型是否真的跟着响度走
  if (import.meta.env.DEV) {
    ;(globalThis as unknown as { __player: VoicePlayer }).__player = player
  }
  ;(globalThis as unknown as { __voiceState: () => { active: boolean; mouthLevel: number } }).__voiceState = () => ({
    active: player.active,
    mouthLevel: player.currentLevel,
  })

  window.pet?.onVoice((e) => {
    if (e.kind === 'stop') return player.stop()
    if (e.data) player.enqueue(decodeBase64(e.data), e.format ?? 'mp3')
  })

  // 托盘菜单「跟她说话」—— 她被隐藏或拖远时，这是唤起对话的入口
  window.pet?.onOpenComposer(() => showComposer(true))

  w.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
      const text = w.input.value.trim()
      if (!text) return
      w.input.value = ''
      // 发完就收起来：桌宠平时该是干净的一个人站在那儿，
      // 不该常驻一条输入栏。想再说话点她一下就行
      showComposer(false)
      void submit(text)
    } else if (e.key === 'Escape') {
      showComposer(false)
    }
  })

  // 点到别处就收起来，跟系统里其他浮层的行为一致
  w.input.addEventListener('blur', () => {
    // 延后一帧：Enter 提交时会先 blur 再走 keydown 的后续逻辑，立即收起会打断
    setTimeout(() => {
      if (document.activeElement !== w.input) showComposer(false)
    }, 120)
  })

  async function submit(text: string): Promise<void> {
    bubble.clear()
    cancelSettle()
    // 先给反馈，再等模型 —— 顺序反了就是 2.4 秒的死寂。
    //
    // 用 smile 而不是 thinking：生成出来的 thinking 素材是「半闭眼 + 脸红 +
    // 嘴角下撇」，读出来像不高兴或者困，完全不像在想事情。
    // 等回复时挂一张温和专注的脸，比挂一张臭脸强得多
    w.view.setEmotion('smile')
    currentTurn = await (window.pet?.send(text) ?? Promise.resolve(0))
  }

  const off = window.pet?.onEvent((e: ChatStreamEvent) => {
    // 只丢**过期**轮次，不能只认自己发起的那一轮 ——
    // F 阶段的主动打扰由主进程发起，渲染层这边 currentTurn 还是旧值，
    // 按「必须等于」过滤会把她主动说的话全部静默吞掉。
    if (e.turnId < currentTurn) return
    if (e.turnId > currentTurn) {
      // 主进程发起的新轮次：跟上它，并清掉上一轮残留
      currentTurn = e.turnId
      bubble.clear()
    }

    if (e.kind === 'error') {
      stopMouth()
      w.view.setEmotion('speechless')
      bubble.showNow(e.hint ? `${e.message}\n（${e.hint}）` : e.message, true)
      scheduleSettle()
      return
    }
    if (e.kind === 'done') {
      stopMouth()
      bubble.finish()
      // 一整轮讲完才排回落。放在每句 sayEnd 上的话，
      // 她连说两三句时脸会一亮一灭地闪
      scheduleSettle()
      return
    }

    const ev = e.event
    if (ev.type === 'say') {
      bubble.startLine()
      // 新一句来了，取消上一轮排好的回落
      cancelSettle()
      if (ev.emotion) w.view.setEmotion(resolveEmotion(ev.emotion))
      if (ev.gesture) {
        const g = resolveGesture(ev.gesture)
        if (g) w.view.playGesture(g)
      }
      startMouth()
    } else if (ev.type === 'text') {
      bubble.push(ev.value)
    } else if (ev.type === 'sayEnd') {
      stopMouth()
    }
    // memory / mood 事件在 D 阶段接记忆与人格数值，当前无视
  })

  addEventListener('beforeunload', () => off?.())
}
