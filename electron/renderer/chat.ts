/**
 * 连接聊天输入、主进程流式事件、消息气泡、语音播放和角色表现。
 *
 * 本模块消费 preload/index.ts 提供的 PetBridge，并将解析事件映射为 UI 状态；
 * 文本流和回合终止状态由 chatState.ts 及本模块的 DOM 控制器共同维护。
 */
import type { ChatStreamEvent } from '../shared/ipc.ts'
import { gateTurnEvent } from './turnGate.ts'
import { VoicePlayer, decodeBase64 } from './audio/player.ts'
import { resolveEmotion, resolveGesture, type CharacterView } from './character/types.ts'
import { settledEmotion } from './chatState.ts'
import { Bubble } from './ui/bubble.ts'

/** 对话 UI 接线：输入栏、主进程流式事件、气泡、语音和角色表现共享同一回合状态。 */

export interface ChatWiring {
  view: CharacterView
  bubble: HTMLElement
  bubbleText: HTMLElement
  composer: HTMLElement
  input: HTMLInputElement
  /** 由主进程经 IPC 推入的睡眠状态；渲染层不读时钟。 */
  sleepingNow: () => boolean
  /** 注册角色点击回调；点击与拖动的区分由 ui/pointer.ts 完成。 */
  onTap: (handler: () => void) => void
}

/**
 * 初始化聊天输入、事件订阅、语音播放和角色状态转换。
 *
 * @param w 聊天 DOM 元素、角色视图、睡眠状态读取器和主进程回调集合。
 * @returns 无返回值；初始化完成后由事件监听器驱动后续回合。
 * @throws Error 依赖的 DOM bridge 或事件接口在运行时不可用时由调用方环境抛出。
 * @sideEffects 注册键盘、失焦、主进程事件和页面卸载监听器；创建 Bubble 与 VoicePlayer，
 * 并可能将音频和聊天事件更新到角色视图。
 */
export function setupChat(w: ChatWiring): void {
  const bubble = new Bubble(w.bubble, w.bubbleText)
  let currentTurn = 0
  // 主进程推送合成音频后在此播放，并由 RMS 采样驱动口型。
  const player = new VoicePlayer(w.view)

  /** 打字节奏的近似口型定时器。有真语音时它必须让路。 */
  let mouthTimer = 0

  /**
   * 表情回落定时器。
   *
   * 在整轮消息完成后恢复默认表情，避免表情在多句 ``say`` 分段之间反复切换。
   * 仅在 ``done`` 事件后调度，而不是在每个 ``sayEnd`` 后调度。
  */
  let settleTimer = 0
  const SETTLE_MS = 6000

  /**
   * 取消当前表情回落计时器。
   *
   * @returns {void} 无返回值；不存在活动计时器时安全返回。
   * @remarks 新的流式回合开始前必须清除旧计时器，避免旧回合在新表情仍显示时覆盖角色状态。
   */
  const cancelSettle = (): void => {
    clearTimeout(settleTimer)
    settleTimer = 0
  }

  /**
   * 在当前回合结束后安排表情回落。
   *
   * @returns {void} 无返回值；重复调用会先取消旧计时器再重新计时。
   * @remarks 延迟读取睡眠状态和默认表情，确保回落时使用最新的主进程状态，而不是回合开始时的快照。
   */
  const scheduleSettle = (): void => {
    cancelSettle()
    settleTimer = window.setTimeout(() => {
      settleTimer = 0
      w.view.setEmotion(settledEmotion(w.sleepingNow()))
    }, SETTLE_MS)
  }

  /**
   * 停止无语音时使用的近似口型定时器。
   *
   * @returns {void} 无返回值；有真实音频播放时保留 VoicePlayer 当前口型电平。
   * @remarks 清除近似定时器可以避免它与真实音频 RMS 采样同时写入同一角色视图。
   */
  const stopMouth = (): void => {
    clearInterval(mouthTimer)
    mouthTimer = 0
    // 有真实音频时由 VoicePlayer 独占口型写入，避免两个定时器竞争同一值。
    if (!player.active) w.view.setMouthOpen(0)
  }

  /**
   * 说话时嘴动。
   *
   * 配置 TTS 时由真实音频响度驱动；未配置时使用低成本定时器近似，避免文本流
   * 更新而角色完全没有口型。真实音频开始播放后立即停止近似驱动。
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
   * 输入框需要窗口焦点；setInteractive 只控制鼠标事件，不能替代 focusInput。
   * 仅在用户明确打开输入栏时请求焦点，避免打断其他窗口的输入。
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

  // 点击角色区域开合输入栏；拖动由 ui/pointer.ts 的位移阈值过滤。
  w.onTap(() => showComposer(!w.composer.classList.contains('show')))

  // 开发环境暴露只读播放器状态，便于验证口型是否跟随音频响度。
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

  // 托盘菜单可在角色隐藏或移出当前视野时重新打开输入栏。
  window.pet?.onOpenComposer(() => showComposer(true))

  w.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
      const text = w.input.value.trim()
      if (!text) return
      w.input.value = ''
      // 发送后收起输入栏，避免桌宠长期占用交互区域；再次输入由点击或托盘入口打开。
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

  /**
   * 清理当前输入并提交一条聊天消息。
   *
   * @param text 已去除首尾空白的用户文本。
   * @returns 后端确认消息进入缓冲后完成。
   * @throws Error bridge 请求失败时向事件处理方传播。
   * @sideEffects 清空气泡、取消表情回落并设置等待中的表情。
   */
  async function submit(text: string): Promise<void> {
    bubble.clear()
    cancelSettle()
    // 先更新生成中视觉反馈，再等待模型首个 token，避免请求期间页面无变化。
    w.view.setEmotion('smile')
    await window.pet?.send(text)
  }

  const off = window.pet?.onEvent((e: ChatStreamEvent) => {
    const gated = gateTurnEvent(currentTurn, e)
    if (!gated.accept) return
    currentTurn = gated.currentTurn
    if (gated.started) {
      bubble.clear()
    }
    if (e.kind === 'start') {
      return
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
      // 整轮完成后才恢复表情，避免多句分段之间出现闪烁。
      scheduleSettle()
      return
    }
    if (e.kind === 'silent') {
      stopMouth()
      bubble.clear()
      // 静默回合没有后续文本或音频，立即离开等待表情。
      cancelSettle()
      w.view.setEmotion(settledEmotion(w.sleepingNow()))
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
    // memory/mood 由后端完成持久化和状态更新，渲染层无需重复处理。
  })

  addEventListener('beforeunload', () => off?.())
}
