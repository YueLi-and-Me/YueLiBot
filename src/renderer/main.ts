import { setupChat } from './chat.ts'
import { syncComposerToCharacter } from './ui/layout.ts'
import { setupPointer } from './ui/pointer.ts'
import { SpriteCharacterView } from './character/sprite/SpriteCharacterView.ts'
import { EMOTIONS, GESTURES, type CharacterView, type Emotion } from './character/types.ts'

/**
 * 桌宠渲染层入口。
 *
 * 换素材只改 VITE_CHARACTER，换渲染方案只改这里 new 的是哪个 CharacterView。
 * `/character/fixture` 是管线自检用的合成素材，不是给 app 用的。
 */
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

function fail(msg: string): void {
  errorBox.textContent = msg
  errorBox.style.display = 'flex'
  // 出错时让整窗可交互，否则报错信息点不到也关不掉
  window.pet?.setInteractive(true)
}

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

  // 点击穿透命中检测、窗口拖动、点/拖区分都在这里
  let onTapHandler: () => void = () => {}
  setupPointer({
    canvas,
    onTap: () => {
      window.pet.userInteracted()
      onTapHandler()
    },
  })

  // 输入栏贴在她脚下、与她同中线。按实际渲染框算，不写死坐标
  syncComposerToCharacter(canvas, document.getElementById('composer') as HTMLElement)

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

  // 视觉请求会传出一张前台截图；用角色身上的小光点给出知情提示，
  // 不弹窗、不抢焦点，也不把任何画面内容带到渲染层。
  window.pet.onVisionWatching(({ watching }) => visionIndicator.classList.toggle('show', watching))

  // 开发期把实例挂出去，方便在控制台直接驱动表情/口型做验证
  if (import.meta.env.DEV) {
    ;(globalThis as unknown as { __view: CharacterView }).__view = view
  }

  // 只读状态钩子，生产构建里也留着：自检要验「说完话表情有没有松回平静」，
  // 而那个只能在真 Electron 里跑（浏览器没有 window.pet，事件处理器不注册）
  ;(globalThis as unknown as { __state: () => unknown }).__state = () => ({
    emotion: view.currentEmotion,
    ready: view.ready,
    ...(globalThis as unknown as { __voiceState?: () => { active: boolean; mouthLevel: number } }).__voiceState?.(),
  })
}

/**
 * 开发期手动验证用：← → 切表情、G 放动作、M 模拟说话、Ctrl+Shift+Q 退出。
 *
 * 两个必须避开的坑（窗口改成可聚焦后才出现的）：
 *  · 输入框获得焦点时这些键必须全部让路 —— 否则打字打到空格就切表情
 *  · 退出不能绑 Escape。Escape 是输入框的取消键，误触一下整个 app 就没了；
 *    而且没有托盘之前，退出是唯一出口，更不该这么容易触发
 */
function setupDebugKeys(view: CharacterView): void {
  let emotionIndex = 0
  let talking = 0

  addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === 'q') return window.pet?.quit()

    // 正在输入就完全让路
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
      // 模拟一段 TTS 驱动的口型：3 秒随机开合
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
