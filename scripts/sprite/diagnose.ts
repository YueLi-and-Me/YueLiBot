/**
 * 生图接口诊断，用于区分网络、认证和模型可用性问题。
 *
 *   npm run sprite:diagnose
 *
 * 分三步逐层检查，每步单独报告结果：
 *   1. 代理是否生效（能不能出网）
 *   2. Key 是否有效（ListModels，最便宜的判据）
 *   3. 目标模型是否在可用列表里
 */
import { createProvider, setupProxy } from './providers/index.ts'

const BASE = (process.env.GEMINI_BASE_URL?.trim() || 'https://generativelanguage.googleapis.com').replace(/\/+$/, '')

/** 诊断脚本只允许向 HTTPS 端点发起请求，拒绝其它协议的目标。 */
function httpsEndpointOf(pathAndQuery: string): URL {
  const parsed = new URL(`${BASE}${pathAndQuery}`)
  if (parsed.protocol !== 'https:') {
    throw new Error(`诊断目标必须是 HTTPS，收到 ${parsed.protocol}//${parsed.host}`)
  }
  return parsed
}

interface ModelInfo {
  name?: string
  supportedGenerationMethods?: string[]
  supported_generation_methods?: string[]
}

/**
 * 执行一个诊断步骤并将异常转换为失败结果，保证后续步骤继续输出。
 *
 * @param label 终端中展示的步骤名称。
 * @param fn 返回诊断结果文本的异步检查函数。
 * @returns {Promise<boolean>} 检查成功返回 ``true``，检查抛错时记录错误并返回 ``false``。
 * @sideEffects 向标准输出写入步骤状态和错误文本；不修改配置文件。
 */
async function step(label: string, fn: () => Promise<string>): Promise<boolean> {
  process.stdout.write(`  ${label} … `)
  try {
    console.log(await fn())
    return true
  } catch (err) {
    console.log(`✗\n     ${err instanceof Error ? err.message : String(err)}`)
    return false
  }
}

/**
 * 读取环境配置并执行网络、密钥和目标模型诊断。
 *
 * @returns 所有诊断步骤完成后的 Promise。
 * @sideEffects 发起外部模型服务请求并向标准输出打印诊断信息；不修改配置文件。
 */
async function main() {
  const proxy = setupProxy()
  const key = process.env.GEMINI_API_KEY?.trim() ?? ''
  const wanted = process.env.GEMINI_IMAGE_MODEL?.trim() || 'gemini-3.1-flash-image'

  console.log('─'.repeat(60))
  console.log('  生图接口诊断')
  console.log('─'.repeat(60))
  console.log(`  baseURL  : ${BASE}`)
  console.log(`  proxy    : ${proxy ?? '未配置（直连）'}`)
  console.log(`  key      : ${key ? `已设置（长度 ${key.length}，前缀 ${key.slice(0, 4)}…）` : '未设置'}`)
  console.log(`  目标模型 : ${wanted}`)
  console.log('─'.repeat(60))

  // 1. 出网
  await step('[1/3] 出网检查', async () => {
    const res = await fetch('https://www.gstatic.com/generate_204', { signal: AbortSignal.timeout(15_000) })
    return `✓ 可达（HTTP ${res.status}）`
  })

  // 2. Key 有效性
  let models: ModelInfo[] = []
  const keyOk = await step('[2/3] Key 有效性（ListModels）', async () => {
    const res = await fetch(httpsEndpointOf('/v1beta/models?pageSize=200'), {
      headers: { 'x-goog-api-key': key },
      signal: AbortSignal.timeout(30_000),
    })
    const text = await res.text()
    if (res.status !== 200) throw new Error(`HTTP ${res.status} — ${text.slice(0, 300)}`)
    models = (JSON.parse(text).models ?? []) as ModelInfo[]
    return `✓ Key 有效，可见 ${models.length} 个模型`
  })

  // 3. 目标模型可用性
  if (keyOk) {
    await step('[3/3] 目标模型可用性', async () => {
      const ids = models.map((m) => (m.name ?? '').replace(/^models\//, ''))
      if (!ids.includes(wanted)) {
        const imaging = ids.filter((n) => n.includes('image')).sort()
        throw new Error(
          `列表中没有 ${wanted}。\n     当前账号可见的图像模型：${imaging.length ? imaging.join(', ') : '（一个都没有）'}\n     把可用的那个填进 .env 的 GEMINI_IMAGE_MODEL`,
        )
      }
      const m = models.find((x) => (x.name ?? '').endsWith(wanted))
      const methods = m?.supportedGenerationMethods ?? m?.supported_generation_methods ?? []
      return `✓ 可用${methods.length ? `（支持：${methods.join(', ')}）` : ''}`
    })

    // 只列走 generateContent 的。imagen-* 系列用的是 :predict 端点，
    // 当前 provider 不支持该参数，不在诊断结果中展示，避免引导用户填写无效配置。
    const imaging = models
      .filter((m) => {
        const id = (m.name ?? '').replace(/^models\//, '')
        const methods = m.supportedGenerationMethods ?? m.supported_generation_methods ?? []
        return id.includes('image') && methods.includes('generateContent')
      })
      .map((m) => (m.name ?? '').replace(/^models\//, ''))
      .sort()

    if (imaging.length) {
      console.log(`\n  可选图像模型（均支持 generateContent）：\n${imaging.map((n) => `    · ${n}`).join('\n')}`)
    }
  } else {
    console.log('\n  Key 这一关就没过，说明问题在凭证本身，跟模型和网络无关：')
    console.log('    · 之前泄露的那个 Key 如果已在 AI Studio 删除，.env 里必须换成新建的')
    console.log('    · 到 https://aistudio.google.com/apikey 确认 Key 还在、并复制最新的一个')
    console.log('    · 注意别把引号、空格或换行一起粘进 .env')
  }

  // 顺带确认 provider 能造出来（配置项拼写错误在这里就能暴露）
  console.log('')
  try {
    const p = createProvider()
    console.log(`  provider 构造正常：${p.name} / ${p.model}`)
  } catch (err) {
    console.log(`  provider 构造失败：${err instanceof Error ? err.message : String(err)}`)
  }
  console.log('')
}

main().catch((err) => {
  console.error(`\n诊断脚本自身出错：${err instanceof Error ? err.stack : String(err)}`)
  process.exitCode = 1
})
