/**
 * 底图生成与定稿。
 *
 *   # 生成候选（参考图可给多张，重复 --ref 即可）
 *   npx tsx scripts/sprite/base.ts --ref ./ref.png --desc "银发蓝瞳，水母主题连衣裙，气质温柔"
 *
 *   # 选择第 2 张作为底图
 *   npx tsx scripts/sprite/base.ts --pick 2
 *
 * Windows 下带参数的脚本建议直接使用 npx tsx 调用，避免 npm 参数转发差异。
 *
 * 底图是后续差分编辑的基准，其构图和清晰度决定角色一致性上限；脚本支持生成
 * 多张候选并单独选择定稿文件。
 */
import { readFile, writeFile } from 'node:fs/promises'
import { parseArgs } from 'node:util'
import { baseImagePrompt } from './config.ts'
import { CharPaths } from './paths.ts'
import { createProvider, ProviderError, refineKind, setupProxy, withRetry } from './providers/index.ts'

const { values } = parseArgs({
  options: {
    name: { type: 'string', default: 'yueli' },
    ref: { type: 'string', multiple: true, default: [] },
    desc: { type: 'string' },
    count: { type: 'string', default: '4' },
    pick: { type: 'string' },
    model: { type: 'string' },
  },
  allowPositionals: false,
})

/**
 * 底图候选默认用快模型：这一步本来就要 4 张各不相同的图，
 * 一致性毫无意义，没必要为它付 10 倍的等待时间。
 * 差分阶段（gen.ts）才切回慢而稳的那个。
 */
const FAST_MODEL: Record<string, string> = {
  seedream: 'doubao-seedream-5-0-pro-260628',
  ark: 'doubao-seedream-5-0-pro-260628',
}

/**
 * 将指定编号的底图候选复制为工作目录中的定稿底图。
 *
 * @param paths 角色素材路径集合。
 * @param n 候选编号，必须为正整数。
 * @returns 文件复制完成后的 Promise。
 * @throws Error 候选文件不存在或目标文件不可写。
 * @sideEffects 写入 raw/base.png 并输出下一步提示。
 */
async function pick(paths: CharPaths, n: number) {
  const src = paths.rawBaseCandidate(n)
  let buf: Buffer
  try {
    buf = await readFile(src)
  } catch {
    throw new Error(`找不到候选图 base-${n}.png。先生成候选：npx tsx scripts/sprite/base.ts --ref <参考图> --desc "<角色描述>"`)
  }
  await paths.ensureRawDirs()
  await writeFile(paths.rawBase, buf)

  console.log(`✓ 已定稿：base-${n}.png → raw/base.png`)
  console.log('\n下一步：npm run sprite:gen')
}

/**
 * 调用图像供应商生成多张底图候选并保存到工作目录。
 *
 * @param paths 角色素材路径集合。
 * @param refs 参考图文件路径列表，可为空。
 * @param desc 角色和构图描述，不能为空。
 * @param count 候选数量，范围为 1 到 12。
 * @returns 至少一张候选写入成功后的 Promise。
 * @throws Error 描述为空、参考图不可读或所有候选请求均失败。
 * @sideEffects 读取参考图、配置代理、调用图像供应商并写入 raw/base-N.png。
 */
async function generate(paths: CharPaths, refs: string[], desc: string, count: number) {
  if (!desc.trim()) {
    throw new Error('缺少 --desc。给一句角色描述即可，例如：--desc "银发蓝瞳，水母主题连衣裙，气质温柔"')
  }
  if (refs.length === 0) {
    console.log('⚠ 没给 --ref 参考图，将纯靠文字描述生成，风格随机性会大很多。')
  }

  const refBufs: Buffer[] = []
  for (const r of refs) {
    try {
      refBufs.push(await readFile(r))
    } catch {
      throw new Error(`读不到参考图：${r}`)
    }
  }

  setupProxy()
  const which = (process.env.SPRITE_PROVIDER || 'gemini').trim().toLowerCase()
  const provider = createProvider({ model: values.model || process.env.ARK_IMAGE_MODEL || FAST_MODEL[which] })
  await paths.ensureRawDirs()

  const prompt = baseImagePrompt(desc)
  console.log(`provider ${provider.name} / ${provider.model}　参考图 ${refBufs.length} 张　候选 ${count} 张\n`)

  let ok = 0
  for (let i = 1; i <= count; i++) {
    // 每张候选独立重试和落盘，单张失败不会阻断剩余候选生成。
    process.stdout.write(`  [${i}/${count}] 生成中… `)
    try {
      const img = await withRetry(() => provider.generate({ prompt, refs: refBufs, seed: Date.now() + i }), {
        onRetry: (n, delay) => process.stdout.write(`(限流，${Math.round(delay / 1000)}s 后第 ${n} 次重试) `),
      })
      await writeFile(paths.rawBaseCandidate(i), img)
      console.log(`✓ ${(img.length / 1024).toFixed(0)} KB`)
      ok++
    } catch (err) {
      console.log(`✗ ${err instanceof Error ? err.message : String(err)}`)
    }
  }

  if (ok === 0) {
    throw new Error('一张都没成功。跑 npm run sprite:diagnose 看是哪一环的问题。')
  }

  console.log(`\n候选已生成 ${ok}/${count} 张，在 ${paths.raw}`)
  console.log('\n挑图时重点看三件事：')
  console.log('  1. 全身完整入画，头顶和脚都没被裁掉')
  console.log('  2. 正面站姿、双臂自然下垂 —— 姿势越简单，后续改表情越不容易连带改动身体')
  console.log('  3. 背景干净纯白，没有多余元素和文字水印')
  console.log('\n选好后：npx tsx scripts/sprite/base.ts --pick <编号>')
}

/**
 * 解析命令行模式并执行候选选择或批量生成。
 *
 * @returns 当前操作完成后的 Promise。
 * @throws Error 参数不合法、候选缺失或生成流程失败。
 */
async function main() {
  const paths = new CharPaths(values.name!)

  if (values.pick) {
    const n = Number(values.pick)
    if (!Number.isInteger(n) || n < 1) throw new Error(`--pick 需要一个正整数，收到：${values.pick}`)
    return pick(paths, n)
  }

  const count = Number(values.count)
  if (!Number.isInteger(count) || count < 1 || count > 12) {
    throw new Error(`--count 需要 1–12 之间的整数，收到：${values.count}`)
  }
  return generate(paths, values.ref!, values.desc ?? '', count)
}

main().catch((err) => {
  const detail = err instanceof ProviderError ? err.detail : undefined
  console.error(`\n✗ ${err instanceof Error ? err.message : String(err)}`)
  if (detail) console.error(`  详情：${detail}`)
  if (err instanceof ProviderError) {
    console.error(`  分类：${refineKind(err.kind, detail)}　—— 详细排查跑 npm run sprite:diagnose`)
  }
  process.exitCode = 1
})
