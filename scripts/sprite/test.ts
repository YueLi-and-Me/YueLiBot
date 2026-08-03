/**
 * 冒烟测试 —— 跑通它再谈别的。
 *
 *   npm run sprite:test
 *
 * 验证四件事：Key 能用、网络能通、模型能调、响应能解析出图。
 * 失败时给人话诊断，而不是甩一个 HTTP 状态码。
 */
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { createProvider, ProviderError, refineKind, setupProxy, withRetry, type FailureKind } from './providers/index.ts'

const OUT_DIR = resolve('scratch')
const GEN_FILE = resolve(OUT_DIR, 'sprite-test-1-generate.png')
const EDIT_FILE = resolve(OUT_DIR, 'sprite-test-2-edit.png')

/** 刻意用一个平淡无奇的题材，避免安全策略把测试本身拦下来。 */
const TEST_PROMPT = '一只戴着蓝色针织帽的柴犬，全身，正面站立，卡通插画风格，纯白背景'

/**
 * edit 是管线的主力（27 张里 23 张走它），必须单独验。
 * 尤其要确认它不改画布尺寸 —— 尺寸一变，整套对齐的前提就没了。
 */
const TEST_EDIT = '保持这张图的构图、画布尺寸、角色姿势和位置完全一致，只把针织帽的颜色改成红色。背景保持纯白。'

const HINTS: Record<FailureKind, string[]> = {
  auth: [
    'Key 无效或未授权。检查：',
    '  1. .env 里的 GEMINI_API_KEY / ARK_API_KEY 是否填了、有没有多余的引号或空格',
    '  2. Key 是否已被删除或轮换',
    '  3. 到 https://aistudio.google.com/apikey 重新建一个',
  ],
  region: [
    '★ 这不是 Key 的问题 —— Key 发出去了，是 Google 按来源 IP 拒绝了访问。',
    '  Gemini API 在部分国家/地区不开放，中国大陆直连必然撞这个。',
    '',
    '  两条路，任选其一：',
    '  A. 挂代理：在 .env 里加一行（端口改成你自己代理软件的）',
    '       HTTPS_PROXY=http://127.0.0.1:7890',
    '     然后重跑 npm run sprite:test',
    '',
    '  B. 换国内直连的 provider：在 .env 里改',
    '       SPRITE_PROVIDER=seedream',
    '       ARK_API_KEY=<火山引擎方舟的 Key>',
    '     方舟控制台：https://console.volcengine.com/ark',
  ],
  quota: [
    '配额限制。先看上面详情里的 limit 值：',
    '',
    '  · limit: 0  → 不是「用完了」，是免费层对该模型的额度本来就是零。',
    '                Gemini 的图像模型是纯付费功能，必须开通结算才能调用。',
    '                去 https://aistudio.google.com/apikey 给对应项目开启付费层，',
    '                Google AI Pro 订阅附送的 $10/月 Cloud 额度可直接抵扣。',
    '                按 flash-image 的单价，跑完整套素材（约 27 张）加重试也就一两美元。',
    '',
    '  · limit: 非零 → 是真的撞到速率上限，等窗口重置即可。',
    '                批量脚本已内置指数退避会自己等，不用管。',
  ],
  model: [
    '模型不存在或当前账号无权访问。检查：',
    '  · .env 里的 GEMINI_IMAGE_MODEL 拼写',
    '  · 可选值：gemini-3.1-flash-image / gemini-3-pro-image / gemini-3.1-flash-lite-image',
    '  · 图像模型在免费层的开放范围会变动，换一个型号再试',
  ],
  network: [
    '网络不通。大陆直连 Google 基本必然失败，需要代理：',
    '  1. 在 .env 里设 HTTPS_PROXY=http://127.0.0.1:7890（改成你自己的端口）',
    '  2. 或在 .env 里把 GEMINI_BASE_URL 指向可用的中转地址',
    '  3. 实在折腾不动就切国内直连：SPRITE_PROVIDER=seedream，填 ARK_API_KEY',
  ],
  blocked: ['被内容安全策略拦截。测试提示词很干净还被拦，通常说明账号或区域策略偏严，建议改用 seedream。'],
  empty: [
    '请求成功但响应里没有图像数据。多半是该模型不支持图像输出，',
    '或接口形态又变了。把上面的「详情」贴给我，我改解析逻辑。',
  ],
  unknown: ['未分类错误。把上面的「详情」贴给我。'],
}

async function main() {
  const proxy = setupProxy()
  const provider = createProvider()

  console.log('─'.repeat(56))
  console.log('  生图管线冒烟测试')
  console.log('─'.repeat(56))
  console.log(`  provider : ${provider.name}`)
  console.log(`  model    : ${provider.model}`)
  console.log(`  proxy    : ${proxy ?? '未配置（直连）'}`)
  console.log('─'.repeat(56))
  await mkdir(OUT_DIR, { recursive: true })

  const onRetry = (n: number, delay: number, err: unknown) => {
    const why = err instanceof ProviderError ? err.message : String(err)
    console.log(`  ↻ 第 ${n} 次重试（${Math.round(delay / 1000)}s 后）：${why}`)
  }

  // sharp 延迟到成功路径才加载：它一旦初始化就会挂住 libuv 句柄，
  // 失败时提前退出会撞 Windows 上的 uv assert
  const { default: sharp } = await import('sharp')

  // --- 1/2 文生图 ---
  process.stdout.write('[1/2] 文生图（generate）… ')
  let t = Date.now()
  const genImg = await withRetry(() => provider.generate({ prompt: TEST_PROMPT }), { attempts: 3, onRetry })
  const genSec = ((Date.now() - t) / 1000).toFixed(1)
  await writeFile(GEN_FILE, genImg)
  const genMeta = await sharp(genImg).metadata()
  console.log(`✓ ${genSec}s　${genMeta.width}×${genMeta.height} ${genMeta.format}　${(genImg.length / 1024).toFixed(0)} KB`)

  // --- 2/2 指令编辑（管线主力）---
  process.stdout.write('[2/2] 指令编辑（edit）… ')
  t = Date.now()
  const editImg = await withRetry(() => provider.edit({ base: genImg, instruction: TEST_EDIT }), { attempts: 3, onRetry })
  const editSec = ((Date.now() - t) / 1000).toFixed(1)
  await writeFile(EDIT_FILE, editImg)
  const editMeta = await sharp(editImg).metadata()
  console.log(`✓ ${editSec}s　${editMeta.width}×${editMeta.height} ${editMeta.format}　${(editImg.length / 1024).toFixed(0)} KB`)

  console.log('')
  if (genMeta.format !== 'png' || editMeta.format !== 'png') {
    console.log('  ⚠ 输出不是 PNG —— 下游抠图需要 alpha 通道，检查 providers/image.ts 的归一化')
  }
  if (genMeta.width !== editMeta.width || genMeta.height !== editMeta.height) {
    console.log(`  ⚠ edit 改变了画布尺寸（${genMeta.width}×${genMeta.height} → ${editMeta.width}×${editMeta.height}）`)
    console.log('     这会摧毁对齐的前提。process 阶段仍会按包围盒归一，但角色比例可能对不上，')
    console.log('     跑完整套后务必到预览页开 diff 逐张确认。')
  } else {
    console.log(`  ✓ edit 保持了画布尺寸 ${editMeta.width}×${editMeta.height}，对齐前提成立`)
  }

  console.log(`\n  两张图已保存到 ${OUT_DIR}`)
  console.log('  打开对比一眼：帽子该变红，其余（姿势、构图、背景）应当纹丝不动。')
  console.log('  改动越少，说明这个模型做差分的一致性越好，整套素材的质量上限就越高。')
}

main().catch((err) => {
  const detail = err instanceof ProviderError ? err.detail : undefined
  const kind: FailureKind = err instanceof ProviderError ? refineKind(err.kind, detail) : 'unknown'
  const msg = err instanceof Error ? err.message : String(err)

  console.error('\n✗ 失败')
  console.error(`  原因 : ${msg}`)
  if (detail) console.error(`  详情 : ${detail}`)
  console.error('')
  for (const line of HINTS[kind]) console.error(`  ${line}`)
  console.error('')

  // 用 exitCode 而非 process.exit()，让事件循环自然收尾 ——
  // 硬退出会在 Windows 上触发 libuv 的 UV_HANDLE_CLOSING 断言崩溃
  process.exitCode = 1
})
