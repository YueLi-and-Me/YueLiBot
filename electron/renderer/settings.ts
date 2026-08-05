import type { YueliConfig } from '../shared/ipc.ts'

/**
 * 设置窗口：首次启动引导 + 后续编辑共用一套表单。
 *
 * URL 的 `?mode=first-run` 区分两种模式：首次启动时标题/文案不同，
 * 保存成功后不显示"重启"按钮（主进程本来就还没启动后端，不存在"重启"这回事），
 * 而是显示"正在启动…"——主进程收到保存成功的信号后会自己关掉这扇窗、继续正常流程。
 */

const params = new URLSearchParams(location.search)
const isFirstRun = params.get('mode') === 'first-run'

const form = document.getElementById('settings-form') as HTMLFormElement
const pageTitle = document.getElementById('page-title') as HTMLElement
const pageSubtitle = document.getElementById('page-subtitle') as HTMLElement
const statusText = document.getElementById('status-text') as HTMLElement
const saveButton = document.getElementById('save-button') as HTMLButtonElement
const restartButton = document.getElementById('restart-button') as HTMLButtonElement

if (isFirstRun) {
  pageTitle.textContent = '欢迎使用月璃'
  pageSubtitle.textContent = '第一次启动需要先填一些基本信息，之后随时能从托盘菜单里的"设置"回来改。'
}

let loadedConfig: YueliConfig | null = null

function getByPath(obj: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, key) => {
    if (acc && typeof acc === 'object') return (acc as Record<string, unknown>)[key]
    return undefined
  }, obj)
}

function setByPath(obj: Record<string, unknown>, path: string, value: unknown): void {
  const keys = path.split('.')
  let cursor = obj
  for (let i = 0; i < keys.length - 1; i++) {
    const key = keys[i]!
    if (typeof cursor[key] !== 'object' || cursor[key] === null) cursor[key] = {}
    cursor = cursor[key] as Record<string, unknown>
  }
  cursor[keys[keys.length - 1]!] = value
}

function populateForm(config: YueliConfig): void {
  for (const el of Array.from(form.elements)) {
    const name = (el as HTMLInputElement).name
    if (!name) continue
    const value = getByPath(config, name)
    if (value === undefined) continue
    if (el instanceof HTMLInputElement && el.type === 'checkbox') {
      el.checked = Boolean(value)
    } else if (el instanceof HTMLInputElement || el instanceof HTMLSelectElement) {
      el.value = String(value)
    }
  }
  syncToggleVisibility()
}

/** tts.enabled / vision.enabled 两个开关控制各自区块是否可见。 */
function syncToggleVisibility(): void {
  for (const body of Array.from(document.querySelectorAll<HTMLElement>('[data-toggle-target]'))) {
    const targetName = body.dataset.toggleTarget!
    const toggle = form.elements.namedItem(targetName) as HTMLInputElement | null
    body.classList.toggle('collapsed', !toggle?.checked)
  }
}

form.addEventListener('change', (e) => {
  if ((e.target as HTMLElement)?.matches?.('input[type="checkbox"][name$=".enabled"]')) syncToggleVisibility()
})

function collectFormValues(base: YueliConfig): YueliConfig {
  // 表单里没出现的字段（比如 vector.* 和 llm.timeout_ms）保留原来读到的值，
  // 不能被表单提交时悄悄清空。
  const result = JSON.parse(JSON.stringify(base)) as Record<string, unknown>
  for (const el of Array.from(form.elements)) {
    const input = el as HTMLInputElement | HTMLSelectElement
    if (!input.name) continue
    if (input instanceof HTMLInputElement && input.type === 'checkbox') {
      setByPath(result, input.name, input.checked)
    } else {
      setByPath(result, input.name, input.value)
    }
  }
  return result as unknown as YueliConfig
}

function setStatus(text: string, kind: 'ok' | 'error' | '' = ''): void {
  statusText.textContent = text
  statusText.className = `status-text ${kind}`
}

form.addEventListener('submit', async (e) => {
  e.preventDefault()
  if (!loadedConfig) return
  saveButton.disabled = true
  restartButton.hidden = true
  setStatus(isFirstRun ? '正在保存…' : '保存中…')
  try {
    const config = collectFormValues(loadedConfig)
    const result = await window.settings?.save(config)
    if (!result?.ok) throw new Error(result?.error ?? '未知错误')
    loadedConfig = config
    if (isFirstRun) {
      setStatus('保存成功，正在启动月璃…', 'ok')
      // 主进程监听同一次 save 调用的成功结果，会自己关掉这扇窗并继续启动——
      // 这里不用再做什么，保留提示文字直到窗口被关掉。
    } else {
      setStatus('已保存', 'ok')
      restartButton.hidden = false
    }
  } catch (err) {
    setStatus(`保存失败：${err instanceof Error ? err.message : String(err)}`, 'error')
  } finally {
    saveButton.disabled = false
  }
})

restartButton.addEventListener('click', () => {
  window.settings?.restartBackend()
  setStatus('已发送重启指令…', 'ok')
})

async function init(): Promise<void> {
  try {
    loadedConfig = (await window.settings?.read()) ?? null
    if (loadedConfig) populateForm(loadedConfig)
  } catch (err) {
    setStatus(`读取配置失败：${err instanceof Error ? err.message : String(err)}`, 'error')
  }
}

void init()
