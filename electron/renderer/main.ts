/**
 * 初始化桌宠渲染层、角色视图、聊天控制器和窗口交互。
 *
 * 本入口组装 character、chat 和 ui 子模块，不直接调用 Electron 主进程 API；
 * 所有跨进程操作均通过 preload/index.ts 提供的 bridge 完成。
 */
import { setupChat } from './chat.ts'
import { syncComposerToCharacter } from './ui/layout.ts'
import { setupPointer } from './ui/pointer.ts'
import { SpriteCharacterView } from './character/sprite/SpriteCharacterView.ts'
import { EMOTIONS, GESTURES, type CharacterView, type Emotion } from './character/types.ts'

/** 渲染层资源入口；素材目录由 `VITE_CHARACTER` 决定，角色视图实现由本模块组装。 */
const ASSETS_BASE = import.meta.env.VITE_CHARACTER ?? '/character/yueli'

const canvas = document.getElementById('character') as HTMLCanvasElement
const errorBox = document.getElementById('error') as HTMLDivElement
const visionIndicator = document.getElementById('visionIndicator') as HTMLDivElement
let asleep = false
let activeView: CharacterView | null = null

// 状态只由主进程推送；收到「醒着」时只解除 sleepy，避免覆盖正在说话的表情。
window.pet.onSleepState(({ asleep: nextAsleep }) => {
  const wasAsleep = asleep
  asleep = nextAsleep
  if (!activeView) return
  if (asleep) activeView.setEmotion('sleepy')
  else if (wasAsleep) activeView.setEmotion('normal')
})

/**
 * 在渲染层展示初始化错误，并临时恢复窗口交互能力。
 *
 * @param msg 要展示的错误文本。
 * @returns 无返回值。
 * @remarks 错误框需要可点击和可关闭，因此失败时强制取消窗口点击穿透。
 */
function fail(msg: string): void {
  errorBox.textContent = msg
  errorBox.style.display = 'flex'
  // 错误框需要可交互，否则用户无法点击错误信息或关闭窗口。
  window.pet?.setInteractive(true)
}

/**
 * 加载角色素材并初始化角色视图、指针、聊天和窗口状态桥接。
 *
 * @returns 渲染层初始化完成后的 Promise。
 * @throws 不向上抛出素材加载错误；错误会通过 {@link fail} 展示并终止后续初始化。
 * @remarks 方法注册输入、视觉提示和调试状态监听器，实际跨进程操作全部经 preload bridge 完成。
 */
async function main(): Promise<void> {
  const view = new SpriteCharacterView({ canvas, assetsBase: ASSETS_BASE })

  try {
    await view.load()
  } catch (err) {
    fail(`素材加载失败\n${err instanceof Error ? err.message : String(err)}\n\n路径：${ASSETS_BASE}`)
    return
  }
  activeView = view
  if (asleep) view.setEmotion('sleepy')

  // 指针模块统一处理点击穿透、窗口拖动以及点击与拖动的区分。
  let onTapHandler: () => void = () => {}
  setupPointer({
    canvas,
    onTap: () => {
      window.pet.userInteracted()
      onTapHandler()
    },
  })

  // 输入栏和消息气泡按角色实际渲染边界定位，避免依赖固定坐标。
  syncComposerToCharacter(
    canvas,
    document.getElementById('composer') as HTMLElement,
    document.getElementById('bubble') as HTMLElement,
  )

  setupDebugKeys(view)
  setupChat({
    view,
    bubble: document.getElementById('bubble') as HTMLElement,
    bubbleText: document.getElementById('bubbleText') as HTMLElement,
    composer: document.getElementById('composer') as HTMLElement,
    input: document.getElementById('input') as HTMLInputElement,
    sleepingNow: () => asleep,
    onTap: (h) => {
      onTapHandler = h
    },
  })

  // 视觉请求可能上传前台截图；只显示采集状态指示，不弹窗、不抢焦点，也不把图像内容传入渲染层。
  window.pet.onVisionWatching(({ watching }) => visionIndicator.classList.toggle('show', watching))

  // 开发构建暴露视图实例，便于在控制台验证表情、动作和口型切换。
  if (import.meta.env.DEV) {
    ;(globalThis as unknown as { __view: CharacterView }).__view = view
  }

  // 保留只读状态钩子供 Electron 自检读取；普通浏览器没有 window.pet，不会注册真实事件处理器。
  ;(globalThis as unknown as { __state: () => unknown }).__state = () => ({
    emotion: view.currentEmotion,
    ready: view.ready,
    ...(globalThis as unknown as { __voiceState?: () => { active: boolean; mouthLevel: number } }).__voiceState?.(),
  })
}

/**
 * 注册开发期调试快捷键：方向键切换表情、G 播放动作、M 模拟语音口型、Ctrl+Shift+Q 退出。
 *
 * @param view 当前角色视图实例。
 * @returns 无返回值。
 * @remarks 输入框或文本域获得焦点时不拦截快捷键；退出使用组合键，避免与输入框的 Escape 行为冲突。
 */
function setupDebugKeys(view: CharacterView): void {
  let emotionIndex = 0
  let talking = 0

  addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'q') return window.pet?.quit()

    // 输入框获得焦点时保留所有按键，避免编辑文本触发表情或动作切换。
    const el = document.activeElement
    if (el instanceof HTMLInputElement || el instanceof HTMLTextAreaElement) return

    if (e.key === 'ArrowRight' || e.key === ' ') {
      emotionIndex = (emotionIndex + 1) % EMOTIONS.length
      view.setEmotion(EMOTIONS[emotionIndex] as Emotion)
      console.log('emotion →', EMOTIONS[emotionIndex])
    } else if (e.key === 'ArrowLeft') {
      emotionIndex = (emotionIndex - 1 + EMOTIONS.length) % EMOTIONS.length
      view.setEmotion(EMOTIONS[emotionIndex] as Emotion)
      console.log('emotion →', EMOTIONS[emotionIndex])
    } else if (e.key.toLowerCase() === 'g') {
      const g = GESTURES[Math.floor(Math.random() * GESTURES.length)]!
      view.playGesture(g)
      console.log('gesture →', g)
    } else if (e.key.toLowerCase() === 'm') {
      // 使用固定 3 秒窗口模拟 TTS 驱动的口型变化，结束后明确恢复闭合状态。
      clearInterval(talking)
      const until = Date.now() + 3000
      talking = window.setInterval(() => {
        if (Date.now() > until) {
          clearInterval(talking)
          view.setMouthOpen(0)
          return
        }
        view.setMouthOpen(Math.random())
      }, 90)
    }
  })
}

main()
